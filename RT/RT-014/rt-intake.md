# RT-014 rt-intake：共享不可变 Canonical Evidence Store

- 状态：implementation_done (由 RT-014 独立实现 Agent 交付；等待新独立验收 Agent 黑盒验收)
- Profile：Spec-Standard
- 依赖：
  - RT-011（`CanonicalEnvelope v1` schema、`canonical_json_bytes`、`canonical_sha256`、`compose_report_key`、
    `new_object_id`/`OBJECT_ID_REGEX`、`SHA256_HEX_REGEX`、`SOURCE_NAMESPACE_REGEX`、`REPORT_ID_REGEX`、
    `strict_json_loads`、`ContractError`）
  - RT-012（`InstanceLayout.child_fd("shared")`、`cwk_atomic_file` 的 dirfd + `write_atomic` /
    `cas_write` / `exclusive_lock` / `open_dir_nofollow` / `mkdir_at` / `read_file` /
    `recover_orphans` / `child_exists`）
- 与 RT-013 关系：并行开发。不共享任何模块或状态。

## 一、目标（严格范围）

1. 在宿主机内部实现一份共享、不可变的 Canonical Evidence Store：
   - 输入：RT-011 冻结的 `cwk.canonical_report.v1` envelope。
   - 输出：不可变 opaque 对象存储（sharded objects/）、每 `report_key` 的追加式 catalog、
     per-report 细粒度锁 + CAS、staging 恢复入口、校验式 reader。
2. 提供最小公开库 API：
   - `SharedEvidenceStore.open(layout)`
   - `.initialize()`
   - `.publish(envelope) -> PublishReceipt`
   - `.read_version(report_key, canonical_sha256) -> dict`
   - `.recover() -> RecoveryReport`
3. 严格不提供任何“枚举/列表/存在性探测”接口，任何 CLI/HTTP 也一律不新增。
4. Store 内所有路径段都必须是 opaque ID；`report_id` 通过带 domain-separator 的 SHA-256 映射到
   `catalog_key = r_<base32(16 bytes)>`，源 `report_key` 只保存在受校验 catalog 内。

## 二、非目标

- 不解析源端凭据，不与 CWork API 通信，不读 `CWORK_APP_KEY`，不访问 tenant workspace / DocDB / Cloud。
- 不新增 CLI/HTTP，不新增 tenant runtime；不修改 `cwk_tenant_cli.py` dispatcher。
- 不实现 tenant view、access grant、路由、投影、Query Broker、Scheduler。
- 不实现枚举 object/catalog/report 的接口；不暴露 SHA-existence 探测。
- 不实现 legacy 迁移；不复用 `cwk_raw_store` 的写路径。
- 不自标 PASS；仅标 implementation_done，由独立验收 Agent 黑盒验收。

## 三、公开面（冻结签名，供 RT-015/RT-016/RT-017 消费）

Python 模块：`scripts/cwk_shared_evidence.py`

- `class SharedEvidenceStore`
  - `@classmethod open(layout: InstanceLayout) -> SharedEvidenceStore`
  - `initialize() -> None`  幂等地创建 `shared/objects/`、`shared/report-versions/`、
    `shared/staging/`、`shared/locks/` 四个 0o700 子目录。
  - `publish(envelope: dict) -> PublishReceipt`  幂等发布单个 canonical envelope。
    envelope 必须已由调用方序列化为 dict；RT-014 会用 RT-011 `validate_canonical_envelope`
    再次严格校验（含 canonical_sha256 recompute 与 30+ forbidden 字段深扫）。
  - `read_version(report_key: str, canonical_sha256: str) -> dict`  校验式读回。
    校验对象 SHA、JCS 往返、schema、catalog 关联；任一失败抛 `SharedEvidenceError`
    并且不泄露路径/字节。
  - `recover() -> RecoveryReport`  幂等清理 staging + per-report `.cwk-tmp-*`，
    并对 catalog↔object 关联做只读一致性核验（catalog 指向不存在的 object 或
    hash 不符时报 issue，但绝不删除或改写 catalog/object）。

- `@dataclass class PublishReceipt`
  - `report_key: str`
  - `canonical_sha256: str`
  - `object_id: str`
  - `catalog_key: str`
  - `is_new_version: bool`   True = 新 version_key 首次落盘；False = 幂等复用现有 object
  - `is_new_report: bool`    True = 该 report_key 首次出现
  - `catalog_revision: int`  该 catalog 已落盘 entry 总数（包含本次）
  - `catalog_head_sha256: str`  更新后的 head sha256

- `@dataclass class RecoveryReport`
  - `staging_orphans_removed: list[str]`
  - `catalog_dirs_scanned: int`
  - `catalog_issues: list[dict]`   每项含 `code` + 有限 opaque 引用，无路径/正文
  - `objects_verified: int`

- `class SharedEvidenceError(Exception)` with stable `code` in
  `{"contract", "not_initialized", "not_found", "sha_mismatch", "canonical_drift",
    "catalog_conflict", "corrupt_catalog", "orphan_object", "report_key_mismatch"}`。

## 四、目录布局（RT-014 独占子结构）

```
CWK_INSTANCE_ROOT/shared/
├── objects/
│   └── <2-char shard>/
│       └── <object_id>.json         # 不可变，NFC+JCS+UTF-8 字节
├── report-versions/
│   └── <catalog_key>/               # 完全 opaque，无 report_id 出现
│       ├── catalog.jsonl            # 每行一个 cwk.report_version.v1
│       └── catalog.head             # cwk.rt014.catalog_head.v1（CAS 主键）
├── staging/                         # cwk_atomic_file 的 .cwk-tmp-* 落地
└── locks/
    └── <catalog_key>.lock           # fcntl.flock per-report
```

- `catalog_key = "r_" + base32(sha256(b"cwk-rt014-report-key-v1\x00" + report_key.utf8)[:16])`；
  低位不为零的 base32 尾字符被拒绝（复用 RT-011 base32 尾校验规则）。
- object 分片：`object_id[2:4]`，2 字符 base32；所有 shard 目录 0o700，按需 mkdir。

## 五、语义

- 幂等：同一 `(report_key, canonical_sha256)` 无论调用多少次都返回同一 `object_id` +
  `is_new_version=False`；不会产生第二个 object 或第二条 catalog entry。
- 版本追加：同一 `report_key` 下不同 `canonical_sha256` 产生新 `object_id` +
  新 catalog entry；旧对象永远保留。
- report 身份互斥：`(source_namespace_A, id)` 与 `(source_namespace_B, id)` 映射到不同
  `catalog_key`，即使正文相同也不合并；跨 namespace 的相同 id 不共享 object。
- 顺序不变式：`validate` → `staging tmp write` → per-report `flock` → `write_atomic(exclusive)` 发布对象 →
  `cas_write` catalog.jsonl（整文件重写）+ `cas_write` catalog.head。
- catalog 失败：object 保留为孤儿，可被 `recover()` 记录为 `catalog_issues`；但 catalog
  绝不指向不存在或哈希不符的 object。
- reader：读回时逐层校验对象字节 SHA、JCS 往返、schema、`report_key` 关联、catalog 匹配；
  任一失败一律 fail closed 并抛 `SharedEvidenceError`。
- rollback 语义：本 RT 无 rollback API；对象一旦落盘绝不被本模块删除或改写；catalog 只
  追加。上层若要“回退版本”，仅可通过独立 pointer/审计事件，且必须由上层实现，绝不
  由本 RT 提供接口。

## 六、必测（黑盒/负面）

1. A/B 同版本幂等（同 report_key + 相同 canonical 正文，两次 publish 得到同一 object_id、
   catalog 只增长 1）。
2. 同 report 新版本追加（不同 canonical 正文 → 新 object_id + 新 catalog entry，旧对象
   仍可读）。
3. namespace/report 绝缘（相同 report_id 不同 namespace，或相同 namespace 不同 report_id
   即使正文相同，也生成不同 catalog_key、object_id）。
4. 深层 tenant/lane/reply/attachment/临时 URL/credential 注入被 RT-011 forbidden 深扫拒绝。
5. 伪造 `verified_shared_extensions_ref`（非法 version/sha 或引用不存在的 manifest）被拒。
6. 并发多线程 publish：同 `(report_key, sha)` 与不同 sha 混合，最终 catalog 与 object
   完全一致，无重复 entry、无孤儿；CAS 冲突路径能观察到并自动重试或返回稳定错误。
7. Fault injection：
   - 对象写入成功后 catalog 写入前抛异常 → 下次 `recover()` 报告孤儿 object；下一次
     publish 相同 sha 时会重新写 object（因为 catalog 不识别旧孤儿）——两个对象都保留，
     catalog 只引用第二个；reader 仍可校验成功。
   - 临时文件残留（模拟崩溃）→ `recover()` 通过 `recover_orphans` 清理，正式对象不受影响。
8. 篡改检测：
   - 对象字节被翻转一位 → reader `sha_mismatch`。
   - 对象 JSON 被替换成语义等价但键序改变的非 JCS 字节 → reader `canonical_drift`。
   - catalog entry 中 `object_id` 被换成另一个已存在对象 → reader `report_key_mismatch`。
   - catalog.head sha256 与 catalog.jsonl 内容不符 → reader `corrupt_catalog`。
9. Symlink / hardlink / TOCTOU：
   - 把 object 文件替换为指向别处的 symlink → `open_dir_nofollow` / `O_NOFOLLOW` 拒读。
   - 把 object 文件复制为 hardlink（nlink>1）→ `read_file` 拒读。
   - staging 目录内被预填 attacker 名字的临时文件不影响 write_atomic（temp name 是
     `secrets.token_hex`）。
10. Rollback 语义：任何 `SharedEvidenceStore` 实例都无 `rollback`/`delete_object`/
    `truncate_catalog` 方法；反射断言这些 attribute 不存在。
11. 错误 opacity：所有 error 消息不含 `CWK_INSTANCE_ROOT` 绝对路径、object 字节、object
    是否存在的细节；用 `str(exc)` 断言只出现稳定 `code` 与最小 opaque 描述。
12. 全套 RT-011/012/013 回归 + 全 tests 通过。

## 七、回滚

- 删除 `scripts/cwk_shared_evidence.py`、`PR/PR-001-multitenant-knowledge-spaces/contracts/rt014/`、
  `tests/test_rt014_*.py`、`RT/RT-014/` 即可；
- 未修改 RT-011/012/013 任何文件；`git diff --check` 干净；
- 未新增 cron、feature flag、生产环境变量或密钥；
- 已发布 shared object 不会因回滚被删除（本 RT 从不 unlink 对象）。

## 八、明确不做（防越权）

- 不解析或访问 tenant view、access grant、tenant registry、agent binding、credential
  reference、CWork API、DocDB、Cloud；这些属于 RT-015/RT-017/RT-022+ 的边界。
- 不写 audit 日志到 `audit/`（RT-024 负责）；本 RT 仅通过异常和 `RecoveryReport` 结构化
  数据向上层反馈。
- 不注册 tenant CLI provider；不向 dispatcher 加行；不新增 config 文件。
- 不接触 `cwk_raw_store.py` / `cwk_thread_timeline.py` / `cwk_collection_state.py` /
  `cwk_collect_live.py` / `cwk_nightly_pipeline.py` / `cwk_sync_mirror_to_docdb.py`。
