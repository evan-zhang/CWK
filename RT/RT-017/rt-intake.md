# RT-017 rt-intake：Per-tenant Collector Worker

- 状态：planned（仅冻结契约；尚未实现、尚未测试、尚未独立验收）
- Profile：Spec-Standard
- 依赖：RT-013、RT-014、RT-015；VG-A 已在合成 authority 下通过，但不代表真实权限接口可用。
- 实现 Agent：`agent-rt017-impl`
- 独立验收 Agent：`agent-rt017-verify`（不得与实现 Agent 相同）

## 1. 目标

把现有混合采集器拆为一次只处理一个 tenant 的宿主机 Worker，并以冻结接口输出：

1. `CanonicalEnvelope`；
2. `TenantViewEnvelope`；
3. `AccessObservation`；
4. `TenantEventEvidenceEnvelope v1`（tenant-scoped、固定为 staged）。

Worker 使用 RT-013 Credential Broker 的短租约，不接受命令行明文密钥；通过 RT-014/015 公共 writer 发布共享 canonical、权限观察和 tenant view。评论、回复、审批正文不得因仍属 overlay 而丢失，但在没有权威授权 receipt 时只能进入不可查询的 staged evidence 层。

## 2. 范围

### 必须交付

- CWork 只读 Source Adapter 与单 tenant `collect_once` Worker。
- tenant-scoped state、run、retry、lock、temporary staging 与 crash recovery。
- 四路 envelope 的严格 schema/语义校验和幂等发布。
- `cwk.tenant_event_evidence.v1`、purge receipt、collector run manifest 契约。
- event body 的 allowlist、NFC、长度、URL/token/HTML/未知字段拒绝与 review。
- Access Ledger authority 集成的生产注入点和 CWork authority adapter 所有权收口。
- RT-015 cleanup outbox 的版本化 `staged_event` consumer 兼容修订。
- **新增版本化 snapshot-only 公共 ABI `AccessLedger.list_profile_eligible(*, snapshot)`**，供 RT-019 sampler 在 `profile_pending` 下合法读取候选语料；不放宽 `list_query_eligible` 的 `pilot|active` 语义，也不暴露正文/路径。
- 注入 RT-017 自有 `CanonicalVersionProviderV1`，使 eligibility 的 current
  `canonical_sha256` 只来自 RT-014 catalog 点查并经 public `read_version` 复核；禁止从
  grant/view/私有 catalog 推断。
- 消费 Wave-0 neutral `PilotAdmissionProviderV1`：Worker 绑定 `collector_run`，Profile
  eligibility 绑定 `profile_workflow`；仅 `pilot` 强制 admitted，
  `profile_pending/active` 保持既有语义。
- legacy collector façade；默认仍走 legacy，新的多租户 Worker 不进入 nightly/cron。
- fake CWork + fake authority 的双租户正常、异常、恶意测试。

### 明确不做

- 调度、公平性、全局配额、生产 cron（RT-018/RT-026）。
- Router、Projector、Query Broker（RT-020～RT-023）。
- 真实 AppKey、真实 tenant enable、Cloud/DocDB 写入或生产部署。
- 把一次 bounded list 缺项解释为撤权、删除或 event 不可见。
- 在 authority 能力未知时把 `granted` 自行提升为 `active`。

## 3. 外部能力与 fail-closed 边界

当前 RT-015 的真实 `AuthorityAdapter` 是 `conservative_unknown`，默认拒绝；只有 test-only fake signer。RT-017 对 source-specific 权限验证负责，但不得把“使用 tenant AppKey 成功读到报告”当作可签发 active grant 的权威证明。

冻结规则：

- 若 CWork 能提供可验证的授权/lease/撤权接口，RT-017 实现 `CWorkAuthorityAdapter` 与 `AuthorityReceiptProviderV1`，receipt 必须绑定 tenant、report key、grant key、purpose、签发者、签发时间、到期时间和防重放标识。
- 若真实接口仍无法证明，`CapabilityResult` / authority probe 必须为
  `conservative_unknown`，而 capability activation receipt 必须**完全缺席**；Collector
  仍可写 canonical、`AccessObservation(initial_status="granted")` 与 staged evidence，
  但不得写 active tenant view，不得把 staged body 交给 AI/Router/Index/Query，也不得
  宣称具备真实试点条件。
- fake authority 只用于测试和 VG-B 合成门禁；报告必须明确标记 synthetic，不能升级真实 capability 状态。

## 4. 不可拆分的 Stage-01 兼容修订

现有 RT-015 无生产 authority 注入点，cleanup outbox v1 也没有 `staged_event` consumer，
且公共 `list_query_eligible` 只放行 `pilot|active`，无法服务 `profile_pending` 的 Profile
取样。`cwk_access_ledger.py` 只有 policy 预声明的一个 Stage-01；实现 RT-017 前必须以
同一个独立兼容提交一次性完成：

1. 为 `AccessLedger` 增加显式 `authority_adapter` 构造参数或等价 versioned factory；省略时仍使用 fail-closed default，禁止复用 `_register_test_authority` 私有后门。
2. 新增 cleanup outbox v2（不得静默修改 v1），consumer allowlist 增加 `staged_event`；reader 兼容 v1/v2，新的 revoke 必须排队 staged-event 清理。
3. **新增公共只读方法 `list_profile_eligible(*, snapshot)`**（详见 `specs/技术方案.md` §2.1）。它是 `list_query_eligible` 之外的独立 eligibility 判定，只多放行 `profile_pending`，其余授权条件（grant `active`、lease 未过期、`auth_epoch` 匹配、tenant 一致）与 query 路径完全一致。**严禁**修改 `list_query_eligible` 的既有语义或允许状态集。
4. 注入 `CanonicalVersionProviderV1` 与 constructor-bound
   `PilotAdmissionProviderV1(purpose="profile_workflow")`；eligible record 的 SHA 只来自
   provider 的五字段 current-version snapshot。`pilot` 要求 valid+admitted，其他允许
   状态不新增准入依赖。
5. 兼容提交只改变上述完整 surface，不改变 RT-015 状态机、既有 query eligibility、
   opaque 错误与 v1 历史记录。
6. Stage-01 须由独立验收 Agent 整体验证；不得先签“仅 eligibility”receipt 后再二次
   修改 `cwk_access_ledger.py`。Stage-01 未 PASS 时 RT-019 不得开工。

上述修改全部落在 RT-017 已预声明的 `cwk_access_ledger.py` evolution stage 内，附
append-only evolution receipt 与 migration note。**RT-019 不得自行修改 `cwk_access_ledger.py`，
也不得直接扫描 ledger 私有目录**（如 `tenants/<id>/access/grants/`）——这两种做法都是契约违规。

## 5. 代码所有权

RT-017 独占：

- 修改 `scripts/cwk_collect_live.py`；
- tenant-scoped 适配 `scripts/cwk_collection_state.py`；
- 新增 `scripts/cwk_tenant_collector.py`、`scripts/cwk_cwork_source.py`、`scripts/cwk_tenant_event_evidence.py`、`scripts/cwk_cwork_authority.py`、`scripts/cwk_canonical_version_provider.py`；
- 新增 RT-017 schema、测试与本交付包。

除上节列明的最小 RT-015 兼容修订外，不修改 RT-011～016 实现；写共享对象、grant、view 只能调用公开 API。

## 6. 进入下一阶段的门禁

只有在下列全部成立后才允许进入 RT-018：

- RT-017 定向测试、全量回归、secret scan、Wiki smoke 通过；
- 独立黑盒验收明确 `PASS`；
- fake CWork 双租户证明 canonical 去重、overlay/event body 隔离、断点续跑与单租户故障隔离；
- authority 状态在 receipt 中被准确标记为 `verified` 或 `conservative_unknown`；未知不得伪装为通过；
- `pilot` Collector 的 `collector_run` 准入在启动与发布前各复核一次；缺失、deny、过期、
  revision 回退或快照漂移不产生半 manifest；
- 无生产开关、真实密钥或 cron 变更。

真实 authority 为 `conservative_unknown` 时，能力激活 receipt 正确保持缺席；这不阻塞
RT-017 core 独立 PASS 或进入 RT-018，但会阻塞 RT-026/G6/G7。RT-018 完成后另行执行
VG-B；RT-017 PASS 本身不代表 VG-B、G4 或生产可用。
