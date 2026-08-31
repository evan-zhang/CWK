#!/usr/bin/env python3
"""governance-audit — 当前代码树的归属与演化管辖自检（RT-030 建立）。

为什么需要它
------------
在 RT-030 之前，本仓库唯一的代码归属机制是 PR-001 安全登记表里的
``managed_script_inventory``：它只覆盖 ``^scripts/cwk_[a-z0-9_]+\\.py$`` 这一个命名
空间加 ``install.sh``，合计 65 个文件。其余 513 个受跟踪文件没有任何归属判据。
其中至少 6 个是真正的运行时产品文件——最典型的是
``scripts/cwk_wiki_batch_driver.sh``：100755 可执行、带默认模型字面量、会写
``runs/``，却仅仅因为后缀是 ``.sh`` 就被上面那条正则静默漏掉。

本脚本回答一个问题：**当前这棵树上，每个文件归谁管、怎么改？**
判据面是 ``git ls-files`` 全集，不是「新增的才算」。

用法
----
    make governance-audit                                  # 推荐入口
    python3 .aodw-next/06-project/governance-audit.py [--root <dir>] [--json]

退出码：0 全绿 / 1 有硬失败 / 2 用法或内部错误
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

MANIFEST_REL = ".aodw-next/06-project/governance/code-ownership-manifest.json"
OVERLAY_REL = ".aodw-next/06-project/governance/script-evolution-v2.json"

MANIFEST_SCHEMA = "cwk.governance.code_ownership_manifest.v1"
OVERLAY_SCHEMA = "cwk.governance.script_evolution_overlay.v2"
V2_RECEIPT_SCHEMA = "cwk.governance.script_evolution_receipt.v2"

_RULE_KINDS = {"exact", "exact_set", "prefix", "delegated"}


class Finding:
    """一条判据结果。severity 为 error 的条目决定退出码。"""

    __slots__ = ("severity", "code", "message")

    def __init__(self, severity: str, code: str, message: str) -> None:
        self.severity = severity
        self.code = code
        self.message = message

    def as_dict(self) -> dict:
        return {"severity": self.severity, "code": self.code, "message": self.message}

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{self.severity} {self.code}: {self.message}>"


class AuditResult:
    def __init__(self) -> None:
        self.findings: List[Finding] = []
        self.stats: Dict[str, object] = {}

    def error(self, code: str, message: str) -> None:
        self.findings.append(Finding("error", code, message))

    def warn(self, code: str, message: str) -> None:
        self.findings.append(Finding("warn", code, message))

    @property
    def errors(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "warn"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def codes(self) -> Set[str]:
        return {f.code for f in self.findings}

    def error_codes(self) -> Set[str]:
        return {f.code for f in self.errors}


# ── 基础工具 ────────────────────────────────────────────────────────────────


def sha256_file(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def file_mode(path: Path) -> Optional[str]:
    try:
        st = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode):
        return "120000"
    return "100755" if st.st_mode & stat.S_IXUSR else "100644"


def collect_tracked_files(root: Path) -> Optional[List[str]]:
    """受跟踪文件全集。用 -z 以正确处理非 ASCII 与空格路径。"""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(root),
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return sorted(p for p in out.decode("utf-8").split("\0") if p)


def load_json(path: Path) -> Optional[object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ── 规则匹配 ────────────────────────────────────────────────────────────────


def resolve_delegated_set(
    root: Path, manifest: dict, rule: dict, result: AuditResult
) -> Set[str]:
    """把 delegated 规则解析成一个**封闭的声明集合**。

    这是本清单不使用宽泛 glob 的关键：成员资格由上游权威文件里已声明的路径决定，
    而不是由通配符决定。因此往 scripts/ 里新增一个 cwk_*.py 不会被自动吸收。
    """
    authority_id = rule.get("authority")
    selector = rule.get("selector")
    authority = next(
        (a for a in manifest.get("upstream_authorities", []) if a.get("id") == authority_id),
        None,
    )
    if authority is None:
        result.error(
            "GA-RULE-AUTHORITY",
            f"规则 {rule.get('id')} 引用了未声明的权威 {authority_id!r}",
        )
        return set()
    data = load_json(root / authority["path"])
    if not isinstance(data, dict):
        result.error(
            "GA-RULE-AUTHORITY",
            f"规则 {rule.get('id')} 的权威文件无法解析：{authority['path']}",
        )
        return set()
    if selector != "managed_script_inventory":
        result.error(
            "GA-RULE-SELECTOR", f"规则 {rule.get('id')} 使用了未知 selector {selector!r}"
        )
        return set()

    inventory = data.get("managed_script_inventory")
    if not isinstance(inventory, dict):
        result.error("GA-RULE-SELECTOR", "权威文件缺少 managed_script_inventory")
        return set()

    # 只接受该权威**自己**的命名空间语义：命名空间正则 + explicit_managed_paths。
    # 这与 PR-001 自身的闭合判据完全一致
    # （tests/test_pr001_security_gate_contracts.py::
    #   test_managed_script_inventory_is_a_closed_three_family_partition）。
    #
    # 这道过滤不可省略：entries[].owner_code_path_prefixes 里同时装着
    # RT/RT-0NN/specs/*.md、tasks/*.md 这类文档选择器。不过滤的话，31 个文档
    # 会被这条 runtime 规则吸走并被当成产品代码——那恰好是本 RT 要消灭的
    # 「宽泛选择器吞掉未知文件」。
    pattern = inventory.get("namespace_pattern")
    if not isinstance(pattern, str):
        result.error("GA-RULE-SELECTOR", "权威文件缺少 namespace_pattern")
        return set()
    try:
        namespace = re.compile(pattern)
    except re.error as exc:
        result.error("GA-RULE-SELECTOR", f"namespace_pattern 非法正则：{exc}")
        return set()
    explicit = {
        path for path in inventory.get("explicit_managed_paths", []) if isinstance(path, str)
    }

    def admit(path: object) -> bool:
        return isinstance(path, str) and (
            bool(namespace.fullmatch(path)) or path in explicit
        )

    declared: Set[str] = set(explicit)
    for row in inventory.get("legacy_frozen_files", []):
        if isinstance(row, dict) and admit(row.get("path")):
            declared.add(row["path"])
    for entry in data.get("entries", []):
        if not isinstance(entry, dict):
            continue
        for path in entry.get("owner_code_path_prefixes", []):
            if admit(path):
                declared.add(path)
    for dep in data.get("shared_abi_dependencies", []):
        if not isinstance(dep, dict):
            continue
        for binding in dep.get("exact_paths", []):
            if isinstance(binding, dict) and admit(binding.get("path")):
                declared.add(binding["path"])
    return declared


def rule_matches(rule: dict, path: str, delegated: Set[str]) -> bool:
    kind = rule.get("kind")
    if kind == "exact":
        return path == rule.get("path")
    if kind == "exact_set":
        return path in set(rule.get("paths") or [])
    if kind == "prefix":
        prefix = rule.get("prefix") or ""
        return bool(prefix) and path.startswith(prefix)
    if kind == "delegated":
        return path in delegated
    return False


def in_zone(path: str, zone: str) -> bool:
    """空串代表仓库根目录（路径中不含 /）。"""
    if zone == "":
        return "/" not in path
    return path.startswith(zone)


# ── 各项判据 ────────────────────────────────────────────────────────────────


def check_manifest_shape(manifest: dict, result: AuditResult) -> None:
    if manifest.get("schema") != MANIFEST_SCHEMA:
        result.error(
            "GA-SCHEMA", f"清单 schema 应为 {MANIFEST_SCHEMA}，实际 {manifest.get('schema')!r}"
        )
    for key in ("rules", "domains", "evolution_mechanisms", "exact_only_zones"):
        if not isinstance(manifest.get(key), list):
            result.error("GA-SCHEMA", f"清单缺少列表字段 {key}")
    seen: Set[str] = set()
    for rule in manifest.get("rules", []):
        rid = rule.get("id")
        if not rid:
            result.error("GA-SCHEMA", "存在没有 id 的规则")
            continue
        if rid in seen:
            result.error("GA-SCHEMA", f"规则 id 重复：{rid}")
        seen.add(rid)
        if rule.get("kind") not in _RULE_KINDS:
            result.error("GA-SCHEMA", f"规则 {rid} 的 kind 非法：{rule.get('kind')!r}")


def check_upstream_integrity(root: Path, manifest: dict, result: AuditResult) -> None:
    """上游冻结契约必须逐字节相符——『没有伪造历史』的可复验证明。"""
    for authority in manifest.get("upstream_authorities", []):
        path = root / authority["path"]
        actual = sha256_file(path)
        if actual is None:
            result.error("GA-UPSTREAM", f"权威文件缺失：{authority['path']}")
            continue
        if actual != authority.get("sha256"):
            result.error(
                "GA-UPSTREAM",
                f"权威文件已被改动：{authority['path']}\n"
                f"      期望 {authority.get('sha256')}\n"
                f"      实际 {actual}\n"
                "      冻结契约不允许原地修改；需要演化请走前向叠加层。",
            )


def check_classification_closure(
    root: Path, manifest: dict, tracked: Sequence[str], result: AuditResult
) -> Dict[str, dict]:
    """每个受跟踪文件必须被恰好一条规则（按顺序首次命中）认领。"""
    rules = manifest.get("rules", [])
    delegated_cache: Dict[str, Set[str]] = {}
    for rule in rules:
        if rule.get("kind") == "delegated":
            delegated_cache[rule["id"]] = resolve_delegated_set(root, manifest, rule, result)

    assignment: Dict[str, dict] = {}
    hits: Dict[str, int] = {r.get("id"): 0 for r in rules}
    orphans: List[str] = []

    for path in tracked:
        matched = None
        for rule in rules:
            if rule_matches(rule, path, delegated_cache.get(rule.get("id"), set())):
                matched = rule
                break
        if matched is None:
            orphans.append(path)
        else:
            assignment[path] = matched
            hits[matched["id"]] += 1

    if orphans:
        listed = "\n".join(f"        {p}" for p in orphans[:40])
        more = "" if len(orphans) <= 40 else f"\n        …… 另有 {len(orphans) - 40} 个"
        result.error(
            "GA-ORPHAN",
            f"有 {len(orphans)} 个受跟踪文件没有任何归属规则认领：\n{listed}{more}\n"
            "      每个文件都必须有主。请在 code-ownership-manifest.json 里为它登记"
            "归属、管理域、变更入口与演化路径。",
        )

    for rule in rules:
        if hits.get(rule.get("id"), 0) == 0:
            result.error(
                "GA-STALE-RULE",
                f"规则 {rule.get('id')} 匹配到 0 个文件——失效规则会让清单慢慢偏离现实，"
                "请删除它或修正它的选择器。",
            )

    result.stats["tracked_total"] = len(tracked)
    result.stats["orphans"] = len(orphans)
    result.stats["rule_hits"] = hits
    return assignment


def check_exact_only_zones(
    manifest: dict, assignment: Dict[str, dict], result: AuditResult
) -> None:
    """exact-only 区里禁止前缀规则——这是防管理盲区的主要机制。"""
    zones = manifest.get("exact_only_zones", [])
    for rule in manifest.get("rules", []):
        if rule.get("kind") != "prefix":
            continue
        prefix = rule.get("prefix") or ""
        if not prefix or prefix in (".", "./", "/"):
            result.error(
                "GA-ZONE", f"规则 {rule.get('id')} 使用了会吞掉整棵树的前缀 {prefix!r}"
            )
            continue
        for zone in zones:
            if zone == "":
                continue
            if prefix.startswith(zone) or zone.startswith(prefix):
                result.error(
                    "GA-ZONE",
                    f"规则 {rule.get('id')} 在 exact-only 区 {zone!r} 内使用了前缀规则"
                    f" {prefix!r}。该区只允许精确路径或委派规则，否则新放进去的文件会被"
                    "静默吸收，正是 RT-030 要消除的盲区。",
                )

    # 正向确认：区内每个文件确实由精确/委派规则认领。
    for path, rule in assignment.items():
        for zone in zones:
            if in_zone(path, zone) and rule.get("kind") == "prefix":
                result.error(
                    "GA-ZONE",
                    f"{path} 位于 exact-only 区 {zone!r}，却被前缀规则 {rule.get('id')} 认领",
                )


def check_domain_requirements(manifest: dict, result: AuditResult) -> None:
    domains = {d["id"]: d for d in manifest.get("domains", []) if isinstance(d, dict)}
    mechanisms = {
        m["id"] for m in manifest.get("evolution_mechanisms", []) if isinstance(m, dict)
    }
    for rule in manifest.get("rules", []):
        rid = rule.get("id")
        domain_id = rule.get("domain")
        domain = domains.get(domain_id)
        if domain is None:
            result.error("GA-DOMAIN", f"规则 {rid} 引用了未声明的 domain {domain_id!r}")
            continue
        if domain.get("requires_owner") and not rule.get("owner"):
            result.error(
                "GA-OWNER",
                f"规则 {rid}（domain={domain_id}）没有 owner。"
                "runtime 文件无主等于没人为它的变更负责。",
            )
        if domain.get("requires_change_entry") and not rule.get("change_entry"):
            result.error(
                "GA-CHANGE-ENTRY", f"规则 {rid}（domain={domain_id}）没有 change_entry"
            )
        if domain.get("requires_evolution_path"):
            path_id = rule.get("evolution_path")
            if not path_id:
                result.error(
                    "GA-EVOLUTION",
                    f"规则 {rid}（domain={domain_id}）没有 evolution_path。"
                    "受管控文件必须有一条『怎样才算被授权地改』的路径，"
                    "不能只靠固定哈希永久放行。",
                )
            elif path_id not in mechanisms:
                result.error(
                    "GA-EVOLUTION",
                    f"规则 {rid} 的 evolution_path {path_id!r} 不是已登记的演化机制",
                )


def check_sensitive_pins(root: Path, manifest: dict, result: AuditResult) -> None:
    """敏感文件的指纹漂移探测。pin 不是放行条件，是探测器。"""
    for rule in manifest.get("rules", []):
        if not rule.get("sensitive"):
            continue
        pin = rule.get("pin")
        target = rule.get("path")
        if not target:
            result.error("GA-PIN", f"规则 {rule.get('id')} 标了 sensitive 但没有单一 path")
            continue
        if not isinstance(pin, dict) or not pin.get("sha256"):
            result.error("GA-PIN", f"敏感规则 {rule.get('id')} 缺少 pin.sha256")
            continue
        if not rule.get("evolution_path"):
            result.error(
                "GA-PIN",
                f"敏感规则 {rule.get('id')} 只有 pin 没有 evolution_path——"
                "这正是『靠固定哈希永久放行』的形态。",
            )
        full = root / target
        actual = sha256_file(full)
        if actual is None:
            result.error("GA-PIN", f"敏感文件缺失：{target}")
            continue
        if actual != pin["sha256"]:
            result.error(
                "GA-PIN-DRIFT",
                f"敏感文件内容已变但清单未更新：{target}\n"
                f"      清单 {pin['sha256']}\n"
                f"      磁盘 {actual}\n"
                f"      授权改法：{rule.get('change_entry')}",
            )
        actual_mode = file_mode(full)
        if pin.get("mode") and actual_mode != pin["mode"]:
            result.error(
                "GA-PIN-DRIFT",
                f"敏感文件权限位变化：{target} 清单 {pin['mode']} / 磁盘 {actual_mode}",
            )


# ── 前向演化叠加层（DI-001 / DI-002） ───────────────────────────────────────


def _load_v2_receipts(root: Path) -> List[Tuple[str, dict]]:
    receipts: List[Tuple[str, dict]] = []
    rt_dir = root / "RT"
    if not rt_dir.is_dir():
        return receipts
    for owner_dir in sorted(rt_dir.iterdir()):
        target = owner_dir / "receipts" / "script-evolution-v2"
        if not target.is_dir():
            continue
        for item in sorted(target.iterdir()):
            if item.is_file() and item.suffix == ".json":
                data = load_json(item)
                rel = str(item.relative_to(root))
                if isinstance(data, dict):
                    receipts.append((rel, data))
                else:
                    receipts.append((rel, {}))
    return receipts


def check_overlay(root: Path, overlay: dict, result: AuditResult) -> None:
    if overlay.get("schema") != OVERLAY_SCHEMA:
        result.error("GA-V2-SCHEMA", f"叠加层 schema 应为 {OVERLAY_SCHEMA}")
        return

    inherits = overlay.get("inherits") or {}
    # CR-1 继承完整性
    policy_path = root / inherits.get("policy_ref", "")
    policy_sha = sha256_file(policy_path)
    if policy_sha is None:
        result.error("GA-V2-INHERIT", f"找不到 v1 策略：{inherits.get('policy_ref')}")
        return
    if policy_sha != inherits.get("policy_sha256"):
        result.error(
            "GA-V2-INHERIT",
            "CR-1 违反：policy_v1.json 已被改写。前向叠加层的全部意义就是不动 v1；"
            f"期望 {inherits.get('policy_sha256')} 实际 {policy_sha}",
        )
        return
    registry_path = root / inherits.get("registry_ref", "")
    registry = load_json(registry_path)
    registry_sha = sha256_file(registry_path)
    if not isinstance(registry, dict) or registry_sha is None:
        result.error("GA-V2-INHERIT", "找不到或无法解析安全登记表")
        return
    if registry_sha != inherits.get("registry_sha256"):
        result.error(
            "GA-V2-INHERIT",
            f"CR-1 违反：安全登记表已被改写；期望 {inherits.get('registry_sha256')} "
            f"实际 {registry_sha}",
        )
    # CR-2 交叉一致
    if registry.get("script_evolution_policy_sha256") != inherits.get("policy_sha256"):
        result.error(
            "GA-V2-INHERIT",
            "CR-2 违反：登记表内记录的 policy sha256 与叠加层继承的不一致——"
            "两个独立来源必须指向同一版 v1。",
        )

    policy = load_json(policy_path)
    if not isinstance(policy, dict):
        result.error("GA-V2-INHERIT", "v1 策略无法解析")
        return
    v1_paths = {
        row["target_path"]: row
        for row in policy.get("evolvable_paths", [])
        if isinstance(row, dict) and isinstance(row.get("target_path"), str)
    }

    receipts = _load_v2_receipts(root)
    by_path: Dict[str, List[Tuple[str, dict]]] = {}
    declared_slots = {
        slot.get("target_path")
        for slot in overlay.get("continuation_slots", [])
        if isinstance(slot, dict)
    }
    legacy_pins = {
        row["path"]: row
        for row in (registry.get("managed_script_inventory") or {}).get(
            "legacy_frozen_files", []
        )
        if isinstance(row, dict) and isinstance(row.get("path"), str)
    }

    for rel, data in receipts:
        if data.get("schema") != V2_RECEIPT_SCHEMA:
            result.error("GA-V2-RECEIPT", f"v2 回执 schema 非法：{rel}")
            continue
        missing = [
            f for f in overlay.get("receipt_required_fields", []) if not data.get(f)
        ]
        if missing:
            result.error("GA-V2-RECEIPT", f"v2 回执 {rel} 缺字段：{', '.join(missing)}")
            continue
        target = data["target_path"]
        if target not in declared_slots and target not in legacy_pins:
            result.error(
                "GA-V2-RECEIPT",
                f"v2 回执 {rel} 指向的 {target} 既不是续演槽位、也不是 legacy 成员",
            )
            continue
        expected_dir = f"RT/{data['owner_rt']}/receipts/script-evolution-v2/"
        if not rel.startswith(expected_dir):
            result.error(
                "GA-V2-RECEIPT",
                f"CR-5/CR-6 违反：回执 {rel} 落点与 owner_rt {data['owner_rt']} 不符",
            )
            continue
        note = root / data["migration_note_ref"]
        if not note.is_file():
            result.error(
                "GA-V2-RECEIPT",
                f"v2 回执 {rel} 的 migration_note_ref 指向不存在的文件："
                f"{data['migration_note_ref']}",
            )
            continue
        by_path.setdefault(target, []).append((rel, data))

    # CR-3 / CR-4：续演槽位的序号与链尖交接
    for slot in overlay.get("continuation_slots", []):
        if not isinstance(slot, dict):
            continue
        target = slot.get("target_path")
        v1_row = v1_paths.get(target)
        if v1_row is None:
            result.error(
                "GA-V2-SLOT", f"续演槽位 {target} 在 v1 的 evolvable_paths 里不存在"
            )
            continue
        if slot.get("v1_max_ordinal") != v1_row.get("max_ordinal"):
            result.error(
                "GA-V2-SLOT",
                f"槽位 {target} 记录的 v1_max_ordinal 与 v1 实际值不符"
                f"（{slot.get('v1_max_ordinal')} vs {v1_row.get('max_ordinal')}）",
            )
        if slot.get("v2_ordinal_start") != (v1_row.get("max_ordinal") or 0) + 1:
            result.error(
                "GA-V2-SLOT",
                f"CR-3 违反：槽位 {target} 的 v2_ordinal_start 必须等于 v1 max_ordinal+1",
            )
        owner_rts = set(slot.get("owner_rts") or [])
        if not owner_rts <= set(v1_row.get("owner_rts") or []):
            result.error(
                "GA-V2-SLOT",
                f"CR-6 违反：槽位 {target} 的 owner 超出 v1 声明的 owner_rts",
            )

        disk = sha256_file(root / target)
        if disk is None:
            result.error("GA-V2-SLOT", f"续演槽位目标文件缺失：{target}")
            continue
        tip = slot.get("v1_chain_tip_sha256")
        chain = sorted(by_path.get(target, []), key=lambda item: item[1].get("ordinal", 0))
        expected_ordinal = slot.get("v2_ordinal_start")
        for rel, data in chain:
            if data.get("ordinal") != expected_ordinal:
                result.error(
                    "GA-V2-CHAIN",
                    f"v2 链序号不连续：{rel} 期望 ordinal={expected_ordinal}"
                    f" 实际 {data.get('ordinal')}",
                )
                break
            if data.get("ordinal") > slot.get("v2_max_ordinal", 0):
                result.error(
                    "GA-V2-CHAIN", f"{rel} 的 ordinal 超出槽位 v2_max_ordinal"
                )
                break
            if data.get("from_sha256") != tip:
                result.error(
                    "GA-V2-CHAIN",
                    f"CR-4 违反：{rel} 的 from_sha256 与上一链尖不符\n"
                    f"      期望 {tip}\n      实际 {data.get('from_sha256')}",
                )
                break
            if data.get("owner_rt") not in owner_rts:
                result.error(
                    "GA-V2-CHAIN", f"CR-6 违反：{rel} 的 owner_rt 不在槽位 owner 列表内"
                )
                break
            tip = data.get("to_sha256")
            expected_ordinal += 1
        else:
            if tip != disk:
                result.error(
                    "GA-V2-CHAIN",
                    f"CR-4 违反：{target} 的链尖与磁盘内容不符。\n"
                    f"      链尖 {tip}\n      磁盘 {disk}\n"
                    "      该文件被改过但没有留下 v2 回执。",
                )

    # DI-002：legacy 族的有主演化路径
    check_legacy_family(root, overlay, legacy_pins, by_path, result)


def check_legacy_family(
    root: Path,
    overlay: dict,
    legacy_pins: Dict[str, dict],
    by_path: Dict[str, List[Tuple[str, dict]]],
    result: AuditResult,
) -> None:
    legacy = overlay.get("legacy_evolution") or {}
    if not legacy.get("default_steward"):
        result.error(
            "GA-LEGACY-OWNER",
            "legacy_evolution 没有 default_steward——53 个 legacy 文件会重新变成无主状态"
            "（DI-002 的原始症状）。",
        )
    declared_count = legacy.get("member_count")
    if declared_count is not None and declared_count != len(legacy_pins):
        result.error(
            "GA-LEGACY-OWNER",
            f"legacy 族成员数变化：叠加层记 {declared_count}，登记表实有 {len(legacy_pins)}",
        )
    if not legacy.get("authorized_change_procedure"):
        result.error(
            "GA-LEGACY-OWNER", "legacy_evolution 缺少 authorized_change_procedure"
        )

    for path, row in sorted(legacy_pins.items()):
        disk = sha256_file(root / path)
        if disk is None:
            result.error("GA-LEGACY-DRIFT", f"legacy 成员文件缺失：{path}")
            continue
        if disk == row.get("sha256"):
            continue
        # 漂移了：必须有一份能解释它的 v2 回执，而不是直接改指纹放行。
        explained = False
        for rel, data in by_path.get(path, []):
            if data.get("from_sha256") == row.get("sha256") and data.get("to_sha256") == disk:
                explained = True
                break
        if not explained:
            result.error(
                "GA-LEGACY-DRIFT",
                f"legacy 成员 {path} 的内容与登记表指纹不符，且没有任何 v2 回执解释它。\n"
                f"      指纹 {row.get('sha256')}\n      磁盘 {disk}\n"
                "      授权改法见 script-evolution-v2.json 的 authorized_change_procedure：\n"
                "      开 RT → 写 v2 回执（from=指纹, to=新哈希）→ 写 migration note → "
                "更新登记表指纹 → 复跑门禁。",
            )


# ── 例外与补偿控制（DI-003） ────────────────────────────────────────────────


def _makefile_targets(text: str) -> Dict[str, List[str]]:
    """极小的 Makefile 目标解析：目标名 -> 配方行。避免引入依赖。"""
    targets: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        if line.startswith("\t"):
            if current:
                targets.setdefault(current, []).append(line.strip())
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z0-9_.\-/ ]+):(?!=)", line)
        if match:
            names = match.group(1).split()
            current = names[0] if names else None
            for name in names:
                targets.setdefault(name, [])
        elif "=" in stripped:
            current = None
    return targets


def _workflow_runs_make_ci(text: str) -> bool:
    """判断 workflow 里是否真的有一个 `run:` 步骤在执行 `make ci`。

    不能对全文做 `make ci` 的子串匹配——ci.yml 的注释里就写着
    「make ci = make test + …」。那样一来，把 `run: make ci` 改成
    `run: make test` 而注释照留，这条补偿控制会假绿。
    只认可执行位置：`run:` 的行内标量，或 `run: |` 块标量里的命令行。
    """
    block_scalars = {"|", ">", "|-", ">-", "|+", ">+"}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        match = re.match(r"^(\s*)(?:-\s+)?run:\s*(.*)$", line)
        if not match:
            continue
        indent, inline = match.group(1), match.group(2).strip()
        if inline and inline not in block_scalars:
            if re.search(r"\bmake\s+ci\b", inline.split("#", 1)[0]):
                return True
            continue
        # 块标量：往下吃掉所有缩进更深的行。
        base = len(indent)
        while index < len(lines):
            body = lines[index]
            if body.strip() and (len(body) - len(body.lstrip())) <= base:
                break
            index += 1
            command = body.strip()
            if command.startswith("#"):
                continue
            if re.search(r"\bmake\s+ci\b", command.split("#", 1)[0]):
                return True
    return False


def check_exceptions(root: Path, manifest: dict, result: AuditResult, today: _dt.date) -> None:
    exceptions = manifest.get("exceptions", [])
    for exc in exceptions:
        eid = exc.get("id", "<无 id>")
        for field in ("owner", "trigger_condition", "exit_criteria", "review_by", "scope_limit"):
            if not exc.get(field):
                result.error(
                    "GA-EXCEPTION",
                    f"例外 {eid} 缺少 {field}。无边界的例外就是永久豁免——"
                    "每条例外必须有主、有触发条件、有退出标准和复查日期。",
                )
        review_by = exc.get("review_by")
        if review_by:
            try:
                due = _dt.date.fromisoformat(review_by)
            except ValueError:
                result.error("GA-EXCEPTION", f"例外 {eid} 的 review_by 不是 ISO 日期：{review_by}")
            else:
                if due < today:
                    result.error(
                        "GA-EXCEPTION-EXPIRED",
                        f"例外 {eid} 已于 {review_by} 到期，必须重新评估：\n"
                        f"      退出标准：{exc.get('exit_criteria')}\n"
                        "      到期例外不会自动续期——这是它区别于永久 warn 的地方。",
                    )
        controls = exc.get("compensating_controls") or []
        if not controls:
            result.error(
                "GA-EXCEPTION",
                f"例外 {eid} 没有补偿控制。既然豁免了原有防线，就必须说明靠什么兜底。",
            )
        verify_compensating_controls(root, eid, controls, result)


def verify_compensating_controls(
    root: Path, eid: str, controls: Sequence[dict], result: AuditResult
) -> None:
    """补偿控制不是一句声明，必须当场可验证。"""
    makefile = root / "Makefile"
    text = makefile.read_text(encoding="utf-8") if makefile.is_file() else ""
    targets = _makefile_targets(text)
    ci_yml = root / ".github/workflows/ci.yml"
    ci_text = ci_yml.read_text(encoding="utf-8") if ci_yml.is_file() else ""

    for control in controls:
        cid = control.get("id", "<无 id>")
        if cid == "CC-1":
            if not _workflow_runs_make_ci(ci_text):
                result.error(
                    "GA-CONTROL",
                    f"例外 {eid} 的补偿控制 {cid} 失效："
                    ".github/workflows/ci.yml 没有任何 run: 步骤在执行 make ci。"
                    "DI-003 之所以可接受，前提就是 CI 是权威门。",
                )
        elif cid == "CC-2":
            recipe = " ".join(targets.get("ci", []))
            if "governance-audit" not in recipe:
                result.error(
                    "GA-CONTROL",
                    f"例外 {eid} 的补偿控制 {cid} 失效："
                    "make ci 里不再包含 governance-audit。"
                    "本 RT 建立的门被摘掉了。",
                )
        elif cid == "CC-3":
            if "governance-audit" not in targets:
                result.error(
                    "GA-CONTROL",
                    f"例外 {eid} 的补偿控制 {cid} 失效：Makefile 没有 governance-audit 目标",
                )
        else:
            result.error(
                "GA-CONTROL", f"例外 {eid} 声明了无法验证的补偿控制 {cid}"
            )


# ── 编排 ────────────────────────────────────────────────────────────────────


def audit(
    root: Path,
    *,
    tracked: Optional[Sequence[str]] = None,
    today: Optional[_dt.date] = None,
) -> AuditResult:
    result = AuditResult()
    today = today or _dt.date.today()
    root = Path(root)

    manifest = load_json(root / MANIFEST_REL)
    if not isinstance(manifest, dict):
        result.error("GA-SCHEMA", f"读不到或无法解析清单：{MANIFEST_REL}")
        return result

    check_manifest_shape(manifest, result)
    check_upstream_integrity(root, manifest, result)

    if tracked is None:
        tracked = collect_tracked_files(root)
        if tracked is None:
            result.error("GA-GIT", f"无法在 {root} 上执行 git ls-files")
            return result

    assignment = check_classification_closure(root, manifest, tracked, result)
    check_exact_only_zones(manifest, assignment, result)
    check_domain_requirements(manifest, result)
    check_sensitive_pins(root, manifest, result)
    check_exceptions(root, manifest, result, today)

    overlay = load_json(root / OVERLAY_REL)
    if not isinstance(overlay, dict):
        result.error("GA-V2-SCHEMA", f"读不到或无法解析前向叠加层：{OVERLAY_REL}")
    else:
        check_overlay(root, overlay, result)

    by_domain: Dict[str, int] = {}
    for rule in assignment.values():
        by_domain[rule.get("domain", "?")] = by_domain.get(rule.get("domain", "?"), 0) + 1
    result.stats["by_domain"] = by_domain
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="当前代码树的归属与演化管辖自检",
    )
    parser.add_argument("--root", default=".", help="仓库根目录")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not (root / ".aodw-next").is_dir():
        print(f"governance-audit: {root} 下没有 .aodw-next/", file=sys.stderr)
        return 2

    result = audit(root)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": result.ok,
                    "stats": result.stats,
                    "findings": [f.as_dict() for f in result.findings],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if result.ok else 1

    print("=== governance-audit：当前代码树归属自检 ===")
    total = result.stats.get("tracked_total", 0)
    by_domain = result.stats.get("by_domain", {}) or {}
    print(f"受跟踪文件：{total}")
    for domain in sorted(by_domain):
        print(f"  {domain:<18} {by_domain[domain]:>4}")

    for finding in result.findings:
        tag = "[FAIL]" if finding.severity == "error" else "[WARN]"
        print(f"governance-audit: {tag} {finding.code}: {finding.message}", file=sys.stderr)

    print()
    if not result.ok:
        print(f"governance-audit: 未通过（{len(result.errors)} 项硬失败）")
        return 1
    if result.warnings:
        print(f"governance-audit: 通过（{len(result.warnings)} 条告警，不阻断）")
    else:
        print("governance-audit: 通过——每个受跟踪文件都有主、有变更入口、有演化路径")
    return 0


if __name__ == "__main__":
    sys.exit(main())
