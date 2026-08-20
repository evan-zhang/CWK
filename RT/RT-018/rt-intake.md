# RT-018 rt-intake：宿主机 Tenant Scheduler

- 状态：planned（仅冻结契约；尚未实现、测试或独立验收）
- Profile：Spec-Standard
- 依赖：RT-012、RT-013、RT-017；RT-017 必须独立 PASS。
- 实现 Agent：`agent-rt018-impl`
- 独立验收 Agent：`agent-rt018-verify`

## 1. 目标

在宿主机用稳定、版本化的 JobProvider ABI 错峰运行每 tenant 作业，保证 tenant 单写者、状态矩阵、配额、公平性、有界重试、熔断、崩溃恢复和故障隔离。RT-018 只实现新的 Scheduler，不修改 legacy nightly、installer、cron 或生产开关。

## 2. 必须交付

- `JobProviderV1`、`JobSpecV1`、`JobResultV1`、`RunManifestV1` 与 provider registry ABI。
- CollectorJobProvider adapter（调用 RT-017 公开 Worker）；不复制采集逻辑。
- 按 tenant 的错峰计划、单写锁、持久队列/重试、熔断、资源配额和运行 manifest。
- 宿主机重启后的 stale-running recovery 与幂等续跑。
- 零漂移消费 Wave-0 neutral PilotAdmission ABI；CollectorJobProvider 构造注入
  `purpose=collector_run`，只对 `pilot` 在启动与提交前重验。
- 至少两个 fake tenant 的故障隔离、安全、恶意与公平性测试。
- RT-018 独立验收；随后单独执行 VG-B。

## 3. 非目标

- 不实现 Collector、Router、Projector 或 Broker 内部逻辑。
- 不直接依赖尚未交付的 RT-021；未来作业通过同一 ABI 注入。
- 不修改 `cwk_nightly_pipeline.py`、安装入口、launchd/cron 或统一 feature flag。
- 不启用真实租户、真实 AppKey、生产 schedule、Cloud/DocDB 写入。
- 不把 `profile_pending` 加入常态 Scheduler；其样本采集仍由 RT-017 有界按需入口触发。

## 4. JobProvider 冻结决策

Provider registry 使用显式对象注入，不扫描 CWD、PYTHONPATH、环境变量或用户目录：

- `JobProviderRegistryV1(providers=[...])` 只接受已由可信 composition root 导入且 ABI 校验通过的 provider；provider name/version/job kinds 必须唯一。
- RT-018 首期只注入 `CollectorJobProviderV1`。
- RT-021 等未来 RT 可交付新 provider 对象；最终由 RT-026 composition root 显式加入，不需要修改 Scheduler ABI 或动态加载任意模块。
- JobSpec 只能保存 opaque input reference，不保存 credential、正文、query、URL、绝对路径或任意命令。

## 5. 状态与安全边界

- 常态调度仅允许 `pilot/active`；`pilot` 必须由 shared
  `PilotAdmissionProviderV1.snapshot(*, agent_snapshot)` 给出 valid+admitted，provider
  purpose 在构造期固定为 `collector_run`。生产 adapter 由 RT-026 提供；在 RT-026 前
  只能用显式 fake 测试/按需调用，不得生产运行。
- `draft/profile_pending/suspended/offboarded` 不启动新任务；检查发生在 enqueue、lease、execute 前三处。
- `active` 不调用 PilotAdmission。`pilot` 在实际启动前与 run manifest/终态提交前各
  重验；deny/unavailable/expiry/revision 回退或快照漂移都不得留下半提交。
- RT-018 的入场证据是 RT-017 core 独立 PASS，**不是**
  `cwork-authority-source` capability activation receipt。真实 authority 仍为
  `conservative_unknown` 时，该 receipt 正确缺席不阻塞 RT-018 的合成/fail-closed
  scheduler 实现与验收；它仍会阻塞 RT-026/G6/G7 与生产 pilot。
- 一个 tenant 的 lock、429、timeout、坏配置、provider crash、满盘或熔断不能阻塞另一个 tenant。
- Scheduler 不解析 AppKey；Provider 在执行阶段自行通过 RT-013 Broker 获取短租约。

## 6. 代码所有权

新增：

- `scripts/cwk_tenant_scheduler.py`
- `scripts/cwk_job_provider.py`
- `scripts/cwk_collector_job_provider.py`
- RT-018 schemas/tests/交付文档

禁止修改 legacy nightly、RT-017 Worker 内部、RT-012 Registry 状态机和生产配置。

## 7. 完成门禁

RT-018 只有在定向测试、全量回归、secret scan、Wiki smoke 和独立黑盒验收全部 PASS 后才可标记 completed。之后的 VG-B 必须以 fake CWork 同时运行至少两个 tenant，验证共享去重、视角隔离、限流/超时不扩散和重启续跑；RT-018 PASS 不自动等于 VG-B PASS。
