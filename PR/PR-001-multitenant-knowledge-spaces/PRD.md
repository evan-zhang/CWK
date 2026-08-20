# PR-001：CWK 多租户共享证据与专题知识空间需求文档

> 状态：RT-011～RT-016 completed；Wave-1 按剩余工作执行冻结进入 **RT-017 与 RT-022
> 两条并行链**。RT-019 是 RT-017 的下游，必须等 RT-017 的完整 Stage-01
> AccessLedger compatibility surface（authority injection、cleanup v2、Profile
> eligibility、CanonicalVersionProvider 与 PilotAdmission）
> 独立验收 PASS 后才能开工，不是第三条独立分支。
> 门禁状态以 `contracts/gates/gate_registry_v1.json` 的 `receipt_path` 机读判定：
> VG-A 已有机器 receipt（synthetic PASS / `conservative_unknown`），VG-B～VG-E 尚无
> receipt 文件即 NOT RUN。Wave-0 只完成了**只读可行性 receipt**，未证明任何外部能力。
> VG-A 的 synthetic 结论**永不升级、永不重跑、永不重解释**；它的能力缺口只能由
> RT-017（`cwork-authority-source`）与 RT-023（`gateway-identity-transport`）两份
> capability activation receipt 闭合（`contracts/gates/synthetic_closure_map_v1.json`）。
> 两份 receipt 当前均不存在，故缺口 OPEN、go/no-go 今天为 NO-GO。
> activation receipt 由 RT-017 的 **A-07** 与 RT-023 的 **T023-13** 在各自 RT 独立验收
> PASS 之后签发，有冻结的有界 TTL（authority 90 天 / gateway-identity 30 天）并按
> append-only 归档链续签，绝不原地覆盖。
> Security Gate SG-00～SG-10 的机器权威在 `contracts/security/`（registry + registry
> schema + 单一 receipt schema，说明见该目录 README）：十份
> `security-receipts/RT-0{17..26}/receipt.json` 当前**全部不存在即 NOT RUN**；
> 中央计划 §5.1 的矩阵是派生文档，冲突以 registry 为准。
> 版本：0.1.0
> 日期：2026-08-19
> Owner：CWK 项目
> 目标阶段：受控多租户试点，不直接全员生产
> 需求来源：`references/讨论决策记录.md`

## 1. 摘要

CWK 当前是一套单用户 Local-First 工作协同知识系统。它已经能够采集用户有权访问的工作协同汇报，保存不可变原文及评论/审批时间线，生成 Wiki，并通过自然语言查询回读原文证据。

本 PR 将 CWK 改造成“一次安装、多租户使用”的 Gateway 级能力：每位用户使用本人的工作协同身份，由宿主机独立采集；相同汇报版本的原始证据只保存一份；每个用户拥有独立的访问关系、知识方案、专题空间、索引和查询权限；普通 Agent 在沙箱中只读查询本租户数据。

本 PR 同时引入用户与 AI 共创的 `knowledge-profile`。AI 先分层读取 100–200 篇历史汇报，提出实体颗粒度、专题空间和低价值路由方案；用户确认后再全量处理。TBS、SFE 等专项作为逻辑独立知识空间，不复制 raw；报销、常规付款等低价值内容进入冷归档，不参与精编和持续跟踪，但默认不物理删除。

## 2. 问题陈述

### P-01：现有部署无法安全支撑一台 Gateway 上的多用户

当前 `.env`、镜像、`runs`、采集状态、锁、日志、重试队列和 DocDB 映射均是单实例。简单地让多个 Agent 指向同一项目，会导致密钥、状态、权限和知识视图串扰。

### P-02：为每位用户复制 raw 会造成重复存储和管理成本

同一篇汇报可能被大量用户共同看到。若每位用户保存一份完整 raw，1000 名用户看到同一篇汇报就产生 1000 份正文副本。正文版本本身应去重，用户差异应表达为访问关系和视角覆盖。

### P-03：统一、全自动的实体抽取不适合所有用户

不同用户关注对象和问题不同。抽取过细会制造大量噪声、歧义和治理成本；抽取过粗又会影响查询。系统需要统一事实契约，但允许用户确认实体颗粒度、别名、专题和路由规则。

### P-04：全量知识化浪费模型、存储和注意力

报销、常规付款等低价值工作协同若与 TBS、SFE 等重点项目同等精编、索引和展示，会稀释知识空间并增加成本。

### P-05：多个“独立知识库”如果复制事实会出现冲突

TBS、SFE 等专题需要独立视图和索引，但不能各自复制和改写原文。需要“一份事实、多份受控投影”的知识空间模型。

## 3. 产品目标

### O-01：一次安装，多租户运行

同一 Gateway 机器只维护一套 CWK 引擎。新增用户通过创建 tenant、绑定 Agent、配置密钥引用和知识方案完成启用。

### O-02：共享证据去重，权限严格隔离

同一来源、同一汇报、同一版本正文只物理保存一次；所有查询必须经过可信 Agent→tenant 绑定和租户 ACL，跨租户泄漏为零。

### O-03：让用户可靠查询本人的工作协同

用户可以对本人有权访问的汇报进行自然语言提问；关键结论必须回链并验证 raw；未知、歧义、无权限或无证据问题安全拒答。

### O-04：用户与 AI 共创知识方案

新用户不需要逐个配置实体。AI 基于分层样本提出方案，用户只确认高影响规则，形成版本化、可回滚的知识画像。

### O-05：一个用户多个专题知识空间

支持 TBS、SFE、云端虾等多个逻辑空间；同一汇报可进入多个空间；各空间拥有独立索引和派生视图，但共享同一事实证据。

### O-06：低价值内容可安全退出知识链路

低价值内容可进入冷归档，不精编、不索引、不跟踪；发生正文、评论、审批或状态变化后重新路由；默认不自动物理删除。

### O-07：可试点、可观测、可回滚

在不改变现有生产 nightly 的前提下完成影子迁移和受控试点。任何租户故障不影响其他租户，所有关键状态可审计、可恢复。

## 4. 非目标

- 第一阶段不全员开放。
- 不在每个沙箱安装完整采集系统。
- 不给每个租户复制完整 raw。
- 不允许用户传入或覆盖 `tenant_id`、共享 raw 路径或其他租户 mirror。
- 不把 workspace 当作安全隔离。
- 不启用 Cloud-First，不依赖云端 raw 完成首期查询。
- 不引入向量数据库作为首期前置依赖。
- 不允许用户修改基础事实 Schema 或原文。
- 不未经用户确认自动合并、拆分高影响实体。
- 不由分类器自动物理删除低价值汇报。
- 不让 Agent 自动回复、审批、标记已读或修改工作协同状态。
- 首期不提供“break-glass 全租户读取”或任何跨租户临时授权通道；管理员诊断只能通过审计日志和不含正文的健康指标进行。任何未来 break-glass 都必须是限时、双人审批、全审计的独立 PR，不复用本 PR 的查询入口。
- 首期不承诺具体 P50/P95、RTO/RPO 和每租户资源配额数值。相关指标一律以“安全默认 + 后续基准替换”表述：设计阶段只固定行为（fail closed、限流、错峰、单写者）；RT-011 仅验证外部契约，P50/P95、容量与配额由 RT-024 测量，RTO/RPO 由 RT-025 clean-room 恢复测量，RT-026 只消费两类基线做 go/no-go。
- 本 PR 的代码开发不等于生产部署；真实密钥迁移、cron 切换和试点启用需要独立授权。

## 5. 用户和角色

### R-01：普通用户

拥有一个绑定的 Agent 和工作协同身份；确认知识方案；查询本人内容；管理自己的专题、关注和冷归档规则。

### R-02：租户 Agent

在沙箱中响应用户问题；不持有 AppKey；只能通过受控查询接口访问固定 tenant；不能绕过 ACL。

### R-03：宿主机采集 Worker

使用租户密钥引用读取工作协同；维护访问关系和租户视角；写入共享证据和租户运行状态；不修改工作协同。

### R-04：平台管理员

安装和升级 CWK；创建/禁用 tenant；绑定 Agent；配置密钥引用；查看健康和审计；不能通过普通查询接口越权模拟用户。

### R-05：知识方案 AI

基于经授权样本生成方案建议和影响分析；不得直接激活高影响实体合并、拆分或忽略规则。

### R-06：审核者

审核需求、设计、代码、安全门禁和试点结果；不得由实现 Agent 自行批准交付。

## 6. 关键用户旅程

### J-01：管理员初始化租户

1. 管理员执行 `cwk tenant init`。
2. 系统生成稳定 tenant_id 和隔离目录。
3. 管理员绑定可信 Agent ID。
4. 系统记录密钥引用，不保存明文到租户配置。
5. doctor 验证路径、权限、依赖和密钥可用性。
6. tenant 进入 `profile_pending`，尚不对 Agent 开放查询。

### J-02：用户与 AI 共创知识方案

1. 用户描述职责和常见问题。
2. 系统按时间、来源、发送人、类型和活跃度抽样 100–200 篇。
3. AI 生成知识空间、实体、别名、路由和冷归档草案，每项带样例和影响范围。
4. 用户确认、拒绝或修改高影响建议。
5. 系统用 20–30 篇 holdout 样本展示预览。
6. 用户确认后激活 `knowledge-profile v1`。
7. 系统全量路由已有访问汇报并构建专题索引。

### J-03：宿主机采集并去重共享 raw

1. Worker 使用租户身份发现新增/更新汇报。
2. 解析稳定正文与租户视角字段。
3. 计算规范化正文哈希并写入不可变共享对象。
4. 写入 report version catalog。
5. 写入或更新租户访问关系和视角覆盖。
6. 按租户 profile 路由到一个或多个知识空间。
7. 仅对 `index` 内容生成必要派生知识。

### J-04：用户查询专题问题

1. Gateway 把可信 Agent ID 交给查询入口。
2. 查询入口解析固定 tenant_id，不接受用户提供 tenant 参数。
3. 在该租户的一个或多个知识空间召回候选。
4. ACL 前置过滤 report_id。
5. 回读共享 raw 前再次校验 ACL。
6. 输出答案、证据和“截至时间”。

### J-05：低价值内容冷归档和重新路由

1. 路由器判断报销或常规付款为 `archive_no_index`。
2. 保留 raw、访问关系、路由理由和哈希，不生成 Wiki/索引。
3. 新评论、审批或正文变化触发重新路由。
4. 若变成重要事项，可进入专题空间并补建派生知识。

### J-06：权限撤销

1. 工作协同或管理员产生撤权事件。
2. 系统把 access grant 标记 revoked。
3. 从租户索引、缓存、临时结果和知识空间成员关系中移除 report_id。
4. 后续查询即使直接给出 report_id 也拒绝访问。
5. 共享 raw 只有在不存在其他有效访问者且满足保留策略时才进入单独的数据生命周期流程。

## 7. 功能需求

### FR-01：机器级共享引擎

- CWK 引擎安装一次，所有 tenant 使用同一受控版本。
- 引擎代码对普通租户只读。
- 升级和回滚必须记录版本及兼容性检查。

### FR-02：租户注册与生命周期

至少提供：

```text
cwk tenant init
cwk tenant bind-agent
cwk tenant knowledge-init
cwk tenant enable
cwk tenant disable
cwk tenant doctor
```

租户状态固定为 `draft / profile_pending / pilot / active / suspended / offboarded`。合法跃迁：

```text
draft -> profile_pending
profile_pending -> pilot | suspended | offboarded
pilot -> active | suspended | offboarded
active -> suspended | offboarded
suspended -> profile_pending | pilot | active | offboarded
offboarded -> (terminal)
```

`suspended` 恢复时必须由管理员指定目标状态，并重新验证该状态的全部前置 receipt；不得只恢复旧进程。状态权限矩阵：

- `draft`：只允许管理员创建 tenant、绑定 Agent、配置 credential reference；禁止解析凭据、采集、Profile AI、调度和查询。
- `profile_pending`：允许宿主机解析凭据、执行有界的按需样本采集、Proposal/Preview；禁止普通查询和常态化 Scheduler。
- `pilot`：仅对 allowlist 开放宿主机采集、Scheduler、Profile 和 Query Broker；必须受独立 feature flag 约束。
- `active`：允许正常宿主机采集、Scheduler、Profile 版本治理和 Query Broker。
- `suspended`：禁止解析凭据、采集、Scheduler 和查询；只允许管理员诊断、轮换/撤销凭据、复核与状态恢复。
- `offboarded`：终态；禁止所有数据面操作，只允许按已授权保留/清理策略处理审计和墓碑。

`pilot` 的 allowlist 事实只能来自 Wave-0 冻结的 neutral
`PilotAdmissionProviderV1`，调用形式固定为
`snapshot(*, agent_snapshot)`，purpose 在 provider 构造期绑定为
`collector_run | profile_workflow | query_broker`，调用方不得逐次改 purpose。
快照恰好九字段，TTL 满足 `0 < expires_at-as_of <= 300s`，并以
`cwk-pilot-admission-snapshot-v1\0` 绑定完整性。只有 `pilot` 强制
`admitted=true` 且快照当前有效；`profile_pending/active` 继续遵守本节既有状态语义，
不得被该 ABI 引入新的准入依赖。缺 provider、deny、过期、错 tenant/purpose、hash
漂移或 revision 回退一律 fail closed；生产 policy adapter 只属于 RT-026。

### FR-03：可信 Agent—tenant 绑定

- 一个普通 Agent 在同一时刻只能绑定一个 tenant。
- 绑定由管理员在宿主机完成，普通用户不可修改。
- 查询入口从可信运行上下文获取 Agent 身份，而不是从自然语言或请求体获取（见 FR-17）。
- 存储层不保存原始 `agent_id` 明文，只保存 `agent_id_hash = HMAC(secret, agent_id)`；`secret` 由宿主机 secret backend 管理，管理员可轮换但不能读取历史值。审计事件、绑定记录、日志、缓存键均只出现 hash，不回显原始 `agent_id`。
- 所有绑定变更必须审计，且触发 tenant `auth_epoch` 递增，令旧缓存与 in-flight 请求同步失效。

### FR-04：租户独立运行目录

每租户独立拥有：

```text
config/ access/ state/ runs/ locks/ logs/ retries/ cache/ knowledge-spaces/
```

- 路径必须位于 `CWK_INSTANCE_ROOT/tenants/<tenant_id>`。
- 禁止目录穿越、绝对路径覆盖和软链接逃逸。
- 一个租户的锁、重试或磁盘错误不得阻塞其他租户。

### FR-05：宿主机密钥引用

- 每租户配置只保存 secret reference，不保存 AppKey 明文。
- AppKey 不进入 Git、Prompt、Agent workspace、沙箱、普通日志或错误栈。
- 支持密钥轮换、禁用和健康检查。

### FR-06：共享原始证据对象

- 同一规范化正文内容仅保存一个不可变对象。
- report version catalog 保存来源命名空间、report_id、版本哈希、创建/发现时间和对象引用。
- 对象写入采用临时文件、fsync/原子 rename 或等价原子机制。
- 对象校验失败时 fail closed，不得发布不完整版本。
- `report_id` 未验证全局唯一前必须包含 `source_namespace`。

### FR-07：共享字段与租户视角分离

- 解析器输出 canonical evidence envelope 和 tenant view envelope。
- 在完成双身份一致性差异验证（`cwk contract compare-user-views`，详见 DESIGN §11）通过并显式经由 `verified_shared_extensions` 升级流程激活之前，评论（reply）、审批/事件节点（node）、附件元数据以及任何附件下载/预览用的临时 URL 一律进入 tenant view overlay，不得写入 canonical evidence object，也不得进入 report version catalog 的稳定字段。
- 为避免遗漏对决策有影响的评论/审批正文，Collector 可以把经 allowlist 提取、规范化并校验的正文保存为**tenant-scoped staged event evidence**；`TenantViewEnvelope v1` 仍只保存 event ID/hash。该暂存证据不是访问关系 SoR，也不是查询、路由、AI、索引或派生层输入；只有后续满足 `active` grant、未过期 lease 与显式可见事件引用（或权威 event-level receipt）后，后续消费者才可读取。
- 临时下载 URL（含 attachment presign、preview link、跳转短链）不允许出现在 canonical object 中，即便未来升级验证通过也不允许，因为它们本身包含租户身份或时间敏感 token。
- 个人读取、待办、lane、角色、允许操作、可见事件集合等只能进入 tenant view。
- 升级路径：字段从 tenant overlay 提升为 `verified_shared` 必须经过 M0 契约探针输出 + 独立审核 + 版本化 `verified_shared_extensions` 清单登记；未登记的字段一律按 overlay 处理，采集器和 canonicalizer 遇到未知字段默认丢入 overlay，永不写 canonical。

### FR-08：访问关系账本

每条关系必须包含以下字段（对应 DESIGN §5 C-08 `cwk.access_grant.v1` schema）：

```text
tenant_id, source_namespace, report_id, roles, visibility_scope,
permission_source, auth_epoch, granted_at, last_verified_at,
lease_expires_at, revoked_at, status
```

状态机固定为：

```text
discovered -> granted -> active -> revalidation_due -> active|revoked
revoked   -> purge_pending -> purged
```

- 只有 `active` 允许作为查询候选来源；`discovered / granted / revalidation_due`（任何情况下均 fail closed）`/ revoked / purge_pending / purged` 均不得进入候选。
- 账本是查询权限的唯一来源，不允许从 Wiki 路径、shared object 存在性或 tenant space index 推断权限。
- 访问关系 lease 过期或复核失败时，按 fail-closed 策略处理，不允许“宽限可读”。
- SLA：首期采用“安全默认 + 后续基准替换”表述：
  - 有权威撤权事件时，Broker 逻辑拒绝目标 ≤60 秒、派生清理目标 ≤5 分钟；
  - 无权威撤权事件时，lease 复核间隔上限 15 分钟；
  - RT-011 负责验证权威撤权/复核能力是否存在；最终数值由 RT-024 在真实实现链上测量，再由业务在 RT-026 试点 go/no-go 前确认冻结，未测量前不虚构指标。
- 支持增量授权、撤销和审计回放；每条状态跃迁必须携带主体、原因、`auth_epoch` 前后值和证据引用。

### FR-09：租户采集和调度

- 每租户独立采集状态、写锁、限流、重试和运行 manifest。
- 允许错峰调度和租户级并发上限。
- 租户失败不影响其他租户继续运行。
- 只允许读取工作协同，不修改源端状态。
- 没有公司级变更流时允许每租户发现，但共享正文仍去重。
- 评论、回复、审批和状态变化可形成 tenant-scoped staged event evidence 与“事件变化”信号；本轮列表中未出现某事件绝不等同于撤权、删除或不可见，事件正文也不得写入 run manifest、日志、错误或 prompt。
- 对 `pilot` tenant，Collector 与 Scheduler 都必须以构造期绑定
  `purpose=collector_run` 的 shared PilotAdmission provider 在启动前和发布/提交前
  各重验一次；任一次 deny/unavailable/expiry/revision 回退或快照漂移都丢弃整次作业，
  不发布半成品。`profile_pending` 有界样本采集与 `active` 正常采集不新增此准入依赖。

### FR-10：知识画像生成

- 分层抽样 100–200 篇，抽样过程可复现。
- AI 草案包含推荐专题、核心实体、别名、合并/拆分、关注类别、冷归档类别、常见查询和路由阈值。
- 每项建议包含真实样例引用、出现数量、覆盖率和影响范围。
- AI 输出只能形成 proposal，不能直接激活高影响变更。
- Profile eligibility 返回的 `canonical_sha256` 必须由 RT-017 注入的
  `CanonicalVersionProviderV1.resolve_current(*, report_key)` 点查 RT-014 catalog，
  并经公开 `SharedEvidenceStore.read_version(report_key, canonical_sha256)` 复核；不得从
  grant、tenant view 或私有 catalog 推断。若 tenant 为 `pilot`，Proposal/manifest/
  Profile/route/projection 的持久化边界还必须使用 `purpose=profile_workflow` 的
  PilotAdmission 快照重验；`profile_pending/active` 保持既有语义。

### FR-11：用户确认和 holdout 预览

- 高影响实体合并、拆分、忽略和冷归档规则必须显式确认。
- 使用 20–30 篇未参与方案生成的汇报展示路由、摘要和可回答问题预览。
- 用户可修改、拒绝或暂缓建议。
- 用户确认后生成不可变 profile 版本和 SHA-256。

### FR-12：知识画像版本化

- Profile 版本状态固定为 `draft/proposed/preview/confirmed/active/superseded`，不存在 `rolled_back` 状态。
- 回滚是一条追加式 `profile_pointer_rollback` 审计事件：原子地把 active pointer 指向先前 `superseded` 版本，并同步将当前 `active` 版本置为 `superseded`、目标版本置为 `active`；Profile 内容和历史事件不得覆盖或删除。
- 每个派生产物记录 profile version 和 SHA。
- profile 变化生成影响清单，触发必要的重新路由和索引重建。
- 只支持通过 `profile_pointer_rollback` 事件切回最近一个 `superseded` 版本；不允许跨过最近版本回到更旧版本，也不允许修改版本内容。

### FR-13：多知识空间

- 一个 tenant 可创建多个逻辑空间。
- 一篇汇报可属于零个、一个或多个空间。
- 每空间可配置抽取重点、实体视图、索引、日报、风险和行动视图。
- 每个空间由稳定 opaque `space_id`（如 `sp_...`）标识，用户可读 slug（如 `tbs`、`sfe`）只是 UI/CLI 别名，slug→ID 映射存放于租户配置。slug 允许改名而 ID 不变；所有派生投影、审计、路由决策、缓存键使用 opaque `space_id`，不能使用 slug。
- 所有空间结论必须回链共享 raw，不允许形成独立事实副本。
- 空间停用不删除 raw 或访问关系。

### FR-14：多标签路由器

路由结果必须包含：`disposition`、`space_ids[]`、置信度、reason codes、证据、profile version 和决定时间。`space_ids[]` 的元素只能是 opaque `space_id`，不得持久化用户可读 slug。

- 确定性规则优先。
- 已确认实体/别名次之。
- AI 分类用于补充和模糊判断。
- 低置信、规则冲突或新类型进入 `review`。
- 路由器不能返回物理删除动作。

### FR-15：重新路由

下列事件触发重新路由：正文变化、新评论/审批、状态变化、profile 激活、实体规则变化、用户纠正、空间配置变化。

- 重新路由幂等。
- 保存前后决策和差异。
- 从 `archive_no_index` 进入 `index` 时补建派生知识。
- 从 `index` 退出时清除该租户空间索引，但保留 raw。
- staged event evidence 只能在后续 ACL 门禁确认后作为重新路由的内容输入；在此之前仅可作为不可查询的保留性证据与变化信号。

### FR-16：冷归档

- `archive_no_index` 不生成 AI 摘要、不进入普通检索和日报。
- 保留 raw、基础元数据、访问关系、路由理由和变化游标。
- 冷归档查询入口通过 FR-17 的 `include_dispositions=["index","archive_no_index"]` 显式打开，默认关闭；仍需 ACL 双门禁；不提供“搜索全部含冷归档”的隐式旁路 API 或额外 CLI。
- 物理保留期限不在第一阶段自动执行；到期策略在 RT-026 完成试点准备后，作为独立发布活动由业务确认，未定前默认保留但不索引。

### FR-17：受控查询入口

- 普通沙箱不能直接读取共享对象目录。
- 查询入口使用可信 Agent 身份获取 tenant。可信身份获取方式按 DESIGN §5 C-13：
  - 首期强制采用 OpenClaw 受控 Tool 调用；Agent 身份由 Gateway 在 Tool 调用元数据中注入，请求体字段不可覆盖；
  - 本机 UDS + `SO_PEERCRED`（或等价 peer credential）作为后备本机实现，仅在 OpenClaw Tool 通道不可用时启用，仍不接受请求体中的 `agent_id`；
  - loopback HTTP + 用户自报 `agent_id` 的实现被明确禁止；G1 冻结政策/ADR/能力探针，真实运行时合规由 RT-023、VG-D、G5 验收。
- 先以 active grant 与 space membership 取交集，再进入候选召回；raw 回读前执行第二次 ACL，并复核 `auth_epoch` 与 `lease_expires_at` 是否仍匹配请求初始快照，避免旧索引元数据泄漏和 in-flight 撤权穿透。
- 允许限定空间，但请求字段统一为 `space_selector[]`，元素只能是当前 tenant 的 opaque `space_id`；用户可读 slug 不进入 Broker 契约，由 RT-023 提供受身份约束的 `list_spaces`/slug 解析接口。
- 请求可选字段 `include_dispositions`（默认 `["index"]`）用于显式声明是否召回 `archive_no_index` 或 `review`；每个 disposition 独立设置 `limit`、超时和并发上限，`archive_no_index` 默认 limit 不高于 `index` 的一半，且不得扫描共享 raw。未显式列入的 disposition 不会返回。
- 不允许请求参数注入 tenant、共享 root、绝对路径、`agent_id`、`credential_ref` 或 profile 版本。
- Query Broker 对 `active` tenant 不调用 PilotAdmission；对 `pilot` 的成功请求（包括
  cache hit）必须以 `purpose=query_broker` 恰好调用两次：检索/cache lookup 前一次，
  fresh context/grant/profile 二次复核之后、EvidenceReader 回读与返回之前一次。
  deny/unavailable 不缓存；cache key 绑定 tenant status、policy revision 与 admission
  snapshot SHA，consumer 维护 `(tenant_id,purpose)` revision high-water，回退或两次
  snapshot 漂移整请求拒绝且无部分 evidence bundle。

### FR-18：证据型自然语言问答

- 关键结论提供 raw 引文、作者、时间、report_id/安全链接和“截至时间”。
- 明确实体查询优先使用实体硬范围。
- 歧义实体要求澄清。
- 无权限、未知实体、无证据或 ACL 状态过期时不得返回高置信答案。
- 同一事实在多个空间中的派生表达不得冲突；冲突时回退 raw 并标记需要复核。

### FR-19：审计与可观测性

- 按租户展示采集、访问关系、路由、索引、查询、错误和成本状态。
- 查询审计记录 tenant、Agent、空间、候选 report_id、ACL 决策、证据读取和结果状态；不记录完整敏感正文。
- 所有 profile、绑定、授权、撤权和管理员操作可回放。

### FR-20：备份、恢复和迁移

- 共享 raw、report catalog、access ledger 和 tenant profile 加密备份。
- 支持空机恢复演练。
- 当前单用户镜像通过影子导入和逐对象 crosswalk 核验迁移；分别验证 `legacy_source_sha256` 与 `canonical_sha256`，不要求 legacy 原始字节哈希等于规范 JSON 哈希。Broker 可用后的 legacy/new 查询 diff 在 RT-026 完成。
- 迁移失败可回滚到现有 Local-First 运行，不改变当前 nightly。

## 8. 非功能需求

### NFR-01：安全隔离

- 跨租户数据泄漏测试为 0。
- 路径穿越、软链接逃逸、缓存串扰、日志串扰、临时文件串扰全部拒绝。
- 共享 raw 目录不得直接暴露给普通沙箱。

### NFR-02：密钥安全

- AppKey 不出宿主机密钥边界。
- 明文密钥不得写磁盘日志、manifest、测试快照或模型 Prompt。
- 密钥禁用后下一次采集必须 fail closed，并不影响已有、仍授权的只读数据。

### NFR-03：数据完整性

- canonical object、report version、tenant view 和路由记录均带 SHA-256。
- 同一汇报版本跨用户重复采集后物理对象计数仍为 1。
- 迁移前后报告数量、版本和 crosswalk 一致；`legacy_source_sha256` 与 `canonical_sha256` 分别验证，不要求两种序列化哈希直接相等。

### NFR-04：故障隔离

- 单租户超时、模型失败、锁冲突或 DocDB 失败不阻塞其他租户。
- 重试有界、持久化并带租户维度。
- 共享对象发布失败不得产生可引用的半成品 catalog。

### NFR-05：性能

- 首期 10 个租户、每租户 1 万篇汇报规模下，查询候选过滤不依赖全量扫描共享 raw。
- ACL 前置过滤使用索引化访问关系。
- 去重命中不重复写正文。
- 具体 P50/P95、容量和配额指标在 RT-024 合成基准完成后冻结；RT-011 只固定契约与安全默认，未测量前不得虚构数字。

### NFR-06：可恢复性

- 共享 raw、ACL、profile 和绑定均可从加密备份恢复。
- 恢复后对象 SHA、有效授权数量和 profile SHA 全部一致。
- 恢复演练不依赖原机器绝对路径。

### NFR-07：可审计性

- 每项用户确认和 AI 建议有主体、时间、证据、影响范围和前后版本。
- 每个 query 可追溯到固定 tenant、profile 和 ACL 快照。

### NFR-08：成本治理

- AI 抽取、路由、精编和问答按 tenant、模型和任务记录调用/失败/重试/token（若供应商提供）。
- 冷归档不默认消耗精编额度。

### NFR-09：兼容性

- Python 3.11+（与 CWK 现有 RT 统一，取消 3.10 支持以对齐 typing/asyncio 新语义与 `tomllib` 使用）。
- 现有单用户 Local-First 作为兼容模式保留至迁移验收完成。
- 不破坏现有 raw、Wiki、RT-007 时间线和 RT-010 实体查询契约。

## 9. 分类体系

为避免一个“类型”同时承担多个维度，采用四套正交分类：

### C-01：证据角色

`canonical_evidence / tenant_view / derived_knowledge / audit_record`

### C-02：路由处置

`index / archive_no_index / review`

### C-03：知识空间成员关系

多值 `space_ids[]`，值必须是 opaque ID，例如 `sp_5a92f7c1b3`；`general/tbs/sfe` 只可作为用户可读 slug，不进入持久化成员关系。

### C-04：租户生命周期

`draft/profile_pending/pilot/active/suspended/offboarded`

任何实现不得把 C-01 到 C-04 合并成单个枚举。

## 10. 权威数据源

- 工作协同源端：汇报正文、源端时间、作者和源端权限事实。
- 共享 evidence object + version catalog：本地不可变原始证据。
- tenant access ledger：查询是否允许的唯一权限账本。
- Gateway Agent binding：Agent 属于哪个 tenant 的唯一绑定来源。
- active knowledge-profile：租户知识治理和路由规则的唯一配置源。
- route decision log：某篇汇报为何进入/退出空间的审计源。
- derived Wiki/index：可重建派生物，不是事实或权限来源。

## 11. 状态机和异常策略

### Tenant

采用 FR-02 的唯一状态图和状态权限矩阵；任何组件不得另行定义 `disabled/enabled` 等别名状态。Query Broker 仅允许 `pilot/active`，其中 `pilot` 还必须命中 allowlist；Scheduler 仅允许 `pilot/active`，`profile_pending` 只允许有界按需样本采集。

### Knowledge profile

```text
draft -> proposed -> preview -> confirmed -> active
active -> superseded
superseded -> active  (仅通过原子的 profile_pointer_rollback 事件，同时替换当前 active pointer)
```

`profile_pointer_rollback` 是审计事件，不是 Profile 版本状态。

### Report routing

```text
observed -> canonicalized -> access_bound -> index|archive_no_index|review
                                           -> rerouted on change
access_bound -> revoked
```

异常默认：身份、ACL、哈希、路径或 profile 不可验证时 fail closed；AI 分类失败进入 review；派生知识失败保留 raw 和访问关系，不把整篇视为丢失。

## 12. 验收标准

### AC-01：共享存储

- 两个 tenant 采集同一汇报版本后，共享正文对象只有一个。
- report catalog 有两个访问关系但指向同一对象 SHA。
- 新版本产生新对象或新版本引用，旧版本可回读。

### AC-02：租户隔离

- 双租户相同/不同汇报场景全部通过。
- report_id 枚举、路径穿越、软链接、缓存、日志、临时文件和参数注入均不能越权。
- 无效 Agent、未绑定 Agent 和 suspended tenant fail closed。

### AC-03：权限撤销

- 撤权后，候选召回、空间索引、缓存和 raw 回读均拒绝该 tenant。
- 其他仍授权 tenant 不受影响。
- RT-011 只确认撤权/复核外部能力；目标 SLA 在 RT-024 实测后冻结，并在 RT-026 试点 go/no-go 中引用；测试需支持可配置 SLA。

### AC-04：知识方案共创

- 100–200 篇分层样本可复现。
- 20–30 篇 holdout 与训练样本不重叠。
- 高影响合并、拆分和忽略规则未经确认不能 active。
- profile 可回滚，回滚后路由和空间索引与目标版本一致。

### AC-05：路由和知识空间

- 同一汇报可同时进入 TBS、SFE。
- 低置信内容进入 review，不能进入自动删除流程。
- 冷归档内容不出现在默认查询/日报，但变化后可重新路由。
- 多空间引用相同 raw 时事实哈希一致。

### AC-06：查询

- 所有关键结论有当前 tenant 有权访问的 raw 证据。
- 无权限 report_id 从候选阶段即被排除，raw 回读仍再次校验。
- 歧义要求澄清；不存在或无证据问题不返回 high confidence。
- 现有 CWK Wiki smoke 和 RT-010 回归全部通过。

### AC-07：故障隔离与恢复

- 一个 tenant 的采集、模型或同步失败不影响其他 tenant 完成。
- 租户锁互不阻塞。
- 加密备份空机恢复后对象、ACL、profile 哈希一致。

### AC-08：试点门禁

第一阶段：2–3 名技术用户运行 14 天，要求数据遗漏 0、跨租户泄漏 0、调度 14/14 成功、密钥轮换/禁用有效、恢复演练成功。

第二阶段：5–10 人运行 30 天，调度成功率至少 99%，不存在 P0/P1 安全事件后，才允许提交全员生产放量决策。

## 13. 发布门禁

- G0：需求、设计和开发计划经独立 Claude Code 审核无 blocker。
- G1：外部数据契约验证工具完成；双身份样本未完成时只能使用保守覆盖模型。
- G2：租户路径、绑定和密钥安全测试通过。
- G3：共享对象和迁移哈希测试通过。
- G4：profile、路由和多空间测试通过。
- G5：沙箱查询 ACL、撤权和越权测试通过。
- G6：完整回归、备份恢复和最终 Claude Code 验收通过，由**新鲜**最终独立验收者签发。
- G7：用户另行授权后才能部署、配置真实密钥或切换 cron。

机器权威是 `contracts/gates/release_gate_registry_v1.json` 及其两份 receipt schema；
上面七行是**派生说明**，冲突以 registry 为准。三点必须在此写清：

- **G0 不是 registry 条目**：它是 RT 之前的文档评审门禁，证据是叙述性 Markdown，
  只作为 ref_id 出现在 G1 的前置集里。其满足条件被冻结为
  `reviews/审核报告-wave0-final.md`，**该报告当前不存在，因此 G1 恒为 NOT_RUN**。
- **G1～G6 是验证，G7 是授权**，两者不同信任根、不同 schema、不同文件名
  （`receipt.json` vs `authorization.json`）、不同 domain separator。G7 的唯一机器前置
  恰为 `{G6}`，必须由**外部信任根**与人类授权者签发；任何项目内 agent 或测试签名者
  签的 G7 一律无效。G7 只授权 M4 受控试点，**绝不授权扩大切换（M5）或 GA**。
- **所有七个 release gate 当前均为 NOT_RUN**：`release-gate-receipts/` 不存在。
  缺失不得从下游门禁、叙述文档或"前置全绿"推断。

## 14. 开放问题

开放问题不阻塞代码骨架，但必须在对应 gate 前关闭或采用保守默认：

1. report_id 全局唯一性。
2. 两个身份下正文、评论、审批和附件的差异。
3. 公司级变更流和权威访问关系 API。
4. 权限撤销 SLA。
5. 冷归档和物理删除保留期。
6. secret manager 选择和轮换接口。
7. 目标 Gateway 是否提供不可伪造的 Tool 元数据、可用 peer credential、限流与 SLA；transport 政策已确定为受控 Tool 优先、UDS 后备，不再作为开放选型。
8. 单 Gateway 租户数、并发和资源配额。
9. 知识空间首期已冻结为不嵌套、每 tenant 最多 32 个；扩大上限属于后续容量决策。
10. 用户离职、Agent 删除和 tenant 转移流程。

## 15. 参考资料

- `references/讨论决策记录.md`
- `references/当前架构与差距.md`
- `references/安全威胁模型.md`
- `DESIGN.md`
- `plans/开发计划.md`
- `reviews/审核报告-r1.md`
- `reviews/整改记录-r1.md`
- `reviews/审核报告-r2.md`
- `reviews/整改记录-r2.md`
- `reviews/审核报告-r3.md`
- `reviews/交叉复核-r3.md`
- `reviews/整改记录-r3.md`
- `reviews/审核报告-r4.md`
- `../../docs/DESIGN.md`
- `../../docs/INTERNAL_DISTRIBUTION.md`
- `../../docs/RUNTIME_STATUS.md`
- `../../RT/RT-008/`
- `../../RT/RT-010/`
