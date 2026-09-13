# RT-055 Amendment 11 — INVALID 收口，选型主任务未完成

## 唯一结论

**本修订 INVALID / INVALID_CLOSED；不是 READY，不是 A/B 质量 NO-GO，也不允许切流。**
确认并修复了公开可复现的原生 `finalizing` 状态合同缺口；Amendment 10 私有 `REQUEST_FAILED` 的因果根因仍为 **UNKNOWN**。本次没有正式 A/B 成绩。

- 起始本地 HEAD：`010801c98c352324b304f6885e0b7c2f6f998cdc`。
- 新源码提交：`fdfcfb6dfef57862cf5efd478e88820419e7c322`。
- 主 run：`ac1ca0c7-6983-4f6e-91ce-8eb45e7673af`；新 migration：`aee2e2eb-4cd7-4f97-a864-5ce6c3782529`。
- 新 window：`3330a46e-bfdd-4639-bb0c-ea5c1b3df622`；archive：`feb9590b-8322-43ef-8567-808d14e2cd21`。
- 旧 ead/89ed migration 与 be3/7652 window 永久只读；无旧 coordinator 重启、候选重放、after 重试。

## 诊断和修复边界

只读日志分类前先核对旧6流firewall、归档清单及独立postscan0；只输出封闭码/计数/布尔。旧B首库 imported30/completed29/pending1/failed0、IMPORT、未耗尽7200秒。三次 MISSING_TABLE 标记不能证明因果；没有捕获到具体响应子型或 finalizing 状态。**旧根因 UNKNOWN，不把新公开复现反推成私有证据。**

固定公开 upstream `8d7298fb5d759973cb1e481cadc5ecdf16dca599` 的 `internal/types/knowledge.go`（43–69行）及 `internal/types/interfaces/knowledge.go`（305–314行）定义：finalizing仍有附加子任务，最后子任务才晋升completed。旧适配器把这个合法状态当成 CandidateError/REQUEST_FAILED。最小修复让它继续轮询同一ID，未把它当completed；completed前禁止下一POST，terminal失败和统一deadline原样保留。没有改WeKnora core、算法、质量门、题池、seed/tier/floor、7200或A/B对称门。另把既有常量异常分成闭合请求错误码，不导出HTTP body或私有值，不新增重试。

公开红测：串行16项中1 fail/3 error；诊断12项中3 fail。修复后28项全绿；断掉finalizing处理的行为破坏被3项检出，恢复后4项全绿。RT055回归265项、0fail/error/skip；142编译通过；源码提交前AODW/governance通过。不是全仓CI声明。

## 实际硬门及停止点

新source部署与全集内存firewall容量预检通过。首次公开隐私验证：正常调用、认证错误canary、日志净化、telemetry关闭、外网拒绝探针、forbidden_reads=0均通过；**loopback_models_only=false**。

- socket采样108，external_socket_observations=1，observer_errors=0；三种进程及loopback listener均有观测。
- 该计数表示lsof端点字符串未匹配现有loopback判据，**不能证明已发生外部数据传输**。该观察器回执未保留可用于继续定性的端点原文，子型保持UNKNOWN；不猜来源、不豁免硬门、不放宽sandbox/egress。
- public privacy唯一1次；公开workload实际0次（源码绑定预授权最多1次，但前门失败），没有重跑取PASS。
- main runtime/workspace/scoring readiness、正式before、freeze、随机顺序、no-Popen coordinator precheck和正式coordinator全部未执行。A/B attempt=0/0；arm/exposure/query/score/complete/result逐库全部0。builder/verifier仍1/1，42/31/42不变。

## cleanup → after唯一尝试 → abort/decision

失败前新window尚未创建。仅将本次预留的新UUID建立为**终态收口窗口**，不开放readiness、不抽顺序、不创建候选。

1. synthetic controller已cleanup成功；同窗正式cleanup再次按所有权检查，failures=0、无临时数据面。
2. 按本次收口要求调用原after实现**仅1次**。由于隐私前门失败，fresh before合法地从未创建，原实现立即拒绝：after FAIL / RuntimeError，libraries_measured=0，无after快照、无comparison。没有借旧before、补写before、重试after或把失败冒充测量完成。
3. 追加abort与唯一INVALID decision，原因 `PUBLIC_PRIVACY_EXTERNAL_SOCKET_OBSERVATION`；closeout `INVALID_CLOSED`。formal状态中的INVALID是终态追加记录，不是coordinator运行回执（coordinator=0）。

**本窗before/after不变性不可测，comparison=null；完整NAS/index仍UNKNOWN。** 历史各窗comparison原字节保留，services/config已有false未抵销。独立最终Gateway3×HTTP200、ok/read_only=true，仅证明当时健康，不补足NAS/index测量。

## 独立复核

- 新公开privacy日志：2个workspace、3条流，firewall/EOF/close/归档清单/postscan及独立scan通过；没有正式日志（正式未运行）。
- 旧Am10 B6流firewall verified/scan PASS、独立命中0；coordinator verified=false/CHILD_NONZERO、postscan/独立scan0，原失败布尔不回填。
- 历史 `390079` 文件、旧窗文件集合、27个归档源码与96材料字节核验不变；builder/verifier1/1。
- 新窗账本各项全0；task/runtime/tmp/TLS/精确UUID容器/卷/网络/镜像/service残留0；独立audit进程已退出。
- 采用独立脚本复算及主执行者交叉核验，不冒称第三方Agent评审。公开最终Schema/negative/privacy/credential/回归/链接/AODW/governance结果见QA。

## 全20项正式指标（均不可测）

以下null不是0；没有用公开synthetic的时间/资源/质量替代正式结果。

| 指标 | A CWork | A 投前 | A SPBP | B CWork | B 投前 | B SPBP |
|---|---|---|---|---|---|---|
| answerable_count | null | null | null | null | null | null |
| answerable_system_error_count | null | null | null | null | null | null |
| build_seconds | null | null | null | null | null | null |
| exact | null | null | null | null | null | null |
| exact_count | null | null | null | null | null | null |
| exact_hits | null | null | null | null | null | null |
| exact_system_error_count | null | null | null | null | null | null |
| index_bytes | null | null | null | null | null | null |
| leak_count | null | null | null | null | null | null |
| no_answer | null | null | null | null | null | null |
| no_answer_correct | null | null | null | null | null | null |
| no_answer_count | null | null | null | null | null | null |
| no_answer_system_error_count | null | null | null | null | null | null |
| p95_ms | null | null | null | null | null | null |
| peak_rss_bytes | null | null | null | null | null | null |
| recall_at_10 | null | null | null | null | null | null |
| recall_hits_at_10 | null | null | null | null | null | null |
| system_error_count | null | null | null | null | null | null |
| timeout_count | null | null | null | null | null | null |
| total_count | null | null | null | null | null | null |

Gateway四能力（HTTPS query API、每Gateway身份、服务器端KB grants、无直连NAS/search凭据）逐库A/B共24格全部null。机械复杂度来自未变的source-bound runbook数组计数：A=1/6/4，B=2/7/3；不是本窗运行基准，本窗未创建新freeze。

## 公开证据

- [只读旧故障诊断](amendment11-diagnostic.json) / [Schema](amendment11-diagnostic.schema.json)
- [公开源码红绿与回归](amendment11-source-qa.json) / [Schema](amendment11-source-qa.schema.json)
- [公开privacy硬门](amendment11-public-privacy.json) / [Schema](amendment11-public-privacy.schema.json)
- [实际readiness停止点](amendment11-readiness.json) / [Schema](amendment11-readiness.schema.json)
- [独立最终审计与120指标/24能力](amendment11-final.json) / [Schema](amendment11-final.schema.json)
- [唯一decision](amendment11-decision.json) / [Schema](amendment11-decision.schema.json)
- [旧原件与日志最终复核](amendment11-old-preservation.json) / [Schema](amendment11-old-preservation.schema.json)
- [最终QA](amendment11-qa.json) / [Schema](amendment11-qa.schema.json)

只提交公开源码/测试/协议/证据，本地clean；不提交runs或私有材料，不push/merge/清worktree，不改production/NAS/index/alias/config。**Amendment 11已收口，RT-055选型目标仍未达成；无后台任务，不自动新开窗口。**

最终本地QA：8份Schema/1333项负例拒绝、隐私字段/摘要与凭据值扫描0、145旧公开文件及3文档前缀保留、142编译、222链接/31锚点通过；265项RT055回归0fail/error/skip，AODW与998文件治理通过（1项既有宿主handover-pack缺失告警）。未声称全仓CI。
