# RT-013：可信 Agent 绑定与凭据边界

> PR：PR-001（多租户共享证据与专题知识空间）
> 波次：M1（迁移阶段）/ G2（发布门禁）
> Owner：`agent-rt013-impl-opus`（本实现）+ `agent-rt013-verify`（独立验收）
> 依赖：RT-011 冻结契约（v1 schema、`security_defaults.json`、`ADR-0001`），RT-012 Instance Layout + Tenant Registry + Provider Registry
> 基线提交：RT-011 PASS r2、RT-012 PASS

## 背景

RT-011 冻结了 canonical / view / grant / query / probe 等 v1 schema 与 conservative
未知事实清单；RT-012 建立了 opaque `tenant_id`、tenant 六态 FSM、tenant record
CAS + provisioning receipt、`registry/agent-bindings/` 与 `registry/credentials/`
两个空目录以及冻结的 `cwk_tenant_cli` dispatcher。RT-013 的任务是把「Agent →
tenant」绑定与「tenant → AppKey」凭据边界锁死在宿主机，让后续的采集、Broker、
沙箱链具备可靠的身份 + 凭据入口。

需求原文：

- PRD FR-03 可信 Agent—tenant 绑定；`agent_id_hash = HMAC(secret, agent_id)`；
  一 Agent 一 tenant；绑定变更递增 `auth_epoch`；
- PRD FR-05 宿主机密钥引用；租户配置只保存 secret reference；不进入 Git、
  Prompt、Agent workspace、沙箱、普通日志或错误栈；支持轮换、禁用、健康检查；
- PRD FR-17 查询入口使用可信运行上下文（不接受请求体 `agent_id`、`credential_ref`、
  `tenant_id`）；本 RT 提供 `AgentContext` 适配器，只承载最低身份解析；
- PRD FR-02 状态权限：`draft/suspended/offboarded` 拒绝凭据解析；`profile_pending`
  仅允许有界样本采集；`pilot/active` 允许正常数据面；
- DESIGN §C-03 Agent Binding Registry；§C-04 Credential Broker；§C-13 Query
  Broker 消费 `AgentContext`；§9 缓存键含 `binding_epoch`；§10 撤权路径同步；
- 威胁模型 T-01 身份伪造、T-07 AppKey 泄漏、T-10 撤权窗口、T-11 并发。

## 目标

在**不修改** RT-011 冻结契约、RT-012 dispatcher/registry 内部逻辑、legacy
collector/nightly/query、cron 与 Cloud/DocDB 的前提下：

1. 交付 `scripts/cwk_agent_binding.py`：Binding Registry — opaque
   `agent_id_hash = HMAC-SHA256(binding_secret, raw_agent_id)`；`bind` / `rebind` /
   `revoke` / `suspend` / `reactivate` 均通过 CAS 更新 `binding_epoch`；每一次
   变更立即 `bump_auth_epoch(tenant_id)`；写审计 receipt 到
   `registry/agent-bindings/receipts/`；绑定记录本身位于
   `registry/agent-bindings/current/<agent_id_hash>.json`。
2. 交付 `scripts/cwk_credential_broker.py`：Credential Broker — 每租户 opaque
   `secret://<opaque>` reference 存放于 `registry/credentials/<tenant_id>.json`；
   Broker 只把 material 通过最小 env 白名单注入下游子进程；material 从可
   插拔 backend 读取（`env_ref` / `file_ref`），永不写入 argv / 配置 / 日志 /
   audit / receipt / 异常消息。禁用状态或 tenant 非 pilot/active 立即拒绝。
   支持双写引用、原子 pointer 切换、旧引用墓碑的轮换。
3. 交付 `scripts/cwk_agent_context.py`：`AgentContext` 只由信任来源构造
   （构造函数标记 `trusted_source="gateway_authenticated_context" |
   "admin_cli"`）；不接受来自请求体、CLI query 字段、日志文本的 agent_id；
   所有查询/采集入口把 raw agent_id 立即 HMAC，只保留 hash + tenant_id +
   binding_epoch + tenant_auth_epoch 的 snapshot。
4. 交付 `scripts/cwk_tenant_cmd_binding.py` provider（`API_VERSION="v1"`，
   `PROVIDER_NAME="cwk_tenant_cmd_binding"`）：`bind-agent` / `rebind-agent` /
   `revoke-agent` / `suspend-agent` / `reactivate-agent` / `list-bindings` /
   `show-binding` / `set-credential` / `disable-credential` / `rotate-credential` /
   `rotate-binding-secret` / `doctor:binding`（供 dispatcher aggregate）。
5. 交付 RT-013 自有 schema：`agent_binding.schema.json`、
   `credential_ref.schema.json`、`binding_secret_pointer.schema.json`、
   `binding_receipt.schema.json`、`credential_broker_lease.schema.json`；
   `unevaluatedProperties:false + additionalProperties:false` + `enum` +
   `pattern` + 忌讳字段拒绝。
6. 交付独立测试族：`test_rt013_binding.py`、`test_rt013_credential.py`、
   `test_rt013_agent_context.py`、`test_rt013_cli.py`、`test_rt013_attacks.py`、
   `test_rt013_rotation.py`。所有测试使用 `mktemp -d` 的合成 instance root、
   fake secret backend、绝不读取真实 `CWORK_APP_KEY`。
7. 交付 RT-013 标准包：`rt-intake.md`、`specs/需求契约.md`、`specs/技术方案.md`、
   `tasks/开发任务.md`、`reports/实现记录.md`、`reports/交付验证报告.md`；
   更新 `RT/index.yaml` 为 `implementation_done`，**不自写 PASS/completed**。

## 严格边界

- **禁止修改**：RT-011 的 14 张 v1 schema、`security_defaults.json`、
  `verified_shared_extensions_v1.json`、`ADR-0001`、`scripts/cwk_pr001_*.py`；
  RT-012 的 `cwk_atomic_file.py`、`cwk_instance.py`、`cwk_tenant_registry.py`、
  `cwk_tenant_cli.py` 除 `FROZEN_PROVIDER_SLOTS` **一行** 追加以外的任何逻辑、
  `cwk_tenant_cli_api.py`、`cwk_tenant_cmd_core.py`、
  `PR/PR-001-multitenant-knowledge-spaces/contracts/rt012/schemas/*`。
- **禁止修改**：`cwk_collect_live.py`、`cwk_nightly_pipeline.py`、`cwk_wiki_query.py`、
  `cwk_entity_catalog.py`、`cwk_wiki_search_index.py`、`cwk_sync_mirror_to_docdb.py`、
  `Makefile`、`install.sh`、`.env.example`、`config/`、`.serena/`、`.spec-workflow/`、
  任何 legacy nightly / Cloud / DocDB 代码 / cron。
- **禁止**：读取真实 `CWORK_APP_KEY`；发起真实工作协同请求；接入 Cloud/DocDB/
  生产镜像；引入第三方 pip 依赖（stdlib only）。
- **禁止**：为 tenant 提供 `enable-tenant/disable-tenant/release-tenant` — 属
  RT-026；本 RT 只做绑定与凭据面。
- **禁止**：把 credential material 通过 argv、config、log、receipt、audit、
  manifest、异常消息、Prompt、沙箱环境或 Git 泄漏。
- **禁止**：允许沙箱查询请求体、CLI 用户输入或环境变量声明 `agent_id` / `tenant_id` /
  `credential_ref` / `binding_epoch` / `auth_epoch` / `mirror_root`。
- **禁止**：把「未验证真实 Gateway 身份/传输」宣称为 `verified`；`SG-02/G5/G7`
  只由 RT-023 / VG-D / G5 关闭；本 RT 保留 `conservative_unknown`。
- **禁止**：在 dispatcher 里加载 `PYTHONPATH` / `CWD` / env 声明的额外
  provider；`FROZEN_PROVIDER_SLOTS` 只增加 `cwk_tenant_cmd_binding` 一行，
  其他任何 dispatcher 逻辑不动（若确需扩展则在报告里明确阻塞）。

## 判定：仍待外部/后续 RT 决定的事实

以下事实继续锁定为 `conservative_unknown`，RT-013 不解锁：

- `trusted_agent_identity_openclaw_tool` / `trusted_agent_identity_uds_peercred`
  （真实运行时合规由 RT-023、VG-D、G5 验收）；
- `sandbox_transport_openclaw_tool` / `sandbox_transport_uds`；
- `sandbox_transport_loopback_http_self_reported`（政策永禁）；
- `permission_authoritative_events` / `permission_authoritative_api`；
- `report_id_global_uniqueness`；
- `verified_shared_extensions_dual_user_sample`。

RT-013 只把「一 agent 一 tenant」「credential 只出现在 broker 边界」「revoke 立
即拒绝」「secret rotation 双写 + 原子 pointer + 墓碑」写死；不承诺 OpenClaw
Tool / UDS 的运行时通道，也不模拟真实 Gateway 身份。

## 与后续 RT 的接口

- RT-015：AccessLedger 复核 auth_epoch 时可复用本 RT 的
  `AgentContext.snapshot()` 数据结构（tenant_id + auth_epoch + binding_epoch）。
- RT-017：Collector Worker 通过 `CredentialBroker.lease(purpose="collector_run")`
  获取一次性 env；lease 结束即销毁 material。
- RT-022：Query Broker 消费 `AgentContext.from_trusted(...)`；请求体永不可提
  agent_id/tenant_id。
- RT-023：真实沙箱 transport 完成后，把 `TRUSTED_AGENT_SOURCES` 注入本 RT 的
  白名单，不改本 RT 已冻结的解析函数。
- RT-024：审计接口消费 `binding_receipt.v1` / `credential_broker_lease.v1`；
  永不出现 material。
- RT-025：备份只保存 credential reference（`secret://<opaque>`），不备份
  material；恢复后必须重新绑定 secret backend。
- RT-026：`bind-agent` / `rotate-credential` 等 provider 命令由 release runbook
  调用；本 RT 不新增 tenant 状态跃迁 API。
