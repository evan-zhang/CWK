# RT-023 rt-intake：可信沙箱传输、客户端与真实空间集成

- 状态：planned（未实现、未自测、未独立验收）
- Profile：Spec-Standard / Security-Critical
- 目标：把 RT-022 Broker 接到真实 Gateway 控制边界和 RT-021 空间索引，
  让沙箱只能以不可伪造身份查询自己的 tenant。
- 依赖：RT-011 ADR/能力探针、RT-013 binding、RT-021 real index、RT-022
  Broker core；RT-022 与 RT-021 均须独立 PASS 后才能做最终集成。
- 完成条件（**不含 VG-D**）：至少一条真实受控 transport（OpenClaw Tool 首选，
  UDS peercred 后备）在真实沙箱链上可验证，生产 trust-store/verifier 可用，
  真实 `SpaceIndexProvider`/`list_spaces`/`resolve_slug` 接通 RT-022，且 RT-023
  自身的独立验收 Agent 明确 PASS。mock、fake signer 或进程内测试 secret 不能
  用来满足"真实 transport/verifier"。
- 门禁顺序（不可交换，禁止环）：RT-023 独立 PASS → 才执行 VG-D → VG-D receipt
  再作为 RT-024/RT-026/G6 的输入。**VG-D 的 PASS 不是 RT-023 completed 的前置**；
  任何把 VG-D 写成 RT-023 完成条件的表述都是环依赖。

## 一、范围

1. OpenClaw controlled Tool adapter；若运行时不能提供不可覆盖的可信身份，
   才评估本机 UDS + kernel peer credential 后备。
2. production capability trust-store、版本化 receipt 与 verifier；替代
   RT-011 仅测试可用的进程内 HMAC 路径。
3. 真实 `SpaceIndexProvider`、身份约束的 `list_spaces` 与 slug→opaque
   `space_id` 解析。
4. 无凭据、无宿主路径的 sandbox query client。
5. 为 VG-D 准备的真实沙箱攻击面与证据：身份伪造、双 ACL、撤权竞态、SHA 回读
   与隔离攻击。VG-D 本身在 RT-023 独立 PASS **之后**作为独立 wave 集成回执执行。
6. 可提供与 production factory 物理分离的 non-production PilotAdmission sandbox
   adapter，仅供本 RT 的 pilot 调用矩阵测试；不得作为 VG-D/G5 production evidence。

## 二、明确不做

- 不接受 loopback HTTP + 请求体自报身份，不提供临时降级或兼容旁路。
- 不在沙箱安装 collector/store，不挂载 shared raw，不下发 AppKey。
- 不创建 break-glass、管理员模拟用户或跨 tenant space 枚举。
- 若需要正式 OpenClaw Skill，本 RT 文档只冻结 Skill 需求；实际创建/更新
  必须另行通过 Skill Workshop，不直接写 Skill 文件。
- 不启用真实 tenant、不改 cron/nightly/installer、不部署生产。
- 不实现或冒充 RT-026 唯一 production PilotAdmission adapter，不从 sandbox/env/CWD
  读取 release switch/G7/tenant allowlist。

## 三、关键阻塞规则

- Gateway metadata 若可由请求体、Prompt、沙箱环境变量或用户进程覆盖，
  结果为 `conservative_unknown`，RT-023/VG-D 阻塞。
- UDS 只提供共享 uid 而不能唯一映射到受控 Agent 时不合格；不得把 uid
  相同视为身份已验证。
- trust anchor 缺失、receipt signer/target/audience/时效不可验证时 fail
  closed；测试 signer 永远不能升级 production probe。
- 真实沙箱、真实 Gateway 或真实 peercred 环境不可用时，允许完成代码与
  测试，但 RT-023 状态必须保持 blocked/not completed。
- RT-023 的 non-production admission adapter 只证明 Broker 注入边界；VG-D/G5 不得
  引用它关闭 production admission。VG-D 若使用 `active` 非生产 tenant，则 admission
  调用严格为 0；若使用 `pilot`，缺少 RT-026 production-grade adapter 时该项不能形成
  production readiness 结论。

## 四、拟议代码所有权

- 新增：transport adapter、sandbox client、real space provider、trust store、
  capability receipt verifier。
- 版本化 contract：`contracts/rt023/schemas/`；RT-011 v1 文件不静默修改。
- 可对 RT-011 probe 验证入口做带 migration note 的兼容适配，生产 verifier
  与 test-only registry 必须物理/逻辑分离。
- 不修改 QueryRequest 身份字段、不修改 `cwk_tenant_cli.py` 或生产入口。

## 五、回滚

禁用/卸载 adapter 与 client，撤销 trust-store entry 和 socket；Broker、
index、canonical、grant 保持默认关闭且不删除。已签发审计 receipt 保留。
