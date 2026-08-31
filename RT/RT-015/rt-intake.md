# RT-015 rt-intake：Access Ledger、Tenant View 与撤权核心

- 状态：completed（独立黑盒验收 PASS；closure commit `7ba906f`）
- Profile：Spec-Standard
- 依赖：
  - RT-011：`cwk.access_grant.v1`、`cwk.access_observation.v1`、`cwk.tenant_view.v1`
    frozen schema、`validate_access_grant/observation/tenant_view`、
    `validate_access_grant_transition`、`ACCESS_GRANT_STATES`、
    `ACCESS_GRANT_ALLOWED_TRANSITIONS`、`ACCESS_GRANT_QUERY_ELIGIBLE`、
    `canonical_json_bytes/canonical_sha256`、`nfc_normalize`、
    `compose_report_key/parse_report_key`、`TENANT_ID_REGEX`、
    `REPORT_KEY_REGEX`、`SHA256_HEX_REGEX`、`_validate_schema`、
    `_iter_deep_forbidden`、`ContractError`。
  - RT-012：`InstanceLayout.registry_fd("access-ledger")`、
    `InstanceLayout.tenant(...).child_fd("views")`、
    `cwk_atomic_file` 的 dirfd + `write_atomic` / `cas_write` /
    `exclusive_lock` / `read_file` / `child_exists` / `unlink_at` /
    `recover_orphans` / `fsync_dir` / `FILE_MODE` / `DIRECTORY_MODE`；
    `TenantRegistry.get`、`TenantRegistry.bump_auth_epoch`（CAS）、
    `_R.TenantNotFound`。
  - RT-013：`AgentContextSnapshot`（唯一被消费的身份 snapshot；
    RT-015 决不从请求体接受 `agent_id/tenant_id`）。
  - RT-014：`SharedEvidenceStore.read_version(report_key, canonical_sha256)`；
    RT-015 **只读**，绝不写 canonical、不列举 shared/objects/、不新建
    共享目录、不引入 SHA-existence 探测。

- 与 RT-016/017/018/019/020/021/022 关系：
  - RT-016/017 使用 `AccessLedger.observe` 输入 legacy raw / per-tenant
    collector 观察结果；观察永远不能升到 `active`。
  - RT-019/020/021/022 使用 `AccessLedger.check_query_eligibility`、
    `list_query_eligible`、`TenantViewStore.read_view`、
    `AccessLedger.iter_cleanup_outbox` + `ack_cleanup_task` 建索引、
    Broker 授权和撤权清理。

## 一、目标（严格范围）

1. 在宿主机内部实现一份不可复活的 **Access Ledger**：
   - 输入：RT-011 冻结的 `cwk.access_observation.v1` 与
     `cwk.rt015.authority_receipt.v1`。
   - 输出：以 opaque `grant_key = H(tenant_id, report_key)` 为路径的
     per-grant 记录（RT-011 `cwk.access_grant.v1` 封装成
     `cwk.rt015.access_grant_record.v1`）、追加式 state-transition
     event log、七步撤权流水线（intent → mark → CAS bump tenant
     auth_epoch → tombstone → cleanup-outbox → receipt → clear
     journal）、幂等 recovery。
2. 在宿主机内部实现一份 **Tenant View Store**（overlay-only）：
   - 输入：调用方传入的 `cwk.tenant_view.v1`；引用 canonical version 的
     `canonical_sha256` 必须通过 `SharedEvidenceStore.read_version()`
     校验存在。
   - 输出：`tenants/<tenant_id>/views/<view_key>.json`（`view_key =
     H(tenant_id, report_key)`，与 `grant_key` 相同）；仅记录
     `lane/read/todo/reply/node/attachment` overlay；绝不复制 canonical
     正文；`reply_overlay/node_overlay` 只允许 IDs + 可选
     `content_sha256`（不允许 full body）。
   - Read 前后双次 `check_query_eligibility`；in-flight 撤权直接 deny。
3. 提供最小库 API，无 CLI/HTTP：
   - `class AccessLedger.__init__(layout, tenant_registry, shared_store)`
   - `.initialize()`
   - `.observe(observation, actor, reason) -> GrantRecord`
   - `.promote_to_active(*, tenant_id, source_namespace, report_id,
     authority_receipt, actor, reason, lease_ttl_seconds)`
   - `.refresh_lease(...)`
   - `.mark_revalidation_due(...)`
   - `.revoke(...) -> RevokeReceipt`
   - `.check_query_eligibility(*, snapshot, source_namespace,
     report_id, now=None) -> GrantRecord`
   - `.read_grant_snapshot(...)`
   - `.list_query_eligible(*, snapshot)`
   - `.iter_events(...)`（审计用）
   - `.read_tombstone(...)`
   - `.iter_cleanup_outbox(*, tenant_id)`
   - `.ack_cleanup_task(*, tenant_id, outbox_id, consumer, actor,
     reason)`
   - `.recover(*, actor, reason) -> RecoveryReport`
   - `class TenantViewStore.__init__(layout, ledger, shared_store)`
   - `.upsert_overlay(*, snapshot, view_envelope) -> ViewRecord`
   - `.read_view(*, snapshot, source_namespace, report_id) -> ViewRecord`
   - `.purge_for_revoked_grant(*, tenant_id, source_namespace,
     report_id, actor, reason) -> PurgeReceipt`
   - `.recover() -> dict`
4. 撤权 SoR：即使 canonical 存在、旧索引存在、legacy 观察存在，都
   **不能** 推断权限；任何 fail-closed 分支都不给出细节化原因（错误
   `.reason` 属性内部区分，`__str__` 保持不透明）。

## 二、非目标

- 不解析源端凭据、不接触 CWork API、不读 `CWORK_APP_KEY`、不访问
  tenant workspace / DocDB / Cloud / 真实 Broker / 真实 Scheduler。
- 不新增 CLI/HTTP，不修改 `cwk_tenant_cli.py` dispatcher，不加入
  provider registry。
- 不实现真实 authority adapter；仅提供 fail-closed 默认 +
  `FakeSigningAuthority`（HMAC，测试专用，需 `_TEST_AUTHORITY_TOKEN`）。
- 不实现 tenant space index / cache / archive metadata / review queue
  的实际清理动作，只写幂等 cleanup outbox；对应消费者由后续 RT 实现。
- 不实现权限发现或撤权事件源（这些属于 RT-017/018/权威 authority）。
- 不迁移 legacy 数据（RT-016 只能做 hash/crosswalk/review，见下文
  「三 · 边界」）。
- 不宣称完成 G3 / VG-A / M3 / 生产迁移。RT-015 只交付逻辑授权/撤权
  消费者契约；VG-A 由后续独立验收 Agent 判定。

## 三、边界（RT-016 / legacy timeline 交互）

- `cwk.tenant_view.v1` 是 RT-011 冻结契约。RT-015 **不扩展** 该 schema。
  `reply_overlay / node_overlay` 只允许 `reply_id/node_id + 可选
  content_sha256`（+ `type` / `visible`）；完整 reply / node 正文
  **绝不进入** tenant view。
- Legacy RT-007 timeline snapshot 含完整 reply/node payload 且无可信
  顺序。RT-016 只能对 timeline 做 hash 校验 / crosswalk / review 队列
  入队，不能通过 RT-015 的接口把 payload 上升为 `verified_shared`
  或塞进 tenant view。任何 payload 层面的迁移都需要新一轮 RT-011 级
  独立评审后新增 `cwk.tenant_view.v2` 或 `verified_shared_extensions_v2`。
- Access observation 由 RT-011 schema 限定为
  `discovered / granted`；无论 RT-016/017 传入什么 `initial_status`，
  RT-015 都不会把 grant 直接推到 `active`。要激活必须获得
  `cwk.rt015.authority_receipt.v1` + 通过 authority adapter 验证。默认
  adapter fail-closed。

## 四、公开接口冻结签名（供后续 RT 消费）

见 `../specs/技术方案.md §3-§5` 与 `../specs/需求契约.md`。

## 五、独占的文件与目录

- 新建
  - `scripts/cwk_access_ledger.py`
  - `scripts/cwk_tenant_view.py`
  - `PR/PR-001-multitenant-knowledge-spaces/contracts/rt015/schemas/`
    - `access_grant_record.schema.json`
    - `state_transition_event.schema.json`
    - `revoke_intent.schema.json`
    - `revoke_receipt.schema.json`
    - `access_tombstone.schema.json`
    - `cleanup_outbox.schema.json`
    - `authority_receipt.schema.json`
    - `tenant_view_record.schema.json`
  - `tests/test_rt015_schemas.py`
  - `tests/test_rt015_ledger.py`
  - `tests/test_rt015_revocation.py`
  - `tests/test_rt015_view.py`
  - `tests/test_rt015_attacks.py`
  - `tests/_rt015_helpers.py`
  - `RT/RT-015/rt-intake.md`
  - `RT/RT-015/specs/需求契约.md`
  - `RT/RT-015/specs/技术方案.md`
  - `RT/RT-015/tasks/开发任务.md`
  - `RT/RT-015/reports/实现记录.md`
  - `RT/RT-015/reports/交付验证报告.md`

- 修改
  - `RT/index.yaml`（RT-015 状态 → `implementation_done`）

- 绝不修改（RT-015 零漂移契约）：
  - RT-011：`scripts/cwk_pr001_contracts.py`、`scripts/cwk_pr001_probes.py`、
    `PR/.../contracts/schemas/*`、`PR/.../contracts/security_defaults.json`、
    `PR/.../contracts/verified_shared_extensions_v1.json`。
  - RT-012：`scripts/cwk_instance.py`、`scripts/cwk_atomic_file.py`、
    `scripts/cwk_tenant_registry.py`、`scripts/cwk_tenant_cli.py` 与其
    provider registry、`PR/.../contracts/rt012/`。
  - RT-013：`scripts/cwk_agent_binding.py`、`scripts/cwk_agent_context.py`、
    `scripts/cwk_credential_broker.py`、`scripts/cwk_tenant_cmd_binding.py`、
    `PR/.../contracts/rt013/`。
  - RT-014：`scripts/cwk_shared_evidence.py`、`PR/.../contracts/rt014/`。
  - Legacy：`cwk_collect_live.py`、`cwk_nightly_pipeline.py`、
    `cwk_raw_store.py`、`cwk_wiki_query.py`、`cwk_entity_catalog.py`、
    `cwk_wiki_search_index.py`、`cwk_docdb_cloud.py`、
    `cwk_sync_mirror_to_docdb.py` 等 legacy 模块。

## 六、安全默认

- Access grant 状态严格遵守 RT-011 冻结 v1 图：
  `discovered → granted → active → revalidation_due → active|revoked`；
  `revoked → purge_pending → purged`。只有 `active` + 未过期 lease +
  与 snapshot 匹配的 `auth_epoch` 才允许 query；其余（含 `discovered
  / granted / revalidation_due / revoked / purge_pending / purged` 及
  未知）全部 fail closed，无宽限。
- Authority receipt 默认 fail closed（`_FailClosedAuthority`）；测试
  只可 register `FakeSigningAuthority`，需 module-private
  `_TEST_AUTHORITY_TOKEN`。真实 authority 保持 `conservative_unknown`。
- 撤权先 append intent journal（自此 query 一律 deny）；随后 CAS mark
  revoked、CAS bump tenant auth_epoch、写不可查询 tombstone、写幂等
  cleanup outbox（`tenant_view/space_index/cache` 至少三个消费者
  枚举）、写 receipt、unlink journal。任何点崩溃后 `recover()` 只
  向前推进；tombstone 一旦存在，观察 / 促升 / 双 revoke 全部拒绝。
- Actor / reason：仅允许可打印 ASCII（`\x20-\x7e`）；长度上限
  128/256；NUL/CR/LF/ESC/DEL/TAB 一律拒绝（`LogInjectionDetected`）；
  schema 层再次 `\A[\x20-\x7e]+\Z` 校验。
- 所有 state event 记录 `actor`、`reason`、
  `tenant_auth_epoch_before/after`、`record_revision_before/after`、
  `evidence_refs`（受限字符集，无正文/URL/credential）。
- 目录默认 `0o700`，文件默认 `0o600`；每个 tenant 独立子树；
  `O_NOFOLLOW` + `dirfd` 全链路；每 grant 独立 `flock`；符号链接/
  硬链接/预置临时文件/TOCTOU 全部 fail closed。
- 错误 `__str__` 只包含稳定 `code` 与 opaque `grant_key`（可选），
  不含绝对路径、正文字节、临时 URL、`agent_id` 明文、credential。
- 未通过 `AgentContextSnapshot` 的查询接口（`check_query_eligibility`
  / `read_grant_snapshot` / `list_query_eligible`）不接受裸 tenant_id；
  写路径（`observe / promote / revoke`）接受 tenant_id，但要求
  已合法（通过 RT-012 tenant registry 存在检查）。

## 七、验收关注（供独立黑盒验收 Agent 参考）

RT-015 独立验收报告至少应验证：
- Schema 覆盖：deep forbidden、additionalProperties:false、
  actor/reason `\A[\x20-\x7e]+\Z`；违反项 fail closed。
- 状态机：`discovered → granted → active → revalidation_due →
  active|revoked` + `revoked → purge_pending → purged` 全部合法迁移
  可达；非法迁移 (`active → granted`、`revoked → active`、
  `purged → *`) 拒绝。
- Lease 与 authority：默认 adapter fail-closed；fake HMAC signer
  验证签名/绑定/receipt_type/lease 过期；observation 无法升 active。
- 撤权 crash-safety：每一步（intent-only、marked-only、
  epoch-only、tombstone-only、outbox-only、receipt-only）注入后
  `recover()` 完成；再次 `revoke` 返回同一 receipt；双 revoke 不
  bump 第二次 auth_epoch。
- Snapshot / cache / in-flight：旧 snapshot（stale tenant_auth_epoch）、
  in-flight intent、tombstone 都能 deny；view read 二次 recheck 生效。
- 隔离：两个 tenant 共享同一 canonical 版本，但 grant/view 独立；
  revoke A 不影响 B；跨 tenant snapshot 无法查询另一 tenant。
- 文件系统：路径穿越/编码变体/符号链接/硬链接/TOCTOU/preexisting
  temp 全部 fail closed；`0o700`/`0o600`；不新增枚举 API。
- 零漂移：`tests/test_rt015_schemas.py::FrozenFilesZeroDriftTests`
  通过（RT-011~014 模块与 schema 的 SHA-256 与 HEAD 一致）。
- 全套单测（RT-011~014 + RT-015）通过。

## 八、回滚

- 删除 RT-015 新增文件即可；未修改 RT-011~014 任何文件；未启用生产
  运行时；未接入真实 Broker / Scheduler / cron / Cloud / DocDB /
  installer。
- 撤权后写入的 tombstone 与 cleanup outbox 是不可逆语义；即使删除
  RT-015 模块，也不会自动恢复已撤权 grant（这是设计目标之一）。
