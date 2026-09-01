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


# ── nightly 运行时取值：与 cwk_nightly_pipeline 逐条对齐 ────────────────────
#
# 合同必须描述**那条被排期的命令实际会做什么**，不是「大致会做什么」。交接单里
# 的 argv 固定为 `--config <cfg> --run-name <n> --date <d>`，没有别的开关，所以
# 这里只需要复刻 nightly 在**没有其它命令行参数**时的取值规则。
#
# 为什么不把上游 nightly 模块导进来复用它的函数：那个模块在导入时就会执行
# `load_local_env(PROJECT/'.env')`，把 .env 里的东西塞进本进程的环境变量。
# 激活向导绝不能因为「渲染一份合同」而顺手把凭据读进自己的进程。所以这里重写一
# 份**等价实现**，并由 tests/test_rt032_contract_fidelity.py 直接调用上游真函数
# 做等价性对拍——上游一改，那组测试就红。
#
# 与上游 `env_bool` 逐字一致：只有这五个字面量算真，**其它任何取值都算假**，
# 不存在「看不懂就回落到默认值」。少写一个 "y" 就会让合同在 CWK_SYNC_DOCDB=y
# 时说「不发布」，而实际那晚会发布。
NIGHTLY_ENV_TRUE = ("1", "true", "yes", "y", "on")

# 被排期的任务只会拿到交接单 env_allowlist 里的变量（CWORK_APP_KEY），拿不到
# 任何 CWK_* 开关。因此「当前 shell 里的 CWK_* 变量」和「排期后真实的环境」是
# 两个环境，合同必须把两者是否等价说清楚。
NIGHTLY_SETTING_KEYS = (
    "detail_cap",
    "continuation_cap",
    "backfill_cap",
    "backfill_page_size",
    "backfill_enabled",
    "source_completeness",
    "source_completeness_lookback_days",
    "sync_docdb",
)


class NightlyConfigError(ValueError):
    """配置里的取值会让 nightly 直接崩在启动阶段。

    上游对整数是 `int(...)` 硬转、对 lookback 有 0..31 的范围检查，转不动就
    `ValueError`/`SystemExit`——那条被排期的命令根本跑不起来。合同不能替它编一个
    默认值糊过去，否则用户确认的是一份永远不会发生的运行。
    """


class ScheduledEnvironmentMismatch(ValueError):
    """合同取值依赖当前 shell 的 CWK_* 变量，而被排期的任务拿不到它们。

    交接单的 env_allowlist 只有 CWORK_APP_KEY。若某个设置是靠 shell 环境变量
    才成立的，宿主那条任务会解析出**另一个值**，于是用户确认的合同与实际夜跑
    不是同一件事。fail closed：不出交接单，请用户把值写进配置文件。
    """


def nightly_env_bool(env: Mapping[str, str], name: str) -> Optional[bool]:
    """`cwk_nightly_pipeline.env_bool` 的等价实现（未设置返回 None）。"""

    value = env.get(name)
    if value is None:
        return None
    return value.strip().lower() in NIGHTLY_ENV_TRUE


def _nightly_int(value: Any, *, where: str) -> int:
    """复刻上游的 `int(...)` 硬转，包括它会抛的那些异常。"""

    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise NightlyConfigError(
            f"{where} is not an integer; the scheduled nightly command would abort "
            "before it started, so no contract can describe it"
        ) from exc


def _resolve_nightly_int(
    config: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    key: str,
    env_key: str,
    default: int,
) -> tuple[int, str]:
    """上游 `int(config_value(args, config, key, os.environ.get(env_key, default)))`。

    注意优先级：整数是 **config > env > 字面默认值**（env 只是 `config_value`
    的默认值参数），与下面的布尔开关**不同**。这个不对称不是笔误，是上游的既有
    行为；合同要描述现实，就得照抄。
    """

    if key in config:
        return _nightly_int(config[key], where=f"config.{key}"), "config"
    if env_key in env:
        return _nightly_int(env[env_key], where=f"environment {env_key}"), "env"
    return default, "default"


def _resolve_nightly_flag(
    config: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    key: str,
    env_key: str,
    default: bool,
) -> tuple[bool, str]:
    """上游 `env_bool(env_key) if 已设置 else bool(config.get(key, default))`。

    布尔开关是 **env > config > 默认值**。另注意上游对配置值用的是 Python 真值
    而非解析：`{"backfill_enabled": "false"}` 在上游是**真**。照抄，因为合同的
    价值在于把这种反直觉的配置如实摊开给用户看，而不是替他猜意图。
    """

    from_env = nightly_env_bool(env, env_key)
    if from_env is not None:
        return from_env, "env"
    if key in config:
        return bool(config[key]), "config"
    return bool(default), "default"


def _resolve_nightly_sync_docdb(
    config: Mapping[str, Any], env: Mapping[str, str]
) -> tuple[bool, str]:
    """上游 `bool(config.get("sync_docdb", env_bool("CWK_SYNC_DOCDB") or False))`。

    这一条又是 **config > env > False**，与 backfill_enabled 相反。交接单的 argv
    不带 `--sync-docdb`、不带 `--no-publish-mirror`、不带 `--cloud-first`，所以
    命令行那一层恒为假，不参与。
    """

    if "sync_docdb" in config:
        return bool(config["sync_docdb"]), "config"
    from_env = nightly_env_bool(env, "CWK_SYNC_DOCDB")
    if from_env is not None:
        return bool(from_env), "env"
    return False, "default"


def resolve_nightly_runtime(
    config: Mapping[str, Any], env: Optional[Mapping[str, str]] = None
) -> dict:
    """算出被排期的那条 nightly 命令实际会用的取值。

    返回 ``{"settings": {...}, "sources": {setting: config|env|default}}``。
    取值无效时抛 :class:`NightlyConfigError`——上游会崩，合同就不能假装它能跑。
    """

    env = env if env is not None else os.environ
    settings: dict[str, Any] = {}
    sources: dict[str, str] = {}

    for key, env_key, default in (
        ("detail_cap", "CWK_DETAIL_CAP", DEFAULT_DETAIL_CAP),
        ("continuation_cap", "CWK_CONTINUATION_CAP", DEFAULT_CONTINUATION_CAP),
        ("backfill_cap", "CWK_BACKFILL_CAP", DEFAULT_BACKFILL_CAP),
        ("backfill_page_size", "CWK_BACKFILL_PAGE_SIZE", DEFAULT_BACKFILL_PAGE_SIZE),
        (
            "source_completeness_lookback_days",
            "CWK_SOURCE_COMPLETENESS_LOOKBACK_DAYS",
            DEFAULT_LOOKBACK_DAYS,
        ),
    ):
        settings[key], sources[key] = _resolve_nightly_int(
            config, env, key=key, env_key=env_key, default=default
        )

    for key, env_key, default in (
        ("backfill_enabled", "CWK_BACKFILL_ENABLED", True),
        ("source_completeness", "CWK_SOURCE_COMPLETENESS", True),
    ):
        settings[key], sources[key] = _resolve_nightly_flag(
            config, env, key=key, env_key=env_key, default=default
        )

    settings["sync_docdb"], sources["sync_docdb"] = _resolve_nightly_sync_docdb(config, env)

    lookback = settings["source_completeness_lookback_days"]
    if lookback < 0 or lookback > 31:
        # 上游 `raise SystemExit(...)`：命令启动即失败。
        raise NightlyConfigError(
            "source_completeness_lookback_days must be between 0 and 31; the scheduled "
            "nightly command would refuse to start"
        )

    return {"settings": settings, "sources": sources}


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


SCOPE_MIRROR_KINDS = ("personal", "team")
SCOPE_LANES = DAILY_LANES + BACKFILL_LANES
SCOPE_KEYS = ("mirror_kind", "subject_ref", "authorized_lanes", "read_only")
# 主体标识是个**标识符**，不是一句话：不留空格，也就不留自由文本的位置。
_SCOPE_SUBJECT_REF = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@-]{0,63}\Z")


class ScopeSchemaError(ValueError):
    """范围文件不符合闭合 schema。"""


def normalize_scope(scope: Any) -> dict:
    """把第一道门要确认的「授权可见范围」收敛成闭合 schema。

    为什么必须闭合：这个对象会被原样写进 `discovery-report.json`，再由 AI 念给
    用户听。任何自由文本字段都是一条直通 Agent 上下文的注入通道，而它偏偏又是
    「用户到底授权了什么」的权威表述。所以只认四个字段、四种类型，多一个键就
    拒绝——宁可让调用方改文件，也不把调用方给的任意对象转发出去。

    lane 顺序会被归一到固定次序，因此同一份授权不会因为书写顺序不同而算出两个
    哈希、把第一道确认无端作废。错误消息只说键名与允许值，不回显调用方内容。
    """

    if not isinstance(scope, Mapping):
        raise ScopeSchemaError("scope must be a JSON object")
    keys = set(scope)
    missing = sorted(set(SCOPE_KEYS) - keys)
    extra = len(keys - set(SCOPE_KEYS))
    if missing or extra:
        # 缺的键名可以直说，那是 schema 自己的词表；多出来的**只报个数**。键名
        # 是调用方写的字符串，把它抄进错误消息就等于把任意文本转发给读这条消息
        # 的 AI——而这个函数存在的理由正是不做这种转发。用户想知道自己多写了
        # 什么，对照后半句列出的四个允许键即可。
        raise ScopeSchemaError(
            f"scope keys mismatch missing={missing} unexpected_key_count={extra}; "
            f"the only allowed keys are {list(SCOPE_KEYS)}"
        )

    mirror_kind = scope["mirror_kind"]
    if mirror_kind not in SCOPE_MIRROR_KINDS:
        raise ScopeSchemaError(f"scope.mirror_kind must be one of {list(SCOPE_MIRROR_KINDS)}")

    subject_ref = scope["subject_ref"]
    if not isinstance(subject_ref, str) or not _SCOPE_SUBJECT_REF.match(subject_ref):
        raise ScopeSchemaError(
            "scope.subject_ref must be a short identifier "
            "([A-Za-z0-9][A-Za-z0-9._:@-]{0,63}); it is not a free-text field"
        )

    lanes = scope["authorized_lanes"]
    if not isinstance(lanes, list) or not lanes:
        raise ScopeSchemaError("scope.authorized_lanes must be a non-empty list")
    if len(lanes) > len(SCOPE_LANES):
        raise ScopeSchemaError("scope.authorized_lanes has more entries than there are lanes")
    for lane in lanes:
        if not isinstance(lane, str) or lane not in SCOPE_LANES:
            raise ScopeSchemaError(f"scope.authorized_lanes may only contain {list(SCOPE_LANES)}")
    if len(set(lanes)) != len(lanes):
        raise ScopeSchemaError("scope.authorized_lanes contains a duplicate lane")

    if scope["read_only"] is not True:
        # 只读不是一个可以谈判的选项：范围文件声称别的，就不是这个产品的范围。
        raise ScopeSchemaError("scope.read_only must be literally true")

    return {
        "mirror_kind": mirror_kind,
        "subject_ref": subject_ref,
        "authorized_lanes": [lane for lane in SCOPE_LANES if lane in set(lanes)],
        "read_only": True,
    }


def compute_scope_sha256(scope: Mapping[str, Any]) -> str:
    """授权可见范围的稳定摘要（第一道确认绑定的对象）。

    先归一再哈希：确认绑定的必须是校验过的闭合对象，而不是调用方递进来的原物。
    """

    return hashlib.sha256(
        _SCOPE_DOMAIN + canonical_json_bytes(normalize_scope(scope))
    ).hexdigest()


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
        # 归一后的闭合对象，不是调用方原物：报告要被 AI 念给用户，不能替任意
        # 输入当传声筒。
        "authorized_visible_scope": normalize_scope(scope),
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
    """从**实际配置 + 实际环境**渲染每日执行合同。

    取值规则不是本模块自己发明的，而是 `cwk_nightly_pipeline` 在
    `--config/--run-name/--date` 这条固定 argv 下的真实行为（见
    :func:`resolve_nightly_runtime`）：整数是 config > env > 默认，布尔开关里
    `backfill_enabled` / `source_completeness` 是 env > config > 默认，而
    `sync_docdb` 又回到 config > env > False。合同照抄这份不对称，因为它描述的
    是现实，不是理想。

    额外记录两件事：每个取值**来自哪一层**，以及**去掉 shell 里的 CWK_* 变量后
    结果是否相同**。后者决定交接单能不能出——被排期的任务只拿得到 CWORK_APP_KEY。
    """

    env = env if env is not None else os.environ

    resolved = resolve_nightly_runtime(config, env)
    settings = resolved["settings"]
    sources = resolved["sources"]
    # 被排期的任务看不到任何 CWK_* 变量，用空环境再算一次就是它真实的取值。
    scheduled = resolve_nightly_runtime(config, {})["settings"]
    env_only = [key for key in NIGHTLY_SETTING_KEYS if settings[key] != scheduled[key]]

    caps = {
        "detail_cap": settings["detail_cap"],
        "continuation_cap": settings["continuation_cap"],
        "backfill_cap": settings["backfill_cap"],
        "backfill_page_size": settings["backfill_page_size"],
    }
    backfill_enabled = settings["backfill_enabled"]
    source_completeness = settings["source_completeness"]
    sync_docdb = settings["sync_docdb"]
    lookback = settings["source_completeness_lookback_days"]

    contract = {
        "schema": EXECUTION_CONTRACT_SCHEMA,
        "generated_at": generated_at,
        "profile_sha256": profile_sha256,
        "sources": {
            "daily_lanes": list(DAILY_LANES),
            "backfill_lanes": list(BACKFILL_LANES) if backfill_enabled else [],
            "backfill_enabled": backfill_enabled,
            "backfill_rotation": "round_robin",
            # 补采关掉时 late_data_lookback_days 那一趟根本不会跑，所以这个开关
            # 必须和天数一起出现，否则合同会声称一个不存在的回溯范围。
            "source_completeness_enabled": source_completeness,
        },
        "caps": caps,
        "runtime_resolution": {
            "sources": dict(sources),
            # 交接单 argv 只有 --config/--run-name/--date，命令行那一层不参与取值。
            "resolved_for_argv": "config_run_name_date_only",
            "scheduled_environment_equivalent": not env_only,
            "settings_requiring_shell_environment": env_only,
        },
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


_SOURCE_LABELS = {
    "config": "配置文件",
    "env": "当前 shell 的环境变量",
    "default": "上游默认值",
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
        f"- 来源完整性补采：{'开' if sources.get('source_completeness_enabled') else '关'}",
        f"- 迟到数据回看：前 {contract['late_data_lookback_days']} 个业务日"
        + ("" if sources.get("source_completeness_enabled") else "（补采已关，本项不生效）"),
        "",
        "## 产出与发布",
        f"- 产物：{', '.join(contract['outputs'])}",
        f"- 派生内容发布到 DocDB：{'开' if publishing['sync_docdb'] else '关'}",
        "- raw 原文：只留在本地，不回写、不上传",
        "",
        "## 这些取值从哪里来",
    ]
    resolution = contract.get("runtime_resolution") or {}
    for name in NIGHTLY_SETTING_KEYS:
        origin = (resolution.get("sources") or {}).get(name)
        if origin:
            lines.append(f"- {name}：{_SOURCE_LABELS.get(origin, origin)}")
    needs_shell = list(resolution.get("settings_requiring_shell_environment") or [])
    if needs_shell:
        lines.extend(
            [
                "",
                "> 警告：以下取值来自当前 shell 的 CWK_* 环境变量，而被排期的任务"
                "只会拿到 CWORK_APP_KEY，届时会解析出**另一个值**："
                + "、".join(needs_shell)
                + "。把它们写进配置文件后重新渲染，否则不会出交接单。",
            ]
        )
    lines.extend(["", "## 绝不执行的动作"])
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

    合同若依赖当前 shell 的 CWK_* 变量，同样拒绝出单（`ScheduledEnvironmentMismatch`）：
    env_allowlist 只有 CWORK_APP_KEY，宿主那条任务复现不出这份合同。
    """

    resolution = contract.get("runtime_resolution") or {}
    needs_shell = list(resolution.get("settings_requiring_shell_environment") or [])
    if needs_shell:
        raise ScheduledEnvironmentMismatch(
            "the contract depends on shell environment variables the scheduled task will "
            f"not receive ({', '.join(sorted(needs_shell))}); move those values into the "
            "config file, re-render the contract and re-run the pilot"
        )
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
