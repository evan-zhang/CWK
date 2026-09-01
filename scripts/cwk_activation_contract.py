#!/usr/bin/env python3
"""RT-032 只读发现画像、每日执行合同、试跑门禁与调度交接（确定性计算层）。

四件事，全部由代码从**既有只读回执与实际配置**算出来，不接受模型猜测：

1. `build_discovery_report`：只读既有运行回执与派生索引，产出发现报告。
   报告严格区分「实体名称/别名数」「候选实体族数」「用户已确认实体数」三个数，
   并标注它只代表当前授权可见范围与发现日期范围。
2. `build_execution_contract` / `compute_contract_sha256`：从真实配置渲染
   每日执行合同，并给出稳定摘要。配置一变，摘要就变，已有确认随之失效。
3. `evaluate_pilot`：只读 nightly manifest、验收回执与采集回执判定试跑。
   任何一项不达标——包括**根本没给采集回执**——都只能进 DEGRADED，
   进不了 PILOT_PASSED。
4. `build_scheduler_handoff` / `validate_schedule_receipt`：产出机器可读的调度
   交接，并校验宿主回填的外部任务标识。

**边界**：本模块不创建、不修改、不删除任何计划任务，不调用 OpenClaw / Gateway /
cron，也不假设任何 OpenClaw 调度 API 存在——仓库里没有这样的本地文档依据。
它只产出「要建一个什么样的任务」的交接单，并在宿主回填任务标识后做校验。

Refs: RT-032
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

_PROJECT = Path(__file__).resolve().parents[1]
if str(_PROJECT / "scripts") not in sys.path:
    sys.path.insert(0, str(_PROJECT / "scripts"))

from cwk_pr001_contracts import canonical_json_bytes  # noqa: E402

DISCOVERY_REPORT_SCHEMA = "cwk.activation_discovery_report.v1"
EXECUTION_CONTRACT_SCHEMA = "cwk.activation_execution_contract.v1"
PILOT_GATE_RECEIPT_SCHEMA = "cwk.activation_pilot_gate.v1"
SCHEDULER_HANDOFF_SCHEMA = "cwk.activation_scheduler_handoff.v1"

_CONTRACT_DOMAIN = b"cwk-activation-contract-v1\x00"
_PROFILE_DOMAIN = b"cwk-activation-profile-v1\x00"
_SCOPE_DOMAIN = b"cwk-activation-scope-v1\x00"

# 采集来源。与 scripts/cwk_collect_live.py 的 lane 划分一致。
DAILY_LANES = ("inbox", "pending", "outbox", "unread", "todo_pending")
BACKFILL_LANES = (
    "history_inbox",
    "history_outbox",
    "history_pending_report",
    "history_todo_pending",
    "history_todo_completed",
)

# 上游默认值的镜像。**不是**凭记忆写死的文案：
# tests/test_activation_contract.py 用 ast 静态解析上游源码，逐个断言这些数字
# 与 scripts/cwk_collect_live.py / cwk_nightly_pipeline.py 里的真实默认值相等。
# 上游一改而这里没跟，测试立刻红。
DEFAULT_DETAIL_CAP = 60
DEFAULT_CONTINUATION_CAP = 15
DEFAULT_BACKFILL_CAP = 20
DEFAULT_BACKFILL_PAGE_SIZE = 20
DEFAULT_LOOKBACK_DAYS = 2

DETAIL_READ_ACTIONS = ("report_body", "basic_info", "node_and_opinion_chain")

FORBIDDEN_ACTIONS = (
    "mark_cwork_items_as_read",
    "reply_or_comment_in_cwork",
    "approve_reject_or_complete_cwork_tasks",
    "delete_cwork_content",
    "send_cwork_messages",
    "write_back_to_raw",
    "upload_raw_to_docdb",
    "mix_other_users_raw_into_this_mirror",
)

OUTPUTS = (
    "daily_digest_markdown",
    "daily_digest_html",
    "action_center",
    "wiki_topic_and_entity_pages",
    "entity_and_relation_index",
    "coverage_and_acceptance_receipts",
)


# ── 小工具 ──────────────────────────────────────────────────────────────────


def load_json(path: Path | str) -> Optional[dict]:
    """读 JSON；文件不存在返回 None（缺料是要如实记录的事实，不是异常）。"""

    p = Path(path)
    if not p.is_file():
        return None
    try:
        with p.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (ValueError, OSError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def sha256_of_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_of_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"1", "true", "yes", "on"}:
            return True
        if low in {"0", "false", "no", "off"}:
            return False
    return default


def upstream_collect_defaults(source: Path | str | None = None) -> dict[str, int]:
    """静态解析 cwk_collect_live.py 里的 argparse 默认值。

    上游的 parser 建在 ``main()`` 内部，直接 import 取不到，而执行 ``main()``
    会真的去采集。所以这里用 ast 只读源码。仅供测试与审计对账使用。
    """

    path = Path(source) if source else _PROJECT / "scripts" / "cwk_collect_live.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wanted = {"--detail-cap", "--continuation-cap", "--backfill-cap", "--backfill-page-size"}
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        flag = node.args[0].value
        if flag not in wanted:
            continue
        for keyword in node.keywords:
            if keyword.arg == "default" and isinstance(keyword.value, ast.Constant):
                if isinstance(keyword.value.value, int):
                    found[flag.lstrip("-").replace("-", "_")] = keyword.value.value
    return found


def upstream_lookback_default(source: Path | str | None = None) -> Optional[int]:
    """静态解析 nightly pipeline 里 source_completeness_lookback_days 的默认值。"""

    path = Path(source) if source else _PROJECT / "scripts" / "cwk_nightly_pipeline.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "get"):
            continue
        if len(node.args) != 2:
            continue
        first, second = node.args
        if (
            isinstance(first, ast.Constant)
            and first.value == "CWK_SOURCE_COMPLETENESS_LOOKBACK_DAYS"
            and isinstance(second, ast.Constant)
            and isinstance(second.value, int)
        ):
            return second.value
    return None


# ── 1. 只读发现与业务画像 ───────────────────────────────────────────────────


def compute_scope_sha256(scope: Mapping[str, Any]) -> str:
    """授权可见范围的稳定摘要（第一道确认绑定的对象）。"""

    return hashlib.sha256(_SCOPE_DOMAIN + canonical_json_bytes(dict(scope))).hexdigest()


def compute_profile_sha256(profile: Mapping[str, Any]) -> str:
    """用户确认后的业务画像摘要（第二道确认原像的一部分）。"""

    return hashlib.sha256(_PROFILE_DOMAIN + canonical_json_bytes(dict(profile))).hexdigest()


def _entity_counts(
    entity_catalog: Optional[dict], entity_registry: Optional[dict]
) -> dict[str, Any]:
    """三个必须互相区分的实体口径。

    - ``entity_surface_count``：原始名称 + 别名去重后的「写法」数；
    - ``candidate_entity_family_count``：算法聚出来的候选实体族数；
    - ``confirmed_entity_count``：**只**数注册表里带真实决策记录的条目。

    三者分别来自不同来源，任何一个都不由另一个推导，这样报告才不会把
    「机器猜的」说成「人确认的」。
    """

    surfaces: set[str] = set()
    families: set[str] = set()
    if isinstance(entity_catalog, dict):
        entries = entity_catalog.get("entities")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("normalized") or entry.get("canonical_display") or entry.get("name")
                if isinstance(name, str) and name:
                    surfaces.add(name)
                for alias in entry.get("aliases") or []:
                    if isinstance(alias, str) and alias:
                        surfaces.add(alias)
                family = entry.get("family_id")
                if isinstance(family, str) and family:
                    families.add(family)

    confirmed = 0
    if isinstance(entity_registry, dict):
        for entry in entity_registry.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("decided_by") and entry.get("decided_at"):
                confirmed += 1

    return {
        "entity_surface_count": len(surfaces),
        "candidate_entity_family_count": len(families),
        "confirmed_entity_count": confirmed,
        "counts_are_independent": True,
    }


def build_discovery_report(
    *,
    scope: Mapping[str, Any],
    collect_manifest: Optional[dict],
    nightly_manifest: Optional[dict],
    acceptance: Optional[dict],
    entity_catalog: Optional[dict] = None,
    entity_registry: Optional[dict] = None,
    generated_at: str,
) -> dict:
    """从既有只读回执生成发现报告。缺料如实记录成 gaps，不猜、不补。"""

    gaps: list[str] = []
    if collect_manifest is None:
        gaps.append("collect_manifest_missing")
    if nightly_manifest is None:
        gaps.append("nightly_manifest_missing")
    if acceptance is None:
        gaps.append("acceptance_missing")
    if entity_catalog is None:
        gaps.append("entity_catalog_missing")

    collect = collect_manifest or {}
    nightly = nightly_manifest or {}
    accept = acceptance or {}

    lookback = _as_int(nightly.get("source_completeness_lookback_days"), DEFAULT_LOOKBACK_DAYS)

    record_counts = {
        "candidate_count": _as_int(collect.get("candidate_count"), 0),
        "selected_daily_count": _as_int(collect.get("selected_daily_count"), 0),
        "selected_backfill_count": _as_int(collect.get("selected_backfill_count"), 0),
        "written_count": _as_int(collect.get("written_count"), 0),
        "pending_count": _as_int(collect.get("pending_count"), 0),
        "processed_count": _as_int(nightly.get("processed_count"), 0),
        "raw_count": _as_int(accept.get("raw_count"), 0),
    }

    lane_counts = accept.get("lane_counts")
    lane_counts = lane_counts if isinstance(lane_counts, dict) else {}

    relations = {
        "relation_items": _as_int(accept.get("relation_items"), 0),
        "unique_relation_pairs": _as_int(accept.get("unique_relation_pairs"), 0),
        "strong_relations": _as_int(accept.get("strong_relations"), 0),
        "suspected_relations": _as_int(accept.get("suspected_relations"), 0),
        # 「未知」= 有配对但证据不足以判强弱的部分。
        "unknown_relation_count": max(
            _as_int(accept.get("unique_relation_pairs"), 0)
            - _as_int(accept.get("strong_relations"), 0)
            - _as_int(accept.get("suspected_relations"), 0),
            0,
        ),
    }

    report = {
        "schema": DISCOVERY_REPORT_SCHEMA,
        "generated_at": generated_at,
        "coverage_caveat": (
            "counts reflect only the currently authorized visible scope and the stated "
            "discovery date range; they are not a claim about the whole organisation"
        ),
        "authorized_visible_scope": dict(scope),
        "business_date_range": {
            "end": nightly.get("date"),
            "late_data_lookback_days": lookback,
        },
        "source_lanes": {
            "daily": list(DAILY_LANES),
            "backfill": list(BACKFILL_LANES),
            "daily_lane_count": len(DAILY_LANES),
            "backfill_lane_count": len(BACKFILL_LANES),
            "backfill_rotation": "round_robin",
        },
        "record_counts": record_counts,
        "lane_record_counts": lane_counts,
        "topic_count": _as_int(accept.get("recurring_topic_proposals"), 0),
        "entities": _entity_counts(entity_catalog, entity_registry),
        "relations": relations,
        "completeness": {
            "daily_source_complete": bool(collect.get("daily_source_complete", False)),
            "daily_source_failure_count": _as_int(collect.get("daily_source_failure_count"), 0),
            "source_completeness_failures": list(
                nightly.get("source_completeness_failures") or []
            ),
        },
        "gaps": gaps,
    }
    report["report_sha256"] = sha256_of_json(
        {k: v for k, v in report.items() if k != "generated_at"}
    )
    return report


# ── 2. 每日执行合同 ─────────────────────────────────────────────────────────

# 不进哈希的字段：时间戳与本地路径。它们一变不代表合同语义变了。
_CONTRACT_VOLATILE_KEYS = ("generated_at", "contract_sha256")


def build_execution_contract(
    *,
    config: Mapping[str, Any],
    env: Optional[Mapping[str, str]] = None,
    profile_sha256: str,
    run_at_local: str,
    timezone: str,
    generated_at: str,
) -> dict:
    """从**实际配置**渲染每日执行合同。

    取值优先级与 nightly pipeline 的 `config_value` 对齐（去掉 CLI 一层）：
    config > 环境变量 > 默认值。
    """

    env = env if env is not None else os.environ

    def resolve_int(config_key: str, env_key: str, default: int) -> int:
        if config_key in config:
            return _as_int(config.get(config_key), default)
        if env_key in env:
            return _as_int(env.get(env_key), default)
        return default

    def resolve_bool(config_key: str, env_key: str, default: bool) -> bool:
        if config_key in config:
            return _as_bool(config.get(config_key), default)
        if env_key in env:
            return _as_bool(env.get(env_key), default)
        return default

    caps = {
        "detail_cap": resolve_int("detail_cap", "CWK_DETAIL_CAP", DEFAULT_DETAIL_CAP),
        "continuation_cap": resolve_int(
            "continuation_cap", "CWK_CONTINUATION_CAP", DEFAULT_CONTINUATION_CAP
        ),
        "backfill_cap": resolve_int("backfill_cap", "CWK_BACKFILL_CAP", DEFAULT_BACKFILL_CAP),
        "backfill_page_size": resolve_int(
            "backfill_page_size", "CWK_BACKFILL_PAGE_SIZE", DEFAULT_BACKFILL_PAGE_SIZE
        ),
    }
    backfill_enabled = resolve_bool("backfill_enabled", "CWK_BACKFILL_ENABLED", True)
    sync_docdb = resolve_bool("sync_docdb", "CWK_SYNC_DOCDB", False)
    lookback = resolve_int(
        "source_completeness_lookback_days",
        "CWK_SOURCE_COMPLETENESS_LOOKBACK_DAYS",
        DEFAULT_LOOKBACK_DAYS,
    )

    contract = {
        "schema": EXECUTION_CONTRACT_SCHEMA,
        "generated_at": generated_at,
        "profile_sha256": profile_sha256,
        "sources": {
            "daily_lanes": list(DAILY_LANES),
            "backfill_lanes": list(BACKFILL_LANES) if backfill_enabled else [],
            "backfill_enabled": backfill_enabled,
            "backfill_rotation": "round_robin",
        },
        "caps": caps,
        "detail_read_actions": list(DETAIL_READ_ACTIONS),
        "current_business_day_full_pagination": True,
        "late_data_lookback_days": lookback,
        "outputs": list(OUTPUTS),
        "publishing": {
            "sync_docdb": sync_docdb,
            "publishes_derived_only": True,
            "uploads_raw": False,
        },
        "raw_boundary": {
            "raw_is_local_and_authoritative": True,
            "raw_never_written_back": True,
            "raw_never_uploaded": True,
        },
        "forbidden_actions": list(FORBIDDEN_ACTIONS),
        "schedule_intent": {
            "cadence": "daily",
            "run_at_local": run_at_local,
            "timezone": timezone,
        },
        "read_only": True,
    }
    contract["contract_sha256"] = compute_contract_sha256(contract)
    return contract


def compute_contract_sha256(contract: Mapping[str, Any]) -> str:
    """合同的稳定摘要。

    只对语义字段求哈希（去掉时间戳与摘要字段本身），并用 JCS 规范化，
    因此同一份配置反复生成得到同一个摘要；任何一项来源、上限、回看天数、
    发布开关或运行时间变化都会改变它。
    """

    body = {k: v for k, v in contract.items() if k not in _CONTRACT_VOLATILE_KEYS}
    return hashlib.sha256(_CONTRACT_DOMAIN + canonical_json_bytes(body)).hexdigest()


def contract_drift(contract: Mapping[str, Any], recorded_sha256: Optional[str]) -> dict:
    """比对当前合同与状态里记录的摘要。"""

    current = compute_contract_sha256(contract)
    return {
        "current_contract_sha256": current,
        "recorded_contract_sha256": recorded_sha256,
        "drifted": recorded_sha256 is not None and recorded_sha256 != current,
    }


def render_contract_markdown(contract: Mapping[str, Any]) -> str:
    """给人读的合同复述。内容全部来自合同对象，不另写文案。"""

    caps = contract["caps"]
    sources = contract["sources"]
    schedule = contract["schedule_intent"]
    publishing = contract["publishing"]
    lines = [
        "# CWK 每日执行合同",
        "",
        f"- 合同摘要：`{contract.get('contract_sha256')}`",
        f"- 运行时间：每天 {schedule['run_at_local']}（{schedule['timezone']}）",
        "",
        "## 每天读取哪些来源",
        f"- 日常 lane（{len(sources['daily_lanes'])} 条）：{', '.join(sources['daily_lanes'])}",
        f"- 历史回填 lane（{len(sources['backfill_lanes'])} 条，轮转）："
        + (", ".join(sources["backfill_lanes"]) or "本次未启用"),
        f"- 详情读取动作：{', '.join(contract['detail_read_actions'])}",
        "",
        "## 每轮处理上限",
        f"- 新增/更新/顺延：{caps['detail_cap']}",
        f"- 持续事项：{caps['continuation_cap']}",
        f"- 历史回填：{caps['backfill_cap']}（每页 {caps['backfill_page_size']}）",
        "",
        "## 完整性",
        "- 当前业务日：完整分页",
        f"- 迟到数据回看：前 {contract['late_data_lookback_days']} 个业务日",
        "",
        "## 产出与发布",
        f"- 产物：{', '.join(contract['outputs'])}",
        f"- 派生内容发布到 DocDB：{'开' if publishing['sync_docdb'] else '关'}",
        "- raw 原文：只留在本地，不回写、不上传",
        "",
        "## 绝不执行的动作",
    ]
    lines.extend(f"- {action}" for action in contract["forbidden_actions"])
    return "\n".join(lines) + "\n"


# ── 3. 试跑门禁 ─────────────────────────────────────────────────────────────

# 采集回执必须真的长这样才算「有回执」。字段取自 scripts/cwk_collect_live.py
# 实际写出的 collect-manifest.json；缺字段或类型不对 = 这不是采集器的回执，
# 按缺证处理，不按「大概可以」处理。
COLLECT_RECEIPT_SHAPE: tuple[tuple[str, str], ...] = (
    ("daily_source_complete", "bool"),
    ("daily_source_failure_count", "int"),
    ("written_count", "int"),
    ("errors", "list"),
    ("mutating_commands_called", "list"),
)


def _shape_ok(value: Any, kind: str) -> bool:
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if kind == "list":
        return isinstance(value, list)
    return False


def verify_collect_receipt(collect_manifest: Optional[Mapping[str, Any]]) -> dict:
    """核验采集回执，并抽出要进哈希的既核事实。

    「有没有给」「形状对不对」「本身成没成功」三件事分开判，任何一件不成立
    都不给 PASS。**省略参数不是中立的**：没有采集回执就无法声称当天来源完整，
    所以省略等价于缺证，而不是「这一项不适用」。

    返回值整体会被绑进试跑回执的哈希，其中 ``receipt_sha256`` 是采集回执
    文档自身的摘要——换一份采集证据，试跑回执的哈希必然改变，因而当时
    基于旧证据做出的第二道确认自动作废。
    """

    if collect_manifest is None:
        return {
            "present": False,
            "shape_valid": False,
            "success": False,
            "problems": ["collect_receipt_omitted"],
            "receipt_sha256": None,
            "verified": None,
        }
    if not isinstance(collect_manifest, Mapping):
        return {
            "present": True,
            "shape_valid": False,
            "success": False,
            "problems": ["collect_receipt_not_an_object"],
            "receipt_sha256": None,
            "verified": None,
        }

    problems: list[str] = []
    for name, kind in COLLECT_RECEIPT_SHAPE:
        if name not in collect_manifest:
            problems.append(f"collect_receipt_missing_{name}")
        elif not _shape_ok(collect_manifest[name], kind):
            problems.append(f"collect_receipt_bad_{name}")
    shape_valid = not problems

    receipt_sha256 = sha256_of_json(dict(collect_manifest))

    if not shape_valid:
        return {
            "present": True,
            "shape_valid": False,
            "success": False,
            "problems": problems,
            "receipt_sha256": receipt_sha256,
            "verified": None,
        }

    verified = {
        "daily_source_complete": collect_manifest["daily_source_complete"],
        "daily_source_failure_count": collect_manifest["daily_source_failure_count"],
        "written_count": collect_manifest["written_count"],
        "error_count": len(collect_manifest["errors"]),
        "mutating_command_count": len(collect_manifest["mutating_commands_called"]),
    }
    if verified["daily_source_complete"] is not True:
        problems.append("collect_receipt_daily_source_incomplete")
    if verified["daily_source_failure_count"] != 0:
        problems.append("collect_receipt_has_source_failures")
    if verified["error_count"] != 0:
        problems.append("collect_receipt_has_errors")
    if verified["mutating_command_count"] != 0:
        problems.append("collect_receipt_called_mutating_commands")

    return {
        "present": True,
        "shape_valid": True,
        "success": not problems,
        "problems": problems,
        "receipt_sha256": receipt_sha256,
        "verified": verified,
    }


def _evidence_sha256(document: Optional[Mapping[str, Any]]) -> Optional[str]:
    """证据文档自身的摘要；没给就是 None（缺证也是一个要进哈希的事实）。"""

    if document is None:
        return None
    return sha256_of_json(dict(document))


def _mutating_calls(document: Any) -> list:
    """回执自称调用过的写操作。非对象、缺字段当空；非列表的真值当一次调用。"""

    if not isinstance(document, Mapping):
        return []
    value = document.get("mutating_commands_called")
    if isinstance(value, (list, tuple)):
        return list(value)
    return [] if not value else [value]


def evaluate_pilot(
    *,
    nightly_manifest: Optional[dict],
    acceptance: Optional[dict],
    collect_manifest: Optional[dict] = None,
    bound_contract_sha256: str,
    generated_at: str,
) -> dict:
    """判定一次只读试跑是否够格放行定时任务。

    全部谓词必须为真才 PASS。任何一项不达标，结果就是 FAIL，
    调用方只能把状态推进到 DEGRADED——`PILOT_PASSED` 在迁移表里
    根本无法从 FAIL 回执到达。

    三份证据（nightly manifest、验收回执、采集回执）**都是必需的**，
    并且它们各自的文档摘要都进回执哈希：换证据必然换回执，绑在旧回执上的
    第二道确认随之失效。
    """

    nightly = nightly_manifest if isinstance(nightly_manifest, Mapping) else {}
    accept = acceptance if isinstance(acceptance, Mapping) else {}

    checks = accept.get("checks")
    checks = checks if isinstance(checks, dict) else {}

    collect_check = verify_collect_receipt(collect_manifest)
    collect_verified = collect_check["verified"] or {}

    predicates: dict[str, bool] = {
        "nightly_manifest_present": nightly_manifest is not None,
        "acceptance_present": acceptance is not None,
        "collect_receipt_present": collect_check["present"],
        "collect_receipt_shape_valid": collect_check["shape_valid"],
        "collect_receipt_success": collect_check["success"],
        "nightly_overall_pass": nightly.get("overall_pass") is True,
        "nightly_content_quality_pass": nightly.get("content_quality_pass") is True,
        "nightly_not_degraded": nightly.get("degraded") is not True,
        "no_sync_failures": not (nightly.get("sync_failures") or []),
        "no_source_completeness_failures": not (
            nightly.get("source_completeness_failures") or []
        ),
        "acceptance_overall_pass": accept.get("overall_pass") is True,
        "acceptance_no_failures": not (accept.get("failures") or []),
        "acceptance_all_checks_pass": bool(checks) and all(bool(v) for v in checks.values()),
        "acceptance_a4_ok": accept.get("A4_status") in {"PASS", "PASS_LOW_VOLUME"},
        # 缺回执时这一条必然为假：没有证据就不能声称当天来源完整。
        "daily_source_complete": collect_verified.get("daily_source_complete") is True,
        "no_mutating_commands": not _mutating_calls(nightly_manifest)
        and not _mutating_calls(acceptance)
        and not _mutating_calls(collect_manifest),
    }

    failed = sorted(name for name, ok in predicates.items() if not ok)
    receipt = {
        "schema": PILOT_GATE_RECEIPT_SCHEMA,
        "generated_at": generated_at,
        "bound_contract_sha256": bound_contract_sha256,
        "result": "PASS" if not failed else "FAIL",
        "predicates": predicates,
        "failed_predicates": failed,
        "collection_receipt": collect_check,
        "evidence": {
            "nightly_manifest_sha256": _evidence_sha256(nightly_manifest),
            "acceptance_sha256": _evidence_sha256(acceptance),
            "collect_manifest_sha256": collect_check["receipt_sha256"],
        },
        "run_name": nightly.get("run_name"),
        "business_date": nightly.get("date"),
        "processed_count": _as_int(nightly.get("processed_count"), 0),
    }
    receipt["receipt_sha256"] = sha256_of_json(
        {k: v for k, v in receipt.items() if k != "generated_at"}
    )
    return receipt


# ── 4. 调度交接与回执 ───────────────────────────────────────────────────────


class ConfigLocatorError(ValueError):
    """配置文件无法表示成项目相对定位符。

    这是 fail-closed 的落点：宁可当场拒绝出交接单，也不把宿主的绝对路径写进
    Agent 可见的成功负载与 `handoff_sha256`。
    """


# 交接单里唯一被允许的「项目根」表述：只说宿主该**怎么找**，不说它**在哪**。
# 绝对路径同时是主机布局与用户身份的泄露面（/Users/<name>/… 、/home/<name>/…），
# 而宿主本来就知道自己把仓库放在哪——把它回写进交接单没有任何信息增益。
_PROJECT_ROOT_LOCATOR = {
    "kind": "cwk_project_root",
    "resolution_order": [
        "environment variable CWK_PROJECT_DIR",
        "the directory the host already uses to run this repository's scripts",
    ],
    "verify_marker": "scripts/cwk_doctor.py",
    "absolute_path_intentionally_omitted": True,
}

CONFIG_LOCATOR_SCHEMA = "cwk.activation_config_locator.v1"

# 段内一律不接受控制字符；首段不接受以 - 开头（会被下游当成选项解析）。
_UNSAFE_SEGMENT = re.compile(r"[\x00-\x1f\x7f]")


def project_root_locator() -> dict:
    """返回项目根定位契约的独立副本，避免调用方改到模块级常量。"""

    locator = dict(_PROJECT_ROOT_LOCATOR)
    locator["resolution_order"] = list(_PROJECT_ROOT_LOCATOR["resolution_order"])
    return locator


def build_config_locator(*, config_path: Path | str, project_root: Path | str) -> dict:
    """把配置路径压成**项目相对**定位符；压不下去就当场失败。

    宿主 Agent 需要的信息是「在项目根下跑哪一个配置」，不是「这台机器上的绝对
    路径」。因此这里只输出相对段，并附上项目根的解析契约让宿主自己解析。
    配置落在项目之外时不做任何降级——降级的唯一形式就是把绝对路径写回去，
    那正是要防的东西。
    """

    root = Path(project_root).resolve()
    target = Path(config_path)
    if not target.is_absolute():
        target = Path.cwd() / target
    target = target.resolve()

    try:
        relative = target.relative_to(root)
    except ValueError:
        raise ConfigLocatorError(
            "the config file is outside the CWK project directory; describing it would "
            "require an absolute host path, so no scheduler handoff was produced. "
            "Move the config inside the project directory and re-run."
        ) from None

    parts = relative.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ConfigLocatorError(
            "the config path cannot be reduced to a stable project-relative locator"
        )
    if any(_UNSAFE_SEGMENT.search(part) for part in parts):
        raise ConfigLocatorError(
            "the config file name contains control characters and cannot be handed off safely"
        )
    if parts[0].startswith("-"):
        raise ConfigLocatorError(
            "the config path would be parsed as a command-line option and cannot be handed off safely"
        )

    return {
        "schema": CONFIG_LOCATOR_SCHEMA,
        "kind": "project_relative",
        "path": "/".join(parts),
        "resolved_against": "cwk_project_root",
        "project_root_locator": project_root_locator(),
        "absolute_path_omitted": True,
    }


def build_scheduler_handoff(
    *,
    contract: Mapping[str, Any],
    contract_sha256: str,
    profile_sha256: str,
    pilot_receipt_sha256: str,
    config_path: Path | str,
    project_root: Path | str,
    generated_at: str,
) -> dict:
    """产出「请宿主建一个这样的任务」的交接单。

    本仓库不建任务。交接单只描述要跑什么命令、需要哪些环境变量名（**只有名字，
    没有值**）、以及前置条件。真正的创建动作由用户在宿主/OpenClaw 侧完成，
    完成后把外部任务标识回填给 `validate_schedule_receipt`。

    配置位置以项目相对定位符表述，绝对路径既不进负载也不进 `handoff_sha256`；
    无法安全表述时抛 `ConfigLocatorError`，不出交接单。
    """

    locator = build_config_locator(config_path=config_path, project_root=project_root)
    schedule = contract["schedule_intent"]
    handoff = {
        "schema": SCHEDULER_HANDOFF_SCHEMA,
        "generated_at": generated_at,
        "contract_sha256": contract_sha256,
        "profile_sha256": profile_sha256,
        "pilot_receipt_sha256": pilot_receipt_sha256,
        "cadence": schedule["cadence"],
        "run_at_local": schedule["run_at_local"],
        "timezone": schedule["timezone"],
        "config_locator": locator,
        "command_spec": {
            "argv": [
                "python3",
                "scripts/cwk_nightly_pipeline.py",
                "--config",
                locator["path"],
                "--run-name",
                "nightly-{{YYYYMMDD-HHMM}}",
                "--date",
                "{{YYYY-MM-DD}}",
            ],
            "cwd_relative_to_project_root": ".",
            "project_root_locator": project_root_locator(),
            "env_allowlist": ["CWORK_APP_KEY"],
            "secrets_included": False,
            "absolute_paths_included": False,
        },
        "preconditions": [
            "activation state is PILOT_PASSED",
            "second human confirmation is bound to this contract_sha256",
            "pilot receipt result is PASS for this contract_sha256",
        ],
        "host_responsibilities": [
            "resolve the CWK project root locally using project_root_locator",
            "create the scheduled task using the host's own mechanism",
            "return the external task id to `record-schedule`",
            "never embed credentials in the task definition",
        ],
        "repository_does_not": [
            "create, modify or delete scheduled tasks",
            "call OpenClaw, Gateway or cron APIs",
            "assume any specific scheduler API exists",
        ],
        "requires_second_confirmation": True,
    }
    handoff["handoff_sha256"] = sha256_of_json(
        {k: v for k, v in handoff.items() if k != "generated_at"}
    )
    return handoff


def validate_schedule_receipt(
    *,
    handoff: Mapping[str, Any],
    contract_sha256: str,
    external_task_id: str,
) -> dict:
    """校验宿主回填的调度回执是否与本次交接单和当前合同一致。"""

    problems: list[str] = []
    if handoff.get("schema") != SCHEDULER_HANDOFF_SCHEMA:
        problems.append("handoff_schema_unknown")
    if handoff.get("contract_sha256") != contract_sha256:
        problems.append("handoff_contract_mismatch")
    if not isinstance(external_task_id, str) or not external_task_id.strip():
        problems.append("external_task_id_missing")
    return {"ok": not problems, "problems": problems}


def detect_schedule_drift(
    *,
    state_schedule: Optional[Mapping[str, Any]],
    observed_task_id: Optional[str],
    contract_sha256: str,
) -> dict:
    """检查外部调度是否与记录一致。

    发现未知任务只如实报告，**绝不**自动删除或覆盖——那可能是别人的任务。
    """

    findings: list[str] = []
    if state_schedule is None:
        if observed_task_id:
            findings.append("unknown_external_task")
        return {"drifted": bool(findings), "findings": findings, "destructive_action_taken": False}

    if state_schedule.get("bound_contract_sha256") != contract_sha256:
        findings.append("contract_drift")
    if observed_task_id is not None and observed_task_id != state_schedule.get("external_task_id"):
        findings.append("schedule_id_unknown")
    return {"drifted": bool(findings), "findings": findings, "destructive_action_taken": False}
