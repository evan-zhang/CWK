# VG-A 集成门禁 — 合成验证 receipt（implementation_done）

> 状态：`implementation_done`（不自认 PASS；后续由独立 Opus verifier 复核）
> 波次：VG-A（RT-015 后合成集成门禁；来源 `PR-001 plans/开发计划.md` §8）
> 交付形式：`tests/_vga_helpers.py` + `tests/test_vga_*.py`（合成，纯库/纯测试）
> 交付人：`agent-vga-impl-opus`（独立实现 Agent；本 receipt 由其自我记录）
> 复核者：`agent-vga-verify-opus`（待启动，独立 worktree/独立分支）
> 日期：2026-08-19（UTC）
> Python：`/opt/homebrew/bin/python3.11 → Python 3.11.14`（PRD NFR-09）

## 0. 关键裁决与边界（务必逐条核对）

- **当前真实 authority / access relationship 仍 conservative_unknown**。
  VG-A 只在合成 fake authority（RT-015 `FakeSigningAuthority` + 模块私有
  `_TEST_AUTHORITY_TOKEN`）上验证 host 内部链路。**不代表** 生产权威 API 已接入。
- **不代表 G3、M3、生产、真实 Gateway、真实权限源或沙箱传输完成**。VG-A
  只证明 RT-011~RT-015 的合成链路满足 §1~§7 的行为契约。
- **在未取得 active authority 前**：collector / migration 只能保存无正文、
  无临时 URL 的 deferred-view reference 或 review，不能直写
  `TenantViewStore`。VG-A 不扩展也不修改 RT-015 来绕过此边界。
- **不新建 HTTP/CLI/对象枚举接口**。所有断言均通过 RT-015 公共 API
  （`AccessLedger`、`TenantViewStore`、`SharedEvidenceStore`、
  `AgentContextSnapshot`）以及 RT-015 显式导出的 test-only authority
  注册 API（`_register_test_authority` / `_register_fake_signer` /
  `_TEST_AUTHORITY_TOKEN`）访问。测试 hygiene 已由
  `tests/test_vga_zero_drift_and_hygiene.py::VgaHygieneTests
  ::test_vga_tests_use_only_public_api_imports` 断言。
- **零真实密钥 / 零真实 Gateway / 零 DocDB / 零 Cloud / 零 cron**。所有
  fixture 都在 `tempfile.TemporaryDirectory("vga-instance-*")` 里生存
  一次测试再销毁。

## 1. 交付物清单（本 receipt 唯一新增 code artefact）

| 路径 | 用途 |
| --- | --- |
| `tests/_vga_helpers.py` | 两租户合成 fixture、`SyntheticAuthorityContext` 包装、共用工厂函数 |
| `tests/test_vga_shared_canonical.py` | §1 同一 canonical 可被 A/B 共享；grant/view/receipt/error/path 严格隔离 |
| `tests/test_vga_active_only_and_unified_deny.py` | §2 仅 `active`+有效 lease 可读；unknown tenant/report/object 统一 `AccessDenied` |
| `tests/test_vga_revocation_isolation.py` | §3 A 撤权后即刻拒绝、epoch bump、tombstone / cleanup outbox；重启 recovery 不复活；B 保持 active |
| `tests/test_vga_view_overlay_only.py` | §4 View 不含正文、credential、临时 URL、host path、reply/node 完整 payload |
| `tests/test_vga_authority_fail_closed.py` | §5 fail-closed authority：observation ≤ granted；无有效 receipt 不能写 active view，不绕过 RT-015 API |
| `tests/test_vga_identity_and_existence.py` | §6 同 body 不同 source_ns / report_id 不混同身份；撤权/未知 report 无跨租户存在性侧信道 |
| `tests/test_vga_crash_and_concurrency.py` | §7 crash injection/recovery、重复 revoke、CAS 冲突、损坏 object/catalog、A/B 并发 |
| `tests/test_vga_zero_drift_and_hygiene.py` | §8 冻结模块存在性 + 私有 API 使用允许表 + 秘密扫描 + 无新增 CLI/HTTP |
| `PR/PR-001-multitenant-knowledge-spaces/gate-receipts/VG-A-集成验证.md` | 本文件 |

未新增 `scripts/`、`config/`、`docs/`、`RT/`、
`PR/PR-001-multitenant-knowledge-spaces/contracts/`、
`PR/PR-001-multitenant-knowledge-spaces/plans/`、
`PR/PR-001-multitenant-knowledge-spaces/PRD.md`、`DESIGN.md`、
`PR/PR-001-multitenant-knowledge-spaces/reviews/` 中任何文件；
`RT/index.yaml` 未改。

## 2. 验证矩阵与测试对应表

| 任务要求条目 | 覆盖测试 | 数量 |
| --- | --- | --- |
| §1 同一 canonical A/B 共享；grant/view/receipt/error/path 严格隔离 | `test_vga_shared_canonical.SharedCanonicalIsolationTests.*` + `ViewIsolationTests.*` | 9 |
| §2 仅 active+有效 lease 可读；unknown tenant/report/object 统一 deny | `test_vga_active_only_and_unified_deny.ActiveOnlyEligibilityTests.*` + `UnifiedDenyTests.*` + `SnapshotAndStatusGateTests.*` | 12 |
| §3 A 撤权：intent 立即拒绝、epoch bump、tombstone/cleanup、旧 snapshot/旧缓存/重启恢复均拒绝；B 可用 | `test_vga_revocation_isolation.RevocationIsolationTests.*` + `CleanupOutboxIsolationTests.*` | 9 |
| §4 View 无正文/credential/临时 URL/host path/reply/node 完整 payload；只引用 canonical + overlay/hash | `test_vga_view_overlay_only.OverlayNoLeakTests.*` | 11 |
| §5 fail-closed authority：observation ≤ granted；无有效 receipt 不能写 active view/绕过 RT-015 API | `test_vga_authority_fail_closed.ObservationAtMostGrantedTests.*` + `PromoteRequiresAuthorityTests.*` + `ViewStoreCannotBypassLedgerTests.*` | 9 |
| §6 同 body 不同 source_ns/report_id 不混同身份；撤销/未知 object 等无跨租户存在性侧信道 | `test_vga_identity_and_existence.IdentityDistinctnessTests.*` + `NoCrossTenantExistenceSideChannelTests.*` | 7 |
| §7 crash injection/recovery、重复 revoke、CAS 冲突、损坏 object/catalog/head、A/B 并发 | `test_vga_crash_and_concurrency.CrashRecoveryTests.*` + `CasConflictTests.*` + `CorruptionTests.*` + `ConcurrentAbTrafficTests.*` | 12 |
| §8 python3.11 定向 + 完整回归 + `git diff --check` + secret scan + 冻结零漂移 | `test_vga_zero_drift_and_hygiene.VgaHygieneTests.*` + `VgaScopeTests.*` + 本文档 §3 命令与结果 | 6 |
| **合计** | | **75** |

## 3. 复现命令与实测结果（python3.11.14，macOS Homebrew）

工作目录：`/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK/.worktrees/openclaw-worktree-cwk-vga-impl-opus`

### 3.1 VG-A 定向测试

```
python3.11 -m unittest discover -s tests -p "test_vga_*.py"
```

实测：
```
Ran 75 tests in 17.805s

OK
```

分模块统计（用 `python3.11 -m unittest tests.<mod>` 单独运行得到）：
```
test_vga_active_only_and_unified_deny: 12
test_vga_authority_fail_closed:         9
test_vga_crash_and_concurrency:        12
test_vga_identity_and_existence:        7
test_vga_revocation_isolation:          9
test_vga_shared_canonical:              9
test_vga_view_overlay_only:            11
test_vga_zero_drift_and_hygiene:        6
                                       --
                                       75
```

### 3.2 完整回归（含 RT-001~RT-006 + RT-011~RT-015 + VG-A）

```
python3.11 -m unittest discover -s tests -p "test_*.py"
```

实测：
```
Ran 892 tests in 66.534s

OK (skipped=7)
```

### 3.3 基线对齐（不含 VG-A）

```
python3.11 -m unittest discover -s tests -p "test_[!v]*.py"
```

实测：
```
Ran 817 tests in 52.994s

OK (skipped=7)
```

Δ = 892 − 817 = **75**，与 §3.1 完全一致；无回归、无新 skip。

### 3.4 `git diff --check`

```
git diff --check && echo "diff check OK"
```

实测：`diff check OK`（无空白/合并冲突/trailing 问题）。

### 3.5 `git status --short`（仅新增文件，无 modify/delete）

```
?? tests/_vga_helpers.py
?? tests/test_vga_active_only_and_unified_deny.py
?? tests/test_vga_authority_fail_closed.py
?? tests/test_vga_crash_and_concurrency.py
?? tests/test_vga_identity_and_existence.py
?? tests/test_vga_revocation_isolation.py
?? tests/test_vga_shared_canonical.py
?? tests/test_vga_view_overlay_only.py
?? tests/test_vga_zero_drift_and_hygiene.py
```

RT-011~RT-015 冻结模块（`scripts/cwk_pr001_contracts.py`、
`cwk_instance.py`、`cwk_atomic_file.py`、`cwk_tenant_registry.py`、
`cwk_tenant_cli.py`、`cwk_agent_binding.py`、`cwk_agent_context.py`、
`cwk_credential_broker.py`、`cwk_tenant_cmd_binding.py`、
`cwk_shared_evidence.py`、`cwk_access_ledger.py`、`cwk_tenant_view.py`），
`PR/.../contracts/rt011~rt015/`、`RT/RT-011~RT-015/`、
`RT/index.yaml`、`PR/.../PRD.md` / `DESIGN.md` / `plans/*`、
`legacy raw/collector/nightly/query/cron/DocDB` 均 **零字节差**。

### 3.6 敏感扫描（仅新增文件）

```
grep -rIn -E "CWORK_APP_KEY|AppKey|app_secret|APP_SECRET|Bearer [A-Za-z0-9._-]{10,}|CWORK_TOKEN|token=[A-Za-z0-9]{20,}|(sk|pk)_[a-zA-Z0-9]{20,}" \
    tests/_vga_helpers.py tests/test_vga_*.py
```

命中项：全部为字符串字面量（`b"CWORK_APP_KEY"` 等作为 deep-forbidden
字段名扫描目标）或文档字符串（说明"不接触 CWORK_APP_KEY"）；无真实
密钥、AppKey、AppSecret、Bearer token、`.env` 值。VG-A hygiene 测试
`test_secret_scan_has_no_real_secrets` 也从测试内部执行了一次
independent 校验。

### 3.7 Python 编译检查

```
python3.11 -m compileall -q tests/_vga_helpers.py tests/test_vga_*.py
```

实测：exit 0，无告警。

## 4. 关键行为断言快照（供 verifier 快速定位）

- `AccessDenied.__str__ = "[denied] access denied"`：unified deny 覆盖
  未知 tenant / 未知 report / 未 active grant / stale epoch / tombstoned /
  revocation_in_progress 所有分支。断言见
  `test_vga_active_only_and_unified_deny.UnifiedDenyTests
  ::test_unified_deny_shape_across_scenarios`。
- **共享 canonical 单对象**：`test_vga_shared_canonical
  ::test_two_tenants_share_single_canonical_object` 扫描
  `shared/objects/**` 断言物理文件数 = 1，且两次 publish 返回同一
  `catalog_key / object_id / canonical_sha256`。
- **grant_key 隔离**：`grant_key = H(GRANT_KEY_DOMAIN, tenant, report_key)`；
  相同 `report_key` 在 A/B 下产生不同 26 字符 base32 尾部，A 的 grant 文件
  名不会出现在 B 的 `tenants/<b_id>/views/` 或
  `registry/access-ledger/<b_id>/grants/` 目录中。
- **revoke 后 A/B 独立**：`test_vga_revocation_isolation
  ::test_revoke_makes_check_fail_closed_immediately` 断言 A 的
  `tenant.auth_epoch` = 之前 + 1，B 的 `tenant.auth_epoch` 不变；
  A 的 stale snapshot / fresh snapshot 都被拒绝；B 的 snapshot 依然
  `active`。
- **fail-closed authority**：默认 `_FailClosedAuthority` 拒绝任何 receipt；
  `test_vga_authority_fail_closed.PromoteRequiresAuthorityTests
  ::test_default_authority_is_fail_closed_when_no_test_authority_registered`
  在未注册 fake authority 时构造 schema-valid receipt，仍被
  `AuthorityRejected`。
- **无侧门**：`test_vga_authority_fail_closed
  .ViewStoreCannotBypassLedgerTests::test_no_side_door_apis_on_ledger_or_view_store`
  反射断言 `AccessLedger` / `TenantViewStore` 不含
  `upsert_active/force_active/grant_active/reactivate/unrevoke/delete_grant/delete_tombstone/raw_grant_bytes/upsert_without_grant/force_upsert/delete_view/list_all_views/raw_view_bytes`。
- **无枚举**：`test_vga_identity_and_existence
  .NoCrossTenantExistenceSideChannelTests::test_no_enumeration_apis_on_public_surface`
  反射断言 `list_all_tenants/list_all_grants/iter_all_grants/
  resolve_by_report_id/resolve_by_object_id/has_report_id/has_object/
  object_exists/list_reports/list_all_reports` 全部不存在，且
  `AL.__all__` / `TV.__all__` 均不含它们。
- **crash-safe 撤权**：`test_vga_crash_and_concurrency
  .CrashRecoveryTests::test_crash_after_intent_before_completion_is_recovered`
  手写 intent journal（不 monkey-patch），断言 recovery 完成后 tombstone
  在磁盘上并查询继续 deny；
  `::test_missing_receipt_after_intent_still_denies_query` 断言即使 receipt
  被误删，只要 tombstone 还在，查询仍 fail-closed（不可复活语义）。
- **CAS 冲突**：`CasConflictTests::test_bump_epoch_with_wrong_expected_raises_conflict`
  直接触发 `TenantRegistry.bump_auth_epoch` 的 CAS 拒绝路径。
- **损坏读拒绝**：`CorruptionTests::test_corrupt_grant_json_denied_and_isolated`
  截断 A 的 `grants/*.json`，A 拒绝且 reason=`grant_corrupt`，B 完整可用；
  `::test_bit_flip_grant_denied` 对同一文件做 1-bit flip 也命中 fail-closed；
  `::test_corrupt_canonical_object_denied` / `::test_missing_object_returns_not_found`
  破坏共享对象后 RT-014 `read_version` 使用稳定 `code` 拒绝，不泄漏路径；
  `::test_view_read_fails_when_canonical_gone` 覆盖 view→canonical 联动读拒绝。
- **A/B 并发**：`ConcurrentAbTrafficTests
  ::test_concurrent_ab_observation_writes` 用 4 线程并发观察 A/B 两个
  report_id，全部成功且不互扰；`::test_concurrent_revoke_of_a_is_idempotent`
  4 线程并发 revoke 同一 grant，最多一名 writer 成功且 `RevocationInProgress`
  / `GrantStateError` 为允许的 race 分支，最终 receipt.txn_id 唯一。

## 5. 明确未覆盖 / 明确不宣称

VG-A 是集成 receipt，不替代 G3、G4、G5、G6、G7 或 M3、M4、M5：

1. 真实 authority adapter、真实工作协同权限 API、真实 secret backend 未接入；
   仅 fake authority 通过；不宣称 SLA、不宣称权威撤权链路可用。
2. Broker / Scheduler / Space Projector / 沙箱 transport / OpenClaw Tool /
   UDS peer credential 通道全部未在 VG-A 验证；这些属于 RT-018/021/022/023
   与 VG-B/VG-D/G5 的边界。
3. Legacy raw 迁移 / RT-016 双读对账 / M3 未在 VG-A 内验证。
4. 性能 / RTO / RPO / 具体 P50/P95 数字全部按 conservative_unknown 处理，
   留待 RT-024 / RT-025 实测。
5. `cleanup_outbox` 只测试 `tenant_view` 消费路径；`space_index` / `cache`
   的真实消费者由 RT-021 / RT-022 后续实现，接口
   `iter_cleanup_outbox` / `ack_cleanup_task` 在 RT-015 已冻结，
   VG-A `test_ack_cleanup_removes_task_after_last_consumer` 验证的是
   合约层面（三名占位消费者顺序 ack 后 outbox 幂等 unlink）。
6. 本 receipt **不写入** `RT/index.yaml`、不新建 RT-VGA 目录、不改任何
   `RT/RT-011~015/*`；如需将本 receipt 纳入独立验收流程，由后续
   `agent-vga-verify-opus` 决定归档形式。

## 6. 供 verifier 复核的推荐命令列表

```
# 1. 版本 / 环境
python3.11 --version                       # → Python 3.11.14
pwd                                        # 应指向本 worktree 绝对路径

# 2. 冻结物 byte-identical（RT-011~015 模块 + schemas + PRD/DESIGN/计划）
git diff --name-only HEAD~1..HEAD          # 应仅列 VG-A 新增文件
git diff --check                           # 应 exit 0

# 3. 定向 + 完整回归 + 基线
python3.11 -m unittest discover -s tests -p 'test_vga_*.py'      # 75 OK
python3.11 -m unittest discover -s tests -p 'test_*.py'          # 892 OK skipped=7
python3.11 -m unittest discover -s tests -p 'test_[!v]*.py'      # 817 OK skipped=7
python3.11 -m compileall -q tests/_vga_helpers.py tests/test_vga_*.py

# 4. 敏感扫描
grep -rIn -E 'CWORK_APP_KEY|AppKey|app_secret|APP_SECRET|Bearer [A-Za-z0-9._-]{10,}|CWORK_TOKEN|token=[A-Za-z0-9]{20,}|(sk|pk)_[a-zA-Z0-9]{20,}' \
    tests/_vga_helpers.py tests/test_vga_*.py

# 5. 反射式 negative surface（可选人工复核）
python3.11 -c "
import sys; sys.path.insert(0, 'scripts')
import cwk_access_ledger as AL, cwk_tenant_view as TV
for name in ('upsert_active','force_active','grant_active','reactivate',
             'unrevoke','delete_grant','delete_tombstone','raw_grant_bytes'):
    assert not hasattr(AL.AccessLedger, name), name
for name in ('upsert_without_grant','force_upsert','delete_view',
             'list_all_views','raw_view_bytes'):
    assert not hasattr(TV.TenantViewStore, name), name
print('side-door surface: EMPTY')
"

# 6. Cleanup（本 receipt 生成的 tempdir 已在测试 tearDown 中自动清理）
```

## 7. 剩余风险与运维备注

- **R1 (info)**：`test_vga_crash_and_concurrency.CasConflictTests
  ::test_stale_snapshot_denied_when_epoch_bumped` 使用 `TenantRegistry
  .bump_auth_epoch` 作为不相关 auth_epoch bump 的合成入口，这是 RT-012
  已冻结的公开 CAS 接口。若后续 RT-018/RT-024 对 bump_auth_epoch 的
  actor/reason 白名单收紧，需要同步调整测试字符串。
- **R2 (info)**：`ConcurrentAbTrafficTests
  ::test_concurrent_ab_observation_writes` 依赖 `AccessLedger._tenant_fd`
  内部 `create=True` 的目录 mkdir 幂等性；VG-A 已通过 `pre-warm` 观察
  绕开首次 mkdir race。真实生产 collector 应遵循 RT-017 契约先
  `AccessLedger.initialize()`（已在 RT-012/015 boot 阶段调用）。
- **R3 (info)**：VG-A 未 assertion 任何具体 RTO/RPO/P50/P95 数字，遵循
  DESIGN §16 "安全默认 + 后续基准替换" 表述。
