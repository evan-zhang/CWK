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
