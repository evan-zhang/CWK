# RT-055 Amendment 13：当前完整 needle bank 容量硬失败，INVALID 收口

**结论：Amendment 13 已 INVALID_CLOSED；RT-055 选型目标未完成，禁止切流。** 历史失败与新 source 当前门已分层，未要求旧 UNKNOWN/FAIL 变 PASS。新门真实失败发生在候选启动之前；没有缩减 bank、放宽容量门或重复执行取 PASS。

## 当前门结果

- gate controller / bank construction：**1 / 1**；隐私 synthetic runner：**0**。这一差别是实际先后顺序：controller 先构建完整内存 bank，失败后根本没有启动三类候选进程。
- 输入为 builder/verifier **11** 份 JSON 的全部非空 string leaf，加原始 UTF-8、两种 JSON-escaped 变体、公开 canary 和 warmups；共 **6,481** 个 needle。
- bank **285,809,127 bytes（272.6 MiB）**，超过 **134,217,728 bytes（128 MiB）**；最长 needle **47,978,891 bytes（45.8 MiB）**，超过 **8,388,608 bytes（8 MiB）**。两项均触发 CAPACITY。独立终审使用独立递归计数重算一致。
- needle 值未写 argv/env/file/log/receipt，未传给候选。诊断只有 count/bool/enum/bytes，不重建 Filter，不重跑当前门。未靠私有值子集或提高上限通过。
- 当前 socket gate / 新 candidate firewall / postscan：**NOT_RUN，不是 PASS**。候选三类 external=0 和独立 probe PID 的现行强门未豁免，只是没有走到该阶段。
- workload / main readiness / before / freeze / formal：**全 0**；正式 A/B attempt **0/0**；arm/exposure/query/score/complete/result **全 0**。120 项正式指标和 24 项 Gateway 能力均为 **null**；聚合未生成，唯一裁决 INVALID。

## 两层历史证据

1. **historical_invalid_preserved**：Am11 108 samples / 1 external observation 的 PID/phase/state/target 证据缺失，传输 UNKNOWN；Am12 旧日志 expanded scan 的 1 个命中、同时为公开 source 常量、因果 UNKNOWN 和首次 audit FAIL 原样保留。两轮 INVALID_CLOSED、旧 after FAIL 不重开、不重试。
2. **current_source_privacy_gate**：新 source 只以当前一次完整门决定放行，不要求旧观测事后通过。现轮以 FULL_PRIVATE_BANK_CAPACITY 失败关闭，不以历史 socket/log 失败阻断新 source。
3. 部署首次 PRECHECK 还发现更早的零曝光证明在用新判据评旧 source。原部署失败保留（当时 source 安装=0、privacy=0）；追加修复只从原 hash-bound 公开源码提取纯 evaluate 谓词核验原作废证明，不 import 旧模块、不运行 initializer、不使用该历史谓词放行当前门。原判据 FAIL 仍拒绝。随后 source 首次安装成功。

## 唯一收口与独立核验

- 同窗唯一 **cleanup PASS → after FAIL → abort → INVALID decision**；顺序按原件时间独立核验。after 没有 fresh before，0 库、无 snapshot/comparison；这个 FAIL 保留，不重试。
- 历史 **390,258** 文件（覆盖此前 390,197）与 **27** 份归档 source、**96** 份材料原字节保持；builder/verifier **1/1**；题数/层级 **42@T3 / 31@T3 / 42@T2** 不变。
- 历史 Am11 三流、Am10 六条候选流及 coordinator 原件独立复查；旧扩展 scan FAIL 和 Am10 coordinator CHILD_NONZERO 仍保留。没有把“审计采集完成”写成“所有隐私检查 PASS”。
- 当前候选/任务 runtime、临时文件、TLS、UUID 容器/卷/网络/镜像/service 残留 **0**；独立 auditor 确认退出。Gateway **3×HTTP 200 / ok / read_only=true**。
- 完整 NAS/index 不变性仍 **UNKNOWN**；历史 services/config false 未清除。未改生产、NAS/index/alias/config 或 WeKnora core。
- 源码机械复杂度（非实测基准）：A 组件/备份恢复步骤/升级步骤 **1/4/6**；B **2/3/7**。不能用它替代正式指标或决定选型。

## 本地验证与边界

初始 full-bank 修复：278 回归、7 行为破坏检出；补齐历史判据 source 绑定后：280 回归、另 3 行为破坏检出，恢复全绿。源码/公开工件凭据与私有字段检查、闭集 Schema 负例、AODW/governance 另见 QA。未宣称跑完整 make ci，未宣称第三方 Agent 评审。

判据：容量超限、完整 bank 接线、独立 scan、PID/SYN_SENT、历史 hash/原 FAIL 均有会失败的测试。评审：父会话审查两层含义、历史判据与当前门隔离；没有独立 Agent 工具，不冒称已做第三方 Agent 评审。读产出：父会话复核公开回执、唯一裁决、全 null 指标及真实终态；OPS 另起只读审计进程重算。

## 公开证据

- [当前门容量诊断](amendment13-bank-diagnostic.json) · [独立终态](amendment13-final.json) · [唯一裁决](amendment13-decision.json)
- [两层历史保留](amendment13-historical-preservation.json) · [部署与原失败保留](amendment13-deployment.json) · [未放行状态](amendment13-readiness.json)
- [初始源码 QA](amendment13-source-qa.json) · [历史判据修复 QA](amendment13-history-source-qa.json) · [最终 QA](amendment13-qa.json)

技术标识：source `982da5492cfcd7c56719c5e29f1fc075dbab4983`；migration `a5769e70-da2f-447a-84ea-6ba1c21a2eca`；window `be602a43-c84f-4d02-94fa-10cdb7ad268b`。本地提交；不提交 runs/私有材料，不 push/merge/清 worktree。后续若继续，需要新的明确授权和新的 source-bound 当前门；不得重用本次已失败的 gate/controller 或重开本窗。
