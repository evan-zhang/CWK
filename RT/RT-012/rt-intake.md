# RT-012：Instance Layout 与 Tenant Registry

> PR：PR-001（多租户共享证据与专题知识空间）
> 波次：M1（迁移阶段）/ G2（发布门禁）
> Owner：`agent-rt012-impl-opus`（本实现）+ `agent-rt012-verify`（独立验收）
> 依赖：RT-011（v1 契约与 `security_defaults.json` / `verified_shared_extensions_v1.json` 冻结）
> 基线提交：RT-011 独立 PASS r2（92cf40a / e3aa3e0）+ RT-010 hotfix 独立 PASS

## 背景

PR-001 §RT-012 要求在 RT-011 已冻结的契约与安全默认之上，提供**单机共享引擎**下的：

1. 强制显式 `CWK_INSTANCE_ROOT` 的实例根解析器，绝不回退仓库级 `runs/` / `state/` / `.env`；
2. dirfd + `O_NOFOLLOW` 的租户目录布局与 opaque `tenant_id` (`t_[a-z0-9]{26}`) 命名；
3. 唯一权威 Tenant record（`registry/tenants/<tenant_id>.json`）与只读投影
   （`config/tenant.projection.json`，附 `record_revision`/`record_sha256`）；
4. PRD FR-02 精确六态状态机（`draft` / `profile_pending` / `pilot` / `active` / `suspended`
   / `offboarded`）与操作权限矩阵；
5. 记录级 CAS：`record_revision` 与 `auth_epoch` 都以单调 CAS 递增；无直接 set；
6. 事务式 provisioning：`provision-journal` → 目录创建 → 记录 CAS → `provision-receipts`
   commit → 移除 journal；崩溃只能恢复为“完全未提交”或“完全 committed”；
7. 独占的 `cwk_tenant_cli` dispatcher，仅从受信 `scripts/` 绝对目录加载已声明 provider
   槽位，不扫描 CWD / PYTHONPATH / env；
8. 最小 doctor（layout / 权限 / 链接 / inode / registry-tenant 一致性 / 事务残留 / schema
   / revision / quota），绝不读凭据。

后续 RT（RT-013 绑定/凭据、RT-014 canonical store、RT-015 access ledger、
RT-017 collector、RT-018 scheduler、RT-019 profile、RT-020 router、
RT-021 projector、RT-022 broker、RT-023 沙箱、RT-024 audit、RT-025 backup、
RT-026 release）以本 RT 交付为地基；他们**只能新增各自 provider 模块**，
不得回改 dispatcher 或 registry。

## 目标

在**不写生产数据、不持有真实密钥、不改 cron/Cloud/DocDB/nightly** 且
**不修改 RT-011 冻结 schema/CLI/security_defaults** 的前提下：

1. 交付 `scripts/cwk_atomic_file.py`：同目录 `O_EXCL|O_NOFOLLOW` temp → 完整写
   （EINTR/short-write）→ file fsync → anchored rename → parent dir fsync；
   exclusive_lock（fcntl.flock，进程死亡即释放）；CAS（读回 sha256 + rewrite）；
   orphan recovery。**禁止直接复用** `cwk_raw_store.atomic_write` 或任何 legacy
   helper（缺 parent fsync / 缺 O_NOFOLLOW / 缺 anchored rename）。
2. 交付 `scripts/cwk_instance.py`：`InstanceLayout`、`TenantsRoot`、`TenantLayout`；
   opaque tenant/space ID 校验；dirfd + `O_DIRECTORY|O_NOFOLLOW`；
   `resolve_instance_root()` 拒绝：unset / empty / whitespace / NUL / CR / LF /
   相对 / backslash / UNC / 编码变体 / 符号链接 / 非目录 / 不存在。冻结 15 个
   tenant 子目录：`config access views routes knowledge-spaces indexes review
   archive state runs locks retries cache logs tmp`。
3. 交付 `scripts/cwk_tenant_registry.py`：opaque tenant ID 生成
   （`secrets` 26 chars `[a-z0-9]`）；`cwk.rt012.tenant_record.v1` schema 校验
   （复用 RT-011 Draft 2020-12 引擎）；六态 FSM 与操作矩阵；`record_revision`
   / `auth_epoch` 单调 CAS；两阶段 provision transaction（journal + receipt）；
   `credential_ref`/`active_profile_version` 只能 `null`，无 setter；
   quota `cwk.rt012.quota.unset.v1` 结构冻结、值全 null，测量属 RT-024；
   `recover()` 幂等，只回滚匹配 txn receipt 的**未提交且未被 RT-013+ 填充**
   的 staging 树。
4. 交付 `scripts/cwk_tenant_cli_api.py`：`CommandProviderV1`、`CommandSpec`、
   `CommandContext`、`DoctorFinding`、`CliError`、`STABLE_EXIT_CODES`
   （0/2/3/4/5/6）；`COMMAND_PROVIDER_API_VERSION = "v1"`。
5. 交付 `scripts/cwk_tenant_cli.py`：RT-012 独占 dispatcher；仅加载
   `FROZEN_PROVIDER_SLOTS` 中的模块，从 `Path(__file__).resolve().parent` 绝对
   路径读取 provider 文件；`.py` 若为 symlink 拒绝；ABI 校验失败/duplicate
   command name/broken import → EXIT_USAGE；不扫描 CWD/PYTHONPATH/env。
6. 交付 `scripts/cwk_tenant_cmd_core.py`：`init` / `show` / `list` / `doctor`
   / `state-graph`；`init` 只创建 `draft`，无任意状态跳转；doctor 输出
   `cwk.rt012.layout_doctor_report.v1`。
7. 交付 RT-012 自有 schema 族：
   `PR/PR-001-multitenant-knowledge-spaces/contracts/rt012/schemas/`
   下的 `tenant_record.schema.json`、`provision_receipt.schema.json`、
   `instance_layout.schema.json`、`layout_doctor_report.schema.json`、
   `command_spec.schema.json`。绝对不修改 RT-011 冻结的 14 张 schema、
   `security_defaults.json`、`verified_shared_extensions_v1.json`、
   `cwk_pr001_*`（contracts/probes/view_compare/cli）。
8. 交付独立测试族：`tests/test_rt012_atomic_file.py`、`test_rt012_instance.py`、
   `test_rt012_registry.py`、`test_rt012_cli.py`、`test_rt012_attacks.py`。
9. 交付 RT-012 标准包：`rt-intake.md`、`specs/需求契约.md`、`specs/技术方案.md`、
   `tasks/开发任务.md`、`reports/实现记录.md`、`reports/交付验证报告.md`；
   更新 `RT/index.yaml` 到 `implementation_done`，**不自写 completed/PASS**。

## 严格边界

- **不实现** RT-013+：Agent binding、credential/AppKey、ACL/grant/view、
  canonical store、migration、collector/scheduler/cron、Profile/router/
  projector、Query Broker/Tool/UDS、中央审计/容量、backup/recovery、
  release/enable。目录**可预建空 dir**（`shared/ registry/ tenants/ audit/
  backups/ runtime/ staging/` 与 15 个 tenant 子目录），但不写任何 RT-013+
  数据面语义。
- **禁止修改**：`cwk_pr001_contracts.py`、`cwk_pr001_probes.py`、
  `cwk_pr001_view_compare.py`、`cwk_pr001_cli.py`、
  `contracts/schemas/*.schema.json`、`contracts/security_defaults.json`、
  `contracts/verified_shared_extensions_v1.json`、`contracts/adr/*`；
- **禁止修改**：`cwk_collect_live.py`、`cwk_nightly_pipeline.py`、
  `cwk_wiki_query.py`、`cwk_entity_catalog.py`、`cwk_wiki_search_index.py`、
  `cwk_sync_mirror_to_docdb.py`、`Makefile`、`install.sh`、`.env.example`、
  `.serena/`、`.spec-workflow/`、`config/`、任何 legacy nightly 或 Cloud/DocDB
  代码；
- **禁止读取** 真实 `CWORK_APP_KEY`；不发起真实 CWork 请求；不接入
  Cloud/DocDB/生产镜像；不引入第三方 pip 依赖（stdlib only）；
- **禁止** 允许 `enabled/disabled/provisioning/retiring` 状态别名；
- **禁止** enable/disable/release 命令（属 RT-026）；
- **禁止** 通用 `delete`/`purge` 或 recursive glob；只允许基于 txn receipt 精确
  比对的 staging 清理，且必须在整个租户树未被 RT-013+ 填充时才回收。

## 判定：仍待外部/后续 RT 决定的事实

以下事实继续由 RT-011 锁定为 `conservative_unknown`，RT-012 不解锁：

- `report_id_global_uniqueness`；
- `permission_authoritative_events` / `permission_authoritative_api`；
- `trusted_agent_identity_openclaw_tool` / `trusted_agent_identity_uds_peercred`；
- `sandbox_transport_openclaw_tool` / `sandbox_transport_uds`；
- `verified_shared_extensions_dual_user_sample`（<50 共同可见样本）；
- `sandbox_transport_loopback_http_self_reported`（政策永禁）。

RT-012 也把以下能力**明确留给后续 RT**，不在本 RT 内提供：

- 凭据引用、Broker、AppKey 边界 → RT-013；
- Canonical / access ledger / view → RT-014 / RT-015；
- 采集 / 调度 → RT-017 / RT-018；
- Profile / 路由 / 空间 → RT-019 / RT-020 / RT-021；
- Query Broker / 沙箱 → RT-022 / RT-023；
- 中央审计 / 备份 → RT-024 / RT-025；
- Release / 试点 → RT-026。
