# RT-055 OPS 接管验收：INVALID（2026-09-11）

> 最新状态（2026-09-12）：Amendment 3 第四轮已按新增授权执行，在原生隐私门未建立时 INVALID 收口。三库均参与、无延期；未冻结，正式 A/B 与 holdout 消费均为 0。清理及 after 完成，无遗留 OPS 后台任务；生产切流暂停。当前事实见文末第四轮收口，前三轮历史不改写。

## 唯一裁决

**INVALID。不是质量 NO-GO，也没有选出 A 或 B。**

接管后修正 verifier 的输入接线并按第一道门实际运行。六类覆盖通过，但 RT-054 pool 排除检查失败，按授权纪律立即停止实验链。没有生成 freeze，没有正式运行任一候选，也没有为了取 PASS 修改抽样或重跑。本 RT 的选型目标未完成，不关闭 RT。

## 运行边界与接管核验

- 依据 Evan 2026-09-11 15:35 的 §6 授权和 18:46 接管指令执行；工作树基线 `24a2474`，启动时干净。
- OPS 私有实验根目录权限为 0700。builder 已完成，不重新采集、不重新抽样。
- verifier 原来只有 `--mode`，把三个输入错误地定位在自身目录。只增加受限的显式输入参数，让它读取 builder 原位文件；没有移动或复制私有材料，没有修改校验算法或候选代码。原脚本备份留 OPS。
- verifier 以独立后台进程运行并将状态增量写在 OPS；SSH 返回不承担任务生命期。未因会话或 SSH 状态终止任务。
- 没有启动新的模型下载。接管实查显示缓存已完整、没有未完成下载，A/B 均已有冒烟成功回执，与较早交接快照不同。
- 冒烟不计入正式结果：A 三库合计 9 次试验无系统错误；B 三库合计 9 次试验无系统错误，但三次无答案均未答对。B 的运行成功不等于质量通过，不能据此裁决正式候选。

## 第一阶段实际结果

- 三库分别为 42 题，共 126 题。
- 各库六类计数均为 10 / 8 / 10 / 4 / 5 / 5，覆盖检查通过；全部类别分母非零，校验拒绝数为 0。
- RT-054 **重建题池**交集计数：cwork-3m 为 4，docdb-touqian 为 16，spbp-2027 为 15。
- `rt054_pool_excluded=false`，`input_disjoint_verified=false`；verifier 退出码为 3。
- 限定证据含义：现有 verifier 以当前快照重建 RT-054 pool，未打开或带回 RT-054 final holdout。这不是对历史实际 pool 的完整溯源证明；当前重建检查已经失败，足以禁止继续，不能把它说成历史实际 holdout 的精确重叠数量。
- builder/verifier/implementer 的固定角色名与分目录存在，但没有找到足以独立证明三角色分离的运行材料，相关正式证明记为 `null`，不接受聚合脚本的硬编码成功值。

## 正式 A/B、冻结与聚合

- A 正式运行：未启动；B 正式运行：未启动；正式资源创建：0。
- holdout 未被候选消费。私有验证产物仅为审计材料，不是已通过隔离门、可继续消费的正式 holdout。
- freeze receipt 和冻结顺序均未生成。正式计数、比率、P95、索引体积、构建时间、RSS、机械复杂度和 Gateway 四能力没有实测值，全部为 `null`；不把冒烟数字搬入正式报告。
- 原 OPS 聚合装配器实际调用后因缺少 freeze 前提退出 1，未生成合法实验报告；没有绕过其前提或伪造冻结材料。
- `aggregate-report.v2.json` 使用 v2 的闭集字段形状记录中止现场，**不是 schema-valid 的完整 v2 实验结果**。未知值保留 `null`，未通过的证明保留 false；v2 Schema 对这些值拒绝是预期结果，不能为了生成 PASS 而改成零或 true。
- OPS 和本地决策 harness 均退出 2，唯一结果 `INVALID`。本地 `decision.json` 是真实命令输出；其首个拒绝项是未完成运行前冻结，业务停止原因仍是前述 pool 排除失败。

## 清理与生产不变性

- 原 cleanup 脚本已实际执行。首次发现 1 个清理失败：本轮 Go 模块缓存包含只读目录。
- 在核实归属、实际路径和无活跃实验进程后，仅给本轮缓存目录恢复所有者清理权限并删除；原失败回执保留，另存调和回执。调和后未决清理失败为 0。
- 已复核临时 OpenSearch 数据/日志、B 运行/冒烟数据面、sidecar 虚拟环境及模型缓存、Go 构建/模块缓存均已移除；原生 checkout 下未发现剩余数据、存储、日志或上传目录。
- 本实验服务、sidecar、verifier 和清理后台进程均为 0。未发现待调和的正式索引：正式运行从未启动；冒烟数据面已清空。
- 容器零残留没有独立运行时清单证明；不能把原脚本硬编码的 true 当测量，正式字段保留 `null`。
- builder 快照、候选题集、manifest、baseline、verifier 产物及失败审计材料全部仍在 OPS。没有 freeze 可保留，因为它从未生成。原 cleanup 的“保留完成”字段因同时要求 freeze 存在而为 false，不代表现存私有材料丢失。
- 三 Gateway 8787 / 8788 / 8789 的 `/health` 均为 HTTP 200；进程、命令指纹与 builder 基线一致。
- NAS 只执行只读访问；已覆盖的 NAS 元数据指纹与基线一致。但 baseline 没有独立生产配置指纹、完整现有索引清单或全量原文复核证据，不能据此宣称完整生产不变性全部通过。报告中 NAS 完整不变性、现有索引不变性、生产配置不变性均为 `null`。
- WeKnora HEAD 匹配固定官方 commit，tracked core 无改动且工作树 clean。没有 patch core，没有修改生产服务、NAS 内容、既有索引或配置，没有 push。
- 私有审计、脚本、上游 checkout 与公共 bootstrap 二进制留在原实验根目录；它们不是正在提供服务的数据面。没有删除整个实验根目录。

## 尚未解决的执行器问题

以下来自代码阅读，不冒充已运行的正式失败项；本轮没有继续修改这些逻辑。

1. 抽样器没有在派生前排除历史 pool，verifier 的当前快照重建也不能替代历史 pool 的真实排除证明。新的合规独立 holdout 需要另外恢复任务后建立，不能在当前失败集合上删题凑通过。
2. freeze verifier 会把依赖文件清单当普通 digest 项比较，但 receipt 没有对应同名 digest；query-plan、真实依赖、模型权重和 runbook 的冻结核验也不完整。
3. 原聚合脚本用 health 或运行成功推导 Gateway 四能力，并硬编码角色分离；这些必须改为实际验收或未知，不能靠装配器自证。
4. A 把三实例 RSS 总峰值重复填入各库；B 的构建起点记在首次导入返回后。正式资源计量需要先修正口径。
5. sidecar 代码显式保持在线加载模式，既有日志检查只扫预热文本；没有完整的正式运行 egress、日志和 tracing 门证明。未把私有输入送入候选验证这些缺口。
6. 完整配置、现有索引与容器不变性缺少独立基线或清单证明，不能由其它布尔项替代。

## 验收纪律与隐私检查

- **工程判据**：真实 verifier 拒绝不相交证明；真实聚合器拒绝缺失前提；真实本地 harness 输出 INVALID。RT-055 候选/裁决合成回归 44 项全过，但不代表 OPS 正式验收通过。
- **AI 审查**：主会话完整阅读协议、主要执行脚本、候选/决策代码和 Schema，识别上述证明缺口。未安排新的独立 AI 评审，不冒充已经独立复审；失败停止轮不以补评审继续消费 holdout。
- **读产出**：只读 OPS 的白名单计数、布尔证明、任务状态和清理结果；未阅读或导出私有题目、标签、原文或私有摘要。
- **导出判据**：聚合字段闭集与递归隐私扫描通过；Schema 仅出现中止状态的 false/`null` 所导致的常量或类型拒绝，没有缺失字段、额外字段、自由文本或私有 digest。未导出任何实际候选 artifact digest，因为尚未冻结。
- 本地 AODW 门禁通过（仅既有宿主 skill 未安装告警）；governance 检查 791 个受跟踪文件全部有归属；证据链接、递归隐私键检查和 `git diff --check` 通过。
- 本地提交范围仅为本目录证据和 `rt-lite.md`，不包含 `docs/handover/`，不 push。

## 终态

本轮已经安全停止并完成清理调和、保留审计与失败回写；无后台任务。正式选型仍未完成。下一轮需先解决历史 pool 排除、三角色证据与执行器证明缺口，再按新的明确指令建立独立 holdout；不得沿本集合继续取 PASS。

## 22:13 授权后的“修好重跑”：权威历史池缺失，停止

### 授权与唯一阻塞

- Evan 于 2026-09-11 22:13 授予围绕可信唯一裁决的常设决策权；本次按 22:14 指令接管，基线为 `1c4d609d88fea8fbbe9529693c2bdbe86002acf3`，工作树 clean。OPS 修复审计 run UUID 为 `57c32529-0400-446d-88f9-c410a7c1203e`；这是随机运行标识，不是 case/corpus 或来源摘要。
- **唯一阻塞：没有找到能绑定 RT-054 历史实际题池的权威私有记录或完整来源项清单。** 不用当前快照、固定 seed 或代码重建结果代替历史事实。停止依据是本次明确规定的第一阶段第 3 项，不是再次请求逐步授权。
- 这是“缺少可核验权威记录”的结论，不是声称全机、离线备份或无权访问的位置绝不存在副本。恢复条件是把真实历史题池/完整来源项清单及可核验的历史绑定证据恢复到 OPS 0700 私有位置；不需要把私有内容发回本地或会话。

### 漏排的直接机制

- 实际 builder 的来源顺序生成函数接收全量输入文档；派生题目前没有加载历史 pool，也没有来源项级排除。新 seed 只改变文档顺序，不能让输入集合自动与历史池不相交。
- 实际 verifier 调用 RT-054 generator，以**当前 corpus 和固定历史 seed**重新生成 query 集合，再与 RT-055 的规范化 query 集合求交；它没有打开历史实际 pool，也不是来源项级核对。
- 因而上一轮的 4 / 16 / 15 是“当前快照重建 query 集合”的交集，不是历史实际 holdout 的精确重叠数。失败不是通过删除这些交集或换 seed 重抽可以合规修复的问题。
- 原 builder/verifier 两份实际脚本已在 OPS 私有审计区备份，运行审计保留。未改候选算法，也未在缺少权威输入时先写一个近似排除器来继续实验。

### 权威记录查找与证据边界

- 先检查已知临时位置，再用脱离 SSH 会话的后台检索查找历史运行目录、private pool/manifest、holdout、来源项清单和归档；原检索及调整记录均留 OPS。
- 初始宽范围检索进入了挂载数据面；只停止本次确认归属的两个检索进程组，随后改为 OPS 本地文件系统检索。没有把检索调整当作实验失败，也没有停止 Gateway 或候选服务。
- 最终本地盘检索完成，覆盖 4 个存在的入口，得到 88 个匹配位置；结合历史目录内容，共检查 81 份结构化记录，无解析/大小限制失败。没有找到 RT-054 私有 Schema 记录、非 RT-055 的题目/来源项记录或匹配归档。
- 找到的 5 个 RT-054 留存目录权限为私有，但每个都只有 4 个 Python 字节码缓存；没有历史 runtime ownership marker、原始私有 corpus/candidates/verified/freeze。目录存在或权限为 0700 不能充当题池留存证明。
- 本地盘搜索有 6 个权限拒绝位置，未扩权访问；可读范围与这些覆盖限制均记入 OPS 私有审计。路径、文件名、来源标识和私有 digest 均未导出。
- 仓库历史证据与缺失机制吻合：[RT-054 最终聚合](../../RT-054/evidence/stage-b-ops-quality-20260909.json) 的 `cleanup.case_files_zero=true`、`workdirs_zero=true`、`cleanup_failures=0`；[历史执行器](../../../scripts/kb_stage_b_ops_benchmark.py) 的 finally 逐个删除私有 corpus/candidates/verified/freeze，随后删除运行根目录。这个聚合回执能证明历史清理结果，不能恢复已删除的来源项清单。

### 整体作废、终态与未执行项

- 旧候选题集及其验证副本共 2 份，各自完整 126 题，已在 OPS 整体归档并标记禁止消费；原件与归档件字节级一致。没有删除个别题目、修改标注或重抽。
- 新 holdout 构建 0 次，freeze 0 次，正式 A/B 0 次，正式数据面资源创建 0；未消费旧 holdout，也未下载模型、创建索引或启动候选栈。私有审计保留在随机 RT-055 根下的 0700 目录。
- 终态实查：OPS RT-055 实验/检索进程 0；三 Gateway 的 8787 / 8788 / 8789 均 HTTP 200。没有待清理或待调和的新正式数据面资源。
- 停止发生在第二阶段之前，**本次没有采集完整生产不变性基线/对比证明**，不把“没有执行生产写操作”或 health=200 填成完整不变性 PASS。现有索引、alias、生产配置及 NAS 未执行写入或删除；这些边界陈述不替代逐项比较证据。
- 执行器修复、三角色证明、Gateway 四能力、freeze/依赖/模型/runbook、日志/egress、资源测量和正式决策均未继续。它们是被第一道硬门挡住的后续阶段，不是另外提出的新授权阻塞。
- 当前终态仍为 **INVALID，未选出 A/B，也没有三库新成绩**。既有 `aggregate-report.v2.json` / `decision.json` 保留上一中止轮原件，不另造一个假装 schema-valid 的新报告；RT 保持未完成，不进入生产实现。

### 本地复核

- RT-055 候选与裁决合成回归：44 项通过；只证明现有行为与失败关闭，不冒充新正式实验。
- OPS 与本地对既有中止报告实际重跑 harness，均退出 2、返回 INVALID；本地输出与既有 `decision.json` 相同。Schema 本身自检通过；旧中止报告仍被拒绝，仅有 22 个 const、146 个 type 拒绝，均属原有 false/null 中止字段，没有缺失/新增字段类错误。不能称该中止报告 schema-valid。
- 递归聚合隐私检查、新增文本凭据模式检查、6 条本地证据链接、候选/测试代码未变检查和 `git diff --check` 通过。AODW 门禁及 53 项花名册一致性通过；governance 的 791 个受跟踪文件全有归属。宿主 handover-pack 未安装仍是既有非阻断告警。
- 主会话读取脱敏后的实际抽样/核验控制流，以及 OPS 的结构计数、完整归档比对、搜索覆盖和终态回执；没有读取私有题面，也没有声称新增独立 AI 复审通过。

## 22:53 裁决后的运行前修订（不消费 holdout）

父会话明确认定 RT-054 原始权威池按合同销毁、不可恢复；不再等待恢复数据或逐步批准。新的 [排除权威 R 协议](../experiment-protocol.md#21-排除权威-r运行前修订evan-2026-09-11-2253-裁决) 引用 [原 acceptance 清理节](../../RT-054/evidence/stage-b-ops-acceptance-20260909.md#清理与不变性) 第 74 行及 [quality JSON](../../RT-054/evidence/stage-b-ops-quality-20260909.json) 第 112 行 `/cleanup/workdirs_zero=true`；两处均实读核对。

R 是当前完整快照的确定性排除权威，不是历史精确记录。builder 在来源级进行 doc_id/query/token 三路径排除，verifier 独立回读验证输出三类零交集及成员逐项核销。内容跨 doc_id 迁移且同时改名的理论风险保留；约两天漂移窗口与已检出 4/16/15 重叠，只说明机制有效，不消除理论风险。旧 126 题整体归档作废不动。

[测试证据](r-revision-tests.json)：14 项行为测试先红（10 failures）后绿，两种断开/绕过实际行为的破坏实验都被拒绝，恢复后绿；RT-055 回归 58 项通过。全部为合成数据。修订时新 holdout 构建、freeze、正式候选运行均为 0。本节不是正式实验成绩或 schema-valid v2 结果；下一步在首个独立本地提交后同步 OPS，建立一次全新 holdout。

修订提交前检查：递归聚合隐私、18 条相对链接、diff、AODW 和 797 文件治理审计通过（仅既有宿主 handover-pack 告警）。隔离快车道先执行 2572 项测试；复制出的源码缺少 Git index，导致 3 failures/1 setUpClass error，均属于 Git 元数据检查。为该纯源码临时副本建立 Git index 后，只重跑受影响的 10 项，全部通过；随后三类 smoke 与 AODW/governance 均通过。没有把初次 `make ci` 的退出 2 写成退出 0，也未重复已通过的整套测试。


## 当前 R 修订轮失败收口（23:28 接管指令）

### 唯一裁决与提交边界

**INVALID，原因是 CATEGORY_COVERAGE_INSUFFICIENT。不是 A/B 质量 NO-GO；三库均没有正式成绩。** 前 worker 的模型通道/会话退出不是协议失败原因，也不构成重试资格。本次只完成 after、精确清理、证据和本地第二提交，不关闭 RT。

- 首提交：`45a6080a01f4fb6f3ed1aeaa16e7f2d13dad59d4`，已在建立新池前冻结 §2.1 与行为测试。接管时 HEAD 精确匹配、worktree clean。
- 当前随机 run UUID：`3bf93a6e-be8e-4c4d-835d-f17eb277f8c2`，由 ignored 运行指针读取并核对 OPS 所属根。builder/verifier/R/helper 代码与首提交一致，baseline/角色包装器与本地采集实现字节一致；只返回一致布尔，不返回私有摘要。
- [本轮 abort evidence](r-round-abort.json) 通过 [专用闭集 Schema](r-round-abort.schema.json)。它只记录冻结前失败及收口，不是正式 A/B harness 或可选型报告；禁止自由文本、私有键/值、私有 digest。唯一动态标识为 canonical UUID，首提交为公开协议常量。
- [旧 v2 报告](aggregate-report.v2.json) 和 [旧 decision](decision.json) 与首提交字节不变，仍对应第一历史中止轮。没有把它们改成这轮结果，也没有重新运行正式 harness。原 v2 不接受中止 false/null，因此不为过 Schema 伪造数值。
- 第二提交是此文、rt-lite、本轮证据/Schema/QA 和必要的中止格式说明；提交 ID 在父会话回执给出（避免文件自引用未来提交 hash）。不含 scripts/tests、runs/ 或 docs/handover/，不 push。

### 单次构建、实际六类计数与 R 证明

六类顺序固定为：**标题/文件名、精确编号/日期、正文稀有短语、表格行、无答案变体、近邻干扰**；每库目标为 **10 / 8 / 10 / 4 / 5 / 5**。

- **cwork-3m：42**，实际 **10 / 8 / 10 / 4 / 5 / 5**，覆盖通过。
- **docdb-touqian：31**，实际 **10 / 3 / 5 / 3 / 5 / 5**；精确、正文、表格分别缺 5、5、1。
- **spbp-2027：16**，实际 **3 / 1 / 3 / 3 / 3 / 3**；六类均不足，分别缺 7、7、7、1、2、2。
- 共 89 题。builder PASS，verifier FAIL/exit 3；coordinator 记录 builder=1、verifier=1，独占构建 claim 与已产物 seed 一致。没有重新构建、换 seed、补题、删题或重新执行 verify-cases。
- 各库 doc_id/query/token 三类交集均 **0 / 0 / 0**；2158 个跨库合并 R 成员在每库逐项核销 2158，未核销均 0。各库“当前不存在”成员为 590 / 1918 / 1802，属于核销分类，不是把 R 扩成三倍成员数。
- 独立重建 R 一致、完整来源 snapshot reread 一致、候选共同资格过滤一致、私有材料完整性、输入不相交均通过；题目拒绝 0，所有类别分母均非零。**非零不等于达到目标；R 隔离通过不抵销覆盖失败。** 这些是 verifier 当时的回读证明，不代表其后 NAS 从未变化。
- R 仍只是当前快照确定性权威，不是被销毁历史池的精确副本；迁移来源并同时改名的理论风险保留，不宣称历史语义零泄漏。

### 角色、未执行阶段与正式资源

- builder/verifier 均有独立执行记录、不同 process/execution identity、顺序时序和代码绑定。角色包装器记录禁止跨角色读取 0、候选 imports 0；这不等同于操作系统级隔离或三方完整证明。
- implementer 目录文件数 0；本轮没有新的 implementer 执行材料，**三角色完整分离记 null**，不沿用硬编码 true。
- freeze 不存在，正式输出 0，候选 A/B 正式调用和 holdout 消费均 0；没有第二次正式实验。正式质量、资源、Gateway 四能力均未测量，逐库 A/B 成绩为 null。health=200 不是四能力验收。
- 正式索引、服务、容器、数据面创建计数均为 0：证据依据是实际 coordinator 在 verifier 失败即停止、未下发候选实现、空 implementer、无冻结/正式运行材料，加上精确 UUID 的当前服务/容器/卷/网络/镜像标签清单为 0。没有发出待确认创建请求，无需扫描其它前缀调和。它不是所有远端检索 endpoint 的全局不存在证明。

### before / after：实测相同项、漂移与未证明项

原 baseline 采集器已归档保留，after 使用同一份代码，独立脱离 SSH 运行并写可靠终态。baseline-after 与独立 NAS scope-after 均 PASS，意味着**采集完成，不意味着不变性通过**；未重采 before 或重跑 after 取 PASS。

- **NAS 覆盖口径**：逐库递归 walk_files，逐批 getinfo 核对完整文件集合、size、mtime、isdir；独立 scope 逐目录检查返回行数等于 total，并与 baseline/after 集合求等。before 遍历目录数 613 / 166 / 171，after 为 616 / 166 / 171；各次 scope 均完整且集合对齐。没有以 health 推导 NAS 结论。
- **cwork-3m：不变性 false**。文件 1206 → 1248，新增 42、删除 0、原有文件元数据变化 7；其中既有 index 文件集合仍为 4 个，2 个 size/mtime 改变。其余变化包括非 index 元数据，未导出文件名/路径。漂移原因未归属，不宣称实验导致，也不擅自解释成已证实的生产定时同步；未修复或回滚生产。
- **docdb-touqian / spbp-2027：已覆盖文件集合和元数据一致**，分别 315 → 315、317 → 317；各 4 个既有 index 的集合/元数据一致。
- **Gateway：已覆盖比较通过**。8787/8788/8789 的 PID/命令投影一致，health 去除观察时间后的内容一致，收口实时 HTTP 均 200。每端口 4 条启动脚本/显式配置/可选配置文件指纹及指定环境变量投影一致，共 12 条端口作用域文件记录；三库 kb.json 内容指纹一致。
- **容器：已覆盖比较通过**。实际 docker ps -a/inspect 前后均 4 个；ID、名称、镜像、运行/启动/重启、Config、HostConfig、Mounts 投影一致。并非容器卷内容全量比较。
- **服务清单：false**。launchctl 标签数 537 → 537，但实际集合新增 1、移除 1，均为 Apple 标签；变化不匹配本轮 UUID 或记录的实验 PID。保留真实差异，未把 verifier 自然退出算成生产漂移，也未把未知系统服务变化擦掉。
- **进程边界**：baseline 只记录三 Gateway 和 native search 进程，没有通用生产进程全集。配置指纹中不含本轮 UUID 材料。角色/采集进程只进入实验存活计数，不进入 Gateway 指纹；生产进程全集不变性为 null，不因实验进程最终为 0 就标 true。
- **索引边界**：实际容器镜像筛选出的搜索 endpoint 前后为 0，native OpenSearch 进程前后为 0，此空集合投影一致；NAS index 的范围仅 `_system/` 中名称包含 index 的现存文件，比较的是集合和元数据。未全面探测非该发现规则的检索服务，未读取 alias/mapping 和每个 index 全内容，完整既有索引/alias 不变性为 null。
- **其余 null**：NAS 全文件内容、全部生产配置（含未列入的脚本依赖、配置与环境）、全部生产进程、容器卷内容。文件集合/size/mtime 一致不等于内容逐字节一致。拒绝采集器硬编码 `all_items_measured=true` 的全局含义。

因此：**已测投影整体不变性 false，完整生产不变性未证明**。本轮所有生产/NAS 操作均限只读；不修改既有 index/alias/config，不以本次收口“顺手修复”实测漂移。

### 精确清理终态与审计保留

- 先复核 after/补证任务已退出，相关实验进程 0，再仅在本轮 UUID 根内处理临时文件。6 个根执行器脚本先逐字节复制为 OPS 私有审计源码，再移除可执行入口；8 个派生字节码文件清除。builder/verifier 的可审计实现保留，没有删除 verifier 材料或整个根目录。
- 正式非审计数据面本来为 0，最终仍为 0；精确 UUID 容器、服务、卷、网络、镜像标签均实查 0，未确认资源 0、cleanup failures 0。没有删除其它前缀或既有资源。
- 清理前选定的 **37 份**私有/审计文件在清理后逐字节指纹复核一致，包括新 R、source snapshot、holdout/manifest、独占 claim、回读/verifier、before/after/比较及审计。全部内容和指纹留 OPS。
- 旧候选与验证副本各 126 题的 **2 份整体作废归档**仍在 OPS，与旧原件字节一致；不改标注、不删题、不重新消费。本轮 89 题也写入 INVALID/禁止消费的私有处置回执，保留原材料。
- 收口最终 OPS 相关进程 0；没有活跃可恢复任务。公共中止证据及其 Schema 也留 OPS 一份供父会话核对。未 patch WeKnora core、未下载或启动候选栈、未 push。

### QA、审查与未决

- **工程判据**：RT-055 `test_rt055_*.py` 准确集合为候选 23、裁决 21、R 排除 14，共 58 项，全部通过。只重跑合成回归，没有重新消费私有 holdout。新增中止 Schema 自检/验证，以及坏字段、自由枚举和非 null 正式成绩的拒绝检查；旧 v2 按原有 false/null 合同被拒绝，原 decision 保留。
- **隐私与仓库检查**：递归 JSON 禁键、秘密模式、所有本地相对证据链接、AODW/governance、staged 范围与 `git diff --check` 的最终计数见 [本轮 QA](r-round-closeout-qa.json)。OPS 原始成员、标题、文件名、路径、正文及私有 hash 从未导出；第一次导出器因审计源码空白匹配而在输出前断言失败，修正本地 ignored 投影后通过，未触发任何重新构建/验证/after。
- **AI 审查**：本接管会话核对真实采集/角色/清理控制流与证据边界，不能冒充新增独立复审；第二提交之后交父会话复核。
- **读产出**：只读取 OPS 实际白名单计数/布尔、集合/指纹比较结果与收口回执；真实私有材料只由 OPS 上的白名单投影读取。
- **未决**：cwork 元数据及两项 index 漂移原因、Apple 服务标签变化归属、未覆盖的不变性项目；三角色完整材料以及 freeze/原生日志和 egress/资源计量/Gateway 四能力等正式阶段仍未完成。覆盖硬门已经终止本轮，不将这些列为可以重试的资格，不新建第二次正式实验，不关闭 RT。

**本轮失败收口完成；唯一下一步是父会话复核第二个本地提交。**

## Amendment 3 本地第一提交（2026-09-12）

### 预承诺、迁移与合规边界

本次接管基线 `c86519425e260a45a11a89917cb0c6600d46a11f`，前序协议提交 `45a6080a01f4fb6f3ed1aeaa16e7f2d13dad59d4`。保留已有未提交实现，不 reset/checkout、不重启任何实验。本地第一提交必须先于后续另行授权的第四轮；此次无 OPS/NAS/生产连接、无候选部署、无生产切流、无 push、无 WeKnora core 改动。此前正式候选调用为零；历史冒烟不属于正式 A/B。本修订针对容量证据门，不使用候选成绩重跑取 PASS。

固定 T3=doc_id+query+token → T2=doc_id+query → T1=doc_id，同一 builder run、同一 seed，独立完整派题、首个有效 tier 停止，不重启、不补题、不拼池。六类 floor 3/2/3/3/3/3、总数至少 16；31@T3 满足 floor 不要求目标 42，16 且 exact=1 不满足。T1 仍不足则 DEFERRED，全 DEFERRED 为 INVALID。参与库保持原质量/资源/效用门，延期指标只接受 NOT_RUN_DEFERRED 对象，不参与任何 A/B/GO-NO-GO；全部生产切流暂停。

Schema/harness 升级 v3，v2 原合同可从基线回读，当前入口明确拒绝旧 v2，不自动伪造 tier/角色证明。Schema 负责闭集与 floor 状态，harness 重算轨迹和分母，verifier 重新派出完整集合再逐项比对；Schema-valid 本身不是实测证据。`role_separation_level` 必须是受支持枚举；单 uid 只报告 PROCESS_LEVEL_SEPARATION_SINGLE_UID，不接受 null 或靠固定角色名称自证。

### 真实本地测试记录

- [公开合成证据](amendment3-local-tests.json) 通过 [严格白名单 Schema](amendment3-local-tests.schema.json)。只含固定协议枚举/公开前序提交和数值，不含题面、标识、私有路径、正文或私有 hash。
- 继承 72 项测试通过；新增 24 项初次红为 12 failures/1 error，其中两项误用无表格容量 fixture。仅修正 fixture 后、实现尚未修复时重跑仍为 11 failures/0 errors。该修订前红证据针对继承未提交实现，不冒称前一会话已做 TDD。
- 修复后完整 RT-055 103 项通过、0 skip；中间一次 96 项出现 3 errors，原因是继承 fixture 共用全局 floor 字典被反例污染，改为独立拷贝后通过，不为预期结果放宽生产规则。
- 四项代码破坏（独立临时源码副本，不改断言输入）：tier 选择提前通过→6 tests/4 failures；verifier 选择核验断线→7/2；延期指标拒绝断线→8/1；角色枚举拒绝断线→12/1。均 exit 1，恢复原树后 103 项 exit 0。
- 本地真实子进程验证三角色不同 PID、同 UID、独立 0700 cwd、禁止读取探针和重复 claim 拒绝。合成完整入口验证 builder 只读一次固定输入、verifier 独立回读、同一 claim/seed；空库延期、全延期、假 claim、伪造延期、账本/交集/未核销、错误 tier/删题/加题/混 seed 均有拒绝判据。并非 OPS 实测。

### 未决与判据边界

继承 OPS runner 已完整审阅、保留并作本地修复；原生服务/网络沙箱/日志 canary、二进制来源绑定、全量生产不变性和精确清理未在此轮执行。本地依赖 jsonschema 可用；原生集成没有得到本次授权，状态是 NOT_RUN_NOT_AUTHORIZED，不是环境跳过的 PASS。baseline 对未覆盖的完整内容/索引/配置保留 null，已知漂移保留 false；当前证据不足会令正式 harness INVALID，不允许假填 true。Gateway 实验 HTTPS shell 不等于真实生产部署证明。

本会话做代码/协议自检，未安排新的独立 AI 复审；读过真实 synthetic 产出与旧公开 abort，不读取私有题面。历史 r-round-abort/Schema/QA、aggregate-report.v2.json、decision.json 与基线逐字节不变。R 的历史语义迁移风险及旧 cwork/系统标签漂移仍未解决，不把本地收口当 RT 选型完成。

### 最终本地门禁

- 完整 RT-055：103 tests，0 failures、0 errors、0 skip；四次行为破坏均红，恢复后全绿。decision 反例与 v2 迁移拒绝均实测。
- py_compile：20 个 rt055 脚本及 candidate/decision，共 22 个文件全部通过；3 份 Schema 自检、abort 与新增公开证据验证通过。
- 递归隐私禁键、私有 digest 和秘密模式零发现；33 个提交文件已扫描。37 条本地链接及 7 个锚点通过。6 份历史 JSON/Schema 与基线字节一致。
- AODW：79 fixture、受管 RT 门禁、53 个 RT 花名册通过；governance：822 个 tracked files 全有归属。仅既有 handover-pack 宿主未安装告警，非阻断。
- staged 精确允许列表为 33 文件；不含 runs/、docs/handover/、凭据、私有材料或 WeKnora core。git diff/check 与 staged 范围通过。首提交 hash/提交后 clean 状态由 Git 实查后随交付回执给出，不在提交内自引用未来 hash。
- 本地原生日志/模型/网络/OPS 集成未运行；完整全仓 CI 未重跑，不把局部验收冒充 make ci-full。

## Amendment 3 第四轮 OPS 收口

### 结论与证据

**INVALID / NATIVE_CONFIDENTIALITY_GATE_INCOMPLETE；不是质量 NO-GO，不选择 A 或 B。** 本轮失败链与 finally 收口完成，RT 选型目标未完成，保持 in_progress；全部生产切流暂停。

- [公开中止证据](amendment3-ops4-abort.json) / [闭集 Schema](amendment3-ops4-abort.schema.json)：只有固定枚举、公开提交、UUID、布尔、计数及未测 null。私有题面、原文、标识、路径和私有 digest 不回传。
- [本地唯一输出](amendment3-ops4-decision.json)：既有 decision CLI 实际退出 2，输出 INVALID / AGGREGATE_CONTRACT_INVALID / deferred=[]。abort 通过自己的 Schema，但不是正式 v3 aggregate；CLI 拒绝把未运行材料当作完整聚合。**这不是已经完成 A/B 质量或效用裁决。** 既有 harness 和历史 decision 未修改。
- [本轮 QA](amendment3-ops4-qa.json)，闭集定义为同一 Schema 的 `$defs.qa`。实现未变，复用本轮已完成的 105 项回归与四项行为破坏证据；对新文档和证据另做合同、反例、隐私、链接与治理检查。

### 授权、修复及恢复

00:50 新授权覆盖仅做本地第一提交的阶段停止条件。预承诺为 `2cc386394610bd22f8a833d80304de95651d552e`；唯一必要执行器修复以成功 before 合成路径实际复现退出 3（1 test / 1 failure），只修 after 比较分支缩进，保留独占 claim 和 false/null，再以 `db0d6b66efaeea6b1e67bd33cd26e7d9af58b09c` 提交并同步 OPS。105 tests、0 skip 通过。未改采样、tier、质量门、效用或 WeKnora core。

41 个公开实现文件同步时逐字节核验，清理时归档顶层执行器源文件，最终再次核对 41 个文件一致。32 份旧私有脚本版本及差异留 OPS。第三轮 8 份材料整体封存，最终原件与封存件仍同字节；旧 freeze、正式结果及消费均为 0，未拼接或消费旧池。

setup、builder、verifier、before、隐私预检与 cleanup/after 均以原进程和独占状态恢复。builder=1、verifier=1、before=1、after=1；会话中断后先对账，没有重新派发已完成动作、reseed 或重启实验。

### 真实题池和角色门

参与库为 cwork-3m、docdb-touqian、spbp-2027，deferred=[]。按“标题 / 精确 / 正文 / 表格 / 无证据 / 近邻”排列：

- cwork-3m：42@T3，10/8/10/4/5/5。
- docdb-touqian：31@T3，10/3/5/3/5/5；达到六类 floor 即有效，不补到 42。
- spbp-2027：16@T3 为 3/1/3/3/3/3，精确类不足，正确拒绝该 tier；同一次 build/seed 独立完整派出的 T2 为 42，10/8/10/4/5/5。不是给 T3 补题或混池。

独立 verifier 全部检查通过，拒绝数 0；本地再重算轨迹和合计。三库 identifier/query 交集均为 0；两个 T3 库 token 交集为 0，SPBP T2 token 交集为 **114**，如实保留放宽维度，不冒称 T3 全隔离。每库均核销完整 R 的 2158 个成员，未核销 0；R 是当前快照重建，历史语义迁移风险仍在。

三角色为 3 个实际独立进程、1 个 UID、独立 0700 工作区；3 次真实禁止读取探针通过，禁止读取计数 0。等级仅为 **PROCESS_LEVEL_SEPARATION_SINGLE_UID**，不是 OS 用户隔离或任意 syscall 沙箱。

### 隐私门实测与停止原因

上游固定提交 `8d7298fb5d759973cb1e481cadc5ecdf16dca599` 的可达性、HEAD、clean tree 预检通过；二进制 Go buildinfo、revision、unmodified、sqlite_fts5 标记在 OPS 核验。这些不是后续未发生的正式 freeze 绑定。

现有 sandbox 实测外部连接被拒、loopback 可用；同一策略下 IPv4/IPv6 loopback bind 均成功。未放宽网络策略，也不能据此推断全部原生依赖能运行。原版 synthetic runner 的 A 启动在 300 秒就绪等待后失败：日志有 9 处 BindTransportException、14 处 Operation not permitted，未观察到 started。这是日志出现次数，不是 9 次独立实验。A 冒烟 FAILED，隐私 runner 退出 1。

观察到 search 进程 1，B native 与 sidecar 均为 0；正常 native canary、鉴权后的错误 canary，以及 Langfuse/OTEL 关闭和模型调用仅 loopback 的运行态证明均未完成。对应 false 表示“未证明”，不声称观察到这些功能开启。11 份日志未检出 canary，网络观察无错误且未观察到外部 socket，**但未运行路径没有证据，零检出不等于隐私通过**。不接受硬编码成功值、未鉴权错误探针或 health 替代运行态证明。

因此按硬门 INVALID，不启动 B、freeze 或正式候选；不改 core、不放宽沙箱、不重跑取 PASS。这是本轮缺失的具体证明，不推断所有可能配置下都无法运行。

### 正式指标、清理与 after

- freeze 回执与独立核验均不存在。正式 A=0、B=0、结果文件=0、holdout 消费=0。三库两候选 Recall@10、exact、no-answer、leak、P95、索引量、构建时长、峰值 RSS 均为 **NOT_RUN_PRIVACY_GATE_INVALID + null**，不是零分，也不是 DEFERRED。
- 精确清理本轮已记录 PID 和 UUID 资源，额外移除 7 个工具/缓存子树及 11 个字节码文件。最终相关进程、临时数据/缓存、容器、卷、网络、镜像标签、服务条目均为 0，清理失败 0。
- 96 份保留的私有/审计文件在清理后及 after 后均逐字节一致；私有 holdout、原生日志、预检及审计材料留 OPS。只清本轮拥有的资源，不改旧池或生产。
- before/after 同一已提交采集器各一次，三库完整元数据覆盖的文件数为 1248/316/317；逐库新增、删除、元数据变化均为 0。受测目录集合、索引内容和配置指纹同值。
- 三 Gateway 身份及稳定 health 同值，before、after 和最终即时复查均 3/3 HTTP 200、ok、read_only；4 个既有容器和卷投影同值。
- **服务清单不变性为 false**：539→540，新增 4、减少 3，均不含本轮 UUID，尚未归因、未修复或重建 baseline。受测配置指纹虽同值，按既定规则，服务漂移仍使完整 production_config_unchanged 为 **false**。
- 全部文件字节、全部依赖/进程、卷内容、owner 完整搜索端点登记未覆盖。容器发现的搜索端点为 0，不代表完整端点登记已证明。nas_unchanged、existing_indices_unchanged 保留 **null**，all_items_measured=false；不得从三个 health 正常推出生产全量未变。历史 cwork/标签漂移也未在本轮修复或洗掉。

### 验证三格与交付边界

- **工程判据**：105 tests，0 failure/error/skip；四项已完成的真实行为破坏均红，恢复后 105 全绿。本次实现未改，保留同一代码的实测证据，不重复 OPS 或破坏实验。新增 7 个输入合同反例，拒绝私有键、自由原因、假正式成绩、计数错配、空角色、假 freeze、unknown 提升为 unchanged；不把输入反例冒称行为破坏实验。局部 QA 不等于完整全仓 CI。
- **AI 自检**：主会话审阅真实恢复状态与原生 runner，识别硬编码隐私成功值、未鉴权探针和完整不变性缺口，按缺项停止；未另派独立 AI reviewer，不冒充独立批准。
- **读真实产出**：仅读取 OPS 白名单投影，核对容量轨迹、交集/核销、角色拒读、失败诊断、before/after、清理及本地 INVALID。未读取或回传私有题面、正文和失败样例。

最终仅提交公开中止证据、Schema、CLI 输出、QA 及两份状态文档；8 份历史 JSON/Schema 与 Amendment 3 第一提交保持字节一致。最终提交 hash 和提交后 clean 状态以交付回执、Git 实查为准，不在提交内自引用未来 hash。不合并、不 push、不清理 worktree，不关闭 RT，无遗留 OPS 后台任务。本轮已终止；任何再试须新授权及新协议轮，不能续消费本轮题池。


## 第四轮同一 run 的 pre-freeze 执行器恢复（2026-09-12）

**READY_TO_FREEZE，仅隐私执行器门恢复；不是 A/B 结果。** 父会话后续明确授权覆盖旧“另建协议轮”停止条件，范围见 [恢复授权](../experiment-protocol.md#第四轮-pre-freeze-执行器恢复授权2026-09-12父会话后续指令)。原 INVALID、12 份历史 JSON/Schema、原 before/after 与失败日志全部保留，不把历史失败改成 PASS。

### 根因与最小修复

- 原 JVM 默认 IPv4-mapped IPv6 socket 与 macOS 沙箱规则组合导致 bind 拒绝。只加 IPv4 偏好仍会让该 JVM 的外连探针穿过旧 loopback 规则，**因此没有采用仅加 JVM flag 的不完整方案**。A 现在显式绑定 127.0.0.1，且采用更严的仅入站策略；实际 JDK Socket/NIO 外连均被拒。
- 收紧文件写范围后，OpenSearch 启动器默认临时目录越界；用官方 `OPENSEARCH_TMPDIR` 指向独占目录解决，没有放宽文件写权限。
- 旧清理已删除 embedding venv/模型缓存，公共基座只有锁定依赖清单。合成子目录重新准备该清单和同一公开模型；没有重跑 setup、builder 或 verifier 的原 claim。
- 原生 Go binary 初始化还依赖其旧构建目录中的 jieba 字典。按固定 core 的 go.mod/go.sum 恢复 gojieba v1.4.7 的五份字典，完整 Go module h1 校验通过；只用 upstream 已支持的 `JIEBA_DICT_DIR`，未改 core。五文件进入后续 dependency freeze 清单。
- 启动等待改为检查真实子进程存活；失败启动/初始化会停止子进程并关闭日志句柄。环境观察以实际 JVM argv 为准，跳过 shell bootstrap，不把启动器重写的环境值误判为 JVM 未生效。
- 新恢复回执独立追加并绑定 10 个公开源文件及成功观测。旧 `passed=true` 无运行证据、新源漂移、非法模型 endpoint 或 tracing 环境均被拒；旧网络/失败回执没有覆盖。

### 实际产出与保密证明

三个失败合成尝试均保留：临时目录、缺 embedding 依赖、缺原生字典。第四个独占空合成目录完成全部真实路径：

- A 真实建索引/查询、B 原生摄取/检索各成功一次；真实本地模型调用 **3/3** 成功。
- 有效鉴权先验证；query 和 title 两条错误请求均返回 **400**，不是拿 401 或未执行路径冒充覆盖。
- **13** 份运行日志扫描，正常/错误 canary 检出 **0**；注入 incoming trace context 后，模型请求 trace header 和 native trace response header 都是 **0**。
- 三类真实进程环境、loopback listener 全部观察到；**112** 次按进程 socket 样本，外部连接观察 **0**、观察器错误 **0**。JDK 与 sidecar 的主动外连被拒；非 loopback 模型配置在执行器入口被拒，持久化 native 模型地址实际为本地 sidecar。
- Langfuse/OTEL 关闭由实际环境、成功模型调用及 header/socket 观察联合判定，不是硬编码布尔。socket 采样不冒充全量抓包，操作系统规则与主动拒绝探针提供另一路证据。
- 当前合成执行器主动拒读探针 1 次、禁止读取 0；原三角色禁止读取合计仍 0、0700 权限保持。同 UID 角色分离结论不升级为 OS UID 隔离。

### 清理、保留与停止边界

四个合成尝试已停止，合成服务/索引/导入数据面为 0；移除 **37** 个合成数据/工具子树，失败 0。失败/成功日志和回执继续保留。已批准的公共依赖恢复到同一 run 的私有目录，处于空闲待冻结状态，不是运行中的服务。固定 upstream checkout clean、binary 构建身份与五份字典再次验证。

独立复核 **96** 份原私有/审计材料同字节，七个旧执行器源先归档后更新；源绑定和零消费再核验通过。builder/verifier 仍 **1/1**；freeze、正式 A、正式 B、consumption 全 **0**。精确 UUID 的进程、容器、卷、网络、镜像标签及服务均 0；三个 Gateway 新鲜 HTTP 200。

原 before/after 未重做，既有服务漂移 false 和完整性 UNKNOWN 未消除、未改写；本次通过不抵销正式实验的生产不变性硬门，不关闭 RT，不授权切流。

### 验证三格与交付

- **工程判据**：最初 9 个恢复用例先红（2 failures/7 errors）；扩展至 18 个恢复用例后，完整 RT-055 **123 tests、0 skip**。8 次真实代码破坏分别断开 JVM flag、仅入站策略、endpoint guard、telemetry guard、日志检出、鉴权错误门、sidecar trace 观察、源漂移拒绝，均检出；恢复后 123 全绿。一次 fake model 数组形状错误只修测试 fixture，不算生产故障修复。
- **AI 自检**：本会话检查了“仅加 IPv4 会放行 JVM 外连”、未执行路径空日志、shell bootstrap 误判、清理删除公共依赖和 native 字典的真实断点；没有独立 AI 审批，不自称通过父会话验收。
- **读产出**：读取白名单运行计数、真实 native 错误状态、环境/socket/header/log 观测、依赖 h1 校验和独立字节比较布尔；私有题面、标注、正文、运行路径和私有 digest 留 OPS。
- [公开恢复回执](amendment3-ops4-recovery.json) 与 [闭集 Schema](amendment3-ops4-recovery.schema.json) 不是正式 aggregate。10 个反例拒绝私有字段/自由 reason、伪正式运行/freeze/消费、401、空扫描、外连、tracing 和字节漂移。原候选/抽样/tier/效用及 aggregate Schema 七项受保护文件字节不变。

收口机械核验：13 个暂存文件与磁盘逐字节一致，8 个 Python 文件解析、公开 Schema、OPS 投影逐字段比对、52 条相对链接/10 个锚点、隐私/秘密扫描均通过；恢复回执送入原 A/B CLI 实际退出 2/INVALID，正确拒作正式结果。AODW 门禁及 53 个 RT 花名册通过，829 文件治理通过；只有既有宿主 skill 未安装告警，未扩权安装。不冒称全仓 CI。

本次只本地提交必要恢复文件，不合并、不 push、不清理 worktree。停在 **READY_TO_FREEZE**，无后台任务；后续正式阶段由父会话明确接续，本会话不会自动 freeze 或消费题池。

## 第四轮正式接续：新基线绑定硬门（2026-09-12）

02:58 的接续授权要求保留同一未消费 holdout、同一 run UUID 和全部历史，同时在恢复后新采 formal-before，并用 09e 的实际代码冻结。此次不把旧隐私失败当成当前失败：恢复隐私门仍通过。新问题是 **09e 的 freeze 没有新基线入口**。

### 发现与选择

- OPS 对账确认 96 份原材料字节一致，builder/verifier=1/1，freeze、正式结果、消费和候选进程均为 0；三个 Gateway 为 200。22 份已部署公开实现与 `09e908b19360e08f1ae05b7a3a7ff888127997b6` 一致。原恢复回执绑定了文件字节但没有最终提交号，本次另追加提交来源回执，不覆盖原 provenance。
- [baseline](../../../scripts/rt055_baseline.py) 只有 before/after 两个入口，旧 claim 已存在，再执行会被独占创建拒绝。[freeze](../../../scripts/rt055_freeze.py) 的创建与复核都固定要求旧 `production-before`；只新增 formal-before 文件并不能使它绑定新窗口。
- 修改 freeze 的这段接线又会改变[隐私恢复绑定](../../../scripts/rt055_runtime.py)覆盖的源码字节。合成检查实证：旧 claim 拒绝第二次调用且原件不变；只有新 formal-before 时 freeze 无法创建 receipt；仅改 freeze 源就使原隐私恢复门拒绝。
- 本次按“使用 09e 实际代码、保留原绑定、禁止替换旧基线”的严格解释停止 freeze。**这是本次边界下的执行器接线冲突，不是声称该缺陷不可修复，也不是候选质量失败。** 没有偷偷改 hash、重绑未重验的隐私证据、改名替换旧文件或临时 monkeypatch 冻结逻辑。
- 只读收口控制器复用原采集函数，给此次 before/after 使用新的独占 claim 和私有输出；两侧同样补采目录元数据。它不执行 builder/verifier、freeze 或候选，不写生产。旧 before/after、abort、恢复回执及 96 份保留材料继续按原位置核验。

生产选型仍未完成；正式指标只能记未运行，不能用合成测试、空值补零或旧 aggregate 拼出 v3 成绩。后续修复至少需要统一 before/freeze/verify/aggregate 的窗口引用与 append-only 隐私来源迁移；本次没有实施这项迁移，也不擅自再次认领窗口。

### 实际收口结果

**INVALID / FRESH_FORMAL_BASELINE_BINDING_CONFLICT**。新 formal-before、formal-after 各认领一次并 PASS，收口控制器和低频 watcher 均退出。OPS 先通过[闭集中止 Schema](amendment3-ops4-formal-abort.schema.json)，才导出[公开报告](amendment3-ops4-formal-abort.json)；没有导出私有题面、标签、正文、标题、文件名、路径、locator 或其 digest。

- **参加库/题池**：cwork-3m T3/42、docdb-touqian T3/31、spbp-2027 T2/42；deferred=[]。这些是保留题池数，不是正式运行分母。
- **A/B 正式结果**：三库两候选的十二项计数、Recall@10/Exact/NoAnswer、leak、P95、index bytes/build time/peak RSS、运维步骤与 Gateway readiness 全为 `null`，状态 `NOT_RUN_BASELINE_BINDING_CONFLICT`。freeze 顺序、A/B receipt 均为 null；freeze claim/receipt、A/B result、consumption 均 0。没有产生 aggregate v3，也没有进入质量与代价裁决。[原裁决 CLI 的拒绝结果](amendment3-ops4-formal-decision.json)为 exit 2 / INVALID / AGGREGATE_CONTRACT_INVALID——这是正确拒绝中止报告，不是正式质量成绩。
- **隐私/角色**：复用并重新核验上一次 append-only 的真实 native 恢复证据，不重跑 canary。正常 search/native 各 1；鉴权后 query/title 错误均 400；embedding 3/3；112 次 socket 样本，外连 0；13 份日志，canary 命中 0；tracing header 0、forbidden read 0，恢复门全部 PASS。角色实证为 `PROCESS_LEVEL_SEPARATION_SINGLE_UID`，不升级声称 OS UID 隔离。固定 WeKnora upstream 可达、HEAD 匹配、checkout clean，native binary 身份再次通过。
- **新基线范围**：两侧均覆盖三库全部列出的文件与目录之 size/mtime/isdir；文件数依次 1248/316/317，目录数 616/166/171（各含根目录）。两侧均有 4 个既有容器、537 个服务标签；已发现的容器型 search endpoint 为 0，不将它冒称所有可能索引端点为 0。NAS 路径和字节指纹只在 OPS。
- **实测不变性**：Gateway/PID 与健康内容、容器、卷元数据均 true；NAS、已测索引和已测配置投影均 true。服务集合却新增 1、减少 1，`services_unchanged=false`，因此强口径 `production_config_unchanged=false`。只记录漂移，不归因，也不修改生产“追平”快照。
- **未证完整不变性**：采集没有覆盖全部文件字节、卷内容、所有生产依赖/进程和经所有者枚举的全部索引端点，因此 `nas_unchanged=null`、`existing_indices_unchanged=null`、`all_items_measured=false`。投影相同不等于完整不变；即使接线缺陷修复，这些 false/UNKNOWN 仍不能放行切流。
- **finally 与独立检查**：候选进程、正式/合成数据面、精确 UUID 的容器/卷/网络/镜像标签/服务、未确认创建及 cleanup failure 均 0。96 份原材料和历史回执字节一致；22 份公开执行器源码仍与 09e 一致。三个 Gateway 再次为 HTTP 200。未删除旧失败日志/回执，也未移除已批准但空闲的公共依赖。

### 本次公开 QA

123 项 RT-055 回归（0 skip）与 4 项纯合成接线/不变性检查通过；正式数据未参与本地测试。公开 Schema 的 14 个反例拒绝私有字段/digest、自由 reason、伪正式顺序/分数/NO-GO/消费、错误库划分/角色级别/来源、空日志/tracing 和将未证完整性升级为 true。新报告与 OPS 导出逐字段一致，旧 JSON/Schema 和已有文档前缀保持原字节，脚本/测试/正式契约/协议未改。本次没有独立 AI 审批，不冒称父会话验收或全仓 CI。

工具失败如实保留：`apply_patch` 对本 worktree 的绝对路径拒绝且未写入，后用支持该路径的 `edit` 完成追加；没有重试 OPS 的已认领阶段。工程检查、链接/锚点、隐私扫描、AODW 和治理明细见[公开 QA](amendment3-ops4-formal-qa.json)。

本次仅提交公开中止证据与两份文档；不改规则、不 push、不合并、不清理 worktree。**正式接续的中止收口已完成，RT-055 选型目标未完成、三库切流继续暂停；无后台任务，不自动重启。**


## 同一第四轮：formal window binding migration 本地验收

- 03:34 新授权接续；本节不覆盖上文 e71ee34 abort 或 09e908b recovery。独立迁移提交的父提交是 `e71ee346f42378b58fe03dde673412815503857e`；本提交身份由 Git 回读，不在正文自填未来 hash。
- 改动限定于执行器窗口/claim/来源绑定、必要 v3 结构检查、对应合成测试、本协议/验收/公开合成证据与治理登记；不含运行私料、runs、docs 或 handover。builder/verifier/题池/排除集/tier/seed、候选检索算法与质量/效用函数未修改。
- 判据：新窗口真实 baseline→freeze→verification→after→aggregate 路径在公开合成临时目录执行；错 window、旧 before、after 冒充 before、重复 claim、缺新 privacy、源漂移、改 hash 冒充真实 canary、缺消费记录、synthetic/旧结果替代均拒绝。实际 A/B main 也通过合成服务替身执行，验证 claim 在评分前落盘及二次调用不再评分。完整 RT-055 回归、隔离源码行为破坏、恢复 green 与 compile 的可核验结果见 [合成证据](formal-window-migration-tests.json)。
- 工程检查：独立通读修改后的入口与合成产物，核对窗口引用、源绑定、资源采集原边界和失败状态；未使用已退休 reviewer，也不把执行器自报作为父层验收。当前工具面无可委派的独立 reviewer，故不虚称外部评审。
- 读产出：窗口报告采用闭合 Schema，真实装配结果保留不变性 UNKNOWN，并由原规则判 INVALID；这证明绑定和 fail-closed 接线，不代表 OPS 正式分数。旧 JSON/Schema 原件与既有协议/验收前缀逐字节核对。
- 后续仍属本次持续任务：迁移 commit 后再同步 OPS；新真实 synthetic privacy revalidation PASS/精确清理后，才新建 formal window 并推进 freeze→正式 A/B→cleanup/after→aggregate/decision。不得把这次本地通过当作最终交付。

本次本地 QA 实测：142 项 RT-055 回归 / 0 skip；20 个行为破坏全部检出、还原后 142 green；23 个执行源 py_compile；18 份历史 JSON/Schema 与 2 个追加文档旧前缀不变；67 相对链接 / 11 锚点；AODW 79 fixtures、53 RT 与 837 文件治理通过。仅保留既有宿主 handover-pack 未安装告警，未安装或修改宿主配置；未冒称全仓 CI。


## 2026-09-12 唯一继任接管：冻结后评分接线冲突

本节是上一执行会话终止后的实际对账，不覆盖任何历史状态。修复提交 `1af1362b57e639de87a35a9e5c2ce1af7f296da1` 已落库、工作树 clean；迁移测试 JSON/Schema 完整且校验通过。当前继任控制器按 Evan 指定接管，先只读核查本地 HEAD/mtime、OPS 两根、进程、claim、窗口与隐私来源，再在本地 progress card 及 OPS 独占 checkpoint 追加对账。指定的通用 terminal-state/abort-task-state 文件名未找到，采用实际存在的窗口 status、attempt failure、消费 claim 和不可变审计证据；不因旧摘要声称“未消费”而重跑。

### 已完成但上一会话未交付的操作

- 同一 run `ac1ca0c7-6983-4f6e-91ce-8eb45e7673af`、migration `6e70edd0-f77e-4ea9-b0e6-4cdf765970f6`、window `7bed2d1c-4943-4da6-b6d2-a23aab5c195f`：真实合成隐私重绑 PASS，freeze/verification 各一次，随机顺序 A→B，fresh before/after 各一次 PASS；没有重复 builder/verifier（仍 1/1）。
- 迁移版本参数明确传入后，隐私源绑定、实际观测和 freeze 均重新验证通过；默认无 migration 的旧隐私入口拒绝是预期的版本隔离，不是当前隐私门失败。19 份绑定公开源与迁移提交一致，96 份原材料、499 份迁移归档原件均通过字节核验；497 份当前原件不变，另 2 份仅为原控制器可变 progress/state 观察元数据，原字节已经归档。
- A 的第一次执行已领取 cwork-3m 消费 claim；正式查询调用 0、评分回执 0、完整库结果 0，B 从未启动。A 的候选资源已经 finally 清理，after、额外两份新建临时 TLS 文件清理及公开 abort 投影也已完成。**本轮不再是“freeze/消费 claim 均为 0”。**

### 机械原因与不能重放的协议边界

旧冲突是 before 窗口不一致：freeze 固定引用旧 before，而正式接续创建新 formal-before；修改 freeze 又使旧隐私源码绑定失效。`1af1362` 已通过显式窗口引用及带真实观测的版本化隐私重绑解决该冲突。

新冲突独立存在：[A runner](../../../scripts/rt055_run_a.py) 与 [B runner](../../../scripts/rt055_run_b.py) 每次只向 [score_cases](../../../scripts/kb_retrieval_candidates.py) 传一库 case，但 scorer 初始化三库计数，要求每库 answerable/exact/no_answer 都非零。因此首库的消费 claim 落盘后，必然在第一次 candidate.search 前抛出 `CandidateError('missing scoring category')`。继任会话用三库各自的公开合成输入直接调用冻结版本 scorer，三次均复现拒绝且调用数为 0；没有读取或重开私有 holdout。此前 142 项回归含 scorer 替身接线测试，不能发现这条真实评分函数组合缺陷；回归绿灯不代表正式实验可完成。

现行协议“故障与恢复”明确规定：消费 claim 无可核验完成状态时不重放；全 run 消费 claim 不允许换窗口再消费；冻结后源变更无自动再冻结路径。继任会话只读调用实际 `library_result`，机械拒绝为 **consumption_without_completion_no_replay**。零查询的控制流证明不等同于已完成评分回执，也不取消 claim。当前指令保留单次消费且仅允许快照绑定失效时重建；本次 freeze/隐私绑定仍有效，不能借 scorer 错误重建题池、换 freeze 或删题。于是任务停在 **BLOCKED_PROTOCOL_SINGLE_USE_NO_REPLAY**，未修改 scorer/runner、未改协议、未启动新候选。若继续实验，需要明确的单次消费/冻结恢复协议裁决；不是普通“继续”确认，也不是代码问题无法修复。

### 唯一裁决、成绩与清理

- [OPS 公开中止证据](formal-window-scorer-abort.json) 先在 OPS 通过[闭集 Schema](formal-window-scorer-abort.schema.json)，继任导出与原件逐字段一致。原件中 WAITING_REPLACEMENT_FREEZE_AUTHORIZATION 保留为上一会话历史观察；当前任务硬阻塞以本节及 [QA](formal-window-scorer-qa.json) 为准。
- [唯一裁决 CLI 输出](formal-window-scorer-decision.json)：**INVALID / AGGREGATE_CONTRACT_INVALID，exit 2，deferred_libraries=[]**。这是裁决器正确拒绝不完整中止报告，不是质量 NO-GO；没有伪造 aggregate v3，也没有完成质量/代价选型。
- 三库 cwork-3m、docdb-touqian、spbp-2027 的 A/B 正式质量和资源指标均为 null；A 为 NOT_MEASURED_PREQUERY_ABORT，B 为 NOT_RUN。参加题池仍为 42@T3、31@T3、42@T2；这些是题池数，不是已完成测量分母。DEFERRED=[]。
- 最终重查：相关旧控制器/候选/sidecar/OpenSearch/WeKnora 进程 0，实验数据面 0，RT-055 容器/卷/网络/镜像标签/服务 0，未完成下载 0，cleanup failure 0。主根旧 data 目录为空；约 3.09 GB 的旧公开发行物/依赖分布在 downloads、OpenSearch、WeKnora、JDK、Go、bin，作为已有公开支撑材料保留，并非运行中候选或遗失索引，不擅自删除。旧 claim、私有池、freeze、审计和归档保留，未清算他人资源。
- 当前窗口 services 新增 1/减少 1，services_unchanged=false、production_config_unchanged=false；不归因，不修改生产追平。nas_unchanged=null、existing_indices_unchanged=null、all_items_measured=false，明确保持诚实 UNKNOWN。Gateway/健康内容、容器/卷及已测元数据投影相同；三 Gateway 新鲜 HTTP 200，但不升级为全量不变证明。

### 最终验证与交付范围

实际最终树 RT-055 回归 **142 tests / 0 failures / 0 errors / 0 skip**；14 个公开 Schema 反例全部拒绝，三库公开 scorer 探针全部复现查询前冲突。只提交四份公开证据和本节/rt-lite 的追加；旧 JSON/Schema、协议与全部 scripts/tests 保留原字节。没有独立外部 reviewer，不冒称全仓 CI。工程、链接、隐私、AODW、治理和最终提交核验记录在 QA 与本地/OPS append-only checkpoint。

**中止收口与对账完成；RT-055 选型目标未完成，保持 in_progress，切流暂停。无后台实验，不 push、不合并、不清理 worktree。**


### 接管后并发写入警报：本节收口材料尚未提交

在准备最终工程检查时，05:05–05:08 出现另一写入者对六份 scripts 和新 zero_exposure 测试的修改；来源尚未确认，本会话未写这些源码。前述“142通过/0skip”仅针对并发改动前的树，**不是当前最终树回归结果**；“源码未变”仅指本会话，不能用于声明当前工作树干净。本次证据、QA 与文档均为未提交快照，没有最终提交，不构成已完成交付。已停止代码写入、实验和提交，双方改动均保留；未 reset/stash/clean。OPS 最后只读核验仍为原有效 freeze/隐私绑定、A claim1/B0、评分0、实验进程0，尚无 zero-exposure 迁移部署。当前阻塞同时包含独占执行权失效及单次消费协议；需要先排除并发写入者，再作明确恢复协议裁决。本会话无后台任务。


## Amendment 4：zero-exposure scorer recovery 本地验收

05:01 的当前指令已明确零暴露迁移权限，并把终点限制为 READY_TO_RUN。上述旧会话 BLOCKED 和并发警报作为原始历史保留；迟到的六份收口材料稳定后已独立提交 `cb9d2d88a9fc74fe5f8b0b1b2027527fd5d1648a`，未并入任何修复源码，不把其 142 测试作为本轮证据。

- 门前 OPS 只读核验：A/cwork-3m 旧 claim=1、attempt=1；正式私有 query=0、score=0、library result=0；B claim/attempt=0。原始对账明确未重开 holdout；旧 scorer 公开合成三库各自复现首次 query 前拒绝。旧 freeze/源码/隐私绑定重算一致、96 份材料同字节、builder/verifier=1/1、after/cleanup 已完成、无实验进程/数据面。
- 修复只改 scorer 库域合同和账本接线；A/B 入口测试调用真实 scorer，不再用评分替身。arm 不消费；第一个私有 search 前全局独占 exposure，随后 score/complete 强绑定；曝光后失败、外窗重放、缺严格 void 的 legacy claim 均拒绝。新归档也进入隐私拒读矩阵。
- 工程判据：先跑新测试，基线 10 项中 1 failure/8 errors；实现后完整 RT055、严格 void 反例、真实 A/B main 的合成服务路径与行为破坏/还原检查见 [本轮合成证据](zero-exposure-scorer-tests.json)。只测公开合成数据，不宣称 OPS 质量通过。
- AI 自检：本会话逐段检查异常捕获边界、持久化顺序、跨窗全局唯一性、档案隐私、旧源码归档与新隐私来源分离；工具面无独立 reviewer，不冒称外部审批。新增归档隔离是自检发现并修复的路径。
- 读产出：检查原始私有对账的白名单计数/枚举、单库 scorer 实际输出、ledger 原件链及变造反例；私有 query/title/path/doc_id/body/digest 不出 OPS。旧历史文件和已追加文档前缀字节保持。

本地提交不等于 READY_TO_RUN。后续仅按 Amendment 4 完成 OPS 私有 void、新 synthetic 隐私门、新 before 与 replacement freeze；必须另附真实终态回执，绝不执行正式 A/B。

本次本地实测：156 项完整 RT-055 / 0 skip；14 个行为破坏全部检出，恢复后再绿；33 个 Python 文件编译；7 份规则/题池实现/正式 Schema 与 10 个候选类 AST 不变；24 份历史 JSON 与 3 个文档原前缀不变；86 相对链接/13 锚点、7 个公开 Schema 私有字段反例通过。AODW 和治理通过，仅既有宿主 skill 未安装告警；不冒称全仓 CI。详见 [本地 QA](zero-exposure-scorer-qa.json)。

### OPS 私有归档校验的受控模型链接修正

首次归档已成功写入同一独占目录，void 未写入。新验证器错误拒绝了冻结 HuggingFace 缓存的 22 个内部链接；原依赖摘要和旧 freeze 均重算一致，没有 query/score 或源漂移。只允许 sidecar/hf 内的叶文件链接解析到同一缓存内的文件，并保留原相对名与字节摘要；私有路径/账本与跨目录链接仍拒绝。新增公开合成旧 freeze 的真实链接 fixture 先红（15 项/3 errors），修正后 157 全回归及原 14 行为破坏通过。失败校验代码/日志与已成归档保留，不重做归档；只对同一证据重验。见 [链接恢复回执](zero-exposure-model-link-recovery.json)。


## Amendment 4 OPS 收口：READY_TO_RUN（2026-09-12）

**零暴露恢复已完成；停止在新窗口可运行状态，不执行正式 A/B。** 本节是上述本地验收的 OPS 终态追加，不覆盖任何历史 INVALID、BLOCKED、并发警报或旧测试数量。05:44 接续时先读取已完成终态和退出状态，只完成本地证据收口；没有重复 void、隐私实测、before 或 freeze。

### 已核验的状态与不变边界

- 严格原始对账、冻结公开代码复现及旧来源重算均满足零暴露条件。A/cwork-3m 的旧 claim/attempt 各 1，正式 query/score/library result 均 0，B attempt 为 0；旧 claim 原件保留。只追加私有 `VOID_PREQUERY_NO_EXPOSURE`，绑定旧 run/window/claim/freeze/attempt 和归档证据，未将旧 claim 删除或清零。
- 零暴露 migration：`5e3c6265-8cfd-42b9-a2eb-bef592e171cf`。已有归档在受控模型链接修正后复用、重验，未重新制作或覆盖。旧 window/claim/freeze 原字节及 96 份保留材料再次验证一致；builder/verifier 仍 1/1；同一池仍为 cwork-3m 42@T3、docdb-touqian 31@T3、spbp-2027 42@T2，deferred=[]。不重建、删补或重新筛选题池。
- 必要源码来自已提交 `a4b3c64ebb609eee5d08a8c53dc594798f43ec58`（承接 scorer/ledger 修复 `b157150`）。20 个部署源逐字节核验；此后未改变 OPS 冻结源。最后追加的两个并发/跨窗测试及测试临时目录规范化只涉及本地公开 fixture，不属于新的运行源码部署。
- 新 executioner migration：`a82157ee-324b-4806-ae01-3a4675b1f8d4`。独立公开合成路径真实重验成功：search/native 正常各 1，鉴权会话实际验证，两类 query/title 错误均 400；embedding 3/3；13 份日志 canary=0；模型请求/native 响应 tracing header=0；123 个 socket 样本外连=0、观察器错误=0。实际环境、loopback 模型和主动外连拒绝通过；禁止读取=0、拒读探针=1，全部门重算 PASS。不是复用旧 passed 字段，也不冒称 socket 采样覆盖所有网络包。
- 新合成资源已精确清理：进程/数据面/cleanup failures 均 0。独立检查两个控制器均已退出；三个 Gateway HTTP 200。没有更改 WeKnora core、NAS 或生产设置。此时没有后台实验任务。

### replacement freeze 与停止点

同一 run `ac1ca0c7-6983-4f6e-91ce-8eb45e7673af` 下，新窗口为 **`0155202c-b6a0-40c6-a779-48aff0ab57fe`**。fresh before 认领一次、完成三库采集并验证；replacement freeze 创建一次，verification 和随后独立重算均通过；新随机运行顺序为 **B→A**。freeze 绑定新 migration、该窗口新 before、当前已提交源码及未变 holdout。

旧窗口 `7bed2d1c-4943-4da6-b6d2-a23aab5c195f` 仍为 INVALID，只追加 superseded 回执；旧 freeze 不复用。新窗口 **arm=0、candidate attempt=0、正式 query=0、result=0、after claim=0**；全 run **exposure=0**。新 freeze 是待运行承诺，不是成绩；没有生成正式 aggregate 或新的选型裁决。

旧服务/配置漂移未消除，完整 NAS/index 不变性仍未证明；本次没有新 after，不把新 before 或健康检查当作完整不变性证明。RT-055 继续 in_progress，正式评测/选型未完成，所有生产切流暂停。

### 验证三格与证据

- **工程判据**：最终公开合成 RT-055 回归 159 项、0 failure/error/skip。已完成的 14 项行为破坏证据保留，另一次缓存越界保护断线被 1 个失败测试检出；正常源码恢复后全回归通过。补充两项真实行为测试：两个合法冻结窗口不能重放已曝光库；两份 arm 并发争夺全局 exposure 仅一份可进入搜索，截断 exposure 仍拒重放。一次默认 macOS 临时目录别名造成的测试假失败，仅将公开 fixture 根路径规范化，默认环境最终 159 项通过；未因此修改冻结实现或重跑 OPS。
- **AI 自检**：主会话审阅 scope、首次调用前持久化、全局唯一性、void 严格绑定、source/隐私/freeze 链及停止边界；识别并补强“第二窗口本身不合法也会报错”的弱测试，改为两个都合法的窗口。没有独立外部 reviewer，不自称父会话或外部批准。
- **读产出**：父层独立重新核验 OPS freeze、当前隐私绑定、旧 void/归档/原件、无曝光/新 arm/attempt、Gateway 与控制器退出，再导出白名单计数/布尔/枚举/随机 UUID/公开 commit。所有私有题面、标题、doc_id、正文、文档路径和私有 digest 留 OPS。

[公开 READY 回执](zero-exposure-ready.json)、[闭集 Schema](zero-exposure-ready.schema.json)、[最终本地 QA](zero-exposure-ready-qa.json)。结构校验不替代上述 OPS 真实复核。回执不是正式 aggregate：原裁决 CLI 必须以 exit 2 / INVALID / AGGREGATE_CONTRACT_INVALID 拒绝它，这不构成质量 NO-GO。

仅本地提交公开恢复证据、必要公开测试和两份追加文档；不 push、不合并、不清理 worktree。已完成步骤不重复；本轮恢复任务完成并停在 READY_TO_RUN，后续正式 A/B 需要另行明确授权。

## Replacement 正式执行收口：启动前策略硬门 INVALID（2026-09-12）

本节承接 `c5901f4` 的 READY_TO_RUN，仅记录新授权发生的动作；前述 abort、recovery、ready 均原样保留。本窗口已关闭，不能再按 READY_TO_RUN 恢复。**唯一裁决 INVALID，不选择 A/B，也不是质量评测后的 NO-GO。** 本次完成异常收口，正式比较未完成，RT 仍 in_progress、禁止切流。

### 正式动作与故障边界

- run `ac1ca0c7-6983-4f6e-91ce-8eb45e7673af`；replacement window `0155202c-b6a0-40c6-a779-48aff0ab57fe`；冻结源码 `a4b3c64ebb609eee5d08a8c53dc594798f43ec58`。本次没有改 scripts、冻结配置、候选算法或 WeKnora core。
- 只读 precheck 实测 PASS：fresh before、freeze、freeze verifier 各 1；绑定及先后关系有效；同一 builder/verifier 1/1；旧 void 严格有效、96 份材料及旧窗口/claim/freeze 原字节不变；初始 arm/exposure/attempt/query/score/result 均 0，无活跃实验进程，Gateway 3×200。没有重做 builder/verifier、隐私重验、before 或 freeze。
- 冻结顺序 **B→A**。仅启动未发生的 B：attempt=1，A attempt=0。B 在启动 embedding 服务的进程创建前遭 `RuntimeError`；零新增资源记录、零数据面，arm/exposure/query/score/complete/result 均 0。诊断只读状态、固定错误分类及公开执行源码，没有重新打开私有题池作诊断。
- 硬门为 `FROZEN_RUNTIME_POLICY_DRIFT_PRESPAWN`：OPS 主运行的 loopback 与 inbound-only 策略文件均不等于冻结执行器生成的期望文本，缺少新增 exposure/void/zero-exposure-migration 账本保护。进程启动函数在 `Popen` 前拒绝，因此不能按传输错误重试。已冻结源码/配置不允许修改，本次没有修策略、绕过门禁或新建窗口。这里的 drift 指运行策略与执行器要求不一致，不推断发生时间或外部修改者。
- 历史 synthetic privacy PASS 及其源码绑定依然重算通过；它不等于主正式运行策略匹配。冻结工件核验也仍通过，但没有覆盖这两份主运行策略文件。正式网络策略门明确为 false，不能把历史隐私 PASS 写成正式候选已验收。

### 三库与候选指标

三库参与、`deferred=[]`，保留池为 cwork-3m 42@T3、docdb-touqian 31@T3、spbp-2027 42@T2；这些是题池数，不是正式指标分母或成绩。

每一库的 A 都是 `NOT_RUN_PREVIOUS_CANDIDATE_ABORT`，B 都是 `NOT_MEASURED_PRESPAWN_ABORT`。十二项计数及 leak_count、Recall@10、Exact、NoAnswer、P95、index_bytes、build_seconds、peak_rss_bytes **全部 null**，不以 0 充当测量值；没有错误请求可以计入正式分母，也没有 synthetic/历史成绩混入。候选 Gateway 四能力均未测、保持 null，生产 Gateway 健康检查不能替代四能力验收。

冻结 runbook 的机械计数（组件/升级步骤/备份恢复步骤）为 A **1/6/4**、B **2/7/3**，只代表文档机械复杂度，不代表本次运行性能。完整逐库字段见 [公开 abort](replacement-formal-abort.json)，公开合同见 [闭合 Schema](replacement-formal-abort.schema.json)。

### Finally、不变性与保留

在同窗口执行冻结 coordinator 的 INVALID 收口分支：cleanup 一次，formal-after 一次，三库采集完成并与原 fresh before 重算比较。before/after 文件数均为 1248、316、317；未重建 baseline，after 关闭了本窗口的重放入口。

- 候选进程、正式控制器、临时数据面、新临时文件、新 TLS 文件、当前 UUID 容器/镜像标签/网络/服务/卷、未知归属资源、cleanup failures：终态均 0。B 未创建服务，未发生按模糊名称删除资源。
- 三 Gateway 实时 200，Gateway 身份与健康内容未变；容器和卷观测未变。
- NAS 和既有索引的已测投影一致，但完整不变性仍为 **null**；生产配置已测投影一致，但服务列表比较为 **false**，因此 production_config_unchanged 为 **false**。未列出服务名或推断变化责任，未修复生产，未把局部观测扩大为完整证明。
- builder/verifier 仍 1/1；96 材料、旧 window/claim/freeze、私有 holdout、freeze/audit 历史及旧 `VOID_PREQUERY_NO_EXPOSURE` 保留并重算通过。旧窗口仍永久 INVALID。主运行策略字节保持原样；本次没有私有数据或私有 digest 导出。

### 三格验收与裁决复核

- **可运行判据**：本轮完整 159 项 RT-055 回归，0 failure/error/skip；36 个相关 Python 文件编译通过；公开 abort Schema 必须通过、v3 正式 aggregate Schema 必须拒绝此 abort，禁止伪造缺测分数。OPS 冻结 decision CLI 与本地 `kb_retrieval_decision.py` 对同一公开对象均 fail-closed，exit=2、INVALID、deferred=[]，逐字段比对一致。
- **AI 自检**：核对未重复完成动作、未暴露重试、未改冻结 scripts/config、未导出私有数据、仅当前窗口资源收口；159 回归通过并不能证明生产策略一致性，本次真实启动门检出了本地合成判据未覆盖的部署缺口。
- **读产出**：同窗口真实 after/cleanup/abort/decision 闭环，缺测为 null，不变性 false/null 如实保留。公开证据首轮 Schema 将 B 的备份恢复步骤误写成 4，OPS 投影校验拒绝；按冻结 runbook 的真实 3 步修正公开 Schema 并追加第二版投影，失败投影仍在 OPS 留存。没有重跑候选、after、cleanup，也没有修改冻结源码。

最终公开 QA 见 [QA](replacement-formal-qa.json)，唯一裁决见 [decision](replacement-formal-decision.json)。新增私有字段/伪造成绩反例、递归隐私与 secret、历史原件及文档前缀、链接、AODW/governance、Git diff/check 均纳入本轮 QA；保留一项既有宿主 skill 未安装告警，不改宿主配置，不冒称全仓 CI。仅本地提交公开证据和本 RT 文档，不提交 runs、凭据或交接目录，不 push/合并/清理 worktree。

后续若要恢复比较，需要另行授权解决主运行策略部署与验收的一致性；不得把本节当作修改策略或新建 freeze/window 的授权。

## Amendment 5 本地修复：主 run runtime policy readiness

本次为 06:23 明确授权的独立执行器修复，不是重开旧窗口。两份旧主策略各缺四类账本目录的八条保护规则，网络规则未变；源码冻结和 synthetic 隐私 PASS 曾未绑定主策略准备，Popen 前才被拒。生成时间不能只凭 mtime 定论。旧 abort、历史 claim/void 与 96 材料保留。

新增独占、版本化、公开 socket-only 的主 readiness 回执；before 记录真实开始时间，freeze 前验证先后与源/策略/网络/保护范围，freeze 纳入回执与相关文件；A/B 显式 window 传到 spawn 并重算。正式主根不回退旧策略。候选算法、score/质量门、题池/tier/seed 均不变。

- 判据：完整 RT055、pre-freeze 红绿、独立临时源码行为破坏，见 [本地测试证据](runtime-policy-local-tests.json) 与 [Schema](runtime-policy-local-tests.schema.json)。红为旧实现允许缺主 readiness 的 freeze；测试不执行 OPS 正式候选。
- AI 审查：主工程师复核 policy/version/freeze/spawn 接线、先后顺序与历史不变性；本会话无可用独立 worker，不冒称独立 Agent 审批。
- 读产出：检查真实 diff、公开 JSON/Schema 和拒绝分支；OPS 隐私、main socket、freeze、清理与不变性须另附真实证据，本地绿不代表已经 READY_TO_RUN。

第一提交限必要 scripts/tests/protocol/acceptance/rt-lite/公开 synthetic 证据/governance；不含 runs/docs/handover/私有材料，不 push。

本地实测：174 tests / 0 fail / 0 error / 0 skip，12 个行为破坏全部检出，恢复绿。另以真实 sandbox-exec 在公开临时 root 运行 socket 与文件权限探针：两种策略网络判据 PASS，每种策略对八类目录的 8 次读、8 次写均拒绝，unexpected=0；未创建候选进程。编译 128 文件，35 个旧公开证据文件与三份文档旧前缀不变，111 个相对链接/17 个锚点通过，公开私有字段/digest 扫描 0。未冒称全仓 CI 或 OPS 正式质量验收。


## Amendment 5 OPS 收口：READY_TO_RUN（2026-09-12）

公开权威证据：[READY](runtime-policy-ready.json)、[闭合 Schema](runtime-policy-ready.schema.json)、[最终 QA](runtime-policy-ready-qa.json)。只准备，不运行正式 A/B；RT 仍 in_progress，生产切流禁止。前一 replacement abort 全部保留，旧 window 已 after 关闭，不能重新启用。

- 修复源码：`58951f92656e7a29b334461c7aa6d1148fec4e8f`；实际部署、隐私重验和新 freeze 绑定此同一源码。候选算法、题池/tier/seed/质量门/score、WeKnora core 均未改。
- 新 executioner migration：`e5a11f4d-445c-4c05-90b1-2c7e1fbeb0b0`；私有策略归档：`4845680c-366e-4f75-88b0-5355b4153bcc`；全新 formal window：`835c5188-0f29-4f41-8fe6-119b61917e2d`。新 freeze 随机顺序 **A→B**，未沿用旧 B→A 作选择；before/freeze/verification 各 claim1，new after0。
- 真实执行顺序：新源码公开 synthetic 隐私 PASS/cleanup → 主 run 版本化 runtime-policy-readiness PASS → before 的实测开始 → before snapshot → freeze → verification。独立重算源码、两份策略、network gate、八类受保护目录、全部 readiness 文件绑定；两种策略的 spawn precheck 均 PASS，未调用 Popen/候选查询。
- 隐私重验实测：正常 search/native 各1，鉴权错误 query/title 均400，embedding 3/3，13 日志文件 canary0，Langfuse/OTEL tracing0，122 socket samples / external0 / observer errors0 / forbidden reads0。只有公开 synthetic 数据，不是正式质量成绩。
- OPS 独占归档 1,222 份历史工件；旧 policy/network receipt 原件未动，新策略写新 window 的版本目录。旧 window/attempt/claim/void/freeze 原字节与归档重算一致，96 材料不变；builder/verifier **1/1**，三库保持 **42@T3 / 31@T3 / 42@T2**，无延期。
- 旧 replacement 的 A attempt0、B attempt1，process/arm/exposure/query/score/result0；旧 INVALID/after 不重开。新 window attempt/arm/score/result/after0；global exposure0、正式 query0、正式 result0。
- 独立实时核对：Gateway **3×200**；候选/合成/控制器进程0，临时数据面0，新临时文件0、TLS0、当前 UUID 容器/卷/网络/镜像/service 资源0。公开合成依赖目录已清理，审计/源码/回执保留。
- 两次首读 watcher 早于状态文件生成；只复核并继续观察原 detached worker，未重新 launch controller、重做 claim 或任何已完成阶段。

根因已收窄为“主策略仍等价早期三目录模板，freeze 漏绑主策略准备，synthetic root PASS 被误当成主 root 就绪”。实际比旧记录多缺 formal-windows 一类，四类每份八条规则。网络规则没有放宽；现有 mtime 不足以证明具体生成时间，chronology 仍未证实。完整 NAS/index 不变性仍 UNKNOWN，历史服务漂移没有被修复或抵销。最终证据提交后须 clean、不 push；该证据提交身份由 Git/交付回执给出，避免自引用。

最终本地 QA：174 tests / 0 fail / 0 error / 0 skip，12 行为破坏检出，45 个公开 Schema 反例全部拒绝；128 文件编译，35 旧公开证据/三文档旧前缀不变，118 相对链接/18 锚点通过，私有字段/digest/secret 命中0。AODW 与863文件治理通过；保留一项既有宿主 Skill 未安装告警，不修改宿主配置。git diff/check 通过，未声称全仓 CI。


## Amendment 5 正式执行收口：A 启动失败后 INVALID（2026-09-12）

本节是 READY_TO_RUN 之后新授权的正式执行及失败收口，不改写前述准备成功和历史 abort。唯一裁决 **INVALID**；选型没有完成，不是质量 NO-GO，不允许生产切流。公开权威工件：[abort](amendment5-formal-abort.json)、[闭合 Schema](amendment5-formal-abort.schema.json)、[decision](amendment5-formal-decision.json)、[QA](amendment5-formal-qa.json)。

### 执行边界与失败事实

- 从本地 `2c58167fec86720725e0cfd32f1104501e56d2a3` 接续；执行器始终为冻结的 `58951f92656e7a29b334461c7aa6d1148fec4e8f`。run `ac1ca0c7-6983-4f6e-91ce-8eb45e7673af`；migration `e5a11f4d-445c-4c05-90b1-2c7e1fbeb0b0`；window `835c5188-0f29-4f41-8fe6-119b61917e2d`。本轮没有重新构建题池、重做 privacy/before/freeze/verification 或创建新窗口，没有修改脚本、配置、主策略、候选算法、scorer、WeKnora core。
- 先只读核验同窗口 fresh before、freeze/verifier、runtime readiness、隐私来源、源码绑定、历史保留和零暴露；均通过。严格顺序 readiness→before 开始→before→freeze→verification 重算通过，before/freeze/verification 各 claim1。
- 按冻结随机顺序 **A→B**，正常正式 coordinator 只启动一次。A attempt1；这次已通过 spawn precheck，创建 **1** 个 OpenSearch 进程记录和 **1** 个临时数据目录，但服务启动阶段报 RuntimeError，未进入正式评分。与旧 replacement 的 Popen 前拒绝不同，不得混记成零进程创建。B attempt0，没有重试 A、启动 B 或重放任何库。
- 新 arm/global exposure/正式 query/score/complete/result 均 **0**。硬门后先只读对账，确认候选进程已退出，保存原控制器失败终态，再仅调用冻结 `--closeout-invalid` 分支一次。因此控制器审计 attempt 共 **2**：一次正式执行、一次只做 cleanup→after 的收尾；不把后者隐去，也不把它说成第二次正式运行。
- 固定分类扫描发现 Operation-not-permitted 标记14次，受保护日志目的地的直接错误行36条、日志配置相关类标记480次、目录创建类标记2次。原始日志、目录和任何私有题面均未出 OPS；诊断没有重新打开 holdout。日志目的地受保护与启动失败相关，但**尚未证明它是唯一根因**，没有现场改权限或绕过保护。窗口失败原因保守记为 CANDIDATE_A_STARTUP_FAILED_BEFORE_PRIVATE_SEARCH。
- 独立收尾重算显示 frozen artifacts、主 runtime readiness、隐私 receipt 绑定仍通过；角色审核仍通过，但只有 PROCESS_LEVEL_SEPARATION_SINGLE_UID，不夸大为独立 OS 账号隔离。socket/预启动 readiness PASS 不等于完整候选服务可启动，本次真实启动暴露了这一覆盖缺口。

### 三库逐候选成绩与复杂度

三库都参与、deferred=[]；同一保留题池为 cwork-3m **42@T3**、docdb-touqian **31@T3**、spbp-2027 **42@T2**，builder/verifier 仍 **1/1**。这些是题池数，不是正式成绩分母。

每库 A=NOT_MEASURED_STARTUP_ABORT，B=NOT_RUN_PREVIOUS_CANDIDATE_ABORT。每库每候选的20项正式测量——13个计数字段（含 leak_count）、Recall@10/Exact/NoAnswer 三项比率、P95/index_bytes/build_seconds/peak_rss_bytes 四项资源指标——**全部 null**。共120个缺测值，不填0，不挪用 synthetic 或旧成绩。候选 Gateway 四能力也均 null；三个生产 Gateway 的200不能替代候选接入能力验收。

冻结 runbook 的机械复杂度向量（组件/升级步骤/备份恢复步骤）：A **1/6/4**，B **2/7/3**；仅文档机械计数，不是性能结论。

### Finally、生产观测和保留

同窗口 cleanup1→formal-after1→abort evidence→decision 已完成，三个库 after 均 PASS；before/after 文件数均为 **1248、316、317**，私有基线绑定及 comparison 独立重算通过。after 已永久关闭本窗口的重放入口。

- 实时终态：候选/正式控制器进程0，临时数据面0、新临时文件0、新TLS0，精确 UUID 容器/卷/网络/镜像标签/服务0，cleanup failures0；新增的1份进程审计记录保留，不等于残留运行资源。清理只作用于当前实验资源，未动生产 Gateway、NAS、旧索引、宿主配置。
- 三 Gateway **3×200**，身份与健康内容相等；容器和卷观测相等。NAS、索引和生产配置的已测投影相等，但 NAS/既有索引完整不变性仍 **null**；services_unchanged=false，故 production_config_unchanged=false。没有把局部观测当作完整证明，没有修生产来追平，也没有清除或抵销历史漂移。
- 96份材料、1222份历史归档、旧原件/claim/void/window/freeze、主策略原字节及私有 holdout/audit 均保留并重算通过。旧 replacement 继续永久 INVALID，旧 VOID_PREQUERY_NO_EXPOSURE 严格有效。未导出私有文本、标识、路径或私有 digest。

### 三格验收及本地交付

- **工程判据**：本轮已运行174项 RT-055 回归，0 fail/error/skip，128个 Python 文件编译通过；继续会话没有重复它们，Git 复核源码未变，结果仍适用于最终树。公开 abort 在 OPS 先通过独立闭集 Schema 再导出；正式 v3 Schema 必须拒绝该 abort，OPS 和本地原裁决 CLI 对同一对象逐字段一致：exit2、INVALID、AGGREGATE_CONTRACT_INVALID、deferred=[]。追加负例覆盖私有字段注入、伪造120个缺测成绩、缺失必要段和虚假成功。没有新增执行器行为破坏实验，因为没有修改执行器；不把 Schema 检查当成候选运行证明。
- **AI 自检**：主会话审查了单次执行与只收尾分支的区分、冻结源未变、零正式查询、原日志不出 OPS、未越权修保护或重试。没有可用独立审查 worker，不冒称外部审批。明确记录174项回归未覆盖正式原生日志目的地/保护范围组合，不能用全绿否认真实失败。
- **读真实产出**：读 OPS 白名单启动失败分类、同窗口 cleanup/after/comparison/abort/decision 和本地逐字段复算；核对缺测null、生产false/null、历史与审计保留，不拼接旧结果。

最终 QA 包含隐私/secret 扫描、链接/锚点、历史文件及文档前缀、AODW/governance 与 git diff/check；既有宿主 Skill 告警保留，不修改宿主配置，不冒称全仓 CI。交付仅本地提交两份本 RT 文档和四份公开证据，不 push/合并/删除 worktree，不提交 runs 或交接目录。

本轮失败收口完成，RT 仍 in_progress，选型仍未完成；无后台实验或控制器。恢复需要新的明确授权及独立协议轮，不能因 query=0 或旧 readiness PASS 自动重开本窗口。

## Amendment 6 — runtime workspace 修复验收（本地阶段）

旧日志路径与 sandbox 保护范围冲突已由只读 OPS 分类和两种策略的公开 syscall 重现；
旧 window 未重开、未重试，私有 holdout 未用于诊断。见
[根因证据](candidate-workspace-root-cause.json) 与 [Amendment 6](../experiment-protocol.md#amendment-6--candidate-runtime-workspace-recovery2026-09-12)。

本地交付：A/B 独占0700 runtime 租约、路径/ownership校验、收紧子进程写权限、最终日志
needle 硬门、私有日志留存、精确 cleanup、main-root 公开完整 startup 源码绑定。
本地 PASS 不能替代 OPS runtime/privacy/startup、新 before/freeze 与最终 READY evidence。

判据：完整 RT055 回归和行为破坏；AI 评审：主工程师逐段检查租约/进程/扫描/完成顺序，
未使用已退役固定 reviewer；读产出：真实 syscall 的拒绝/允许分类与 OPS 公开计数。
不冒称全仓 CI；RT 仍 in_progress，正式查询及生产切流没有授权。

### Amendment 6 JVM 诊断补丁（本地）

第一份 source commit 的 OPS synthetic 并未通过：A 在 GC 日志初始化时被只读发行目录拦住，
只产生公开 synthetic 启动日志，cleanup/runtime 残留0，正式 query/exposure0。原回执不改。
补丁改为每租约复制并核对 config，仅重定向 JVM 诊断路径；原配置、heap/算法保持不变。
新的 [本地回归](candidate-workspace-jvm-tests.json) 单独保留，不能把未通过的 OPS 轮改写成 PASS。

### Amendment 6 launcher 补丁（本地）

第二个 synthetic 失败及清理回执保留；没有正式窗口或 query。真实 Bash 破坏/恢复测试确认
here-string 临时写入被拒，而 owned stdin 在同一严格策略下成功。见
[launcher 回归](candidate-workspace-launcher-tests.json)。正式候选算法/模型和原始发行配置不变。

### Amendment 6 路径规范化补丁（本地）

第三个 synthetic 失败（keystore canonical path）及零残留清理均保留。修复只允许本租约
祖先 metadata，不开放目录列表、其它 runtime 或账本。见
[最新完整回归与破坏测试](candidate-workspace-traversal-tests.json)。OPS 未通过前不创建新正式 before。

### Amendment 6 native config 补丁（本地）

A 真实公开启动/检索已成功，B sidecar 已成功；第四个 synthetic 的 native config 缺失失败及
cleanup 全部保留。最新源码把未改动的公开配置复制进各 B 数据目录，并扫描/归档嵌套兜底日志。
见 [最新完整回归](candidate-workspace-native-config-tests.json)。仍不以 synthetic 代替正式指标。

### Amendment 6 native assets 补丁（本地）

官方 loader 的公开 SQLite schema 与 config 同属相对路径运行依赖，现均在独占数据面完整复制、
核对。第五个 synthetic 失败不覆盖；新 [完整回归](candidate-workspace-native-assets-tests.json)
验证配置/schema 不改、目录隔离、数据字节排除静态依赖、嵌套日志扫描与精确 cleanup。


## Amendment 6 interrupted READY 最终验收（2026-09-12）

**独立验证 PASS，停止 READY_TO_RUN。** [公开回执](candidate-workspace-ready.json)、
[闭集 Schema](candidate-workspace-ready.schema.json)、[QA](candidate-workspace-ready-qa.json)、
[QA Schema](candidate-workspace-ready-qa.schema.json)。部署/冻结源码 `ad7d6b6e282ed8a674f25924830c4c5df71137ed` 未修改；
window `c66757c6-93f6-481d-aa92-be7de83b9aa1`，migration `11b62e31-ddbe-469e-baca-8f4553be0299`，冻结顺序 **A→B**。

- 根因：ignored verifier `ASSERT_L017` 混淆全局旧消费 claim 与新 window 计数。34条断言/57次求值，
  唯一真实失败；旧全局 claim1/void1 保留，新 window 各项0。null-device 诊断假阴性修正后已复核，
  不作为 READY 缺口。原中断 FAILED 和 finalizer.claim 留存，没有重跑 finalizer。
- 现有 receipt 独立重算：部署源码、source-bound privacy、main runtime policy、candidate-workspace
  startup、freeze与随机顺序通过；无 Popen precheck通过。主 A3/B3/sidecar3 启动证据完整、已清理，
  本次未重复启动。正式 query/private-read=0 指主公开 startup；旧正式 runner 确曾打开私有输入，
  不改写为“从未发生”。
- 公共 synthetic 既有证据：正常 A/B各1，鉴权错误400/400、embedding3/3；15日志零命中，
  123 socket samples无外连；tracing/forbidden reads/observer errors0。
- 新 before/freeze/verification claim各1；attempt/arm/exposure/query/score/result/after0。
  全局 exposure/score/result0；旧 window A1/B0/after闭合、全历史窗口/claim/void清单及字节不变。
  builder/verifier1/1，三库42@T3/31@T3/42@T2，deferred为空。
- 归档以实际清单计数：96材料/188101752字节；1222前次归档/577143325字节；
  2570本次归档/1156141883字节。三者逐文件重算一致。5次失败 predecessor源码与终态
  沿链绑定、集合完整、每次cleanup0；主和合成候选/controller/临时数据面/runtime残留0，Gateway3×200。

### 三格核验

- **工程判据**：完整 RT055 回归、py_compile、闭集 Schema正例和缺字段/私有字段/计数与状态漂移反例；
  ignored verifier的计数表达式用合成输入复现旧claim1/新window0，并证明新attempt/score/result非0会拒绝。
  隐私/secret/links、历史文档前缀、部署scripts不变、AODW/governance/diff均复核，实际计数见QA。
  11项候选运行目录行为破坏结果复用 `ad7d6b6` 的既有公开源码验收，不冒称本次重跑 OPS 或破坏实验。
- **AI审查**：接管主Agent独立检查“claim的作用域是否与其它计数一致”“是否删除历史来凑全零”
  “是否把真实freeze失败放宽为PASS”。确认只修本地验证逻辑、保留全局硬门和失败历史；
  不冒称外部 reviewer 或实验 builder/verifier 再次运行。
- **读真实产出**：只读OPS既有receipt的闭集投影，交叉核对真实顺序、源码、启动数、归档计数/字节、
  零暴露与旧窗口不变性。私有题面/路径/hash不出OPS；公开证据不是正式aggregate或质量裁决。

下一断点仍为等待正式 A/B 的新明确授权；本次无该授权、不执行、不切流。RT保持in_progress。
完整NAS/index不变性UNKNOWN和历史服务漂移保留；不声称全仓CI。只本地提交公开证据/文档/必要tests。


## Amendment 6 正式执行收口：评分输入硬门 INVALID（2026-09-12）

**唯一裁决 INVALID，未选出 A/B，不是质量 NO-GO。** 本节是最新终态，取代前文 READY 的当前判断；历史 READY、失败、claim/void、freeze 与文档前缀保持原样。

### 授权与实际执行

从 `8750bbdc75cfda061fdab5818c338de81582da8a` clean 接续。启动前在 OPS 零写入重新核对完整公开 READY 投影，逐字段 exact equal；冻结/部署源 `ad7d6b6e282ed8a674f25924830c4c5df71137ed`，run `ac1ca0c7-6983-4f6e-91ce-8eb45e7673af`，migration `11b62e31-ddbe-469e-baca-8f4553be0299`，window `c66757c6-93f6-481d-aa92-be7de83b9aa1`，沿用真实冻结 **A→B**，没有创建新窗口或重新冻结。

正式 coordinator 脱离式启动一次。A仅1个attempt，三实例已启动并走过构建/公开warmup及实验室Gateway探针，随后在首库评分入口留下arm1并报 `CandidateError`。全局exposure0、私有query0、score0、complete0、result0；B attempt0。冻结调用顺序证明失败在 scorer 输入校验、exposure及第一个私有search之前；具体输入子类型没有原始错误回执，不猜测是重复题、查询格式或分母。诊断没有重新打开私有holdout、运行scorer、补题或重放候选。

原正式进程已退出才收口。仅调用冻结 coordinator 的 `--closeout-invalid` 分支一次，不包含候选重跑；因此控制器审计共2次（正式1、只收尾1），不能把它写成总共1次。没有重跑recovery、synthetic、main startup、before、freeze、builder或verifier；scripts/config/core/题池/seed/规则未改。

### 逐库正式指标

| 库 | 保留题池（不是成绩） | A | B |
|---|---|---|---|
| cwork-3m | 42@T3 | 20项均null，评分入口失败 | 20项均null，未启动 |
| docdb-touqian | 31@T3 | 20项均null，前库中止后未评分 | 20项均null，未启动 |
| spbp-2027 | 42@T2 | 20项均null，前库中止后未评分 | 20项均null，未启动 |

20项完整字段为：13计数 `total_count`、`answerable_count`、`recall_hits_at_10`、`exact_count`、`exact_hits`、`no_answer_count`、`no_answer_correct`、`system_error_count`、`answerable_system_error_count`、`exact_system_error_count`、`no_answer_system_error_count`、`timeout_count`、`leak_count`；质量 `recall_at_10`、`exact`、`no_answer`；资源 `p95_ms`、`index_bytes`、`build_seconds`、`peak_rss_bytes`。全部null是未完成正式测量，不是0分、0泄漏或延期，`deferred=[]`。不从synthetic、旧结果、内存丢失值或日志时长补成绩。

机械复杂度来自未改冻结runbook：A组件/升级/备份恢复 **1/6/4**，B **2/7/3**；不是正式性能分数。A的HTTPS查询、逐Gateway身份、服务端库授权、不向客户端泄露NAS/search凭据四项均有本次实验室公开warmup探针实测true；B四项null。它们不是生产Gateway已完成集成的声明，生产健康3×200另行验证。

### 隐私、运行目录与清理

- source-bound隐私、main runtime、workspace startup及freeze绑定只读重算PASS；builder/verifier仍1/1，角色核验PASS，`PROCESS_LEVEL_SEPARATION_SINGLE_UID`，不冒称OS UID隔离或新独立worker审批。B上游/core保持原版。
- A本次正式日志扫描 **39文件、0命中文件、PASS**；租约日志留在OPS、workspace-cleanup回执1。评分`leak_count`仍null，不能拿日志扫描0替代它。
- 三个新进程审计保留。按本次启动时点、精确候选目录和inode/UUID账本核对，仅删除新生成的2个TLS文件；旧tmp文件与Gateway探针回执保留。aux清理在冻结cleanup之前，冻结cleanup在after开始之前。
- 同window cleanup1、after claim1，after三库PASS；窗口永久关闭重放。候选/控制器/runtime/临时数据面/新增tmp/TLS/UUID容器、volume、network、image、service及cleanup failures终态全0；Gateway3×200。
- 96材料、1222前归档、2570本次归档逐文件同字节；5个失败synthetic predecessor链完整且原cleanup0。旧window `835c5188-0f29-4f41-8fe6-119b61917e2d` 保持INVALID、A1/B0/after，global legacy claim1/void1及全历史窗口清单不变。
- 同窗不变性如实为：`nas_unchanged=null`；`existing_indices_unchanged=null`；`production_config_unchanged=false`；`services_unchanged=false`；`gateway_unchanged=true`；`all_items_measured=false`。实测漂移不抵销、不修生产、不重建baseline；完整NAS/index/config未知不能补成true。

### 三格核验

- **工程判据**：本轮实际201项RT055回归0失败/0错误/0跳过，133文件py_compile；闭集abort Schema正例与缺字段/注入私有字段/伪造测量值/账本漂移反例；本地冻结decision CLI与OPS结果逐字段一致，退出2。有效v3 aggregate不存在，v3 Schema必须拒绝abort；不伪造完整报告。隐私/secret/links、历史前缀、冻结scripts、AODW/governance/diff及提交范围见QA。没有重新运行OPS synthetic、行为破坏或全仓CI。
- **AI审查**：接管主Agent检查“arm是否等于曝光”“启动与Gateway公开探针能否代替正式成绩”“是否为了取PASS重试”“独立READY是否涵盖真实scorer输入”。确认arm≠消费、READY未证明完整输入可评分；本次不修范围外缺口，不冒称外部独立reviewer。
- **读真实产出**：只读OPS运行失败类型、arm/exposure/score/complete/result、39文件日志扫描摘要、Gateway布尔探针、cleanup/after/归档/源绑定与闭集输出；不导出私有query/原文/expected/title/filename/path/doc_id/locator/body/片段或私有hash。

[公开abort](amendment6-formal-abort.json) · [闭集Schema](amendment6-formal-abort.schema.json) · [唯一裁决](amendment6-formal-decision.json) · [QA](amendment6-formal-qa.json)。OPS先同window保存公开abort再运行冻结裁决器一次；本地仅接收allowlist，裁决一致。仅本地提交公开证据/QA与两份RT文档，不push、不合并、不清worktree，不含runs/或docs/handover/。失败收口完成，正式A/B比较仍未完成，RT保持in_progress，禁止切流；无后台实验任务。

## Amendment 7 本地修复 — pre-exposure scorer 输入合同

公开合成红测复现同题不同 identity 被旧 scorer 拒绝；OPS 只读复核 4/4/5 个合法跨类别二元组，
未导出 query/expected/doc_id/ordinal 等值或私有 hash。严格 `(kb, ordinal)` trial、legacy
重复拒绝、同题冲突拒绝、完整分母、A/B shared loader、主 input readiness 和 freeze/coordinator
接线见 [Amendment 7](../experiment-protocol.md#amendment-7--scoring-input-readiness2026-09-12)。

判据使用公开 fixture 的真实 pure validator/loader/readiness，测试 before claim 前拒绝、
错 source/window/receipt/after/exposure 时零 attempt/Popen，保留全池逐行计数和原子 exposure。
行为破坏在临时副本运行，恢复绿后记入公开 synthetic QA。源码第一提交仅表示本地修复，
不能代替主 OPS readiness；新 READY 及实测 QA 另行追加，正式 A/B 不执行。

人工式源码自查聚焦：不放宽 legacy、no_evidence 原始语义、私有 identity 不进 repr、
main/synthetic 边界、无正式 attempt 副作用、历史字节不修改。未调用独立 reviewer Agent；
主会话读实际 fixture 输出、diff 和 OPS 闭合投影，不以 worker 自报代替核验。

## Amendment 7 OPS 收口 — READY_TO_RUN（不执行正式 A/B）

权威 [READY 回执](scoring-input-ready.json)、[Schema](scoring-input-ready.schema.json)、
[QA](scoring-input-ready-qa.json)、[根因分类](scoring-input-root-cause.json) 与
[公开合成红绿/破坏证据](scoring-input-tests.json)。

- 源码 commit：`f94b6e1b0f60763443e56ed76c932f90d55f927b`，本地独立提交后部署，未 push。
- run `ac1ca0c7-6983-4f6e-91ce-8eb45e7673af`；新 executioner migration
  `3e5c31a2-8741-420b-b83b-095c32446d95`；新 window
  `d408cfab-40f7-41d3-8ccb-75fb0aaa2b65`；全新随机顺序 **B→A**。
- 主 runtime readiness、A/B/sidecar 各 3 个公开 startup、主 scoring-input readiness、
  新 before/freeze/verify 均 PASS；独立重算核实时间顺序和 source/window/freeze 绑定。
  主 input receipt 严格早于 before；无 Popen spawn precheck、禁止连接的纯私有 input
  preflight PASS；A/B loader 均保留全部 ordinal。formal coordinator **未调用**。
- 完整池 42@T3 / 31@T3 / 42@T2；重复额外行/二元组均为 4/4/5；同库同题语义一致，
  ordinal 唯一，三类分母均正；builder/verifier 仍 1/1。无题池/tier/seed/质量门/
  候选算法/WeKnora core 变更，无删题、合并、换 query。
- 新 controller attempt/candidate attempt/arm/exposure/query/score/result/after 全 0。
  旧 `c66757c6-93f6-481d-aa92-be7de83b9aa1` 保留 INVALID/after，A attempt1/arm1、B0，
  exposure/query/score/result0；旧 claim/void 有效且未改。
- 新归档 5,938 个历史工件；此前 2,570/1,222 文件归档和 96 份材料字节均一致。
  独立核验旧 window 文件清单及所有历史文件字节，未删旧账本。
- 公开 synthetic 隐私门：正常 search/native 各1、鉴权错误400/400、embedding3/3；
  15 个日志、112 个 socket 样本；canary/外连/tracing/forbidden read/observer error 全0。
  synthetic 和 main startup 均完成清理；候选/控制器进程、runtime、新 tmp、TLS、
  UUID 容器/卷/网络/服务/镜像标签均0；Gateway **3×200**。
- 本地完整 RT-055 回归 **219 tests / 0 failures / 0 errors / 0 skips**，11 项行为破坏
  全检出，136 个 Python 文件编译通过；Schema（含嵌套私有字段与叶子损坏反例）、
  隐私/secret、链接、AODW、governance、diff 均通过。仅保留既有宿主 Skill 缺失告警，
  不冒称全仓 CI 或外部独立 reviewer。主会话独立读取、重算 OPS 实物后才收口。
- 观察通道曾中断一次，仅重新连接只读 watcher；没有重启控制器或重复任何主 claim。
  当前无后台任务。完整 NAS/index 不变性仍 UNKNOWN，历史漂移不清除；未修改生产，
  READY 不是正式测评结果，也不授权生产切换或自动执行 A/B。

## Amendment 7 正式窗口收口（2026-09-12）

唯一裁决：**INVALID**。本轮只在既有 READY 上执行冻结 B→A 正式路径；未补跑准备、未新建窗口、未修改实现、未复活旧窗口。

- run：`ac1ca0c7-6983-4f6e-91ce-8eb45e7673af`；window：`d408cfab-40f7-41d3-8ccb-75fb0aaa2b65`。
- executioner migration：`3e5c31a2-8741-420b-b83b-095c32446d95`；source：`f94b6e1b0f60763443e56ed76c932f90d55f927b`。
- 参与库：cwork-3m 42/T3、docdb-touqian 31/T3、spbp-2027 42/T2；deferred=[]。
- formal evaluation complete：`false`；schema-valid v3 aggregate available：`false`。
- 终态：`INVALID/COMPLETE`；边界：`PRE_EXPOSURE_FORMAL_RUN_FAILURE`；原因枚举：`CANDIDATE_LOG_PRIVACY_FAILED`。
- **正式日志保密门禁 FAIL**：6 个候选日志文件中 1 个命中，命中正文/定位符/摘要均未导出。准备阶段 privacy/readiness PASS 不覆盖这次正式日志失败。底层执行失败仅有 RuntimeError 类型证据，不据耗时擅自认定具体异常子型。日志命中数不是检索 `leak_count`，后者仍为未测 null。
- **INVALID 不是 A/B 胜负或 NO-GO 的替代命名**：完整正式证据/生产不变性合同没有通过，不能据此选型或切流；保留已测原值，未测项保持 null，旧漂移没有被清零。
- 本次公开 closeout 属于同窗口失败/收口证据，**不是缺项填零的 v3 正式 aggregate**；OPS 与本地冻结 decision CLI 对此均输出 INVALID。

### 固定分母、质量与资源

固定 trials 与分类分母未改，合法跨类别同 query 不被去重。本轮没有产生正式评分：每库每候选的全部 20 个指标均为 null，保存在公开 JSON；不能用已知池规模、运行耗时或日志命中数回填未测指标，也不能把 null 当成 0。

| 库 | 候选/状态 | total | Recall@10 | exact | no-answer |
|---|---|---:|---|---|---|
| cwork-3m | A/NOT_RUN | —（未测） | —（未测） | —（未测） | —（未测） |
| cwork-3m | B/ATTEMPT_FAILED_BEFORE_MEASUREMENT | —（未测） | —（未测） | —（未测） | —（未测） |
| docdb-touqian | A/NOT_RUN | —（未测） | —（未测） | —（未测） | —（未测） |
| docdb-touqian | B/ATTEMPT_FAILED_BEFORE_MEASUREMENT | —（未测） | —（未测） | —（未测） | —（未测） |
| spbp-2027 | A/NOT_RUN | —（未测） | —（未测） | —（未测） | —（未测） |
| spbp-2027 | B/ATTEMPT_FAILED_BEFORE_MEASUREMENT | —（未测） | —（未测） | —（未测） | —（未测） |

| 库 | 候选 | system_error | answerable_error | exact_error | no_answer_error | timeout | leak |
|---|---|---:|---:|---:|---:|---:|---:|
| cwork-3m | A | —（未测） | —（未测） | —（未测） | —（未测） | —（未测） | —（未测） |
| cwork-3m | B | —（未测） | —（未测） | —（未测） | —（未测） | —（未测） | —（未测） |
| docdb-touqian | A | —（未测） | —（未测） | —（未测） | —（未测） | —（未测） | —（未测） |
| docdb-touqian | B | —（未测） | —（未测） | —（未测） | —（未测） | —（未测） | —（未测） |
| spbp-2027 | A | —（未测） | —（未测） | —（未测） | —（未测） | —（未测） | —（未测） |
| spbp-2027 | B | —（未测） | —（未测） | —（未测） | —（未测） | —（未测） | —（未测） |

| 库 | 候选 | P95 ms | index bytes | build seconds | peak RSS bytes |
|---|---|---:|---:|---:|---:|
| cwork-3m | A | —（未测） | —（未测） | —（未测） | —（未测） |
| cwork-3m | B | —（未测） | —（未测） | —（未测） | —（未测） |
| docdb-touqian | A | —（未测） | —（未测） | —（未测） | —（未测） |
| docdb-touqian | B | —（未测） | —（未测） | —（未测） | —（未测） |
| spbp-2027 | A | —（未测） | —（未测） | —（未测） | —（未测） |
| spbp-2027 | B | —（未测） | —（未测） | —（未测） | —（未测） |

### Gateway、复杂度与证据边界

本轮未到 candidate Gateway 探针阶段，四项能力均未测。本协议要求的是 candidate query 的实验室 HTTPS 授权壳测试，不等于生产三个 Gateway 已接入 A/B；生产 /health 200 也不能替代这些测试。
- A：HTTPS query=—（未测）；per-gateway identity=—（未测）；server-side KB grants=—（未测）；no direct NAS/search credentials=—（未测）。
  - 机械复杂度：components=1；upgrade steps=6；backup/restore steps=4。
- B：HTTPS query=—（未测）；per-gateway identity=—（未测）；server-side KB grants=—（未测）；no direct NAS/search credentials=—（未测）。
  - 机械复杂度：components=2；upgrade steps=7；backup/restore steps=3。

- role separation：`PROCESS_LEVEL_SEPARATION_SINGLE_UID`，只宣称真实测得级别，不宣称 OS UID 隔离或外部独立审查；builder/verifier 仍各 1/1 次。
- privacy、runtime policy、candidate workspace、scoring input readiness 及 source/freeze/upstream 绑定均在 OPS 只读复核。正文、queries、expected、定位符、私有文件名/路径、逐条结果与私有摘要不出 OPS。
- ledger：B={"arms": 0, "attempts": 1, "completed_libraries": 0, "exposure_without_completion": 0, "exposures": 0, "query_calls": 0, "result": 0, "scores": 0}；A={"arms": 0, "attempts": 0, "completed_libraries": 0, "exposure_without_completion": 0, "exposures": 0, "query_calls": 0, "result": 0, "scores": 0}。
- closure：formal launch=1；closeout-only launch=0；candidate restarts=0；before/freeze/verify/after claims=1/1/1/1；scoring readiness claim=1；准备阶段重复=false。
- frozen cleanup→after 顺序核验=true。辅助库存复核发生在 after 之后（`aux_cleanup_before_after_verified=false`），确认新增 tmp/TLS 均为 0、实际删除文件为 0；不是在 after 后补做候选清理。未回写或重复 after。
- 本轮临时候选进程/控制器/runtime/data planes/new tmp/TLS/cleanup failures=0/0/0/0/0/0/0；UUID 容器/卷/网络/镜像/service 归零；三个既有 Gateway 均 HTTP 200。
- 历史：96 retained、5938 archive、此前2570/1222 archive、旧窗口/claim/void/失败 synthetic 历史逐字节保留；旧窗口仍 INVALID。私有 holdout 留 OPS，不做后续自由重跑。

生产 before/after 原始比较（false/null 均按实保留）：

- `all_items_measured` = `false`。
- `containers_unchanged` = `true`。
- `existing_indices_unchanged` = `null`。
- `existing_indices_unchanged_measured_projection` = `true`。
- `gateway_health_content_unchanged` = `true`。
- `gateway_unchanged` = `true`。
- `nas_unchanged` = `null`。
- `nas_unchanged_measured_projection` = `true`。
- `production_config_unchanged` = `false`。
- `production_config_unchanged_measured_projection` = `true`。
- `services_unchanged` = `false`。
- `three_gateways_healthy` = `true`。
- `volumes_unchanged` = `true`。

不据此清除历史漂移、不把未测项补 true、不切生产流量、不将 RT 标记 CLOSED。

证据：
- [OPS 同窗口正式收口](amendment7-formal-closeout.json)。
- [收口闭集 Schema](amendment7-formal-closeout.schema.json)。
- [OPS 唯一裁决](amendment7-formal-decision.json)。
- [本地 QA](amendment7-formal-qa.json)。

QA：本地 RT-055 回归 219 项全部通过，136 个冻结源码/测试文件编译通过；闭集 Schema、固定分母与缺测、OPS/本地裁决、私有字段/摘要与 secret、文档链接、历史前缀、冻结源码与提交范围均核验。aodw-check/governance 以本次 QA JSON 中的实测字段为准，不声称运行完整 repository CI。

## Amendment 8 本地合同与 OPS readiness

旧日志泄漏根因与防火墙、build分类、公开同形负载合同见
[Amendment 8](../experiment-protocol.md#amendment-8--streaming-log-firewall-and-workload-readiness2026-09-12)。
本地RED必须先证明旧 direct-file canary确实落盘；GREEN涵盖每个chunk边界、前缀/重叠、
Unicode/JSON转义、无换行、64MiB cap、drain异常、缺EOF、child nonzero、fd/thread回收、
净化占位符和post-scan0；故意断线/残余拒绝。B terminal failed不能轮询至timeout。
完整 RT055 回归、pycompile、Schema反例、隐私字段/链接/AODW/governance/diff均为独立门。
OPS workload/native/privacy/main readiness尚未通过时，本地GREEN不得称为READY。

### Amendment 8 公开夹具与容量修正

首个 migration 的公开 A 三库 build/search 与隐私门通过；B 在注入锁下首次请求立即失败，
正常导入0，不是 build timeout。保留该失败与cleanup0，不freeze。将注入改为独立公开
SQL失败探针（锁确保不能落文档，预期HTTP500），释放锁后才运行一次完整build。
私有全集needle的安全容量分类显示总字节大于64MiB、不超过128MiB，最长leaf不超过8MiB。
仅pattern bank改为128MiB、各drain共享同一父进程不可变bytes集合；每流日志输入64MiB、
read chunk64KiB、最长leaf8MiB、A/B每库build7200秒均不变。不丢pattern、不用hash替代。
正式input readiness新增读取现有private-corpus并构造完整Filter容量验证，source和corpus
均进入freeze绑定；无Popen、无query、不重做builder/verifier。新migration重跑公开验证。

### Amendment 8 注入时序修正

第二个migration仍为公开readiness失败：正常B首个导入0.084秒请求失败；仅检查这轮公开
净化日志，观测到一次SQLite busy与一次missing table。防火墙完整EOF/关闭和post-scan
均通过，不能把故障注入影响冒称B正常build结论，也不能外推旧私有failure子型。
将刻意SQL失败探针移至每库正常build及公开search完成之后，以免干扰native惰性初始化；
候选build、文档大小/42/31/42分母、7200秒timeout和core均不改。原失败证据和cleanup0保留。

### Amendment 8 同形尺寸与精确字节计量

第三轮在任何SQL故障探针之前即导入失败，未观测到SQL错误。公开upstream服务合同为
manualContentMaxLength=200000字符；首个512KiB ASCII夹具超过该上限。OPS只读布尔
核验确认全部冻结私有canonical documents在原生字符上限内（未导出具体长度或内容）。
改用源码独立编写的中文公开句子，保持42/31/42、512KiB/8KiB字节上界，且正文小于
199000字符，为canonical metadata留余量；A/B文本完全一致。不是截断或重建私有输入。
防火墙进一步同时封顶每流实际输出64MiB；input计量在os.read后、output计量按实际
write返回字节，溢出块也进入input计数（最多input cap+1个64KiB read），不进入日志。
HTTP失败新增封闭状态码枚举，不保留响应消息或私有值；7200秒timeout不变。

## Amendment 8 最终断点 — BLOCKED

部署源码 `a475b048c235b2f4a8b92d845760462b4879c83c`；migration `387d6d9e-5ae4-4ebb-96b7-d993275b6111`。
[公开readiness](amendment8-readiness.json)、[独立OPS复核](amendment8-ops-verification.json)
和[行为破坏QA](amendment8-mutations.json)为本轮入口。
窗口实际创建：false；顺序：None；未运行正式coordinator。
新attempt/arm/exposure/query/result/after全0，旧window保持INVALID/after与log FAIL。
builder/verifier1/1、96材料、5938原归档和全部迁移历史字节保持；Gateway3×200、cleanup0、无候选/runtime。
完整NAS/index不变性仍UNKNOWN，未清除历史漂移；1路Gateway缺read_only字段，未证明只读合同。前三次公开夹具/容量修正失败链保留，不是正式成绩。

公开B硬门尚未通过；不创建window/before/freeze、不消费holdout。源码修复不能替代真实workload PASS。
隐私合成证据已独立按源码重算PASS，但迁移privacy binding未创建；main runtime/workspace/scoring readiness均未运行。
B失败总回执未回填firewall_verified，单独最终drain回执6流全部PASS、20次替换、scan0，原件不改。
投前库native日志有UNIQUE_CONSTRAINT1；CWork的SQLITE_BUSY1来自公开刻意探针，不归因为投前失败，更不归因为旧私有B。

详见[Amendment8完整断点](amendment8-blocked.md)与[最终QA](amendment8-qa.json)。

## Amendment 9 验收边界

按 [协议](../experiment-protocol.md#amendment-9--串行原生导入与新准备门2026-09-12)执行，正式 A/B 不在本次授权中。
验收必须同时有：真实 SQLite RED、串行 GREEN/行为变异、完整 RT055 回归 >=241、编译/Schema/privacy/links/AODW/governance/diff；真实 OPS 同形 A/B 三库 build/search 与 B inflight<=1；final firewall 全流与cleanup0；source-bound privacy/input/main readiness；before/final 两次三 Gateway 合同；新 UUID freeze/verify/order；正式计数全0及旧历史不变。任何准备硬门失败不得标 READY。

机器证据及最终状态：[Amendment 9](amendment9-summary.md)。不把 mock、单库成功、HTTP200 或本地回归替代全套真实准备门。

## Amendment 9 续接终态 — BLOCKED，无正式 A/B

部署源 `6b32584023cd27096e7c2167c0c28f47b380f922` 未改；migration `89edbd10-2905-41f1-9037-62b502890856` 的公开privacy与42/31/42同形仍PASS，未重跑。预留window `7652dbee-3679-4886-a391-ffcf871e712c` 的main runtime/workspace/scoring、严格before健康、before/freeze/verify已完成，冻结顺序B → A未重抽。

最终no-Popen coordinator precheck因旧claim/void验证的`_proof()`调用子进程而被guard拒绝。独立只读复核确认，未放宽guard或删历史。已freeze的原件不能删除或写成未freeze；仅一次cleanup/after尝试，cleanup PASS，after因NAS `TransientStorageError` 为FAIL（已测1库），after快照与comparison未生成。保留失败after claim/status，不重试。window INVALID/after-failed、READY receipt未创建。新attempt/arm/exposure/query/score/result及global formal exposure/query/result全0；after claim=1、after FAIL是失败收口记录，不冒充完整after验证或READY要求的after=0。

96材料、5938/97376/194848归档及历史集合/字节保持；builder/verifier1/1，cleanup0、无运行残留。Gateway before 与独立最终均 3×HTTP200、ok=true、read_only 字段存在且 true；完整NAS/index仍UNKNOWN。只本地公开证据提交，不push、不改source scripts、不执行正式coordinator。根因、全部门、QA和后续授权边界见[Amendment9最终证据](amendment9-summary.md)。


## Amendment 10 唯一正式执行收口 — INVALID

Source `43207eefbe30557b53d556055fa5438c8afe47ee`；本窗 `be3ab168-0568-4d39-a06e-824b4cd26a58`；顺序 B → A，原 coordinator 1次，A/B attempts=0/1。B 首库IMPORT阶段 REQUEST_FAILED；arm/exposure/query/score/complete/result逐库全0，不重放、不启动A。原formal `WAITING_RECONCILIATION / EXECUTION_ATTEMPT_FAILED / RUN_B` 保留，独立失败helper1次完成 cleanup→after→abort/唯一INVALID decision；before PASS，after PASS（claim1不重试），cleanup0，无任务/临时数据面残留。候选6流firewall/scan通过；coordinator firewall false/CHILD_NONZERO、后置scan0，失败值如实保留。正式20指标及Gateway四能力缺测全部null；机械向量A=1/6/4，B=2/7/3来自冻结runbook。96材料、42/31/42、builder/verifier1/1及历史字节保持；旧Am9 after FAIL不重试。完整NAS/index UNKNOWN及历史drift不清除，未改生产，不切流，不以INVALID关闭选型RT。详见[完整证据、逐库指标及QA](amendment10-summary.md)。仅本地提交，不push/merge/清worktree。


## Amendment 11 终态 — INVALID / INVALID_CLOSED（2026-09-12）

源码 `fdfcfb6dfef57862cf5efd478e88820419e7c322` 修复公开可复现的native finalizing等待合同并增加封闭请求错误码；旧Am10 REQUEST_FAILED因果根因仍UNKNOWN。新migration `aee2e2eb-4cd7-4f97-a864-5ce6c3782529` 首次public privacy实测108 socket samples/1 external observation/0 observer errors，loopback_models_only硬门失败；不将其等同外部数据传输，不豁免、不重跑。public workload0，main readiness/before/freeze/随机顺序/formal coordinator均未运行；A/B attempt0/0，逐库arm/exposure/query/score/complete/result全0。

预留新window `3330a46e-bfdd-4639-bb0c-ea5c1b3df622` 仅作终态收口：cleanup PASS→原after实现唯一1次FAIL（fresh before未创建、RuntimeError、0库、无快照/比较，不借旧窗基线不重试）→abort/唯一INVALID decision；formal INVALID为追加abort状态，非正式coordinator执行。120正式指标和24 Gateway能力均null，机械向量A1/6/4、B2/7/3只为未变runbook计数。新public3流firewall/scan通过，旧B6流PASS、旧coordinator false/CHILD_NONZERO保留。独立核验390079历史文件/96材料字节不变，builder/verifier1/1，42/31/42不变、残留0、Gateway3×200/ok/read_only=true。本窗comparison不可测；完整NAS/index UNKNOWN、历史services/config drift不清除。

RT055回归265项与源码红绿/行为破坏通过；最终Schema negatives、隐私/凭据、QA/治理详见[Amendment11完整证据](amendment11-summary.md)。本修订已收口，无后台任务；RT-055选型未完成，不切流、不自动重试，仅本地提交，不push/merge/清worktree。


## Amendment 12 验收增量：历史隐私硬门失败，选型未完成

源修复`6c524bc10369464f0a8cf50d1380504deb64f9aa`已本地272项回归及3项行为破坏验证；新migration`7f3a28e9-195f-4d5e-a4f0-5451b10fdb43`仅迁移隔离源码。历史单次socket缺PID/phase/state/target关联，归因与传输仍UNKNOWN，按明确fail-closed门停止：新privacy=0（上限1）、workload/readiness/before/freeze/formal=0；A/B0/0，逐库全部消费账本0。新terminal-only窗`999c334f-5018-4d83-b571-b406f49fe605`唯一cleanup PASS→after FAIL（无fresh before，0库，不重试）→INVALID裁决，120指标/24能力null；旧窗不重开，旧after/失败布尔不改。历史390197文件覆盖390079旧文件与96材料字节不变，builder/verifier1/1；最终三Gateway健康，资源0，完整NAS/index仍UNKNOWN，历史services/config false保留。

判据：红绿/真实sandbox probe/3项行为破坏；AI检查：主执行者针对未知归因、PID例外边界、空日志与null语义交叉复核（未设独立Agent）；读产出：OPS只读闭集、独立最终审计、唯一decision和公开摘要。首次终审额外全集字符串扫描1文件/1串命中（同为公开源码常量），因果UNKNOWN，扩展扫描FAIL原样保留；补充终审仅完成采集，未重跑实验。不是正式隐私PASS或质量NO-GO，RT-055选型目标仍未完成。

详见[Amendment12完整收口](amendment12-summary.md)、[终审](amendment12-final.json)、[QA](amendment12-qa.json)。


## Amendment 13 — 历史保留与当前 source 独立放行（2026-09-12）

本节只追加，不撤销 Amendment 11/12 的 INVALID、UNKNOWN、FAIL 或首次审计失败。用户已授权新隔离 migration 单次当前隐私门，以及通过之后的新 window 正式 A/B；不是授权重开旧窗或把旧失败变 PASS。

- **historical_invalid_preserved**：Am11 的 108 samples / 1 external observation 缺事件级 PID/phase/state/target，传输 UNKNOWN；Am12 的扩展旧 synthetic 日志 1 个私有字符串命中且同为公开源码精确常量，因果 UNKNOWN、扩展 scan FAIL、first audit failure 原样保留。旧日志只属于旧 source/旧 run；保存并校验其原字节，不要求它事后通过新的测试。Am12 current privacy=0、旧 after 无 fresh before 的唯一 FAIL 均不重试。
- **current_source_privacy_gate**：仅绑定新 source、新 migration、synthetic attempt=1。所有当前执行硬门仍必须通过；旧 UNKNOWN/FAIL 既不替代当前证据，也不构成必须改写旧原件的前置条件。当前 source 的一次调用失败即关闭，不跑 workload/readiness/freeze/A/B。
- controller 在安装禁止读取私有题池的 audit hook **之前**，只在 OPS 内存读取 builder/verifier 全部 JSON 的所有非空 string leaf，并生成原始 UTF-8 与两种 JSON-escaped 变体；含公开 canary 与 warmups。候选不读取私有输入，不接收 bank；bank 值不进入 argv/env/file/log/receipt，不输出 needle hash。输入文件库存/字节绑定单独留 OPS；bank 回执只有 count/bool/bytes。容量超限硬失败，无截断和子集降级。
- search/native/sidecar 的新 synthetic 日志通过 controller-only byte firewall，在任何写盘前使用完整 bank；公开 canary 扫描与独立全私有 byte scan 均须 0，闭合 EOF/FD、无溢出、无错误，归档库存严格核对。源码绑定验证重新检查 bank 输入、三流原件与独立扫描，不能仅改 receipt hash 通过。随后 public workload 日志也使用同一完整私有 bank 加其公开合成 needles；不改 workload 文档、题数或算法。
- socket 合同沿用 6c524bc 的独立 probe PID。候选三类 external 必须为 0，SYN_SENT/UNBOUND/未知归因不豁免；observer 不停。仅专用 PID/公开固定目标/拒绝结果/无 peer/闭合专属 TCP socket/零应用 payload 的主动 canary 可通过；不反推历史传输为 0。
- 先公开红绿/行为破坏/隐私凭据检查并提交 source，再 detached 部署。当前门通过后才进入必要 public workload → main readiness → fresh before → 新随机 freeze/verify → no-Popen/no-query precheck → 新 window 正式 A/B（各最多一次）。任何 exposure>0 不重放；7200、42/31/42、seed/tier/floor、A/B 对称门、builder/verifier 1/1 均不变。
- 成功发布 120 正式指标、24 Gateway 能力、源码机械复杂度、聚合和唯一裁决；未测指标为 null。失败按同窗唯一 cleanup → after → abort/decision 收口。完整 NAS/index 不变性仍 UNKNOWN，历史 services/config false 不清除；无 production/core/NAS/index/alias/config 变更，不 push/merge/清 worktree。
- 最后另起只读终态审计：历史原字节与 96 材料、当前与历史日志分层、ledger/唯一性、无运行残留、Gateway 3×health、before/after、builder/verifier。测试绿不冒称完整 OPS gate 或全仓 CI；实际结果另追加。


### Amendment 13 补充：历史零曝光证明的判据版本绑定

第一次 detached 部署在 PRECHECK 被拒绝：零曝光作废证明调用了当前 `evaluate`，误把后来增加的独立 probe 条件倒推给更早归档的隐私证明。已确认 source 安装=0、archive 创建=0、current privacy=0；原部署 claim/status/terminal/payload 保留，不改成成功。

修复只影响旧作废证明的核验：核对原冻结链和归档源码哈希后，仅提取原公开源码里的纯 `evaluate` 谓词与固定 KINDS；不 import 归档模块，不执行 initializer，不授予 I/O 或 Popen。原判据的 FAIL 仍被拒绝，源码 hash 不符仍被拒绝。旧 exposure 永不可 void、所有旧 claim/私有字节/冻结链检查不变。当前 migration 仍独立要求最新 `evaluate_current` 的完整 bank + 独立 PID 强门，不能使用此历史核验作为放行。

部署后续采用独立 append-only resolution/resume claim：先确认第一次没有 source/隐私/候选副作用，再在 controller 内使用已提交、hash-bound 的新公开核验器做只读 preflight；首次 source 安装和单次 current privacy 预算均未提前消费。旧部署失败回执不覆盖，旧 gate 不重跑。


### Amendment 13 实际终态：INVALID_CLOSED（2026-09-12）

当前完整内存 bank 为 285,809,127 bytes / 最长 needle 47,978,891 bytes，分别超过既有 128 MiB / 8 MiB 上限，CAPACITY 硬失败。controller/bank construction=1/1；synthetic privacy runner=0，workload/readiness/before/freeze/formal=0，A/B=0/0，exposure/query=0。没有子集降级、放宽容量门或重试；当前 socket/firewall/postscan 为 NOT_RUN，不冒充 PASS。

唯一 cleanup PASS → after FAIL（无 fresh before、0 库、不重试）→ abort/INVALID 裁决已独立核验。120 正式指标、24 Gateway 能力全 null；聚合未生成。历史 UNKNOWN/FAIL 及首次部署前置失败均保留，历史原字节与 96 材料、builder/verifier 1/1 保持，残留 0，Gateway 3×200/ok/read_only=true；完整 NAS/index UNKNOWN、历史 services/config false 不清除。RT-055 选型尚未完成。

详见 [Amendment 13 完整收口](amendment13-summary.md)。
