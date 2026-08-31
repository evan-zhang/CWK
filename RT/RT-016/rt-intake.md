# RT-016 rt-intake：Legacy Raw 影子迁移与数据级对账

- 状态：completed（第三轮独立黑盒验收 PASS；171/171 定向、
  1063/1063 全量、27/27 独立攻击；closure commit `470daef`）
- Profile：Spec-Standard
- 依赖：
  - RT-011：`cwk.canonical_report.v1`、`cwk.tenant_view.v1`、
    `cwk.access_observation.v1` 冻结 schema；
    `validate_canonical_envelope / validate_tenant_view /
    validate_access_observation`、`canonical_json_bytes /
    canonical_sha256`、`nfc_normalize`、`compose_report_key /
    parse_report_key`、`TENANT_ID_REGEX / REPORT_KEY_REGEX /
    SOURCE_NAMESPACE_REGEX / REPORT_ID_REGEX / SHA256_HEX_REGEX /
    OBJECT_ID_REGEX`、`_validate_schema` / `_iter_deep_forbidden`、
    `ContractError`。
  - RT-012：`InstanceLayout.child_fd("registry")`、
    `InstanceLayout.child_fd("staging")`、`InstanceLayout.tenant()`、
    `cwk_atomic_file` 的 dirfd + `write_atomic / cas_write /
    exclusive_lock / read_file / child_exists / unlink_at /
    recover_orphans / fsync_dir / FILE_MODE / DIRECTORY_MODE`；
    `TenantRegistry.get / init_tenant`。
  - RT-013：仅通过 `AgentContextSnapshot`（fake-authority 情境下
    构造）；RT-016 主体流程不依赖真实 Agent 绑定。
  - RT-014：`SharedEvidenceStore.publish / read_version / recover`；
    RT-016 只消费 RT-014 公开 API，不列举 shared 内部、不写
    catalog、不引入 SHA-existence 探针。
  - RT-015：`AccessLedger.observe`（唯一被主流程消费的写路径，
    始终写入 `initial_status=granted`）；
    `AccessLedger.promote_to_active`、`TenantViewStore.upsert_overlay`
    仅在测试环境下、经 fake `AuthorityAdapter` 签名的 receipt 通过后
    才会被调用（详见「三 · 边界」）；`_validate_actor_reason` / 撤权
    墓碑 / cleanup outbox 等由 RT-015 内部维护，RT-016 不复用其
    私有函数。

- 与后续 RT / 门禁的关系：
  - RT-017（per-tenant collector）：也会调用
    `AccessLedger.observe`，RT-016 不与之串行，也不共享 crosswalk
    命名空间。
  - RT-018 / RT-019 / RT-020 / RT-021 / RT-022 / RT-023 / RT-024：
    读端消费 RT-015 grant / tenant view，从不读取 RT-016 crosswalk。
    RT-016 crosswalk 仅供 RT-025（clean-room restore）和 RT-026
    （试点切换）在需要时消费。
  - RT-025：加密备份必须覆盖 `registry/rt016-crosswalk/`；恢复后
    crosswalk 与 RT-014 catalog、RT-015 grant 应一一对应。
  - RT-026：在 RT-022/023 上线后，用 RT-016 crosswalk 加 RT-014
    read_version 双读同一份证据；查询 diff、feature flag 切换、
    生产 tenant 启用均属 RT-026 范围，与 RT-016 无直接耦合。

## 一、目标（严格范围）

1. 在不修改任何 legacy raw / RT-007 timeline 数据树的前提下，
   把合成 legacy Markdown/timeline fixture 影子导入宿主机内部
   RT-014 Canonical Evidence Store 与 RT-015 Access Ledger，
   并在 RT-016 专属 tenant-scoped namespace 下写出确定性
   数据级 crosswalk：
   - 三元组 = `CanonicalEnvelope`、`TenantViewEnvelope`、
     `AccessObservation(initial_status="granted",
     observation_source="legacy_raw_decomposition")`。
   - 三类哈希（`legacy_source_sha256` / `canonical_sha256` /
     `object_bytes_sha256`）严格区分，永不宣称相等；同一份
     canonical 允许对应多个 `legacy_source_sha256`（overlay-only
     legacy diff）。
2. 提供 `MigrationReconciler`：以 RT-016 durable crosswalk 作为
   new-side 全集，对每条调用 RT-014 `SharedEvidenceStore.read_version`
   完整校验（无需新增 enumerate API），并与合成 legacy tree 递归
   路径 + 字节 hash 逐条比对，输出严格 schema 校验过的
   `ReconciliationReport`。
3. 对以下情况一律 fail closed（零 canonical / view / grant 写入，
   只写 RT-016 review 记录）：
   - 正文超 1 MiB、缺 author / created_at / source_updated_at、
     未知或异形 frontmatter/JSON、timeline event 覆盖失败、路径
     穿越 / 符号链接 / 硬链接 / TOCTOU、重复 report_id 冲突、
     Unicode NFC 冲突、canonical schema 校验失败。
4. 明确 RT-015 fail-closed blocker（本 RT 不改冻结层）：
   - `TenantViewStore.upsert_overlay` 需要 `active` grant +
     未过期 lease，而 legacy raw 观察只能产生 `granted`；生产
     环境下真实 authority 仍 `conservative_unknown`。因此 RT-016
     的 tenant view 在无 authority receipt 时保存在 crosswalk 内
     的 `tenant_view_envelope` 字段，并把
     `tenant_view_deferred_reason` 置为
     `"no_authority_receipt_available"`；tests 通过 RT-015
     `FakeSigningAuthority` 覆盖真实上线前的行为端到端。
     绝不由 RT-016 自行把 `granted` 升为 `active`，也不宣称完成
     M3/G3/VG-A/生产迁移。

## 二、非目标

- 不解析源端凭据、不读 `CWORK_APP_KEY`、不访问 CWork API、
  不访问 tenant workspace / DocDB / Cloud、不调用真实 Broker /
  真实 Scheduler、不接入真实 cron 或 installer。
- 不修改任何 legacy raw 树（含 `raw/**/*.md` 与
  `raw/_system/timelines/`）；不启用
  `cwk_thread_timeline.capture`；不走
  `cwk_raw_store.raw_index` 的静默去重路径。
- 不新增 CLI / HTTP / daemon；不写入 `cwk_tenant_cli.py` provider
  registry；不引入新的 fixed leaf 到 RT-012
  `REGISTRY_CHILDREN`（RT-016 用 O_NOFOLLOW 直接管理自有子目录）。
- 不完成 legacy 时间线的 payload 迁移（reply/node 完整 body 仍属
  legacy，只做 hash / crosswalk / review）。
- 不宣称 G3 / VG-A / M3 / M4 / M5 完成；不宣称生产迁移准备就绪；
  不启用真实租户 pilot。

## 三、边界（与 RT-015 冻结契约的关系）

- **观察永远不升 `active`**：`AccessLedger.observe` 是 RT-015 冻结
  API，明确对 legacy raw 推断出的 grant 只允许写入 `discovered` /
  `granted`。RT-016 每次调用都传入
  `initial_status="granted"` +
  `observation_source="legacy_raw_decomposition"`；如果 RT-015 未来
  修改允许列表，本 RT 也不会主动尝试其他 status。
- **tenant view 需要 active grant + lease**：`TenantViewStore.upsert_overlay`
  内部会先 `check_query_eligibility`（要求 grant.status == "active"
  + snapshot.tenant_status ∈ {pilot, active} + lease 未过期）+
  canonical 存在校验 + 二次 recheck。RT-016 单纯观察产生的 grant
  始终是 `granted`，因此在生产路径（无 authority receipt）下
  upsert_overlay 一定失败；本 RT 不重试、不绕过，只在 crosswalk 中
  记录 `tenant_view_written=False` 与
  `tenant_view_deferred_reason`。
- **authority adapter 默认 fail-closed**：`_FailClosedAuthority.verify`
  永远抛 `AuthorityRejected`。生产上线前无真实 authority
  integration，RT-016 因此不会尝试 promote。tests 使用 RT-015
  暴露的 `_register_test_authority` / `FakeSigningAuthority` +
  module-private `_TEST_AUTHORITY_TOKEN` 覆盖端到端。
- **timeline payload 不入 tenant view / canonical**：RT-007 timeline
  snapshot 含完整 reply/node payload 且顺序不可信；RT-016 只对
  snapshot 与 event bytes 各自计算 SHA-256 并写入 crosswalk 中的
  `timeline_snapshot_hashes` / `timeline_event_hashes` 数组，
  不解析并入库 payload；覆盖检查失败时 quarantine。
- **不侵入 shared / registry/access-ledger / views 目录**：RT-016 通过
  RT-014 / RT-015 公开 writer 间接写入这三处；自有 durable 数据
  只写到 `registry/rt016-crosswalk/<tenant_id>/`；staging 只在
  `staging/rt016/<run_id>/`。

## 四、公开接口（供后续 RT / 独立验收消费）

```python
# scripts/cwk_legacy_raw_import.py
class LegacyRawDecomposer:
    __init__(*, decomposer_version="v1", normalizer_version="v1")
    decompose(*, raw_bytes, tenant_id, source_namespace,
              run_started_at, source_kind="current_raw",
              timeline_snapshot_bytes=None,
              timeline_event_bytes=None) -> DecomposeResult

class LegacySource:
    __init__(root_path)  # 绝对路径；root 必须是目录，非 symlink
    snapshot() -> dict[str, str]
    re_scan() -> dict[str, str]
    verify_no_drift()          # raise LegacyDriftDetected on any change
    iter_files_with_hashes()   # (rel_path, bytes, sha256)

class ShadowImporter:
    __init__(layout, tenant_registry, evidence_store, access_ledger,
             view_store, decomposer=None)
    initialize()
    import_one(*, tenant_id, source_namespace, raw_bytes, run_id,
               run_started_at, actor, reason, legacy_path_hint,
               source_kind="current_raw",
               timeline_snapshot_bytes=None, timeline_event_bytes=None,
               authority_receipt=None, allow_view_upsert=True
              ) -> ImportReceipt
    import_batch(*, tenant_id, source_namespace, source, run_id,
                 run_started_at, actor, reason,
                 authority_receipt_factory=None) -> list[ImportReceipt]
    read_crosswalk(*, tenant_id, crosswalk_key) -> dict
    iter_crosswalks(*, tenant_id) -> Iterator[dict]
    iter_reviews(*, tenant_id) -> Iterator[dict]
    iter_manifest(*, tenant_id, run_id) -> Iterator[dict]
    recover(*, actor, reason) -> RecoveryReport

# scripts/cwk_migration_reconciler.py
class MigrationReconciler:
    __init__(layout, importer, evidence_store)
    reconcile(*, anchor, run_id, source) -> ReconciliationReport
```

`DecomposeResult`、`ImportReceipt`、`RecoveryReport`、
`ReconciliationReport` 均为 frozen dataclass / 严格 JSON schema
校验过的 payload。

## 五、独占的文件与目录

新增：

- `scripts/cwk_legacy_raw_import.py`
- `scripts/cwk_migration_reconciler.py`
- `PR/PR-001-multitenant-knowledge-spaces/contracts/rt016/schemas/`
  - `decompose_report.schema.json`
  - `migration_crosswalk.schema.json`
  - `review_entry.schema.json`
  - `migration_manifest_entry.schema.json`
  - `reconciliation_report.schema.json`
- `tests/_rt016_helpers.py`
- `tests/test_rt016_schemas.py`
- `tests/test_rt016_decomposer.py`
- `tests/test_rt016_importer.py`
- `tests/test_rt016_reconciler.py`
- `tests/test_rt016_attacks.py`
- `RT/RT-016/rt-intake.md`
- `RT/RT-016/specs/需求契约.md`
- `RT/RT-016/specs/技术方案.md`
- `RT/RT-016/tasks/开发任务.md`
- `RT/RT-016/reports/实现记录.md`
- `RT/RT-016/reports/交付验证报告.md`

修改：

- `RT/index.yaml`（新增 RT-016 行 → `implementation_done`）

绝不修改（RT-016 零漂移契约）：

- RT-011：`scripts/cwk_pr001_contracts.py`、`scripts/cwk_pr001_probes.py`、
  `scripts/cwk_pr001_view_compare.py`、`scripts/cwk_pr001_cli.py`、
  `PR/.../contracts/schemas/*`、
  `PR/.../contracts/security_defaults.json`、
  `PR/.../contracts/verified_shared_extensions_v1.json`。
- RT-012：`scripts/cwk_instance.py`、`scripts/cwk_atomic_file.py`、
  `scripts/cwk_tenant_registry.py`、`scripts/cwk_tenant_cli.py`、
  `PR/.../contracts/rt012/`。
- RT-013：`scripts/cwk_agent_binding.py`、`scripts/cwk_agent_context.py`、
  `scripts/cwk_credential_broker.py`、`scripts/cwk_tenant_cmd_binding.py`、
  `PR/.../contracts/rt013/`。
- RT-014：`scripts/cwk_shared_evidence.py`、`PR/.../contracts/rt014/`。
- RT-015：`scripts/cwk_access_ledger.py`、`scripts/cwk_tenant_view.py`、
  `PR/.../contracts/rt015/`。
- Legacy 数据面：`cwk_collect_live.py`、`cwk_nightly_pipeline.py`、
  `cwk_raw_store.py`、`cwk_thread_timeline.py`、`cwk_wiki_query.py`、
  `cwk_entity_catalog.py`、`cwk_wiki_search_index.py`、
  `cwk_docdb_cloud.py`、`cwk_sync_mirror_to_docdb.py`。

## 六、安全默认

- 每份 legacy raw 的 `legacy_source_sha256` 严格 = 磁盘原始字节
  的 SHA-256；`canonical_sha256` 严格 = RT-011 recipe（NFC + JCS
  after excluding `canonical_sha256` 字段）；`object_bytes_sha256`
  严格 = RT-014 catalog 存储字节的 SHA-256。三者语义独立，crosswalk
  与 review 都同时保存三者，不允许合并或复用。
- Actor / reason 强制 printable ASCII `\x20-\x7e`，长度上限
  128 / 256；违反抛 `LogInjectionDetected`。
- Body 超过 1 MiB 直接 quarantine，永不截断；缺 author / time /
  frontmatter 与未知结构直接 quarantine，永不猜造。
- Legacy tree 通过 `LegacySource` 打开：root 必须绝对路径 + 目录 +
  非 symlink；子树按 `O_DIRECTORY | O_NOFOLLOW` 递归；symlink 与
  `nlink > 1` 的文件跳过；单文件 32 MiB 上限；仅接受
  `[A-Za-z0-9_.\-]+` 命名的 `.md` 叶节点。
- 目录默认 `0o700`，文件默认 `0o600`（继承 RT-012
  `DIRECTORY_MODE` / `FILE_MODE`；测试断言 crosswalk 叶为 0o600）。
- Crosswalk / review / manifest 全部用 CAS + JCS bytes 存盘，读时
  逐字节复核，任何漂移抛 `LegacyImportError(code="corrupt")`。
- 每次 `import_one` 在 `_find_existing_crosswalk_for_legacy` 命中
  同 `legacy_source_sha256` 时直接返回既有 receipt，无副作用；
  同 run 与跨 run 幂等；per-crosswalk `flock` 保证 CAS 写唯一。
- RT-015 `observe` 在 macOS 高并发下的 ENOENT 竞态：RT-016 通过
  自有 8 次 exponential-jitter 重试无声兜住；不修改 RT-015；其他
  错误一律抛 `LegacyImportError(code="ledger_denied")` 供上层
  fail-closed 处理。
- 错误 `__str__` 不含绝对路径、正文字节、临时 URL、`agent_id`、
  credential；只透露稳定 `code` 与可选 `crosswalk_key / review_id`。

## 七、验收关注（供独立黑盒验收 Agent 参考）

RT-016 独立验收报告至少应验证：

- 三类哈希严格区分：同一 legacy raw decompose 后
  `legacy_source_sha256 != canonical_sha256 != object_bytes_sha256`。
- Overlay-only diff：两份 legacy raw 仅 frontmatter 不同 → 两个
  crosswalk 共享同一 `canonical_sha256`；RT-014 catalog 只有一条
  记录。
- 正文版本升级：同 report_id 不同 body → 两个 canonical_sha256、
  RT-014 catalog 两条记录、两个 crosswalk。
- Namespace 区隔：两个 tenant 共享 canonical，但 crosswalk /
  view_key 相互独立；未经授权 tenant 不能列举另一 tenant 的
  crosswalk。
- 所有 quarantine 例子：missing / malformed frontmatter、
  oversize body、缺 author / created_at / source_updated_at、
  unparseable / ambiguous timezone、control chars、timeline
  event 覆盖缺失、malformed report_id、author_source_user_id 非
  法。
- 双哈希篡改：直接改动 crosswalk / review / manifest 字节，
  `read_crosswalk` / `iter_manifest` / `_find_existing_*` 全部
  fail closed。
- Staging / publish / view / observation / crosswalk 崩溃恢复：
  `recover(actor, reason)` 幂等清扫 `.cwk-tmp-*`；durable
  crosswalk 不被恢复流程改写；重复 `import_one` 收回同一
  receipt。
- 同 run / 同 source 并发：`ConcurrentImportTests` 验证多线程
  最终收敛到单一 `crosswalk_key`，无异常泄漏。
- RT-015 CAS / idempotency 冲突：`in_flight_revocation` /
  `authority_receipt_rejected` / `authority_receipt` 缺失均只
  影响 view，不损坏 canonical + observation + crosswalk。
- 路径 / 链接 / TOCTOU：`LegacySource` 拒绝 symlink root，跳过
  子树里的 symlink / hardlink；`legacy_path_hint` 哈希化后无法
  遍出 RT-016 之外目录。
- 零泄漏 / 零 legacy 写入：
  - crosswalk / review / manifest 文本不含
    `app_key / credential_ref / cookie / session_token / 绝对路径`；
  - 合成 legacy tree 递归 hash 在导入前后完全一致；
  - RT-011~015 冻结文件的 SHA-256 与显式 `(path → sha256)` allowlist
    基线（`7ba906f`）完全一致（`FrozenFilesZeroDriftTests`），并由
    `FrozenBaselineExactSetTests` 对
    `contracts/schemas/` + `contracts/rt012~015/schemas/` 做 exact-set
    元校验：任何新增 / 缺失文件都会 fail。第三轮修复不再对
    `HEAD:<path>` 自比较。
- 完整测试：`python3.11 -m unittest discover -s tests -p
  'test_rt016_*.py'` 与 `python3.11 -m unittest discover -s tests`
  （全套）均通过。测试计数以本次交付验证报告为准。

## 八、回滚

- 删除 RT-016 新增文件即可完全回退本 RT。
- 未修改 RT-011~015 任何冻结物；未接入真实 Broker / Scheduler /
  cron / Cloud / DocDB / installer；未启用真实租户。
- 已 durable 写入的 crosswalk / review / manifest 保留（属于
  RT-016 命名空间，可由 RT-025 清理）；RT-014 canonical 与
  RT-015 grant 也保留（属 RT-014 / RT-015 的不可回滚语义）。

## 九、v1.1 remediation（本次追加）

针对独立验收报告（commit `c6a740a`，Verdict PASS with 2 Minor + 2 Info）
在本次 remediation 关闭以下项，仅新增或更新 RT-016 所有权范围代码 /
测试 / 文档；未修改 RT-011~015 冻结物、未修改 legacy 数据面、未修改
`RT/index.yaml`。

- **Minor-1（read 层深度防御）已修复**：新增 `_load_crosswalk_payload`
  + `_validate_crosswalk_integrity` 内部函数，让 `read_crosswalk` /
  `iter_crosswalks` / `_find_existing_crosswalk_for_legacy` / crosswalk
  写入前的 CAS 冲突分支统一走同一整套完整性链路：schema →
  RT-011 tenant_view → 字节 JCS 复核 → **跨字段一致性绑定**（top-level
  `canonical_sha256` 等于嵌套 `publish_receipt / tenant_view_envelope /
  decompose_report` 的对应字段；`object_id / catalog_key /
  catalog_revision` 与 publish_receipt 对齐；`object_bytes_sha256 /
  legacy_source_sha256` 与 decompose_report 对齐；`view_key ==
  observe_grant_key == compute_grant_key(tenant, report_key)`；
  `crosswalk_key == compute_crosswalk_key(tenant, view_key,
  legacy_source_sha256)`；`report_key ==
  compose_report_key(source_namespace, report_id)`；tenant_view_envelope
  的 tenant_id/report_key 与顶层一致）。任一失败抛
  `LegacyImportError(code="corrupt")`。RT-016 v1 写入的所有正当
  crosswalk 均已满足这些绑定（schema 一直要求这些字段必填），故无
  “无可验证完整性绑定的旧记录”被静默接受。

- **Minor-2（跨 namespace 幂等碰撞）已修复**：
  - `compute_review_id(tenant_id, source_namespace, legacy_source_sha256,
    run_id)` 新增 `source_namespace` 参数，纳入域分隔哈希材料。
  - `review_entry.schema.json` 新增必填字段 `source_namespace`；
    `_write_review` 与 payload 一并存储 caller 声明的 namespace，并在
    读取既有 review 时交叉核验 tenant_id / source_namespace /
    legacy_source_sha256 / run_id / review_id 与调用参数完全一致。
  - `_find_existing_crosswalk_for_legacy(tenant_fd, tenant_id,
    source_namespace, legacy_source_sha256)` 的 filter 键新增
    source_namespace；不同 namespace 不会复用第一次的 crosswalk，
    同 namespace 仍幂等。
  - `_find_existing_review(tenant_fd, tenant_id, source_namespace,
    legacy_source_sha256, run_id)` 改为按期望 review_id 精确查找，
    并对读到的 payload 做绑定字段交叉核验。

- **Info-1（JSON 解析错误 taxonomy）已修复**：新增
  `_load_crosswalk_payload / _load_review_payload` helper，在
  `read_crosswalk`、`iter_crosswalks`、`iter_reviews`、`iter_manifest`、
  `_find_existing_crosswalk_for_legacy`、`_find_existing_review`、
  `_write_review` CAS 冲突分支等所有读路径统一把
  `json.JSONDecodeError` / `UnicodeDecodeError` / RT-011
  `ContractError`（duplicate JSON key）包装为
  `LegacyImportError(code="corrupt")`。上层依赖
  `except LegacyImportError` 的 caller 不再会看到 Python 内建异常泄漏。

- **Info-2（unknown_reply_keys 键名回显）**：验收报告标注为可保留信息级
  提示；本次 remediation 未修改（key names only，无 value 泄漏）。

新增/更新文件（严格限于 RT-016 所有权范围）：

- 修改：`scripts/cwk_legacy_raw_import.py`、
  `PR/PR-001-multitenant-knowledge-spaces/contracts/rt016/schemas/review_entry.schema.json`。
- 新增：`tests/test_rt016_remediation.py`（18 项定向 remediation 测试）。
- 更新：本 rt-intake、`reports/实现记录.md`、`reports/交付验证报告.md`。

**未修改**：RT-011~015 任何冻结模块 / schema / test；RT-016 v1 已有的
其他 schema / test / helper；`RT/index.yaml`（保留原 `implementation_done`
状态；`remediation_done` 由验收 Agent 复核后再决定是否升为 `completed`）；
legacy 数据面模块；PRD / DESIGN / 开发计划。

## 十、v2 anchor remediation（本次追加）

针对 acceptance 报告在 `a2789ef` 定位的协调篡改 Major 及关联问题，
本次追加“最小安全修复”（严格 RT-016 owned 范围，不改
RT-011~015 / legacy / `RT/index.yaml`）：

- **v2 identity（新）**：`(tenant_id, source_namespace, source_kind,
  legacy_path_hash, legacy_source_sha256)`；由独立 domain separator
  产生 v2 opaque key，与 v1 opaque 空间正交。同 raw 不同 namespace/
  path/kind 允许共享 RT-014 canonical，但绝不共用 crosswalk /
  review / manifest 记录。
- **v2 契约（新增 4 份 schema）**：`migration_crosswalk_v2` /
  `review_entry_v2` / `migration_manifest_entry_v2` /
  `reconciliation_report_v2`，均 `additionalProperties:false +
  unevaluatedProperties:false + deepForbiddenProperties`，`schema` /
  `identity_version` 双 discriminator。
- **Bound reader（新）**：`_load_crosswalk_payload_bound(raw, *,
  expect)` 是 finder / CAS conflict 的唯一入口；文件名、tenant
  父目录、v2 recomputed key、caller 声明的 namespace / source_kind /
  path_hash / raw_sha 必须完全绑定，否则 `LegacyImportError(code=
  corrupt)`。v1 payload 在 v2 slot 一律拒绝。
- **`ReconciliationAnchor`（新）**：`(tenant_id, source_namespace,
  source_kind, decomposer_version, normalizer_version)`。
  `reconcile(anchor=None)` 直接 `contract` 失败——绝不再退回信任
  crosswalk 自述。每条待验 v2 crosswalk：从 LegacySource 的 dirfd
  按 `legacy_path_hash` 只读定位 bytes；SHA-256 复核 raw_sha；用
  anchor 的 decomposer / normalizer 版本重新 `decompose`；重新计算
  canonical_sha / object_bytes_sha / report_key / view_key / v2
  crosswalk_key 与 crosswalk 逐字段比对；再用重新计算的
  `report_key + canonical_sha` 调 RT-014 `SharedEvidenceStore.
  read_version()`；返回的 canonical envelope 重新 JCS 序列化，与
  自己重新分解的 envelope JCS bytes 逐字节比对；任何 mismatch fail
  closed，绝不计入 `both_equal`。
- **v1 back-compat**：v1 crosswalk / review 仍通过 `read_crosswalk`
  / `iter_reviews` 可读（audit only）；新 importer 幂等 / reconciler
  PASS 一律不采信 v1 记录，v1 crosswalks 直接计入
  `unanchored_v1_count`，永远不 `both_equal`。
- **Manifest namespace/path coverage 修复**：v2 manifest 行必含
  `source_namespace + source_kind + legacy_path_hash`；去重键为
  `(source_namespace, source_kind, legacy_path_hash,
  legacy_source_sha256, outcome)`；同 run 中混入 v1 manifest 行直接
  抛 `corrupt`。

新增文件（严格 RT-016 owned）：

- `PR/PR-001-multitenant-knowledge-spaces/contracts/rt016/schemas/migration_crosswalk_v2.schema.json`
- `PR/PR-001-multitenant-knowledge-spaces/contracts/rt016/schemas/review_entry_v2.schema.json`
- `PR/PR-001-multitenant-knowledge-spaces/contracts/rt016/schemas/migration_manifest_entry_v2.schema.json`
- `PR/PR-001-multitenant-knowledge-spaces/contracts/rt016/schemas/reconciliation_report_v2.schema.json`
- `tests/test_rt016_anchor.py`（33 项定向黑盒测试）

修改文件（严格 RT-016 owned）：

- `scripts/cwk_legacy_raw_import.py`（v2 domain / helpers / bound
  reader / v2 write / v2 finder / v2 review / v2 manifest；v1 loader
  保留可读）
- `scripts/cwk_migration_reconciler.py`（整体重写为 anchor-bound v2）
- `tests/_rt016_helpers.py`（新增 `default_anchor()`）
- `tests/test_rt016_schemas.py`（`test_schemas_exist` 扩容为 v1+v2）
- `tests/test_rt016_reconciler.py`（迁移到 `anchor=` 签名）

**未修改**：RT-011~015 任何字节 / schema / test / 独立验收报告；
legacy 数据面 `cwk_raw_store.py` / `cwk_thread_timeline.py` /
`cwk_collect_live.py` / `cwk_nightly_pipeline.py` /
`cwk_wiki_query.py` / `cwk_entity_catalog.py` /
`cwk_wiki_search_index.py` / `cwk_docdb_cloud.py` /
`cwk_sync_mirror_to_docdb.py` / `cwk_tenant_cli.py` /
`cwk_tenant_cmd_binding.py`；`RT/index.yaml`；RT-016 v1 五份契约
schema 文件；PRD / DESIGN / 开发计划 / 独立验收报告本体。

**检测边界（明确不宣称项）**：v2 anchor remediation 的“检测协调篡改”
承诺只在 RT-016 registry 被攻击者控制 **但同时 LegacySource dirfd 只
读读取路径与 RT-014 SharedEvidenceStore 底层 shared/ 目录彼此独立
且未在同一时间窗口被同一攻击者篡改** 时成立。三方共谋不在本层保证
范围。本次追加**不宣称** G3 / VG-A / M3 完成、生产系统整体不可篡改、
真实 authority 从 `conservative_unknown` 状态改变、真实租户切换准备
就绪或 broker/router/加密备份/legacy vs new diff 已就绪。
