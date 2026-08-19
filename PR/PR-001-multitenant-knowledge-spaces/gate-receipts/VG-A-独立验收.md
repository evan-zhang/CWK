# VG-A 独立验收报告（黑盒 verifier）

> 状态：**PASS（合成集成门禁）** — 仅限 synthetic authority + host-chain；未证明 G3/M3 或生产集成。
> 波次：VG-A（RT-015 后合成集成门禁；对应 `PR-001/plans/开发计划.md §8`）
> 复核人：`agent-vga-verify-opus`（独立 worktree、独立分支，未参与任何实现）
> 复核对象：
> - 基线：`rt/RT-002-interactive-action-center`
> - RT-016 impl：`c51dcb2`
> - VG-A impl receipt：`142f304`（`PR/PR-001-multitenant-knowledge-spaces/gate-receipts/VG-A-集成验证.md`，作者 `agent-vga-impl-opus`，状态 `implementation_done`）
> 日期：2026-08-19（UTC）
> Python：`/opt/homebrew/bin/python3.11 → 3.11.14`（符合 PRD NFR-09）
> Worktree：`/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK/.worktrees/openclaw-worktree-cwk-vga-verify-opus`

---

## 0. 独立性、边界与不能宣称清单

- 我未参与任何 RT-011~RT-016 或 VG-A 的实现，也不修改任何 scripts/、RT/、
  PR/PR-001/contracts/、PRD.md/DESIGN.md/plans/*、legacy raw/collector/
  nightly/cron/DocDB/schemas。本次全部写入限于本报告
  `PR/PR-001-multitenant-knowledge-spaces/gate-receipts/VG-A-独立验收.md`。
- 所有夹具、探针、attack matrix 均在 `mktemp -d /tmp/vga-verify-*` 生存
  一次即销毁；未接触 `CWORK_APP_KEY`、工作协同、Cloud/DocDB、Gateway、cron。
- 本报告的 PASS 仅覆盖 §1~§7 的 **合成 host-chain 集成**（`RT-015
  FakeSigningAuthority` + 私有 `_TEST_AUTHORITY_TOKEN`）。
- **不宣称** G3、M3、真实 Gateway、真实工作协同权限 API、真实沙箱传输、
  Broker/Scheduler、legacy raw 迁移（RT-016 数据级对账）、生产试点、
  SLA/RTO/RPO/性能等任何 downstream 门禁通过。
- **前提约束**：RT-017 只能按"deferred view 且无真实 authority 不落
  TenantView"启动，采集器只允许写入无正文/无临时 URL 的 deferred-view
  reference 或 review；VG-A 未扩展 RT-015 API 允许 side-door 写入。

---

## 1. 复现命令与实测结果

### 1.1 环境

```
$ python3.11 --version
Python 3.11.14
$ which python3.11
/opt/homebrew/bin/python3.11
$ git status --short
# clean
$ git log --oneline -3
142f304 VG-A synthesis: implementation_done (two-tenant fixture + receipt)
c51dcb2 RT-016 implementation: legacy raw shadow migration + data-level reconciler
7ba906f RT-015: record independent acceptance
```

### 1.2 VG-A 定向测试（实现者交付 75 条）

```
$ python3.11 -m unittest discover -s tests -p "test_vga_*.py"
Ran 75 tests in 15.747s
OK
```

分模块（`python3.11 -m unittest discover -s tests -p "<file>"`）：

```
 6  test_vga_zero_drift_and_hygiene.py
 7  test_vga_identity_and_existence.py
 9  test_vga_authority_fail_closed.py
 9  test_vga_revocation_isolation.py
 9  test_vga_shared_canonical.py
11  test_vga_view_overlay_only.py
12  test_vga_active_only_and_unified_deny.py
12  test_vga_crash_and_concurrency.py
==
75
```

### 1.3 完整回归 + 基线对齐

```
$ python3.11 -m unittest discover -s tests -p "test_*.py"
Ran 967 tests in 68.725s
OK (skipped=7)

$ python3.11 -m unittest discover -s tests -p "test_[!v]*.py"
Ran 892 tests in 52.458s
OK (skipped=7)

$ python3.11 -m unittest discover -s tests -p "test_v*.py"
Ran 75 tests in 18.159s
OK
```

Δ = 967 − 892 = **75**，等于 VG-A 交付；`test_v*` 一律映射到
`test_vga_*.py`（`ls tests/ | grep '^test_v'` 只列 VGA 8 个文件）。

> ⚠️ 与 impl receipt §3.2/§3.3 数字差异说明：impl receipt 报告 892/817 = 75
> 的对齐；当前 967/892 = 75 的对齐。差值 75 恰为 RT-016 tests 数量
> （`python3.11 -m unittest discover -s tests -p "test_rt016_*.py"` = 75）。
> 由于 RT-016 c51dcb2 已在 VG-A 142f304 之前落盘，我推断 impl receipt 的
> 全量数字是在 RT-016 尚未被计入的干净环境采集，属于文档-事实不一致但
> 不影响功能门禁；本报告以当前 HEAD 上实测的 967/892/75 为准，请后续
> plan 阅读者按本报告读数。**记为 Minor(doc)**：impl receipt §3.2/§3.3
> 中的 892/817 需要更新，或在下一轮 RT 记录时补一句"RT-016 之前采集"的
> 说明。

### 1.4 diff / hygiene / compile / secret

```
$ git diff --check
# (clean)

$ git diff HEAD~1..HEAD --name-only | sort
"PR/PR-001-multitenant-knowledge-spaces/gate-receipts/VG-A-集成验证.md"
tests/_vga_helpers.py
tests/test_vga_active_only_and_unified_deny.py
tests/test_vga_authority_fail_closed.py
tests/test_vga_crash_and_concurrency.py
tests/test_vga_identity_and_existence.py
tests/test_vga_revocation_isolation.py
tests/test_vga_shared_canonical.py
tests/test_vga_view_overlay_only.py
tests/test_vga_zero_drift_and_hygiene.py
# ↑ VG-A 只新增 tests/_vga_helpers.py + 8 个 test_vga_*.py + impl receipt；
#   scripts/*、RT/*、PR/.../contracts/*、PRD/DESIGN/plan/reviews/index.yaml 全部零字节差。

$ python3.11 -m compileall -q tests/_vga_helpers.py tests/test_vga_*.py
# → exit 0，无告警

$ grep -rInE 'CWORK_APP_KEY|AppKey|app_secret|APP_SECRET|Bearer [A-Za-z0-9._-]{10,}|CWORK_TOKEN|token=[A-Za-z0-9]{20,}|(sk|pk)_[a-zA-Z0-9]{20,}' tests/_vga_helpers.py tests/test_vga_*.py
tests/_vga_helpers.py:11:- No real ``CWORK_APP_KEY``, no real Work-collab, no Cloud/DocDB, no
tests/test_vga_view_overlay_only.py:26:    b"CWORK_APP_KEY",
tests/test_vga_view_overlay_only.py:27:    b"AppKey",
tests/test_vga_view_overlay_only.py:28:    b"app_secret",
tests/test_vga_view_overlay_only.py:175:            for token in (b"credential_ref", b"app_key", b"CWORK_APP_KEY",
tests/test_vga_view_overlay_only.py:192:            for token in (b"CWORK_APP_KEY", b"AppKey", b"app_secret"):
tests/test_vga_zero_drift_and_hygiene.py:155:            re.compile(r"CWORK_APP_KEY\s*="),
tests/test_vga_zero_drift_and_hygiene.py:156:            re.compile(r"AppKey\s*="),
tests/test_vga_zero_drift_and_hygiene.py:157:            re.compile(r"app_secret\s*="),
# ↑ 每一条命中都是 forbidden-token 扫描的目标字符串或文档说明；无任何真实密钥/AppKey/AppSecret/Bearer/token/private-key。
```

### 1.5 RT-011~RT-016 零漂移

```
$ git diff HEAD~1..HEAD -- scripts/ RT/ PR/PR-001-multitenant-knowledge-spaces/contracts/ PR/PR-001-multitenant-knowledge-spaces/PRD.md PR/PR-001-multitenant-knowledge-spaces/DESIGN.md PR/PR-001-multitenant-knowledge-spaces/plans/ | wc -l
0
```

RT-016 的 impl commit `c51dcb2` 在 VG-A `142f304` 之前落盘；本报告运行的
全量回归 967 tests 已覆盖 RT-011~RT-015 + RT-016 + VG-A，全部通过。

### 1.6 私有 API 白名单

VG-A helpers 只从 `cwk_access_ledger` 使用 5 个**在 `__all__` 内 explicitly
导出**的 test-only 私有符号，全部围绕 authority 注册：

```
$ grep -n 'AL\._\|TV\._\|SE\._' tests/_vga_helpers.py tests/test_vga_*.py \
      | grep -v '_TEST_AUTHORITY_TOKEN\|_register_test_authority\|_unregister_test_authority\|_register_fake_signer\|_unregister_fake_signer'
tests/test_vga_identity_and_existence.py:174:            self.assertNotIn(name, H.AL.__all__)
tests/test_vga_identity_and_existence.py:175:            self.assertNotIn(name, H.TV.__all__)
# ↑ 仅两条是 __all__ 反射检查（negative surface），并未消费任何其他私有 API。
```

`_TEST_AUTHORITY_TOKEN` / `_register_test_authority` / `_unregister_test_authority`
/ `_register_fake_signer` / `_unregister_fake_signer` 由 `cwk_access_ledger.__all__`
显式导出（`scripts/cwk_access_ledger.py:2214-2218`），属于契约化的 test-only 通道。

### 1.7 反射式 negative surface（独立执行）

```
$ python3.11 -c "
import sys; sys.path.insert(0, 'scripts')
import cwk_access_ledger as AL, cwk_tenant_view as TV, cwk_shared_evidence as SE

for name in ('upsert_active','force_active','grant_active','reactivate',
             'unrevoke','delete_grant','delete_tombstone','raw_grant_bytes',
             'list_all_tenants','list_all_grants','iter_all_grants',
             'resolve_by_report_id','resolve_by_object_id','has_report_id',
             'has_object','object_exists','list_reports','list_all_reports'):
    assert not hasattr(AL.AccessLedger, name), 'AL leaked: '+name
    assert name not in AL.__all__, 'AL __all__ leaked: '+name

for name in ('upsert_without_grant','force_upsert','delete_view',
             'list_all_views','raw_view_bytes','resolve_by_report_id',
             'has_view','list_all_tenants','iter_all_views'):
    assert not hasattr(TV.TenantViewStore, name), 'TV leaked: '+name
    assert name not in TV.__all__, 'TV __all__ leaked: '+name

for name in ('list_all_objects','iter_all_objects','delete_object',
             'delete_head','delete_catalog','raw_object_bytes'):
    assert not hasattr(SE.SharedEvidenceStore, name), 'SE leaked: '+name
    assert name not in SE.__all__, 'SE __all__ leaked: '+name

print('side-door / enumeration surface: EMPTY (AL / TV / SE)')
"
side-door / enumeration surface: EMPTY (AL / TV / SE)
```

`AccessLedger` 公开方法（`dir(...)`，排除私有下划线）：

```
['ack_cleanup_task', 'check_query_eligibility', 'initialize',
 'iter_cleanup_outbox', 'iter_events', 'list_query_eligible',
 'mark_revalidation_due', 'observe', 'promote_to_active',
 'read_grant_snapshot', 'read_tombstone', 'recover',
 'refresh_lease', 'revoke']
```

`list_query_eligible(snapshot=…)` 仅在 `snapshot.tenant_id` 一级作
`os.scandir`；不接受 tenant 参数、不接受多 tenant list，源文件
`scripts/cwk_access_ledger.py:1981-2030` 有 "Never enumerates other
tenants" 注释加固；本报告确认其作用域仅本租户，不构成跨租户枚举。

`TenantViewStore` 公开方法：

```
['purge_for_revoked_grant', 'read_view', 'recover', 'upsert_overlay']
```

无任何 `delete_view` / `list_all_views` / `force_upsert` 侧门。

---

## 2. 独立攻击矩阵（Python3.11 黑盒探针，仅走公共 API）

我在 `/tmp/vga-verify-*/` 内合成了 4 支独立探针（未依赖 `tests/_vga_helpers`，
只从 `scripts/*` 导入公共 API），每支跑完即销毁。全部命令 & 输出：

```
$ python3.11 /tmp/vga-verify-*/probe1_shared_canonical.py
PROBE-1 §1: PASS
$ python3.11 /tmp/vga-verify-*/probe2_active_revoke_view.py
PROBE-2 §2/§3/§4: PASS
$ python3.11 /tmp/vga-verify-*/probe3_authority_identity.py
PROBE-3 §5/§6: PASS
$ python3.11 /tmp/vga-verify-*/probe4_crash_cas_corruption.py
[dbg] concurrent revoke returns: ['OK', 'OK', 'OK', 'OK']
PROBE-4 §7: PASS
```

矩阵条目：

| 编号 | 攻击 / 场景 | 期望 | 观测 | 判定 |
| --- | --- | --- | --- | --- |
| P1-1 | 同一 canonical envelope 两次 publish | 单一 object；`object_id`/`canonical_sha256`/`catalog_key` 完全一致；`shared/objects/**` 只有 1 个 `o_*` 文件 | 相同 3 项、文件数=1 | ✅ |
| P1-2 | 同 body 不同 `source_namespace` | `object_id` / `catalog_key` 都不同 | 均不同 | ✅ |
| P1-3 | 同 body 不同 `report_id` | `object_id` / `catalog_key` 都不同 | 均不同 | ✅ |
| P1-4 | A/B 各自 promote 同 report | `grant_key` 隔离；A 目录下 grant 文件名不出现于 B | 26 char base32 尾部不同；跨目录零重名 | ✅ |
| P1-5 | A/B 各自 upsert view | view 文件名不跨租户重复 | 零重名 | ✅ |
| P1-6 | A 快照查询 `9999999` (不存在 report) | 统一 `AccessDenied("[denied] access denied")` | 完全命中 | ✅ |
| P1-7 | A 快照查询 `unknown_ns` | 统一 `AccessDenied` | 完全命中 | ✅ |
| P1-8 | 伪造 tenant_id 的 snapshot 查询已存在 report | 统一 `AccessDenied`；不泄露 report 存在性 | 完全命中 | ✅ |
| P1-9 | A 查询 "B-only" report | 统一 `AccessDenied` | 完全命中 | ✅ |
| P2-1 | A active + lease 未过期 → 查询成功 | 返回 `active` grant | ✅ | ✅ |
| P2-2 | snapshot `tenant_auth_epoch` 落后 | 统一 `AccessDenied` | ✅ | ✅ |
| P2-3 | snapshot `tenant_status ∈ {draft, profile_pending, suspended, offboarded}` | 统一 `AccessDenied` | 4/4 命中 | ✅ |
| P2-4 | `now = lease_expires_at + 1h` | 统一 `AccessDenied` | ✅ | ✅ |
| P3-1 | 用真 receipt 撤销 A | epoch bump；A 立即 deny；旧 snapshot 也 deny；tombstone 落盘；cleanup outbox 有条目 | 4/4 命中 | ✅ |
| P3-2 | B 不受 A 撤权影响 | B `auth_epoch` 不变；B 查询仍 `active`；B outbox 空 | 3/3 命中 | ✅ |
| P3-3 | `recover(actor=…)` 幂等 | 返回 `RecoveryReport` | ✅ | ✅ |
| P4-1 | view 文件字节扫描 canonical body/AppKey/Bearer/临时URL/host root | 全部不命中 | 5/5 未命中 | ✅ |
| P4-2 | `TenantViewStore.read_view` 返回 dict | 不含 `body`；含 `canonical_sha256` | ✅ | ✅ |
| P4-3 | 对 A（已撤销）尝试 `upsert_overlay` | `ViewDenied` / `AccessDenied` | 命中 | ✅ |
| P5-1 | 未注册 test authority，默认 fail-closed | schema-valid receipt 也被 `AuthorityRejected` | ✅ | ✅ |
| P5-2 | 观察 `initial_status="granted"` 后 `check_query_eligibility` | 仍 `AccessDenied`；grant.status 只到 `granted`，从不 `active` | ✅ | ✅ |
| P5-3 | 用为 B 签的 receipt 对 A `promote_to_active` | 拒绝（`AuthorityRejected` 或 `AccessLedgerError`） | ✅ | ✅ |
| P5-4 | 篡改 receipt 签名末 4 字符 | `AuthorityRejected` | ✅ | ✅ |
| P5-5 | 反射断言 `TenantViewStore` 无 `upsert_without_grant` / `force_upsert` / `delete_view` / `list_all_views` / `raw_view_bytes` | 全部无 | ✅ | ✅ |
| P5-6 | 无 grant 的 tenant 尝试 `upsert_overlay` | `ViewDenied` / `AccessDenied` / `TenantViewError` | 命中 | ✅ |
| P6-1 | 反射断言 `AccessLedger` 无 `list_all_*` / `iter_all_*` / `resolve_by_*` / `has_*` / `object_exists` | 全部无 | ✅ | ✅ |
| P6-2 | 反射断言 `TenantViewStore` 无 `list_all_*` / `iter_all_*` | 全部无 | ✅ | ✅ |
| P7-1 | A grant JSON 手动写入 `{ this is not valid json` | 统一 `AccessDenied`；B 不受影响 | ✅ | ✅ |
| P7-2 | D grant 中位 bit-flip | 统一 `AccessDenied`；错误消息不泄露临时 root 路径 | ✅ | ✅ |
| P7-3 | `shared/objects/**` 全量清 0 后 `read_version` | 抛 `SharedEvidenceError` 或 `FileNotFoundError`；错误消息不含 tempdir 绝对路径 | ✅（消息稳定，无路径泄漏） | ✅ |
| P7-4 | 手写 revoke intent journal 到 `registry/access-ledger/<t>/revoke-intents/*.journal`（模拟 crash 后未完成 revoke） | `check_query_eligibility` 立即返回统一 `AccessDenied` | ✅ | ✅ |
| P7-5 | `TenantRegistry.bump_auth_epoch(expected_auth_epoch=cur+999)` | `RegistryConflict` | 命中 | ✅ |
| P7-6 | 4 线程 × 2 tenants 并发 `observe`（8 fresh report_id） | 全部成功；无 corruption；文件独立 | 8/8 OK | ✅ |
| P7-7 | 4 线程并发对同一 grant `revoke` | 至少 1 成功；无 corruption | 观测到 4/4 幂等 OK（impl 使用 CAS-based idempotent revoke，比 receipt §4 描述的"至多 1 名 writer 成功"更强） | ✅（更强） |

> P7-7 的观测比 impl receipt §4 描述略强：并发 revoke 全部返回 OK，说明
> impl 已实现事务级幂等 revoke（后到者看到 tombstone 即 no-op）；无
> corruption、无 double-tombstone。仍满足 §7 "重复 revoke/CAS" 的门禁
> 要求，记为 Info（非 Blocker/Major/Minor）。

---

## 3. 发现（Blocker / Major / Minor / Info）

### Blocker：0

### Major：0

### Minor（1 项，均为文档/元数据）

- **Minor(doc-1)** — impl receipt §3.2/§3.3 的全量回归/基线数字（892/817）
  与当前 HEAD 实测（967/892）不一致；差值 75 对应 RT-016 的 75 条测试
  （RT-016 commit `c51dcb2` 在 VG-A commit `142f304` 之前落盘）。VG-A
  自身 Δ=75 一致，功能门禁不受影响。**建议**：impl 后续如需以 impl
  receipt 作为 audit trail，请在文档标注 "RT-016 之前采集"，或在下一
  次 RT 收尾时更新；本 verifier 报告已按当前 HEAD 记录 967/892/75。

### Info（3 项，非门禁问题，供设计与后续 RT 参考）

- **Info-1** — `AccessLedger.observe()` 对已 `active` 的 grant 允许通过
  observation 幂等地重写 `visibility_scope` / `roles` 元数据（不改
  status）。这是源码明确注释的设计（`scripts/cwk_access_ledger.py:1149-
  1158`：`Observation cannot demote; only refresh last_verified_at +
  roles/visibility as metadata`），不违反 §5 "observe≤granted"（status
  永远只能到 `granted`；`active` 仍必须由 `promote_to_active` +
  authority receipt 触发）。但下游 space projector / review queue
  如以 grant.visibility_scope 作为过滤条件，需注意 collector 侧
  observation 可以刷写此字段。**建议**：在 VG-C 的 route/preview 验收
  中显式覆盖这一路径，确保 visibility_scope 变化不会打开新的可见性。

- **Info-2** — `AccessLedger.read_grant_snapshot(snapshot=…)` 是 audit
  路径，会直接抛出 `GrantNotFound` / `GrantCorruption`。这一路径**不是**
  数据面查询，数据面走 `check_query_eligibility`（unified `AccessDenied`
  且 reason 隐藏）。因 `grant_key = H(GRANT_KEY_DOMAIN, tenant, report_key)`
  按租户维度做 SHA 域分离，即使 B 有 grant 而 A 没有，A 用自己的
  snapshot 走 audit 路径也只会得到 A 的 `GrantNotFound`，不能推断出
  B 是否持有对应 report。此路径未构成跨租户 existence 侧信道，属于
  设计意图。

- **Info-3** — VG-A 全部依赖 fake `HMAC-SHA256` 签名 + `FakeSigningAuthority`；
  仅证明 host 内部 authority 契约与状态机；**真实 Gateway、真实工作
  协同权限 API、真实 secret backend、真实沙箱传输、Broker、Scheduler
  全部未接入**，标签保持 `conservative_unknown`。RT-017 只能在
  "deferred view + 无真实 authority 不落 TenantView" 边界下启动；
  RT-018/019/020/021/022/023 仍需在真实 Gateway 控制环境下完成
  各自的独立验收，本报告不预授任何生产/试点/M3-M5 决策。

---

## 4. 与实现者 receipt §4 断言的对齐核对

我在 §2 的攻击矩阵已独立复现（未依赖实现者的 test 文件路径）以下断言：

| impl receipt §4 断言 | 独立复现 |
| --- | --- |
| `AccessDenied.__str__ == "[denied] access denied"` 覆盖所有 unified deny 分支 | ✅ P1-6/7/8/9, P2-2/3/4, P3-1(旧snap), P7-1/2/4 |
| 共享 canonical 单对象；shared/objects/** 只有 1 个 `o_*` | ✅ P1-1 |
| `grant_key = H(GRANT_KEY_DOMAIN, tenant, report_key)` 隔离 A/B | ✅ P1-4 |
| revoke A → tenant.auth_epoch bump；B 不变；tombstone/outbox；旧/新 snapshot 都 deny；B 继续 active | ✅ P3-1, P3-2 |
| 默认 `_FailClosedAuthority` 拒绝任何 receipt | ✅ P5-1 |
| `AccessLedger` / `TenantViewStore` 无任何 side-door 与枚举 | ✅ P5-5, P6-1, P6-2 |
| crash 后 tombstone 保持；即便 receipt 被误删也 deny | ✅ P7-4（intent journal 存在即立即 deny） |
| CAS 拒绝 wrong-expected（`bump_auth_epoch`） | ✅ P7-5 |
| 损坏 grant JSON / bit-flip → deny 且 B 不受影响 | ✅ P7-1, P7-2 |
| view / canonical 联动损坏后拒绝 | ✅ P7-3 + P4-3（撤销后 upsert 拒绝） |
| A/B 并发 observe / 并发 revoke 无 corruption | ✅ P7-6, P7-7 |

---

## 5. RT-017 前置条件（本 verifier 授权范围）

**在** 本报告 PASS 的合成边界下，RT-017 可在以下**约束**内开始独立
implementation（不含 verifier）：

1. **必须**保持 `AccessLedger` / `TenantViewStore` / `SharedEvidenceStore`
   公共 API 冻结（本报告已通过反射断言验证）。
2. **不得**在无真实 authority 的情况下向 `TenantViewStore.upsert_overlay`
   写入包含正文、临时 URL、`credential_ref`、host path、reply/node
   完整 payload 的 view；只允许保存 deferred-view reference 或
   review（PRD FR-05 / DESIGN §7 / VG-A §4）。
3. **不得**新增任何 side-door / 枚举 / 强制 upsert API；本报告禁止
   清单请见 §1.7、P5-5、P6-1、P6-2。
4. RT-017 collector 触发 `AccessLedger.observe()` 时，观察源必须遵循
   `initial_status ∈ {discovered, granted}`（RT-011 schema 已强制），
   并须知悉 Info-1：**observation 会刷写 visibility_scope/roles 元数据**。
5. 任何真实 Gateway / 工作协同 / 沙箱 / secret backend 接入都 **不属于**
   本 VG-A 授权范围；需要在对应 RT 独立复核，且必须重新触发 VG-B 及
   之后的波次门禁。

**不授权**：G3 / M3 / M4 / M5 / 生产试点 / SLA / RTO / RPO / 任何
非合成集成层面的宣称。

---

## 6. 最终裁决

- 合成集成门禁 §1~§7：**PASS**
- 定向 75 tests + 全量 967 tests：**OK / OK (skipped=7)**
- `git diff --check`：**clean**
- 秘密扫描（VG-A 新增文件）：**无真实密钥**
- RT-011~RT-016 模块 + PRD/DESIGN/plan/index.yaml + contracts + reviews：
  **零字节漂移**
- 独立黑盒攻击矩阵（P1~P7 共 33 条）：**全部按预期 fail-closed 或按
  预期通过**
- Blocker: 0 · Major: 0 · Minor: 1（文档数字过期）· Info: 3

**Verdict：VG-A 合成集成门禁 PASS（synthetic authority + host-chain only）**。
RT-017 可在 §5 所列 5 条约束下按"deferred view 且无真实 authority 不落
TenantView"启动 implementation。**明确不宣称** G3 / M3 / 真实 Gateway /
生产集成通过；这些仍属 conservative_unknown，须在对应 RT 与后续
VG-B~VG-E 波次独立复核。

---

## 7. 附：清理与残留

- 探针工作目录 `/tmp/vga-verify-O6wlc1/`（含 `probe1_*.py` ~
  `probe4_*.py`）已在验证结束时保留，供人工回放；测试自身使用的
  `tempfile.mkdtemp(prefix='vga-probe*-')` 目录在探针 `finally` 分支
  中 `shutil.rmtree(ignore_errors=True)` 已销毁。
- 未修改 RT-011~RT-016 任一文件、未新增任何 CLI/HTTP/enumeration API、
  未接触 `CWORK_APP_KEY`、未接触真实 Gateway / Cloud / DocDB / cron。
- 本 branch 唯一 stage 的产物为
  `PR/PR-001-multitenant-knowledge-spaces/gate-receipts/VG-A-独立验收.md`
  （即本报告）。
