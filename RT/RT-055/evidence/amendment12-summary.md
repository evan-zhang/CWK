# RT-055 Amendment 12 — 历史归因 UNKNOWN，fail-closed INVALID 收口

## 结论与停止点

**Amendment12 为 INVALID / INVALID_CLOSED；RT-055选型目标未完成，不是A/B质量NO-GO，不允许切流。**
Amendment11留下的单次external socket无法严格归属于授权denied canary，也不能排除其他外部连接。外部数据传输仍UNKNOWN；没有把“EPERM已出现”推成“那一次观测必然安全”。

- 源码修复提交：`6c524bc10369464f0a8cf50d1380504deb64f9aa`，起点`c9accd531970e9b0e1ca400d26fe5f09572690a2`。
- 新migration：`7f3a28e9-195f-4d5e-a4f0-5451b10fdb43`；仅归档与迁移隔离实验执行器源码，**执行门保持关闭**。
- 新terminal-only window：`999c334f-5018-4d83-b571-b406f49fe605`；archive：`5677597f-1963-4bee-a5b8-939b1844da17`。
- OPS新public privacy **0次（上限1，前置归因门已失败）**，workload/readiness/before/freeze/随机顺序/no-Popen formal precheck均未跑；A/B attempt=0/0，逐库arm/exposure/query/score/complete/result全0。没有以重跑取PASS。

## 只读诊断，证据边界

独立读取17份OPS合成JSON回执，复核2个workspace/3条日志流firewall、EOF、close、归档清单和独立postscan。原观察为108次socket采样、external=1、observer_errors=0，3类进程均有loopback listener。sidecar主动拒绝回执errno=1，Java/network探针也通过。

但是Observer只保留汇总，不保存该次事件的PID/phase/state/target；没有可关联的传输计数。3份进程receipt只证明进程存在，不能替代socket事件绑定。结构化关联证据键为0。**严格因果归因UNKNOWN，硬门失败码HISTORICAL_SOCKET_ATTRIBUTION_UNKNOWN**。旧Am11窗永久INVALID；旧Am10 REQUEST_FAILED根因仍UNKNOWN；所有旧claims/migrations/windows/after只读。

## 前向最小修复（本地验证，不冒称OPS隐私PASS）

- 候选sidecar不再执行主动外连；旧/privacy-probe即使带deprecated开关也返回404。
- 主动connect-only探针改为精确sandbox policy下的独立PID；只允许固定公开目标1.1.1.1:443、父进程记录的探针PID绑定、一次connect、EPERM/EACCES、无peer、专属FD/TCP/CLOSED和无应用payload调用/字节的闭集回执。UNBOUND只在这个精准绑定的专用probe中可作为拒绝证据，不能用于候选豁免。SYN_SENT、ESTABLISHED、UNKNOWN、其他目标、其他FD、非TCP都失败。
- 三类候选进程全程被动观察，任何非loopback仍计external且要求0，包括canary目标、UNBOUND及SYN_SENT。保留OPS PID职责、采样phase/state/scope/count；非公开端点立即丢弃，公开不导出PID/私有值。LISTEN改为实测状态判定。
- 源码绑定重算专用探针receipt，改PID、改状态、加字段、删receipt或只改hash均拒绝。未改candidate算法/quality/seed/tier/floor/题池/42-31-42/7200/A-B对称timeout、WeKnora core、production/NAS/index/alias/config。

本地红测24项中1fail/5error；修复后RT055 **272项通过、0fail/error/skip**，142编译通过。3个真实行为破坏（忽略候选SYN_SENT、断掉probe PID绑定、恢复候选主动探针）全部被测出。macOS真实sandbox的独立probe本地测试通过，但这不是完整OPS三进程privacy gate。无应用payload是connect-only路径与EPERM/无peer的窄结论，**未声称网卡包计数测量，更不反推旧传输为0**。

## 同窗唯一收口与独立终审

新窗口只用于收口，不抽顺序、不消费题池：cleanup唯一调用PASS → 原after唯一调用FAIL（没有fresh before，0库，无snapshot/comparison）→ append-only abort与唯一INVALID decision。没有补造before、借旧before或重试after。

- 本窗120项正式指标、24项Gateway能力全部null，不以synthetic或未测0替代。
- 本窗before/after comparison=null；完整NAS/index不变性UNKNOWN。历史services/config的false原样保留，未被新健康检查抹掉。
- 新formal/runtime日志NOT_RUN，不靠空集合声称firewall PASS。独立重扫旧Am11三流与旧Am10 B六流：原合同firewall/postscan通过；旧Am10 coordinator保留verified=false/CHILD_NONZERO，postscan0，不改写原失败。
- 首次独立审计在LOG_FIREWALL失败，原件保留。额外用全部私有字符串扫描旧合成日志，有1文件/1字符串命中，该字符串也为公开源码精确常量；因果仍UNKNOWN，不直接定为泄露，也不豁免。原公开canary扫描0与扩展扫描FAIL分开记录。后续只补齐终审采集，不重跑privacy/candidate/baseline，不把终审采集完成写成所有隐私检查PASS。
- 独立终审复核历史390197文件（覆盖原390079文件）、27份归档源码、96材料字节保持；builder/verifier仍1/1，题池42/31/42不变。
- 相关task/runtime/tmp/TLS及精确UUID容器/卷/网络/镜像/service残留0；Gateway3×HTTP200、ok/read_only=true；审计进程确认退出。
- 采用独立审计脚本、主执行者交叉复核和产出阅读，不冒称第三方Agent评审；没有全仓CI声明。

## 20项正式指标 × A/B × 三库

以下null表示未测，不是0。

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

Gateway四能力（HTTPS query API、每Gateway身份、服务器端KB grants、无直连NAS/search凭据）×A/B×三库共24格全部null。机械复杂度只来自未变source-bound runbook数组计数：A=1/6/4，B=2/7/3；不是本窗运行基准，未创建新freeze。

## 公开证据

- [历史归因只读诊断](amendment12-diagnostic.json) / [Schema](amendment12-diagnostic.schema.json)
- [源码红绿与行为破坏](amendment12-source-qa.json) / [Schema](amendment12-source-qa.schema.json)
- [新migration归档与源码绑定](amendment12-deployment.json) / [Schema](amendment12-deployment.schema.json)
- [真实硬门停止点](amendment12-readiness.json) / [Schema](amendment12-readiness.schema.json)
- [独立最终审计与全部null指标](amendment12-final.json) / [Schema](amendment12-final.schema.json)
- [唯一INVALID裁决](amendment12-decision.json) / [Schema](amendment12-decision.schema.json)
- [旧失败/日志/字节保持](amendment12-old-preservation.json) / [Schema](amendment12-old-preservation.schema.json)
- [首次终审失败与扩展扫描边界](amendment12-audit-scan-diagnostic.json) / [Schema](amendment12-audit-scan-diagnostic.schema.json)
- [最终QA](amendment12-qa.json) / [Schema](amendment12-qa.schema.json)

只提交公开源码/测试/协议/证据；不提交runs或私有材料，不push/merge/清worktree。Amendment12执行分支已收口，但选型主任务未达成；后续不得把本地修复或新PASS当成对旧UNKNOWN的豁免。

最终本地证据QA：9份闭合Schema、1289项负例拒绝；公开字段/摘要与凭据值扫描0；162旧公开文件与3文档前缀保留；142编译、235链接/31锚点通过。这里的QA通过仅表示失败被如实记录，不改变运行隐私门失败或扩展扫描FAIL。
