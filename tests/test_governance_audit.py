"""RT-030：当前代码树归属与演化管辖门禁的正反向测试。

这里的每一条负向测试都对应一种真实的绕过手法。门禁的价值不在于「当前是绿的」，
而在于「做错了会红」——所以下面大部分用例是故意把仓库改坏，然后断言它确实失败，
并且失败在预期的判据码上（而不是碰巧因为别的原因红了）。

合成仓库而不是改真仓库：负向用例需要把文件改坏、把清单改错，绝不能污染工作树，
更不能碰用户明确保护的 PR-001 工作树。
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = REPO_ROOT / ".aodw-next" / "06-project" / "governance-audit.py"

_spec = importlib.util.spec_from_file_location("cwk_governance_audit", AUDIT_PATH)
assert _spec and _spec.loader
ga = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ga)

REGISTRY_REL = (
    "PR/PR-001-multitenant-knowledge-spaces/contracts/security/"
    "security_gate_registry_v1.json"
)
POLICY_REL = (
    "PR/PR-001-multitenant-knowledge-spaces/contracts/script-evolution/policy_v1.json"
)

# 合成仓库里必须真实存在的目录：判据会去读它们的内容（哈希、指纹、配方）。
_COPY_DIRS = (
    "scripts",
    "config",
    "references",
    "skill/templates",
    ".aodw-next/06-project/governance",
)
_COPY_FILES = (
    "Makefile",
    ".github/workflows/ci.yml",
    REGISTRY_REL,
    POLICY_REL,
    # 门自身的机器：判据会核对它们的指纹（GA-SELF / GA-PIN），必须真实存在。
    # copy2 保留权限位，aodw-check.sh 的 100755 才不会被误报成漂移。
    ".aodw-next/06-project/governance-audit.py",
    ".aodw-next/06-project/aodw-check.sh",
    # v1 演化回执：GA-V2-SLOT 要当场数它们来判断「v1 槽位是否真的用尽」。
    # 不带过来的话，合成仓库里 v1 用量恒为 0，续演槽位会被判成「绕开 v1」。
    "RT/RT-012/receipts/script-evolution/stage-09-cwk-instance-ord1.json",
    "RT/RT-013/receipts/script-evolution/stage-10-cwk-agent-binding-ord1.json",
    "RT/RT-022/receipts/script-evolution/stage-06-cwk-wiki-query-ord1.json",
    "RT/RT-026/receipts/script-evolution/stage-08-cwk-nightly-pipeline-ord1.json",
    # RT-034：legacy 族 cwk_ai_common.py 的首次演化回执 + 迁移说明 + RT 记录。
    # scripts/ 整目录会被复制进合成仓库，GA-LEGACY-DRIFT 需要这份回执在场
    # 才能解释 pin 与磁盘的合法差异。
    "RT/RT-034/receipts/script-evolution-v2/rt034-exec-transport-ord1.json",
    "RT/RT-034/migration-notes/rt034-exec-transport-ord1.md",
    "RT/RT-034/rt-lite.md",
    "RT/RT-035/receipts/script-evolution-v2/rt035-agent-list-compat-ord1.json",
    "RT/RT-035/migration-notes/rt035-agent-list-compat-ord1.md",
    "RT/RT-035/rt-lite.md",
    "RT/RT-036/receipts/script-evolution-v2/rt036-owner-refine-scope-ord1.json",
    "RT/RT-036/migration-notes/rt036-owner-refine-scope-ord1.md",
    "RT/RT-036/rt-lite.md",
    # RT-037：legacy 族 cwk_backfill_range.py 的首次演化回执（inbox 秒级源切换）。
    "RT/RT-037/receipts/script-evolution-v2/rt037-inbox-seconds-source-ord1.json",
    "RT/RT-037/migration-notes/rt037-inbox-seconds-source-ord1.md",
    "RT/RT-037/rt-lite.md",
)

# 每条规则至少要有一个代表文件，否则会触发 GA-STALE-RULE（失效规则）。
# 这本身就是一次对清单的交叉验证：规则集变了，这个列表就得跟着变。
_REPRESENTATIVE_TRACKED = (
    "install.sh",
    "scripts/cwk_ai_common.py",
    "scripts/cwk_wiki_batch_driver.sh",
    "config/entity-family-registry.json",
    "references/relation-gold-v1.json",
    "skill/templates/CONFIG.example.json",
    "scripts/activation_state.py",
    "scripts/setup_app_key.py",
    "Makefile",
    ".github/workflows/ci.yml",
    ".gitignore",
    ".env.example",
    "VERSION",
    "tests/test_governance_audit.py",
    "AGENTS.md",
    "skill/SKILL.md",
    "skill-query/SKILL.md",
    "docs/DESIGN.md",
    "prompts/OPENCLAW_SANDBOX_BOOTSTRAP.md",
    "specs/技术方案.md",
    "tasks/开发任务.md",
    "reports/交付验证报告.md",
    "RT/index.yaml",
    REGISTRY_REL,
    ".aodw-next/06-project/governance/code-ownership-manifest.json",
    ".aodw-next/06-project/governance/script-evolution-v2.json",
    ".aodw-next/06-project/governance-audit.py",
    ".aodw-next/06-project/aodw-check.sh",
    ".aodw-next/06-project/ai-overview.md",
    ".aodw-next/01-core/aodw-constitution.md",
)

FIXED_TODAY = datetime.date(2026, 9, 1)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GovernanceRepo:
    """一个可以随意改坏的合成仓库。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.tracked = list(_REPRESENTATIVE_TRACKED)

    @property
    def manifest_path(self) -> Path:
        return self.root / ga.MANIFEST_REL

    @property
    def overlay_path(self) -> Path:
        return self.root / ga.OVERLAY_REL

    def manifest(self) -> dict:
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def write_manifest(self, data: dict) -> None:
        self.manifest_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def overlay(self) -> dict:
        return json.loads(self.overlay_path.read_text(encoding="utf-8"))

    def write_overlay(self, data: dict) -> None:
        self.overlay_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def rule(self, data: dict, rule_id: str) -> dict:
        return next(r for r in data["rules"] if r["id"] == rule_id)

    def write_file(self, rel: str, content: str) -> None:
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def add_v2_receipt(
        self,
        owner_rt: str,
        name: str,
        *,
        target_path: str,
        ordinal: int,
        from_sha: str,
        to_sha: str,
    ) -> None:
        note_rel = f"RT/{owner_rt}/migration-notes/{name}.md"
        self.write_file(note_rel, f"# migration note for {target_path}\n")
        receipt = {
            "schema": ga.V2_RECEIPT_SCHEMA,
            "overlay_version": "v2",
            "owner_rt": owner_rt,
            "target_path": target_path,
            "ordinal": ordinal,
            "from_sha256": from_sha,
            "to_sha256": to_sha,
            "migration_note_ref": note_rel,
            "rt_ref": f"RT/{owner_rt}/rt-lite.md",
        }
        self.write_file(
            f"RT/{owner_rt}/receipts/script-evolution-v2/{name}.json",
            json.dumps(receipt, ensure_ascii=False, indent=2),
        )

    def audit(self, today: datetime.date = FIXED_TODAY) -> "ga.AuditResult":
        return ga.audit(self.root, tracked=sorted(self.tracked), today=today)


class GovernanceAuditTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name) / "repo"
        root.mkdir()
        for rel in _COPY_DIRS:
            src = REPO_ROOT / rel
            if src.is_dir():
                shutil.copytree(src, root / rel, symlinks=True)
        for rel in _COPY_FILES:
            src = REPO_ROOT / rel
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        (root / ".aodw-next" / "01-core").mkdir(parents=True, exist_ok=True)
        self.repo = GovernanceRepo(root)

    def assertClean(self, result: "ga.AuditResult") -> None:
        self.assertTrue(
            result.ok,
            "合成基线本应是干净的，实际失败：\n"
            + "\n".join(f"  {f.code}: {f.message}" for f in result.errors),
        )

    def assertFailsWith(self, result: "ga.AuditResult", code: str) -> None:
        self.assertFalse(result.ok, f"本应失败但通过了（期望判据 {code}）")
        self.assertIn(
            code,
            result.error_codes(),
            f"失败了但判据码不对。期望 {code}，实际 {sorted(result.error_codes())}",
        )


class TestSyntheticBaselineIsClean(GovernanceAuditTestBase):
    def test_untouched_synthetic_repo_passes(self) -> None:
        """基线必须先是绿的，否则后面所有『改坏就红』的断言都没有意义。"""
        self.assertClean(self.repo.audit())


class TestOrphanDetection(GovernanceAuditTestBase):
    """核心承诺：新增一个没人认领的文件，门必须红。"""

    def test_new_runtime_python_script_is_an_orphan(self) -> None:
        # 关键用例：scripts/cwk_*.py 看起来「符合命名空间」，但 PR-001 的归属表
        # 是**封闭声明集合**，没声明就不算受管。它必须变成孤儿而不是被静默吸收。
        self.repo.tracked.append("scripts/cwk_brand_new_feature.py")
        result = self.repo.audit()
        self.assertFailsWith(result, "GA-ORPHAN")
        self.assertIn(
            "scripts/cwk_brand_new_feature.py",
            "\n".join(f.message for f in result.errors),
        )

    def test_new_shell_script_is_an_orphan(self) -> None:
        """RT-030 的起因：.sh 不匹配 .py 正则，此前会被静默漏掉。"""
        self.repo.tracked.append("scripts/cwk_new_driver.sh")
        self.assertFailsWith(self.repo.audit(), "GA-ORPHAN")

    def test_new_runtime_config_is_an_orphan(self) -> None:
        self.repo.tracked.append("config/new-runtime-registry.json")
        self.assertFailsWith(self.repo.audit(), "GA-ORPHAN")

    def test_new_root_file_is_an_orphan(self) -> None:
        self.repo.tracked.append("SOMETHING_NEW.md")
        self.assertFailsWith(self.repo.audit(), "GA-ORPHAN")

    def test_explicitly_classified_file_passes(self) -> None:
        """正向对照：显式登记之后就该通过——门是可用的，不是只会拒绝。"""
        self.repo.tracked.append("scripts/cwk_new_driver.sh")
        data = self.repo.manifest()
        data["rules"].insert(
            0,
            {
                "id": "R-runtime-new-driver",
                "domain": "runtime",
                "kind": "exact",
                "path": "scripts/cwk_new_driver.sh",
                "owner": "RT-030",
                "management_domain": "cwk-governance-v1",
                "change_entry": "RT → 更新清单 → make governance-audit",
                "evolution_path": "cwk-governance-repin-v1",
                "rationale": "测试用",
            },
        )
        self.repo.write_manifest(data)
        self.assertClean(self.repo.audit())


class TestRuntimeOwnershipRequirements(GovernanceAuditTestBase):
    """runtime / build_ci 文件必须有主、有变更入口、有演化路径。"""

    def test_runtime_rule_without_owner_fails(self) -> None:
        data = self.repo.manifest()
        self.repo.rule(data, "R-runtime-wiki-batch-driver").pop("owner")
        self.repo.write_manifest(data)
        self.assertFailsWith(self.repo.audit(), "GA-OWNER")

    def test_runtime_rule_without_evolution_path_fails(self) -> None:
        data = self.repo.manifest()
        self.repo.rule(data, "R-runtime-wiki-batch-driver").pop("evolution_path")
        self.repo.write_manifest(data)
        self.assertFailsWith(self.repo.audit(), "GA-EVOLUTION")

    def test_runtime_rule_without_change_entry_fails(self) -> None:
        data = self.repo.manifest()
        self.repo.rule(data, "R-runtime-entity-family-registry").pop("change_entry")
        self.repo.write_manifest(data)
        self.assertFailsWith(self.repo.audit(), "GA-CHANGE-ENTRY")

    def test_unregistered_evolution_mechanism_fails(self) -> None:
        data = self.repo.manifest()
        self.repo.rule(data, "R-runtime-relation-gold")["evolution_path"] = "made-up"
        self.repo.write_manifest(data)
        self.assertFailsWith(self.repo.audit(), "GA-EVOLUTION")

    def test_sensitive_file_with_pin_but_no_evolution_path_fails(self) -> None:
        """『只靠固定 hash 永久放行』这一形态本身要被判失败。"""
        data = self.repo.manifest()
        rule = self.repo.rule(data, "R-runtime-config-template")
        rule.pop("evolution_path")
        self.repo.write_manifest(data)
        result = self.repo.audit()
        self.assertFalse(result.ok)
        self.assertTrue(
            {"GA-PIN", "GA-EVOLUTION"} & result.error_codes(),
            f"期望 GA-PIN 或 GA-EVOLUTION，实际 {sorted(result.error_codes())}",
        )


class TestSensitiveDriftDetection(GovernanceAuditTestBase):
    def test_silent_edit_to_sensitive_runtime_file_fails(self) -> None:
        target = self.repo.root / "scripts/cwk_wiki_batch_driver.sh"
        target.write_text(
            target.read_text(encoding="utf-8") + '\necho "偷偷加一行"\n', encoding="utf-8"
        )
        self.assertFailsWith(self.repo.audit(), "GA-PIN-DRIFT")

    def test_silent_edit_to_runtime_config_data_fails(self) -> None:
        target = self.repo.root / "config/entity-family-registry.json"
        target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        self.assertFailsWith(self.repo.audit(), "GA-PIN-DRIFT")

    def test_repinning_after_authorized_change_passes(self) -> None:
        """正向对照：按 change_entry 更新 pin 之后应当通过。"""
        target = self.repo.root / "references/relation-gold-v1.json"
        target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        data = self.repo.manifest()
        self.repo.rule(data, "R-runtime-relation-gold")["pin"]["sha256"] = _sha256(target)
        self.repo.write_manifest(data)
        self.assertClean(self.repo.audit())


class TestExactOnlyZones(GovernanceAuditTestBase):
    def test_prefix_rule_inside_exact_only_zone_fails(self) -> None:
        """加宽 glob 是最容易的作弊手法，必须直接判失败。"""
        data = self.repo.manifest()
        data["rules"].insert(
            0,
            {
                "id": "R-cheat-scripts-catchall",
                "domain": "runtime",
                "kind": "prefix",
                "prefix": "scripts/",
                "owner": "RT-030",
                "management_domain": "cwk-governance-v1",
                "change_entry": "x",
                "evolution_path": "repo-standard-change",
                "rationale": "作弊：想用前缀吞掉整个 scripts/",
            },
        )
        self.repo.write_manifest(data)
        self.assertFailsWith(self.repo.audit(), "GA-ZONE")

    def test_whole_tree_catchall_prefix_fails(self) -> None:
        data = self.repo.manifest()
        data["rules"].append(
            {
                "id": "R-cheat-everything",
                "domain": "docs_governance",
                "kind": "prefix",
                "prefix": ".",
                "owner": "x",
                "management_domain": "x",
                "evolution_path": "repo-standard-change",
                "rationale": "作弊：吞掉整棵树",
            }
        )
        self.repo.write_manifest(data)
        self.assertFailsWith(self.repo.audit(), "GA-ZONE")


class TestStaleRules(GovernanceAuditTestBase):
    def test_rule_matching_nothing_fails(self) -> None:
        """失效规则会让清单慢慢偏离现实，必须当场暴露。"""
        data = self.repo.manifest()
        data["rules"].insert(
            0,
            {
                "id": "R-stale",
                "domain": "runtime",
                "kind": "exact",
                "path": "scripts/cwk_this_never_existed.py",
                "owner": "RT-030",
                "management_domain": "cwk-governance-v1",
                "change_entry": "x",
                "evolution_path": "cwk-governance-repin-v1",
                "rationale": "指向不存在的文件",
            },
        )
        self.repo.write_manifest(data)
        self.assertFailsWith(self.repo.audit(), "GA-STALE-RULE")


class TestUpstreamFrozenContracts(GovernanceAuditTestBase):
    """冻结契约被改写 = 伪造历史，必须硬失败。"""

    def test_tampering_with_frozen_policy_v1_fails(self) -> None:
        policy_path = self.repo.root / POLICY_REL
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["evolvable_paths"][7]["max_ordinal"] = 99  # 「顺手」扩容耗尽的槽位
        policy_path.write_text(
            json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        result = self.repo.audit()
        self.assertFalse(result.ok)
        self.assertTrue(
            {"GA-UPSTREAM", "GA-V2-INHERIT"} & result.error_codes(),
            f"期望上游完整性判据，实际 {sorted(result.error_codes())}",
        )

    def test_tampering_with_security_registry_fails(self) -> None:
        registry_path = self.repo.root / REGISTRY_REL
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["managed_script_inventory"]["legacy_frozen_files"][3]["sha256"] = "0" * 64
        registry_path.write_text(
            json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        result = self.repo.audit()
        self.assertFalse(result.ok)
        self.assertTrue(
            {"GA-UPSTREAM", "GA-V2-INHERIT"} & result.error_codes(),
            f"期望上游完整性判据，实际 {sorted(result.error_codes())}",
        )


class TestLegacyFamilyEvolution(GovernanceAuditTestBase):
    """DI-002：legacy 族从『只能改指纹放行』变成『带证据地改』。"""

    LEGACY_TARGET = "scripts/cwk_ai_common.py"

    def _pin_of(self, path: str) -> str:
        registry = json.loads((self.repo.root / REGISTRY_REL).read_text(encoding="utf-8"))
        row = next(
            r
            for r in registry["managed_script_inventory"]["legacy_frozen_files"]
            if r["path"] == path
        )
        return row["sha256"]

    def test_legacy_drift_without_receipt_fails(self) -> None:
        target = self.repo.root / self.LEGACY_TARGET
        target.write_text(
            target.read_text(encoding="utf-8") + "\n# 悄悄扩一个模型\n", encoding="utf-8"
        )
        self.assertFailsWith(self.repo.audit(), "GA-LEGACY-DRIFT")

    def test_legacy_drift_with_valid_v2_receipt_passes(self) -> None:
        """这就是 DI-002 此前缺失的那条路：有主、有回执、有 migration note。"""
        old_pin = self._pin_of(self.LEGACY_TARGET)
        target = self.repo.root / self.LEGACY_TARGET
        target.write_text(
            target.read_text(encoding="utf-8") + "\n# 授权的模型清单变更\n",
            encoding="utf-8",
        )
        self.repo.add_v2_receipt(
            "RT-030",
            "legacy-ai-common-ord1",
            target_path=self.LEGACY_TARGET,
            ordinal=1,
            from_sha=old_pin,
            to_sha=_sha256(target),
        )
        self.assertClean(self.repo.audit())

    def test_v2_receipt_with_wrong_from_hash_does_not_launder_drift(self) -> None:
        """回执必须真的接上指纹，不能拿一份内容对不上的回执糊弄过去。"""
        target = self.repo.root / self.LEGACY_TARGET
        target.write_text(
            target.read_text(encoding="utf-8") + "\n# 未授权改动\n", encoding="utf-8"
        )
        self.repo.add_v2_receipt(
            "RT-030",
            "bogus",
            target_path=self.LEGACY_TARGET,
            ordinal=1,
            from_sha="f" * 64,
            to_sha=_sha256(target),
        )
        self.assertFailsWith(self.repo.audit(), "GA-LEGACY-DRIFT")

    def test_receipt_with_missing_migration_note_fails(self) -> None:
        old_pin = self._pin_of(self.LEGACY_TARGET)
        target = self.repo.root / self.LEGACY_TARGET
        target.write_text(target.read_text(encoding="utf-8") + "\n# x\n", encoding="utf-8")
        self.repo.add_v2_receipt(
            "RT-030",
            "no-note",
            target_path=self.LEGACY_TARGET,
            ordinal=1,
            from_sha=old_pin,
            to_sha=_sha256(target),
        )
        (self.repo.root / "RT/RT-030/migration-notes/no-note.md").unlink()
        self.assertFailsWith(self.repo.audit(), "GA-V2-RECEIPT")

    def test_removing_default_steward_reopens_di002(self) -> None:
        data = self.repo.overlay()
        data["legacy_evolution"].pop("default_steward")
        self.repo.write_overlay(data)
        self.assertFailsWith(self.repo.audit(), "GA-LEGACY-OWNER")


class TestContinuationSlots(GovernanceAuditTestBase):
    """DI-001：耗尽槽位的前向续演，且不得改写 v1。"""

    def test_ordinal_start_must_continue_v1_without_gap_or_reuse(self) -> None:
        data = self.repo.overlay()
        data["continuation_slots"][0]["v2_ordinal_start"] = 1  # 复用 v1 已用序号
        self.repo.write_overlay(data)
        self.assertFailsWith(self.repo.audit(), "GA-V2-SLOT")

    def test_slot_for_a_path_with_free_v1_capacity_fails(self) -> None:
        """v2 是给用尽者的续命通道，不是绕开 v1 的旁路。

        cwk_tenant_cli.py 的 v1 上限是 2、一条回执都没用过。给它开 v2 槽位
        等于绕开 PR-001 已签名的既有机制去走一条更松的路——必须判失败。
        判据当场数 v1 回执，不接受「声称已用尽」。
        """
        data = self.repo.overlay()
        fresh = copy.deepcopy(data["continuation_slots"][0])
        fresh["target_path"] = "scripts/cwk_tenant_cli.py"
        fresh["owner_rts"] = ["RT-019"]
        fresh["v1_max_ordinal"] = 2
        fresh["v2_ordinal_start"] = 3
        data["continuation_slots"].append(fresh)
        self.repo.write_overlay(data)
        self.assertFailsWith(self.repo.audit(), "GA-V2-SLOT")

    def test_declared_slots_match_real_v1_exhaustion(self) -> None:
        """正向：现有两个槽位对应的 v1 路径确实一条不剩。"""
        used = ga._count_v1_receipts(self.repo.root)
        policy = json.loads(
            (self.repo.root / POLICY_REL).read_text(encoding="utf-8")
        )
        capacity = {
            row["target_path"]: row["max_ordinal"] for row in policy["evolvable_paths"]
        }
        for slot in self.repo.overlay()["continuation_slots"]:
            target = slot["target_path"]
            self.assertEqual(
                used.get(target, 0),
                capacity[target],
                f"{target} 的 v1 槽位并未用尽，不该有 v2 续演槽位",
            )

    def test_owner_outside_v1_declaration_fails(self) -> None:
        data = self.repo.overlay()
        data["continuation_slots"][0]["owner_rts"] = ["RT-999"]
        self.repo.write_overlay(data)
        self.assertFailsWith(self.repo.audit(), "GA-V2-SLOT")

    def test_chain_tip_must_match_disk(self) -> None:
        """槽位文件被改却没留 v2 回执 → 链尖对不上磁盘。"""
        target = self.repo.root / "scripts/cwk_wiki_query.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
        self.assertFailsWith(self.repo.audit(), "GA-V2-CHAIN")

    def test_valid_v2_continuation_receipt_passes(self) -> None:
        data = self.repo.overlay()
        slot = data["continuation_slots"][0]
        target = self.repo.root / slot["target_path"]
        tip = slot["v1_chain_tip_sha256"]
        target.write_text(
            target.read_text(encoding="utf-8") + "\n# RT-022 的真实续演\n", encoding="utf-8"
        )
        self.repo.add_v2_receipt(
            "RT-022",
            "stage-v2-01-cwk-wiki-query-ord2",
            target_path=slot["target_path"],
            ordinal=slot["v2_ordinal_start"],
            from_sha=tip,
            to_sha=_sha256(target),
        )
        self.assertClean(self.repo.audit())

    def test_receipt_filed_under_wrong_owner_directory_fails(self) -> None:
        data = self.repo.overlay()
        slot = data["continuation_slots"][0]
        target = self.repo.root / slot["target_path"]
        tip = slot["v1_chain_tip_sha256"]
        target.write_text(target.read_text(encoding="utf-8") + "\n# x\n", encoding="utf-8")
        # owner_rt 写 RT-022，却把回执塞进 RT-026 的目录
        note_rel = "RT/RT-026/migration-notes/x.md"
        self.repo.write_file(note_rel, "# note\n")
        self.repo.write_file(
            "RT/RT-026/receipts/script-evolution-v2/x.json",
            json.dumps(
                {
                    "schema": ga.V2_RECEIPT_SCHEMA,
                    "overlay_version": "v2",
                    "owner_rt": "RT-022",
                    "target_path": slot["target_path"],
                    "ordinal": slot["v2_ordinal_start"],
                    "from_sha256": tip,
                    "to_sha256": _sha256(target),
                    "migration_note_ref": note_rel,
                    "rt_ref": "RT/RT-022/rt-lite.md",
                },
                ensure_ascii=False,
            ),
        )
        self.assertFailsWith(self.repo.audit(), "GA-V2-RECEIPT")


class TestExceptionBoundaries(GovernanceAuditTestBase):
    """DI-003：例外必须有主、有触发条件、有退出标准，且不能永不过期。"""

    def test_exception_without_exit_criteria_fails(self) -> None:
        data = self.repo.manifest()
        data["exceptions"][0].pop("exit_criteria")
        self.repo.write_manifest(data)
        self.assertFailsWith(self.repo.audit(), "GA-EXCEPTION")

    def test_exception_without_owner_fails(self) -> None:
        data = self.repo.manifest()
        data["exceptions"][0].pop("owner")
        self.repo.write_manifest(data)
        self.assertFailsWith(self.repo.audit(), "GA-EXCEPTION")

    def test_expired_exception_fails(self) -> None:
        """到期例外不自动续期——这是它区别于永久 warn 的关键。"""
        data = self.repo.manifest()
        review_by = data["exceptions"][0]["review_by"]
        expired_day = datetime.date.fromisoformat(review_by) + datetime.timedelta(days=1)
        self.repo.write_manifest(data)
        self.assertFailsWith(self.repo.audit(today=expired_day), "GA-EXCEPTION-EXPIRED")

    def test_exception_without_compensating_controls_fails(self) -> None:
        data = self.repo.manifest()
        data["exceptions"][0]["compensating_controls"] = []
        self.repo.write_manifest(data)
        self.assertFailsWith(self.repo.audit(), "GA-EXCEPTION")


class TestCompensatingControlsAreReal(GovernanceAuditTestBase):
    """补偿控制不是一句声明：把门摘掉，例外当场失效。"""

    def test_removing_governance_audit_from_make_ci_fails(self) -> None:
        makefile = self.repo.root / "Makefile"
        text = makefile.read_text(encoding="utf-8")
        text = text.replace("\t$(MAKE) governance-audit\n", "")
        makefile.write_text(text, encoding="utf-8")
        result = self.repo.audit()
        self.assertFalse(result.ok)
        # pin 漂移与补偿控制失效都应报出来；关键是 CC-2 真的被验证了。
        self.assertIn("GA-CONTROL", result.error_codes())

    def test_ci_workflow_no_longer_running_make_ci_fails(self) -> None:
        workflow = self.repo.root / ".github/workflows/ci.yml"
        text = workflow.read_text(encoding="utf-8").replace("run: make ci", "run: make test")
        workflow.write_text(text, encoding="utf-8")
        result = self.repo.audit()
        self.assertFalse(result.ok)
        self.assertIn("GA-CONTROL", result.error_codes())

    def test_checkout_without_fetch_depth_zero_fails(self) -> None:
        workflow = self.repo.root / ".github/workflows/ci.yml"
        text = workflow.read_text(encoding="utf-8").replace("fetch-depth: 0", "fetch-depth: 1")
        workflow.write_text(text, encoding="utf-8")
        result = self.repo.audit()
        self.assertFalse(result.ok)
        self.assertIn("GA-CONTROL", result.error_codes())

    def test_checkout_with_missing_fetch_depth_fails(self) -> None:
        workflow = self.repo.root / ".github/workflows/ci.yml"
        text = workflow.read_text(encoding="utf-8").replace(
            "        with:\n          fetch-depth: 0\n", ""
        )
        workflow.write_text(text, encoding="utf-8")
        result = self.repo.audit()
        self.assertFalse(result.ok)
        self.assertIn("GA-CONTROL", result.error_codes())

    def test_fetch_depth_comment_does_not_satisfy_the_control(self) -> None:
        self.assertFalse(
            ga._workflow_has_full_history_checkout(
                "jobs:\n"
                "  smoke:\n"
                "    steps:\n"
                "      # fetch-depth: 0\n"
                "      - uses: actions/checkout@v4\n"
                "        with:\n"
                "          fetch-depth: 1\n"
            )
        )

    def test_fetch_depth_in_wrong_step_does_not_satisfy_the_control(self) -> None:
        self.assertFalse(
            ga._workflow_has_full_history_checkout(
                "jobs:\n"
                "  smoke:\n"
                "    steps:\n"
                "      - uses: actions/checkout@v4\n"
                "      - uses: actions/setup-python@v5\n"
                "        with:\n"
                "          fetch-depth: 0\n"
            )
        )

    def test_fetch_depth_must_be_literal_numeric_zero(self) -> None:
        self.assertFalse(
            ga._workflow_has_full_history_checkout(
                "jobs:\n"
                "  smoke:\n"
                "    steps:\n"
                "      - uses: actions/checkout@v4\n"
                "        with:\n"
                "          fetch-depth: '0'\n"
            )
        )

    def test_real_checkout_requests_full_history(self) -> None:
        self.assertTrue(
            ga._workflow_has_full_history_checkout(
                (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
            )
        )

    def test_comment_mentioning_make_ci_does_not_satisfy_the_control(self) -> None:
        """注释里写着 make ci 不算数——只认 run: 里真的在跑。

        这是本文件抓到的第一个真漏洞：早先的实现对全文做子串匹配，
        而 ci.yml 的注释本身就写着「make ci = make test + ...」，
        于是把 `run: make ci` 换掉之后这条补偿控制依然假绿。
        """
        self.assertFalse(
            ga._workflow_runs_make_ci(
                "jobs:\n"
                "  smoke:\n"
                "    steps:\n"
                "      # 和本地同一条命令：make ci = make test + make aodw-check\n"
                "      - name: Repo checks (same entry point as local `make ci`)\n"
                "        run: make test\n"
            )
        )

    def test_make_ci_inside_a_run_block_scalar_counts(self) -> None:
        """换成块标量写法仍应识别——门认的是行为，不是某一种 YAML 排版。"""
        self.assertTrue(
            ga._workflow_runs_make_ci(
                "    steps:\n"
                "      - name: Repo checks\n"
                "        run: |\n"
                "          python -V\n"
                "          make ci\n"
            )
        )

    def test_real_workflow_satisfies_the_control(self) -> None:
        self.assertTrue(
            ga._workflow_runs_make_ci(
                (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
            )
        )

    def test_makefile_parser_finds_the_real_targets(self) -> None:
        targets = ga._makefile_targets(
            (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        )
        self.assertIn("governance-audit", targets)
        self.assertIn("ci", targets)
        self.assertIn("governance-audit", " ".join(targets["ci"]))


class TestGateGovernsItself(GovernanceAuditTestBase):
    """门必须管住自己。

    RT-030 实做时踩到的真问题：首版把 `.aodw-next/06-project/` 整个交给一条
    prefix 规则、落在 docs_governance 域——那个域既不要求演化路径也不要求变更入口。
    于是检查器自己、判据清单自己、v2 叠加层自己全躺在「宽前缀 + 无 pin」下面，
    把 governance-audit.py 掏成 `sys.exit(0)` 门照样绿。这正是本 RT 要消灭的形态，
    只不过发生在门自己身上。
    """

    def test_gutting_the_checker_is_detected(self) -> None:
        """把检查器掏空必须留下痕迹——这是整套管辖最脆弱的一点。"""
        self.repo.write_file(
            ".aodw-next/06-project/governance-audit.py",
            "import sys\nsys.exit(0)\n",
        )
        self.assertFailsWith(self.repo.audit(), "GA-PIN-DRIFT")

    def test_editing_the_overlay_is_detected(self) -> None:
        data = self.repo.overlay()
        data["legacy_evolution"]["default_steward"] = "nobody"
        self.repo.write_overlay(data)
        self.assertFailsWith(self.repo.audit(), "GA-PIN-DRIFT")

    def test_removing_the_checkers_own_rule_fails(self) -> None:
        data = self.repo.manifest()
        data["rules"] = [
            r for r in data["rules"] if r["id"] != "R-buildci-governance-audit-script"
        ]
        self.repo.write_manifest(data)
        self.assertFailsWith(self.repo.audit(), "GA-SELF")

    def test_demoting_gate_machinery_out_of_build_ci_fails(self) -> None:
        """降域是最省事的绕法：docs_governance 不要求演化路径和变更入口。"""
        data = self.repo.manifest()
        self.repo.rule(data, "R-buildci-governance-audit-script")["domain"] = (
            "docs_governance"
        )
        self.repo.write_manifest(data)
        self.assertFailsWith(self.repo.audit(), "GA-SELF")

    def test_unmarking_gate_machinery_as_sensitive_fails(self) -> None:
        """去掉 sensitive 就不做指纹比对了，等于把 pin 悄悄关掉。"""
        data = self.repo.manifest()
        self.repo.rule(data, "R-buildci-evolution-overlay-v2")["sensitive"] = False
        self.repo.write_manifest(data)
        self.assertFailsWith(self.repo.audit(), "GA-SELF")

    def test_self_pin_exemption_cannot_spread_to_other_files(self) -> None:
        """不动点豁免只属于清单本文件；别的规则声明它就是自开免检通道。"""
        data = self.repo.manifest()
        rule = self.repo.rule(data, "R-buildci-governance-audit-script")
        rule["self_pin_impossible"] = True
        rule["self_pin_reason"] = "看起来很正当的理由"
        rule.pop("pin", None)
        rule["sensitive"] = False
        self.repo.write_manifest(data)
        self.assertFailsWith(self.repo.audit(), "GA-SELF")

    def test_manifest_self_pin_exemption_needs_a_written_reason(self) -> None:
        data = self.repo.manifest()
        self.repo.rule(data, "R-buildci-ownership-manifest").pop("self_pin_reason")
        self.repo.write_manifest(data)
        self.assertFailsWith(self.repo.audit(), "GA-SELF")

    def test_project_slot_is_an_exact_only_zone(self) -> None:
        """往门自己的目录里新增文件必须显式登记，不能被任何前缀规则吸收。"""
        self.repo.write_file(
            ".aodw-next/06-project/sneaky-helper.py", "print('hi')\n"
        )
        self.repo.tracked.append(".aodw-next/06-project/sneaky-helper.py")
        self.assertFailsWith(self.repo.audit(), "GA-ORPHAN")

    def test_widening_the_vendor_rule_to_cover_the_slot_fails(self) -> None:
        """撤掉 exclude_prefixes，宽前缀立刻够到 exact-only 区——必须当场判失败。"""
        data = self.repo.manifest()
        self.repo.rule(data, "R-vendor-aodw-framework").pop("exclude_prefixes")
        self.repo.write_manifest(data)
        self.assertFailsWith(self.repo.audit(), "GA-ZONE")

    def test_excluding_an_unrelated_subtree_does_not_grant_zone_immunity(self) -> None:
        """排除项必须真的覆盖该区；拿个无关子目录搪塞不算数。"""
        data = self.repo.manifest()
        self.repo.rule(data, "R-vendor-aodw-framework")["exclude_prefixes"] = [
            ".aodw-next/99-nowhere/"
        ]
        self.repo.write_manifest(data)
        self.assertFailsWith(self.repo.audit(), "GA-ZONE")


class TestRealRepository(unittest.TestCase):
    """对真实仓库的断言。合成用例证明门会咬人，这里证明当前树确实是绿的。"""

    def setUp(self) -> None:
        self.result = ga.audit(REPO_ROOT)

    def test_current_tree_is_fully_governed(self) -> None:
        self.assertTrue(
            self.result.ok,
            "当前代码树未通过归属自检：\n"
            + "\n".join(f"  {f.code}: {f.message}" for f in self.result.errors),
        )

    def test_no_tracked_file_is_unclassified(self) -> None:
        self.assertEqual(self.result.stats.get("orphans"), 0)

    def test_every_tracked_file_is_accounted_for(self) -> None:
        by_domain = self.result.stats.get("by_domain") or {}
        self.assertEqual(
            sum(by_domain.values()),
            self.result.stats.get("tracked_total"),
            "分域计数之和必须等于受跟踪文件总数——差额意味着有文件没被计入任何域",
        )

    def test_the_blind_spot_that_motivated_rt030_is_now_owned(self) -> None:
        """回归锚点：这个文件此前零判据，绝不能再退回无主状态。"""
        manifest = json.loads(
            (REPO_ROOT / ga.MANIFEST_REL).read_text(encoding="utf-8")
        )
        rule = next(
            r
            for r in manifest["rules"]
            if r.get("path") == "scripts/cwk_wiki_batch_driver.sh"
        )
        self.assertTrue(rule.get("owner"))
        self.assertTrue(rule.get("evolution_path"))
        self.assertTrue(rule.get("sensitive"))

    def test_frozen_upstream_contracts_are_untouched(self) -> None:
        """本 RT 声称没改 PR-001 冻结契约——这里当场验证。"""
        manifest = json.loads(
            (REPO_ROOT / ga.MANIFEST_REL).read_text(encoding="utf-8")
        )
        for authority in manifest["upstream_authorities"]:
            with self.subTest(path=authority["path"]):
                self.assertEqual(
                    _sha256(REPO_ROOT / authority["path"]), authority["sha256"]
                )


if __name__ == "__main__":
    unittest.main()
