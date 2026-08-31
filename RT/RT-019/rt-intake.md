# RT-019 rt-intake：Knowledge Profile 提案、确认与版本生命周期

- 状态：planned（仅冻结契约；尚未实现、测试或独立验收）
- Profile：Spec-Standard
- 依赖：RT-014、RT-015，**以及 RT-017 完整 Stage-01 AccessLedger compatibility
  surface 独立验收 PASS**（authority injection、cleanup v2、
  `list_profile_eligible`、CanonicalVersionProvider、PilotAdmission）；
  只读取 active grants 与完整性通过的 canonical evidence。
  RT-019 **不能与 RT-017 真正并行开工**：sampler 需要在 `profile_pending` 下取候选，
  而这只能经 RT-017 新增的公共只读 ABI；扫 ledger 私有目录非法，放宽
  `list_query_eligible` 亦非法。Stage-01 可早于 RT-017 完整 Worker PASS，但不能拆为
  eligibility-only slice；未整体 PASS 时 RT-019 不得开工。
- 实现 Agent：`agent-rt019-impl`
- 独立验收 Agent：`agent-rt019-verify`

## 1. 目标

让用户与零工具 AI 基于本人已授权的代表性报告共创知识方案：确定性抽样、proposal、逐项接受/拒绝/编辑/暂缓、高影响门禁、不可变 Profile 版本、影响清单、active pointer 与追加式回滚。RT-019 只冻结 PreviewProvider ABI；真实 holdout 预览、路由和激活 receipt 由 RT-020 实现。

## 2. 关键冻结决策

### 2.1 一份联合 sample manifest

现有 `cwk.sample_manifest.v1` 已同时包含 `samples` 与 `holdout`，首期明确只产生一份不可变联合 manifest：

- `samples` 供 proposal 生成；`holdout` 永不进入 proposal AI 输入；两集合零重叠。
- `KnowledgeProfile.sample_manifest_ref` 与 `holdout_manifest_ref` 必须引用同一个联合 manifest；语义 validator 强制二者相等。
- `sample_manifest_sha256` 覆盖整个联合 manifest，包括 holdout；不产生第二份易漂移的 holdout manifest。
- Manifest v1 不静默改字段；若后续需要拆分，必须新增版本和 migration note。

### 2.2 授权样本不足

若经 active grant、未过期 lease、canonical integrity 与去重过滤后少于 120 篇，任务进入明确终态 `insufficient_authorized_sample`：不生成 manifest、proposal、preview 或 Profile，不缩小最低样本、不重复取样、不使用非 active grant，也不伪造 PASS。

### 2.3 CLI slot 是显式兼容修订

当前 `cwk_tenant_cli.FROZEN_PROVIDER_SLOTS` 未实际包含 `cwk_tenant_cmd_profile`。RT-019 的 CLI 不可仅新增 provider 文件后宣称可达。实现前必须有独立的 RT-012 compatibility commit：

- 修改范围仅为在冻结 tuple 中加入 `"cwk_tenant_cmd_profile"` 及相应 dispatcher 回归测试；
- 不改变 provider ABI、动态加载政策、错误码或既有命令；
- commit/报告明确标注 RT-012 兼容修订并独立验收；
- 未完成时核心 library 即使通过，RT-019 仍不能 completed。

## 3. 必须交付

- deterministic stratified sampler 与联合 sample manifest writer。
- Proposal AI 安全 runner、proposal schema/validator、partial/final proposal 状态。
- decision log：接受、拒绝、编辑、暂缓，含主体、证据、影响范围和前后值。
- Profile 六状态、immutable version store、pointer、影响清单与最近版本 rollback。
- `PreviewProviderV1` ABI 与 fail-closed null provider；RT-019 测试可用 fake provider，生产 activation 必须等待 RT-020 receipt。
- `cwk_tenant_cmd_profile.py` provider 和明确的 RT-012 slot compatibility revision。
- 正常/异常/恶意/回滚/权限/AI 注入测试。
- 零漂移消费 neutral PilotAdmission ABI：Profile workflow provider 构造绑定
  `profile_workflow`；仅 `pilot` 在 manifest/proposal/Profile 持久化边界重验，
  `profile_pending/active` 保持既有语义。

## 4. 非目标

- 不执行生产路由、索引或空间文件创建。
- 不读取 staged event body（RT-017 未经权威解锁的事件永远不可采样）。
- 不允许 AI 直接激活 Profile 或高影响决定。
- 不创建 arbitrary prompt/tool/shell/network/file access。
- 不启用真实 tenant 查询、cron、部署或生产开关。

## 5. 进入 RT-020 的门禁

定向与全量回归、secret scan、恶意 raw 零工具测试、CLI compatibility 回归和独立验收必须明确 PASS。`insufficient_authorized_sample` 是安全的业务终态，不是可忽略的测试失败；测试需同时证明足量路径成功与不足路径不产生任何 proposal。RT-019 PASS 不代表 Profile 已在生产激活，也不代表 G4/VG-C。
