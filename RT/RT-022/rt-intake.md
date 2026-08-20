# RT-022 rt-intake：Query Broker 授权核心

- 状态：planned（未实现、未自测、未独立验收）
- Profile：Spec-Standard
- 目标：在宿主机内交付不可绕过的查询授权核心；先以 fake
  `SpaceIndexProvider` 开发，RT-023 再接真实空间索引与可信传输。
- 依赖：
  - RT-012：Tenant Registry、生命周期状态、`auth_epoch`。
  - RT-013：可信 `AgentContextSnapshot`、Binding Registry、
    `binding_epoch`；请求体身份字段不是身份来源。
  - RT-014：按 canonical SHA 回读且校验不可变证据。
  - RT-015：active grant、lease、撤权 intent/tombstone、cleanup outbox。
  - Wave-0 neutral PilotAdmission：central runtime/schema/test 已冻结；不是 RT-022 owner
    surface。本 RT 仅注入 `purpose=query_broker` 的 provider，不实现 production adapter。
  - RT-019/RT-021：**仅为真实集成依赖，不是本 RT 的进入条件**。本 RT 冻结注入式
    `ProfileSpaceSnapshotProviderV1` 与 `cwk.profile_space_snapshot.v1`（RT-022 为唯一
    owner），并只用 fake provider 并行实现；Broker 不读 RT-019/021 私有布局或模块，
    生产 adapter 由 RT-021 后续零漂移实现。
- 进入条件：上述 API/版本可读取；任何依赖缺失均 fail closed，不以
  legacy 路径或共享 raw 扫描替代。
- 完成条件：需求契约全部满足、定向与全库回归通过、独立黑盒验收
  明确 PASS；完成 RT-022 不代表 G5 或 VG-D 通过。

## 一、范围

1. 交付 `QueryRequest v2` schema 与强语义 validator，保留 v1 只读兼容，
   不静默改变 v1。
2. 交付 Broker pipeline：可信上下文 → binding → tenant/status/profile →
   space → active grant ∩ membership → retrieval → 二次 ACL → SHA 回读 →
   evidence bundle → 脱敏 audit sink。
3. 冻结 `ProfileSpaceSnapshotProviderV1`、`SpaceIndexProvider`（仅 `retrieve`）、
   `EvidenceReader`、`AuditSink`、安全缓存和时钟/快照 ABI；fake provider 只供本 RT
   黑盒测试。`ProfileSpaceSnapshotProviderV1.snapshot(agent_snapshot)` 一次返回
   profile SHA/版本、default 与 queryable active opaque space IDs、membership/index
   versions，0 space 时仍带 `profile_sha256`。
4. 按 disposition 独立预算 `limit/timeout_ms/concurrency`；冷归档不得
   扫描共享 raw。
5. 提供稳定、等价且不泄露对象存在性的外部错误语义。
6. Pilot 查询门禁：request 不合法时 admission 0 次；`active` 0 次；`pilot` success 与
   cache hit 恰好 2 次（检索/cache lookup 前、返回前），并绑定 revision high-water。

## 二、明确不做

- 不实现 OpenClaw Tool、UDS、peer credential、沙箱客户端或正式 Skill；
  这些属于 RT-023。
- 不让自然语言回答模型访问工具；本 RT 输出 evidence bundle，不负责
  最终答案组织。
- 不新增 break-glass、管理员模拟用户、跨 tenant 查询或按 report ID
  探测接口。
- 不修改 `cwk_tenant_cli.py`、nightly、cron、installer、生产配置或真实
  tenant 开关。
- 不把 fake provider、fake AgentContext 或测试签名器用于 VG-D/生产。
- 不实现 `ProfileSpaceSnapshotProviderV1` 的生产 adapter，不 import 或读取
  `cwk_knowledge_profile.py`/`cwk_space_registry.py`/`cwk_space_projector.py`
  及其磁盘布局；该 adapter 属于 RT-021。
- 不 fork `cwk_pilot_admission_api.py`/schema，不读 env/CWD/RT-026 私有 allowlist；
  production PilotAdmission adapter 只属于 RT-026。

## 三、拟议代码所有权

- 新增：`scripts/cwk_query_contracts.py`、`scripts/cwk_query_broker.py`。
- 独占适配：`scripts/cwk_wiki_query.py` 的 Broker/raw-loader 库函数；
  legacy CLI 不向沙箱暴露。
- 新增版本化 schema：`PR/.../contracts/rt022/schemas/`。
- 新增定向测试：`tests/test_rt022_*.py` 与独立攻击脚本/夹具。
- 绝不修改 RT-011 v1 schema、RT-012 dispatcher、RT-013～015 权威存储。

## 四、安全默认与阻塞规则

- 身份、binding、tenant 状态、profile、space、grant、lease、epoch、SHA
  任一不可验证即拒绝。
- `pilot` 必须由构造期绑定 `purpose=query_broker` 的
  `PilotAdmissionProviderV1.snapshot(*, agent_snapshot)` 同时通过两次；`active` 可查询且
  admission 调用 0 次；其余 tenant 状态一律拒绝。
- 对未知、其他 tenant、已撤权、无 membership、对象不存在和对象损坏，
  外部不得返回可区分的存在性信息。
- 缓存命中仍执行 binding/tenant/grant 二次校验；撤权或 rebind 后旧快照
  和 in-flight 请求都拒绝。
- Broker feature flag 默认关闭；本 RT 不触碰生产开关。

## 五、验收入口

独立验收必须覆盖：请求注入、空/缺省选择器、时间倒置、预算交叉约束、
双 ACL、in-flight revoke/rebind/profile change、A/B 缓存隔离、对象存在性
等价错误、PilotAdmission 调用次数/deny/漂移/high-water/cache、SHA 篡改、冷归档预算
与无 raw 全扫、legacy Wiki/实体回归。

## 六、回滚

关闭 Broker feature flag并删除本 RT 新增模块/schema/测试；不改动索引、
canonical、grant、profile 或 legacy read path。撤权和审计事实不可回滚。
