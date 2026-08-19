# RT-011：外部契约探针与安全基线

> PR：PR-001（多租户共享证据与专题知识空间）
> 波次：M0（迁移阶段）/ G1（发布门禁）
> Owner：`agent-rt011-impl`（本实现）+ `agent-rt011-verify`（独立验收）
> 依赖：无（本 RT 是 PR-001 首个 RT）

## 背景

PR-001 将 CWK 从单用户 Local-First 改造为 Gateway 级多租户。为避免后续 15 个 RT 各自解读契约、伪造外部事实、
在真实密钥或生产数据上试错，PR-001 计划把所有 v1 契约、安全默认与外部事实探针
**全部前置到 RT-011**：先冻结再实现。

## 目标

在**不写生产数据、不持有真实密钥、不改 cron/Cloud/DocDB/nightly** 的前提下：

1. 冻结 PR-001 v1 数据契约的机器可读 schema 与校验器，覆盖
   `ReportKey`、`CanonicalEnvelope`、`TenantViewEnvelope`、`AccessObservation`、
   `AccessGrant`（七状态）、`KnowledgeProfile`（六状态）、`profile_pointer_rollback` 事件、
   `RouteDecision`（`space_ids[]` opaque）、`QueryRequest`（`space_selector[]` opaque）、
   `sample_manifest_v1`、`verified_shared_extensions_vN`、能力探针输出、安全默认；
2. 冻结字节契约：`object_id = "o_" + base32(random 128 bit)`、字符串先 NFC 再 RFC 8785 JCS / UTF-8、
   带 domain separator 的 `profile_sha256` 公式（含 `sample_manifest_sha256` / `prompt_template_sha256` / `model_id`）、
   `ReportKey` 默认 `source_namespace + report_id`；
3. 交付只读外部能力探针骨架，仅允许 `verified | conservative_unknown`；无真实样本时必须
   `conservative_unknown`，不能伪造 PASS，且 fixture/mock 证据永远无法升级；
4. 交付双用户脱敏字段比较器（`cwk contract compare-user-views`）：不接收/保存 AppKey；
   输出共同可见集合、逐字段一致率、reply/node/attachment/临时 URL 差异，以及 shared/overlay 升级建议；
5. 冻结双租户恶意 fixtures 与负向测试：跨 namespace、未知字段、附件/回复/审批差异、
   路径/tenant/agent 注入、schema 漂移、伪造能力探针；fixture 包附 manifest+SHA+canary；
6. 冻结 machine-readable `security_defaults.json` + 身份/传输 ADR（OpenClaw 受控 Tool 优先、
   UDS peer credential 后备、loopback HTTP + 自报 `agent_id` 禁止）；本 RT 只落地政策 / 能力
   探针，真实 transport 由 RT-023 / VG-D / G5 验证。

## 严格边界

- 不实现 RT-012+ 的 tenant runtime、registry、credential broker、canonical store、ACL、
  collector、scheduler、router、projector、query broker；
- 不读取 `CWORK_APP_KEY`，不发起真实 CWork 请求；
- 不改 cron、Cloud、DocDB、生产镜像、nightly 或现有业务数据；
- 不触碰 `.serena/`、`.spec-workflow/`；
- 只新增本 RT 所需文件；如需改变 G0 已冻结语义必须停止并报告，不得自行改 PRD/DESIGN；
- 使用 apply_patch / Edit / Write 编辑；保持现有用户改动。

## 判定：外部未知的默认口径

RT-011 完成时以下事实全部属于 `conservative_unknown`，等待 RT-023 / VG-D / G5 用真实运行时验收升级：

- `report_id_global_uniqueness`；
- `permission_authoritative_events` / `permission_authoritative_api`；
- `trusted_agent_identity_openclaw_tool` / `trusted_agent_identity_uds_peercred`；
- `sandbox_transport_openclaw_tool` / `sandbox_transport_uds`；
- `verified_shared_extensions_dual_user_sample`（<50 篇共同可见样本）；
- `sandbox_transport_loopback_http_self_reported` — 政策禁止，**永远** 不允许升级为 verified。

对应的保守默认全部写进 `PR/PR-001-multitenant-knowledge-spaces/contracts/security_defaults.json` 并有测试锚定。
