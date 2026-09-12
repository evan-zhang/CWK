# RT-055 三库 confidential holdout 实验协议

## 1. 目的与隔离

本实验只回答候选 A 或 B 哪一个成为 CWK 下一条生产实现路线，不是 RT-054 analyzer/mapping 续调。OPS owner 在 RT-054 最终 holdout 消费后，从三库当前快照中未命中 §2.1 排除权威 R 的来源项重新抽样；R 是本轮协议定义的权威依据，不是历史实际题池的精确副本。query、expected/no-answer 标注、原文、标题、文件名、路径、locator、命中片段及这些材料的 hash、case-set 摘要、失败样例永远留在 OPS 0700 私有目录，不提交、不复制到开发机、不进入日志或返回 JSON。

当前 Amendment 3 只接收 `contracts/aggregate-report.schema.json` v3 聚合 JSON；历史 v2 与中止证据保持原样。所有非度量字符串均冻结为协议常量或严格枚举；唯一动态字符串是 canonical run UUID，以及由 OPS 受控 runner 对候选非 confidential 构建 artifact 生成的 `sha256:` digest。禁止人工填充、私有 case/corpus/query/expected/source hash 或任意 opaque token。harness 对字段闭集、固定常量、artifact digest、严格 UUID、分母/分子和比率二次校验并 fail closed。INVALID 与质量 NO-GO 分开：格式、冻结、未测量或核验失败返回 INVALID；合法结果未过质量门返回 NO-GO。

### 冻结前硬门中止的证据格式（本轮终态补记）

§1 的 v2 合同只接受完整正式实验，不能用假零/true 填充未测量项。冻结前已经 INVALID 的轮次按 [中止闭集 Schema](evidence/r-round-abort.schema.json) 输出 [独立 abort evidence](evidence/r-round-abort.json)：状态固定 INVALID、原因固定 CATEGORY_COVERAGE_INSUFFICIENT，三库正式 A/B 成绩只能 null；固定枚举、计数/布尔与 canonical UUID 外不承载私有材料。公开首提交身份为固定协议常量。生产实测差异为 false，未覆盖项为 null，不从 health 或硬编码全测量推导。该格式只记录终止与 cleanup，不运行/替代选型 harness、不授予重试资格，也不放宽 §2.1。既有 aggregate/decision 原件保留并标明历史归属。

## 2. 新 holdout 与运行前冻结

1. OPS builder 固定 `cwork-3m`、`docdb-touqian`、`spbp-2027` 三个 corpus snapshot，私有 manifest 留 OPS。
2. 从经 §2.1 来源项级排除后的源项逐库分层抽样。每库必须有非零 answerable、exact、no-answer 分母，并覆盖标题、正文稀有短语、表格行和近邻干扰。exact 是 answerable 子集；`total_count = answerable_count + no_answer_count`。
3. builder 读取材料建立 case；独立 verifier 回读完整库验证 expected/no-answer、类别覆盖和与 RT-054 pool 不复用。候选实现人员不得参与抽样或 query 派生。
4. 在任何候选运行前，冻结私有 case manifest、corpus snapshot、随机 A/B 顺序，以及两个候选的代码、镜像、配置、mapping、query plan、依赖 digests。聚合报告只带 OPS 受控 runner 生成的候选 artifact `sha256:` digest；绝不导出 case/corpus/query/expected/source hash。receipt id、holdout version、硬件/快照标签和角色 id 全部使用 schema 固定常量，不能承载数据。
5. OPS verifier 必须针对真实文件、镜像、checkout 与运行配置核验 freeze receipt，不能只检查报告中字符串。builder、verifier、candidate implementer 三个固定角色必须互不相同。A/B receipt id 必须各自固定、互异并绑定 candidate id，且 code/image/config/mapping/query-plan 五项不能全部相同。A/B 都要求 `frozen_before_run=true`、`ops_artifacts_verified=true`。B 还须核验固定 commit 可从官方 `github.com/Tencent/WeKnora` 到达、HEAD 精确匹配、tree clean、native config、core 未改；repository、receipt 与 commit 均是协议常量。
6. holdout 单次消费。候选代码、mapping、权重、parser 或配置因结果修改，立即废弃本轮并建立另一套独立 holdout，不得重跑取 PASS。

## 2.1 排除权威 R：运行前修订（Evan 2026-09-11 22:53 裁决）

### 历史事实与本次授权

RT-054 原始权威私有池已在收口时按合同销毁，属于设计内事实、不可恢复，不再等待恢复历史记录：

- [RT-054 OPS acceptance「清理与不变性」](../RT-054/evidence/stage-b-ops-acceptance-20260909.md#清理与不变性) 的清理回执行（当前仓库第 74 行）明确为「私有 workdir=0」，并同时记录临时索引、容器、派生镜像和 cleanup failures 为 0。
- [RT-054 quality JSON](../RT-054/evidence/stage-b-ops-quality-20260909.json) 的 `cleanup` 对象（当前第 105–113 行）内，`/cleanup/workdirs_zero` 在第 112 行为 `true`；同对象的 `case_files_zero=true`、`cleanup_failures=0` 与删除事实一致。行号由文件实读核对，链接指向真实文件，JSON Pointer 才是字段语义依据。

本节在新 holdout 尚未生成、两个候选均未正式运行时修订并先单独本地提交；不消费任何 holdout，不回改原 RT-054 或旧 INVALID 证据。旧 126 题及验证副本继续整体归档作废，不删题、不重采、不重新消费。本节取代此前“必须恢复历史实际 pool”的停止条件，不声称恢复了已删除的记录。

### R 的确定性定义

使用 [原 RT-054 generator](../../scripts/kb_stage_b_ops_cases.py)，与此前 verifier 完全相同的固定 seed `rt054-quality-v1-fixed-seed`、`SAMPLING_VERSION=ops-known-item-stratified-v2`、`SPLIT_VERSION=ops-category-ordinal-calibration-holdout-v1`，对本轮**完整只读当前快照**重建 RT-054 calibration 与 holdout 的来源项/query 集合。版本不符立即拒绝。R 在资格过滤和新随机 seed 派题前形成；R 的成员、成员来源关系和全部私有摘要只留 OPS。

R 的版本为 `rt055-current-snapshot-exclusion-r-v1`，至少包括三个跨库合并集合：

1. `doc_ids`：重建各题的来源文档与近邻干扰文档标识。跨库同值也保守排除。
2. `queries`：重建全部 query，经 NFKC、Unicode 连字符统一、casefold、连续空白折叠和首尾去空白后的值。
3. `tokens`：上述来源项的完整标题、完整文件名、文件名 stem，以及标题/文件名/正文内完整字母数字编号的同规则规范化值。完整字段也作为 token；不把文件扩展名或单个汉字单独当 token，不做候选专用 stopword 或按结果放宽。

每个成员保留其来源项集合，供逐项核销。权威实现见 [R 模块](../../scripts/rt055_exclusion.py)。这是当前快照重建的协议权威，不是历史精确记录；候选不能借本节宣称历史零泄漏已被证明。

### 派题前来源项级排除

[builder](../../scripts/rt055_builder.py) 先为每个来源项枚举可派生 query 的超集，包括所有标题/文件名、编号/日期、正文短语、表格候选，以及所有允许的 no-answer mutation 尝试；不是只检查最终选中的 query。任一路径命中就把**整个来源项**记入私有排除账本，不允许从同一项改用另一题：

- 来源 `doc_id` 命中 R；
- 任一可派生 query 规范化后命中 R；
- 任一标题/文件名/编号 token 命中 R。

先排除，再以一次全新随机 seed 固定可用来源顺序，最后派题；近邻干扰来源同样不得命中 R。过排除优先，即使因此无法满足六类目标也不放宽。检索 corpus 不因来源排除而删掉干扰文档；唯一性/no-answer 的判断继续使用完整库，两个候选使用同一份按原生摄取上限过滤的 canonical corpus，来源资格与候选对称。排除判定不读取新 holdout 的 expected 标注、候选输出或排名，不给 A/B 加任何特判。

builder 在任何源读取前用独占创建文件领取一次构建锁并记录随机 seed；已领取的轮次不覆盖、不重抽。源项 query 超集若遗漏了实际派生的 R query，直接拒绝整轮，不能跳题继续取 PASS。

### 独立 verifier 的拒绝条件

[verifier](../../scripts/rt055_verifier.py) 不导入 builder；以独立进程只读回读完整三库，核对源快照、资格过滤、私有文件完整性和独立重建 R 的一致性，重新计算来源顺序、expected/no-answer 与六类覆盖，不采信 builder 的通过布尔。

逐库检查所有输出题的来源/近邻 `doc_id`、规范化 query、来源 tokens 与 R 三集合均零交集；即使最后 query 干净，来源项其它可派生 query 命中 R 仍拒绝。逐个重算当前来源项应命中的排除理由，核对完整账本；每个 R 成员的全部当前来源必须被 builder 记录排除，其余来源必须确实不在当前回读快照。未核销成员、伪造/重复账本、任一交集、未知来源、任一题被拒或覆盖不足，均拒绝整轮。完整核销且三类零交集只是隔离门通过，仍须其它正式门全部满足；不能把测试通过替代真实 verify-cases。

### 残余风险与证据边界

内容可能跨 `doc_id` 迁移，且标题/文件名/编号也改变；这样的理论泄漏不能靠当前重建彻底消除。RT-054 距本裁决约两天，漂移窗口较小，但时间短不等于无漂移。此前 R 的前身 query 重建检查已命中上一作废集合 **4 / 16 / 15**，说明重建机制能实际检出重用，不能把这些计数说成历史实际 holdout 的精确重叠数，也不能据此宣称理论风险为零。Evan 的运行前裁决接受此残余风险，以本节 R 为本轮排除权威；不把它外推成历史语义 gold。

## 3. 固定候选

### A — `cwk-opensearch-dual-channel-v1`

- deterministic exact resolver 对编号、日期、文件名做通用 NFKC/大小写/连字符/日期格式规范化，只查摄取期 keyword 字段；不得包含 holdout 特判。
- 普通文本走官方 ICU BM25，沿用 Parent/Child 与 body excluded `_source` 投影；先按 `doc_id` collapse，再有界批量 Parent expand。
- 主结果不含 rerank。若资源允许，基础 A 完成并冻结后才运行独立 ablation，单列 Recall/P95 delta，不得混入 A。

### B — `weknora-native-8d7298fb5d759973cb1e481cadc5ecdf16dca599`

- 使用 upstream 可达的原版 commit `8d7298fb5d759973cb1e481cadc5ecdf16dca599` 原生 ingestion/retrieval pipeline。
- 实验外壳只做导入、库隔离、Top-10 评分和资源采集；不得 fork/patch core 或加入 A 的 exact resolver。

## 4. 公平运行、评分与测量合同

- 同一 snapshot、同一 query/expected、top_k=10、超时预算、硬件级别、冷暖口径；两候选都从空临时数据面完整构建。
- 每库回 `total_count / answerable_count / recall_hits_at_10 / exact_count / exact_hits / no_answer_count / no_answer_correct / system_error_count / answerable_system_error_count / exact_system_error_count / no_answer_system_error_count / timeout_count`。A/B 的 total/answerable/exact/no-answer 四个分母必须逐库相等。固定定义：Recall@10=`recall_hits_at_10/answerable_count`；exact=`exact_hits/exact_count`；no-answer=`no_answer_correct/no_answer_count`。`system_error_count = answerable_system_error_count + no_answer_system_error_count`，exact error 是 answerable error 子集，timeout 是 system error 子集；每类 hits/correct 必须 `<= denominator - category errors`，所以任何系统错误都不能与受影响类别满分并存。错误不从分母剔除。任何分母为零、跨候选分母不同、分子越界、比率与计数不一致均 INVALID。
- `p95_ms` 是相同预热后服务端请求至 Top-10 返回的端到端检索 P95，不含 LLM。系统错误仍进入请求延迟样本；未测量或 `<=0` INVALID。
- `index_bytes` 是完整可服务检索数据面；B 包括原生数据库/缓存/对象层。`build_seconds` 从接收规范化输入到全量可查询。`peak_rss_bytes` 是实验启动全部服务/容器之和。三项未测量或 `<=0` INVALID。
- 运维复杂度只回机械向量：组件数、升级步骤数、备份恢复步骤数，均按冻结 runbook 逐项计数且至少 1；不接受自报 `complexity_score`。Gateway 四能力逐项布尔验收，任一缺失即硬门失败。
- verifier 只导出 aggregate-only attestations：三个协议固定且互异的角色 id、角色分离、RT-054 pool 排除、输入不相交、类别覆盖、聚合白名单、无私有 digest。它不导出 case id/hash、query、expected、source、失败样例或可自由填写的 token。

## 5. 硬门与预先承诺的决策效用

逐库、逐候选必须满足 Recall@10 `>=0.90`、exact `=1.00`、no-answer `=1.00`、leak `=0`，且 Gateway 四能力全 true。任何库不得被平均抵销。

1. 仅 A 过门：选 A；仅 B 过门：选 B 并停止 A 产品化；都不过：NO-GO。
2. 都过门时执行本 RT **事前批准的产品决策效用**，不是宣称 A/B 性能比较无偏：A 是 incumbent，默认选 A；B 只有满足以下全部条件才替换：
   - 运维机械向量三个分量分别不高于 A；
   - P95 逐库不得恶化；只有三库每库都至少改善 20%，P95 才算一个显著胜项，禁止相加 P95；
   - index/build/RSS 各自以三库总量比较，`B_total <= 0.80*A_total` 才算显著胜项，同时任一库不得比 A 恶化超过 10%；
   - P95、index、build、RSS 四维至少三个显著胜项。

硬质量门提供公平可比结果；incumbency 与迁移成本政策只在双通过后应用。B 的实际迁移成本目前是**待实验证据**，不得作为已测事实。该政策避免为微小 benchmark 差异建设双栈，同时允许明确优势推翻 A。

## 6. OPS 授权边界（已于 2026-09-11 授权）

Evan 15:35 已授予本节边界，22:53 要求按 §2.1 修订后持续执行；不再等待逐步批准。现有授权只包括：在 OPS 建 RT-055 0700 临时目录并只读三库冻结快照；新建私有 holdout；创建 loopback-only A 临时索引；checkout/核验固定 B commit 并启动 loopback 原生依赖；冻结和核验真实 artifact；采集聚合质量/资源/机械复杂度/Gateway 能力；finally 删除 RT-055 临时索引、服务、容器和导入副本，保留私有 holdout/freeze 审计材料；复核 NAS、Gateway、现有索引和生产配置不变；仅带回 v2 聚合 JSON。

生产切流、持久服务、端口开放、NAS 写入、现有 alias/index/config 修改或删除均不在授权内。

## 7. 独立判断、风险与清理

**主推荐 A，B 是淘汰赛对照，不并行建设。** RT-054 只证明 ICU/body-excluded 历史投影在当时三库相对旧 lexical JSON 缩小 `94.602% / 95.997% / 97.067%`；这约 95% 是历史投影，不是完整 A 的已验证结果，完整 A 必须在新 holdout 和新临时索引复测。RT-054 的 NO-GO 只限当时 analyzer/mapping，不是 OpenSearch 架构失败。A 从机制上把 exact miss 移出 analyzer 排名，并保留已验证存储机制；少量 lexical miss 再由 ICU + collapse + Parent expand 处理。

B 的控制面、摄取、任务、鉴权、来源定位和 Gateway 迁移成本尚待实测。若 B 按上述公平门和事前效用胜出，就停止自研并迁移；否则实施 A，不维持双栈。

污染、候选漂移、缓存偏差、资源漏算、假 no-answer、泄漏或 cleanup 失败都使轮次 INVALID。所有临时资源使用随机 RT-055 prefix；cleanup 只允许该 prefix，且不得触碰现有资源。

## 8. 本地实验适配（2026-09-10 开发续段）

- `scripts/kb_retrieval_candidates.py` 是实验库，不是生产 SearchBackend，不启动服务，也不读取任何 RT-054 case/holdout。部署、临时库创建/授权、角色隔离与冻结仍由 OPS runner 在单独授权后完成。
- 两候选收到同一 canonical 输入：原始 title、filename 两行元数据，加空行和规范化正文。A 的 Parent/Child 与 B 的原生 manual Markdown ingestion 均消费这个输入；不对 A 单独执行整文/模板去重。
- A 摄取和查询共享 NFKC、casefold、Unicode 连字符和有效日历日期规范化。可识别编号、日期、文件名的 query 只走 keyword，多个提取值取 AND；零命中不回退全文。普通文本走官方 ICU BM25，标题/章节/正文权重 3/2/1，无 rerank，doc collapse 后 Top-10 单次有界 Parent mget。请求和展开共享一个截止时间。
- A 两个 index 均是随机 `rt055-<UUID>-c/p`，只清理该实例收到创建确认的资源。创建超时/未确认必须由 OPS 对该精确随机名称调和，不能声称 cleanup 完成；不扫描或删除其它资源。Parent 数据面也必须计入 index_bytes。
- B 固定官方接口经源码核验：`POST /api/v1/knowledge-bases/{id}/knowledge/manual` 的 title/content/status=publish；`GET /api/v1/knowledge/{id}` 逐项核验 parse_status=completed；`POST /api/v1/knowledge-bases/{id}/hybrid-search` 传原始 query_text 与 match_count=10。不注入 A 的 resolver、expected 或 rerank，不补齐去重后的文档数量。返回的所有条目先查导入映射和库域，再按原生排名取前十 chunk 评分。
- B 的新空库、原生配置/模型、认证 transport、checkout/image 固定版本证明及清理在 OPS 外层负责；客户端接口匹配不等于实际运行 artifact 已验证。原生依赖不得向外部模型提供私有输入。
- **日志上线前门**：固定 upstream 的 HybridSearch handler 记录 query、GetKnowledge 记录标题。必须用原生受支持配置禁用涉及私有输入的日志、Langfuse tracing 和模型 egress 并实际验证；不能仅因适配器错误已脱敏就宣布保密门通过。若原生配置无法满足协议，不 patch core，不执行 confidential holdout，报告阻塞。
- `score_cases` 只在 OPS 内消费私有 Case，输出逐库固定计数/比率/P95；不输出 case、query、source、异常内容。超时/错误仍在分母与 P95 样本内，无答案异常不能当作答对；跨库返回计 leak 与错误。它不生成完整 v2 报告，不伪造资源、Gateway、freeze、clean-up 或 verifier 成功证明。
- 当前只有 synthetic transport 测试，无真实 OpenSearch/WeKnora、三库质量、资源或公网 Gateway 测量。OPS 闭环尚未执行，不能因此关闭 RT 或选出生产候选。

## Amendment 3 — 固定容量 tier 与逐库延期（2026-09-12，运行前）

Evan 已批准本 Amendment 3 的容量规则；2026-09-12 00:24 本次指令只授权本地修订、验证和第一提交，明确禁止启动 OPS 第四轮，覆盖 §6 的历史持续执行授权。本节在第四轮 builder、freeze 和任何正式候选调用之前固化；与 §2/§2.1 的全库 T3、目标即硬门约定冲突时，以本节为准。前三轮协议迭代均发生在正式候选运行之前，没有正式候选结果（历史 synthetic/native 冒烟不算正式 A/B）；历史 INVALID 保留，不构成按候选成绩重跑取 PASS。合规提交链为 `45a6080a01f4fb6f3ed1aeaa16e7f2d13dad59d4` → `c86519425e260a45a11a89917cb0c6600d46a11f` → 本 Amendment 3 首提交（提交身份见 Git 与本次回执，避免提交内自引用未来 hash）。先单独提交本节、实现、Schema/harness、红绿测试及治理归属，本次到本地提交即停止，不部署 runner、不夹带运行数据。

### 固定选择与最低证据

- 每库仅在同一个独占构建 claim、同一完整当前快照、同一 R、同一个新随机 seed 内依次评估 **T3=doc_id+query+token → T2=doc_id+query → T1=doc_id**。每一 tier 都从该 tier 完整合格来源重新确定锁序并派出一套完整题池；不是在上一个 tier 上补题。第一次满足全部 floors 即停止；禁止越级、换 seed、重启 builder、补题、删类、把不同 tier/轮次题池拼接。
- 六类顺序与目标仍为 title_filename / exact_identifier_date / body_only_rare_phrase / table_row / no_answer_mutation / near_neighbour = **10/8/10/4/5/5**。逐类 validity floors 为 **3/2/3/3/3/3**，总数至少 **16**；同时满足六类事实上至少 17 题，但总数门仍独立校验。达到 floor 只表示最低裁决证据，不代表达到目标。DocDB 的 10/3/5/3/5/5=31@T3 直接有效；SPBP 的 3/1/3/3/3/3=16@T3 必须红。
- tier 选择只读取 corpus、R、固定 floors 和 seed，不读取候选输出、排名或效用。中间 tier 的题面仅在一次进程的内存中用于容量评估；持久化只有最终选定 tier 的完整题池。私有 manifest 保存全部已评估 tier 的六类计数、floor 判定、同 build/seed 证明与最终状态。verifier 独立重放选择轨迹和完整最终集合，不能只相信 builder 布尔。
- T1 仍不足则库为 **DEFERRED**：最终 tier=null，正式可消费池为空，保留 T3/T2/T1 容量轨迹而不保留中间题面。不得伪造 0 分；A/B 对该库的正式指标唯一闭集表示为 `{"status":"NOT_RUN_DEFERRED"}`，不包含计数、资源或质量。至少一库参与；全 DEFERRED 固定 **INVALID**。
- aggregate 顶层显式列出按协议库顺序排列的 `participating_libraries` 与 `deferred_libraries`；三库必须恰好分区。`library_validity` 保存闭集状态、tier、floors、目标、计数及完整前缀轨迹。参与库 A/B 分母必须等于最终题池计数；错误、资源、quality、Gateway、freeze、cleanup 和生产不变性硬门不放宽。
- R 的 doc_id 始终零交集。T3 还要求 query/token 零交集，T2 要求 query 零交集，T1 不宣称 query/token 零交集；所有 tier 仍逐项核销完整 R 成员及其来源。低 tier 的已放宽维度应实报交集计数，不能标成 T3 全隔离。历史语义迁移风险仍保留。
- DEFERRED 不进入任何 GO/NO-GO、质量、资源合计或候选效用。§5 中“三库”在本轮效用运算中严格替换为“全部参与库”，逐库门和双通过规则不变，不平均抵销。唯一裁决枚举 **A / B / NO-GO / INVALID**，显式带 deferred 列表。所有生产切流当前暂停；`production_candidate_libraries` 仅为研究裁决的参与库范围，不是切流授权。延期库不切流，语料增长过门槛后另行授权补证。

### 角色、执行与证据闭集

- builder、verifier、candidate implementer 使用独立 0700 工作区与独立进程；审计 PID、UID、真实 cwd、目录权限、独占调用 claim 及禁止读取矩阵。builder 禁读 verifier/implementer 数据与候选实现；verifier 可读 builder 私有产物但禁读 builder 代码及候选实现；implementer 禁读 query/expected 和 builder/verifier 私有产物。正式评分执行器另进程读取被冻结的 corpus/verified 输入，不作为 implementer 角色隔离证明。
- Python 进程审计阻断禁止读取并记录计数，启动时实际测试拒读。harness 的 `role_separation_level` 必须为 `PROCESS_LEVEL_SEPARATION_SINGLE_UID` 或 `OS_UID_SEPARATION`，且 `role_audit_verified=true`。同 UID 只能报告前者，不声称 OS 用户隔离或任意 syscall 沙箱；三个固定角色 id 不能替代执行证明，null 一律 INVALID。
- 第三轮三库部分题池整体归档作废并证明正式消费 0；第四轮使用新 UUID、新 seed、新 claim，不加载前三轮题池作为候选池、不消费或拼用旧池。builder/verifier 各一次，SSH 恢复先查 claim/PID/status，不重新提交已领取任务。
- verify 后为本轮独占建立新的 before 时间窗口；旧 cwork 漂移按现状取样，禁止回滚或修复。before/after 同口径实测全部现有 index/alias/统计、三 Gateway health/身份/配置指纹、生产相关进程、容器/卷投影及 NAS 完整文件覆盖元数据；覆盖不足是 UNKNOWN，不由 health 推导。外部漂移记 false，不重建 baseline。
- 正式前以合成 canary 实测正常和错误路径 query/title 日志禁用、Langfuse/tracing 禁用、私有输入模型 egress 限 loopback；不满足则不消费 holdout，不 patch WeKnora core。实际候选从空的新随机 UUID 数据面开始；禁 query/title/异常原文日志，不用预热结果代替四项 Gateway 能力验收。
- freeze 绑定真实 artifact、依赖、模型权重、runbook 及单次输入；query/corpus/expected、路径等私有 digest 一律留 OPS。导出 digest 仅限预先登记的公开代码/发行物/无私有值配置/映射/查询计划/依赖。生产不变性 false/null 可按闭集记录并由 decision 判 INVALID，不能伪造成功以满足 JSON Schema；未完成正式运行的中止证据另用闭集 abort 格式，绝不假填正式指标。
- 所有 OPS 长任务脱离式运行并写状态文件；finally 只清理本轮确认创建资源，创建结果未确认按精确随机名称调和。保留私有 holdout/freeze/审计，不删改生产/NAS/既有 index/alias/config，不 push。

### 第四轮 OPS 执行授权与必要执行器修复（2026-09-12 00:50）

Evan 本次明确授权执行第四轮 OPS 全闭环，覆盖上文只做本地第一提交的阶段停止条件；Amendment 3 第一提交为 `2cc386394610bd22f8a833d80304de95651d552e`，规则和历史证据不改。执行前检查发现 baseline 的 after 比较分支缩进错误：before 成功采集仍引用未定义的 comparison 并失败。合成成功路径实际复现退出 3，新增回归后只修该分支缩进，另测 after 保留 false/null 和原 baseline 不变；先单独提交修复再同步 OPS。未在发现前启动第四轮 builder、verifier、freeze 或候选；不属于按数据改规则、重抽或重跑。授权允许此类必须先提交的真实执行器修复，最终证据提交独立保留。

### v2 → v3 显式迁移与本地交付边界

- v3 新增严格分区、完整容量轨迹、延期未运行指标和非空角色枚举。v2 的历史 Schema/实现可由基线 `c86519425e260a45a11a89917cb0c6600d46a11f` 回读；当前 harness 对 v2 明确报 `LEGACY_V2_REQUIRES_NEW_VERIFIED_RUN`，不自动补角色/floor/tier，也不重新解释历史 INVALID。
- Schema 校验闭集与 floor 布尔、顺序和状态；harness 再重算计数合计、完整轨迹与指标分母等跨字段关系；真实 verifier 独立生成每 tier 的完整题池并比对选择，不共用 builder 的选择分支/floor 函数。三层必须同时成立，Schema-valid 不等于实测有效。
- DEFERRED 指标只接受闭集 `{"status":"NOT_RUN_DEFERRED"}`；不接受零分或夹带其它字段。本实现不另设 null 写法，避免两种语义漂移。
- 本次公开 [合成证据](evidence/amendment3-local-tests.json) 由 [严格 Schema](evidence/amendment3-local-tests.schema.json) 约束，不是 OPS 成绩。角色包装器只在本地合成 0700 工作区与独立子进程中验证；OPS 实测、原生日志/egress、固定 B binary 与 checkout 的构建绑定、全量生产覆盖及资源清理仍未验收。
- 原生 runner 作为待实测源码保留、全部编译检查，不据此宣称可启动第四轮。当前 baseline 对未覆盖完整内容/配置/索引的同值投影输出 null（已测漂移输出 false）；装配器不得把它转成 true，harness 仍判 INVALID。Gateway 实验 HTTPS shell 证明不等于生产部署证明。


### 第四轮 pre-freeze 执行器恢复授权（2026-09-12，父会话后续指令）

本次明确授权覆盖此前“必须另建协议轮”的停止条件，**只恢复同一第四轮的执行器隐私门，止于 READY_TO_FREEZE**。原 INVALID/中止证据不改、不删除；恢复不是正式实验重跑，更不是按候选成绩取 PASS。

- 先对账原 claim、进程、freeze、正式结果、consumption 和保留材料；原 builder/verifier 各一次，不重启、不换 seed、不改 R、tier、分母、质量/效用规则、v3 aggregate Schema 或 WeKnora core。96 份原私有/审计材料以 OPS 内逐文件字节摘要比较，摘要和路径不出 OPS。
- 只修执行器连接、进程生命周期、运行态观测和受支持原生配置。允许在独占 0700 合成子目录恢复已批准的公开工具/依赖/模型缓存；供给阶段不加载任何私有输入。候选服务仍受操作系统网络限制：A 单节点仅入站、明确 IPv4 loopback；B 与 embedding 只连 loopback。禁止通过取消沙箱、外放绑定或模型地址绕过失败。
- 必须真实完成 A 建索引/检索、B 原生摄取/检索与模型调用；鉴权后的正常及错误 query/title canary、原生日志扫描、实际进程环境与 socket 观测、JDK/Python 外连拒绝、非 loopback 模型地址拒绝都需实证。未执行路径、空日志集合或写死布尔不能过门。
- 合成启动/接线失败保留每次独占回执及日志；修复后可在新的空合成子目录复核，不能把这一权限用于 builder/verifier 或正式候选。私有目录对合成 Python 执行器拒读，对原生子进程拒读/拒写；禁止读取数必须为 0，主动拒读探针单列。
- 新恢复回执独立追加，绑定实际成功观测和公开执行器字节；旧失败 gate、source provenance、before/after 和 cleanup 记录不可覆盖。freeze/运行消费者重新计算隐私门并核验源绑定，不接受单个 `passed=true`。原有生产漂移与 UNKNOWN 不被恢复隐私门抵销。
- 完成后停止并精确清理合成服务/索引/导入数据，保留审计和待冻结公开依赖；只提交必要 runner/tests、本协议、验收及公开闭集证据/治理归属。不执行 freeze、正式 A/B、holdout 消费，不动生产/NAS/既有资源，不合并、不 push。后续正式阶段仍须父会话明确接续。


### 同一第四轮 formal window binding migration（2026-09-12 03:34 授权）

本次明确接续同一 UUID，覆盖前节 READY_TO_FREEZE 停止点和 e71ee34 的接线冲突停止点。e71ee34 的 pre-freeze INVALID 是历史执行器中止，不是本次终点；原 Amendment 3、db0d6b6、cab41c7、09e908b、e71ee34 全部保留。不重启 builder/verifier，不改 corpus/R/seed/tier/类别/floors、候选算法、质量阈值或双通过效用。执行器修复先独立本地提交，不 push；OPS 正式质量尚不能由本地合成通过推断。

- **不可变窗口引用**：新的 canonical UUIDv4 `window_id` 显式传入 baseline 的 before/after、freeze create/verify、正式 runner、cleanup 和 aggregate；私有文件位于同一独占 `formal-windows/<UUID>`。before/after 的状态、模式、run/window、私有 artifact 摘要必须一致。after 只比较该窗口 before，且装配时重算比较。默认无 window 的 baseline 仅保留历史入口；正式路径绝不 fallback 到它，也不自动迁移、重命名或覆盖旧 claim。
- **append-only**：baseline 各模式、freeze、verification、每候选执行 attempt、每候选/库消费、逐库评分/资源完成、最终 aggregate/decision 均独占创建。可变的 RUNNING/status 仅是观察投影，不替代不可变的 claim/terminal。重复已完成阶段只读核验，不重跑；全 run 作用域消费 claim 防止换 window 再消费。缺消费完成证据不能猜测“没跑过”。
- **版本化隐私来源**：09e 的 v1 恢复回执与源码集合保持历史原样。新 `executioner-migration-binding.v1` 独立绑定 migration UUID、已提交公开源码 commit/字节、独立新 synthetic attempt 的真实 observation/status/精确 cleanup 和旧恢复原件。观察须重算完整 canary、鉴权后正常/错误 query/title 日志、Langfuse/OTEL、真实模型、loopback listener、socket 与外连拒绝门；只改 hash、缺观察、旧 PASS、当前源或合成执行源漂移均拒绝。该门不读取 holdout。
- **冻结前迁移**：先在 OPS 归档旧公开源码和绑定/归档既有私有材料，再同步该 commit 的必要公开源；复核原 96 份材料字节、角色 1/1、freeze/消费 0。新真实合成门通过并精确清理后，才创建新的 formal window。freeze 绑定该 window 的 before 状态/私有 artifact、迁移隐私回执、完整执行器源、候选/依赖/权重/runbook。冻结后任何源/config/候选变更使该 freeze 失效；无自动再冻结取 PASS 路径。
- **正式 A/B 接线**：按 receipt 随机顺序运行，仅参与库，DEFERRED 无调用。候选服务、canonical/top10/预热/超时/资源采样和算法不变；只新增同窗口读写和单次消费检查。逐库评分前先持久化消费 claim，评分后立即追加评分回执，原有资源测量边界追加完成回执。候选最终结果必须逐库等于这些回执；smoke/旧结果不能通过改 window id 冒充正式结果。
- **故障与恢复**：长任务 OPS detached + status；SSH 断线先对账原 PID。执行 attempt 失败保留独占日志，不覆盖旧结果；未消费的执行器/网络/下载问题可对账恢复，已完成库不重复。消费 claim 无可核验完成状态时不重放；只能如实收口无法恢复的证据缺口。生产漂移不重建 baseline、不修生产。精确 finally/after 后才封装最终 v3 aggregate，从而满足同 window after 的强制绑定；不是用合成或旧窗口数据预填正式指标。
- **v3 结构扩展而非结果规则变更**：`formal_window` 闭集公开字段只含随机引用、公开 commit、运行顺序与已核验布尔；候选 freeze receipt 加同一 window id。Schema 保持不变性 false/null 可记录，harness 仍 fail-closed。旧 v3 缺窗口字段不会自动补齐成为新正式报告，历史证据保持原字节可追溯。

本地验证和范围见 [迁移合成证据](evidence/formal-window-migration-tests.json) 与 [验收追加记录](evidence/acceptance.md)。这些不是 OPS 实测成绩，不是生产切流授权。


## Amendment 4 — zero-exposure prequery void 与 replacement freeze（2026-09-12 05:01 授权）

本次明确授权继续同一第四轮，覆盖上节及历史收口中的单次 claim 不可迁移阻塞；**只恢复到 READY_TO_RUN，不启动正式 A/B**。不重建题池、不重复 builder/verifier、不换 R/seed/tier/floors、不改候选算法、Top-10、错误分母、资源边界、质量门或效用。旧窗口 `7bed2d1c-4943-4da6-b6d2-a23aab5c195f` 永久 INVALID，不复用其 freeze，也不称为已完成评测。前一会话延迟落盘的收口/并发警报保留为历史观察；本节是当前恢复授权，不追溯修改该观察。

### 严格零暴露例外：不能用于已查询或已评分的消费

1. 只允许已登记冻结公开源码可证明的 **PER_LIBRARY_RUNNER_VS_ALL_LIBRARY_SCORER_PREQUERY_CONFLICT**：逐库调用全库 scorer，在首次私有 `candidate.search` 前无条件失败。检查冻结版本完整公开源码摘要，独立解释器用公开三库合成输入复现三次 CandidateError/calls=0；该解释器拒读 holdout，诊断不能解析题面。对账实际私有账本和原始观测：query calls 必须严格为整数 0、score receipts/result 均 0、A 仅原 attempt/claim、B 无 attempt/claim，before/after 与 cleanup 已终止且无活进程。缺证据、任何查询/评分/结果、源漂移或歧义即不可重放。
2. 零调用原始 reconciliation、终态和原始公开 abort 中的 `private_holdout_reopened_for_diagnosis=false` 必须一致；冻结源码、私有字节与原生隐私观测绑定均重算，不接受重新填一个 passed。原 96 份材料、旧 window/claim/freeze/attempt/observations 和旧公开执行源先在 OPS 新独占目录逐文件归档，原件不动。正文与 digest 不出 OPS；完整性哈希读取不是重开题面或重跑 verifier。
3. [void 验证器](../../scripts/rt055_zero_exposure.py) 只能追加 **VOID_PREQUERY_NO_EXPOSURE**，绑定原 run/window/claim/freeze/attempt、migration id 和私有证据 hash。发布前再次确认旧部署源未变、全局 exposure=0、无活进程；发布后每次认可旧 claim 都重算原件/归档与证据绑定。不得删除、覆盖、改名 claim；不得以质量结果、异常率、部分得分或候选优劣触发。任何已有实际暴露都不能被 void。

### 单库评分与不可逆曝光账本

- [scorer](../../scripts/kb_retrieval_candidates.py) 新增 `score_library_cases(candidate,cases,library)`；完整验证且仅接受本库 answerable/exact/no-answer 后才查询，仅回本库 metrics。底层 `score_cases` 可显式指定 expected_libraries；缺省仍要求原三库完整类别。错误仍留在分母及延迟样本内，不修改检索算法或评分规则。
- A/B 都经 [同一 ledger/scorer 接口](../../scripts/rt055_window.py) 运行 **arm → exposure → score → complete**。arm 绑定 window/freeze/attempt，但不是消费；类别错误只留下 arm，不写 exposure。全 run 的候选/库 exposure 在首次实际私有 search 前以独占创建、文件与目录 fsync 持久化；持久化失败在评分异常捕获之外中止，不伪装成一次系统错误并继续查询。
- 同一候选/库跨任何 window 只允许一份 exposure。崩溃留下 exposure 而没有 complete，或留下不完整 exposure 文件，一律不可重试；不同窗口也拒绝。只有严格 void 验证通过的 legacy claim 才不阻塞新 arm/exposure；未确认 claim 继续 fail-closed。公开预热/隐私 canary 不计私有 holdout exposure，绝不以私有题预热。
- score 绑定 exposure id/原件 hash 与 window/freeze；complete 再绑定 score hash，读 result 时重新验证整条链及时间顺序。旧/smoke/外窗结果不得补字段冒充。after 或 superseded 标记后的旧窗口永久禁止新 execution attempt/arm。
- 合成与原生子进程的拒读/拒写矩阵扩展覆盖新 exposure、void、迁移私有归档和 formal-window 审计目录；不因为新增归档留下可绕过原 holdout 隔离的副本。

### 迁移与停止门

先独立本地提交修复、红绿/行为破坏证据与治理归属。OPS 验证严格零暴露后归档/追加 void，再同步此 commit，保留所有历史材料、builder/verifier=1/1 和同一 holdout。新 executioner migration id 必须以当前源真实重跑正常及鉴权错误 query/title 日志、Langfuse/OTEL、loopback model、egress/socket 隐私门；不复用旧 PASS，也不 patch WeKnora core。精确清理新合成资源后，创建全新 formal window、新 before、随机 A/B 顺序及 replacement freeze/verification；绑定当前 migration 与同一未变 holdout。向旧窗口只追加 `superseded-by-zero-exposure-migration` 回执，不覆盖原 INVALID。

最终要求全局 exposure=0、新 arm=0、正式 query=0、replacement freeze 合法、三 Gateway HTTP 200、无活跃 synthetic/native 进程。公开恢复回执只能含计数/布尔/枚举、公开 commit 与随机引用，不含私有 digest。停止 **READY_TO_RUN**，不执行 coordinator 的运行路径、不启动 A/B、不 push、不切流；NAS/index 完整性 UNKNOWN 与生产服务漂移仍如实保留，不被恢复成功抵销。

## Amendment 5 — formal runtime policy readiness（2026-09-12 06:23 授权）

本节只授权修复 pre-spawn 部署缺口、在同一未消费题池上建立全新窗口，停止 **READY_TO_RUN**。旧 replacement window `0155202c-b6a0-40c6-a779-48aff0ab57fe` 的 after 已关闭，永久 INVALID；B attempt=1 保留，不重新执行、不删除 claim，不改旧 freeze/void/abort。任何实际 query/exposure>0 或缺少严格 Popen 前零暴露证据，均禁止迁移。既有 Amendment 4 的旧 claim/void 规则不变。

### 根因与证据边界

只读核实两份主 run 策略与冻结 `a4b3c64` 的期望文本：网络规则相同，但每份均缺 exposure、void-prequery、zero-exposure-migrations、formal-windows 四类目录的读写保护（各 8 行）；旧网络 gate 的 passed 不验证当前策略文本。旧 freeze 未纳入主策略/网络回执，后续 migration 仅重验独立 synthetic root，直到 spawn 才发现主 root 不匹配。这是策略物化与冻结接线缺口，不是候选算法或评分故障。现存文本等价于早期三目录保护版本；仅凭文件 mtime **不能证明**其生成早于 migration，更不能推断谁修改了它。原 abort 原字节保留，本节补全而不追改此前“仅三类”的记录。

### 独占主 run 策略准备

1. 新增 [独立 readiness runner](../../scripts/rt055_runtime_readiness.py)，必须由新 executioner migration 的同一部署源码在 **formal before 启动之前**执行。主 root 的随机 run 身份、归属标记、当前部署源码/commit、migration 与新 window 必须一致。synthetic root 或带 protected-parent 绑定的根不能冒充主 root。
2. 原策略、原网络回执、旧源码和历史材料先在 OPS 受保护的新私有归档中逐字节保存，原件不动。新策略只写独占的 window 版本目录，目录与 claim 均 exclusive；失败 claim 留存，重复调用拒绝。没有“覆盖旧策略再重验”的隐式修复路径。
3. 调用同一 `runtime.network_probe(main root)` 的显式版本输出模式：公开 socket probe 必须实测普通策略允许 loopback、拒绝外连；搜索 inbound-only 策略拒绝全部 outbound。策略保持 loopback/inbound-only、外部 egress deny、根外拒写，builder/verifier 与 consumption/exposure/void/zero-exposure archive/formal-window/readiness 私有范围拒读拒写。新版本目录纳入保护；不改候选算法、WeKnora core、tier/seed/floors/质量门/score/效用，不改生产/NAS/index/alias/config。
4. append-only `runtime-policy-readiness` receipt 绑定主 root、run/window/migration/source commit、完整 migration source set、deployment、两份策略、真实 network gate、claim、受保护根/目录集合和开始/完成时间。验证器从部署源重算 canonical 策略，不接受只重填 passed 或重算错误策略的 hash。
5. [freeze](../../scripts/rt055_freeze.py) 在 freeze claim 前验证主 readiness，要求 ready 时间严格早于 before 的实测开始时间，纳入全部 policy/network/claim/deployment/receipt 文件；freeze verify 再重算。A/B 启动函数显式传 window 给 [spawn precheck](../../scripts/rt055_runtime.py)，在 Popen 前再次验证窗口开放、freeze verification、来源/策略/网络/receipt 文件及冻结摘要；正式主 root 不允许回退 generic 策略。测试可调用独立 precheck，但不得 Popen/查询。

### 验收与停止点

先红后绿；旧/stale 策略无 main readiness、synthetic 冒充、缺账本保护、外连放宽、错 source/migration/window、receipt/策略漂移、重复 claim、after-before 乱序、有 exposure 迁移、闭窗重开均拒绝。行为破坏在公开临时源码副本进行，恢复后跑完整 RT055 回归、compile、Schema/隐私/links/AODW/governance/diff。旧公开证据字节及文档旧前缀保持。

本地源码修复独立提交、不 push；OPS 在新 migration 下真实重跑公开合成隐私门并清理。之后主 readiness → 全新 before → 随机新顺序 freeze → 独立 verify → 只读 spawn precheck；不得因旧顺序或候选成绩选顺序。最终 builder/verifier=1/1、96 材料不变、旧 window/attempt/void/freeze 不变、global exposure/new arm/new attempt/formal query/result=0、Gateway 3×200、无候选/合成进程或临时数据面。公开 READY evidence/Schema/QA 单独提交，保留旧 INVALID，RT 不关闭、不切流；完整 NAS/index 不变性仍 UNKNOWN，历史服务漂移不被本次准备成功消除。

## Amendment 6 — candidate runtime workspace recovery（2026-09-12）

用户明确授权从 Amendment 5 的永久 INVALID 窗口恢复执行器，**不执行正式 coordinator/query**。
旧窗口曾读取私有 runner 输入、A attempt1、进程记录1；这不是“未执行”。其 arm/exposure/query/
score/complete/result 均0，after 已关闭，不重开、不重试、不覆盖。新窗口仍依现行 global exposure=0
规则建立；builder/verifier、题池/tier/seed、候选算法、scorer、质量门、模型与 WeKnora core 不改。

### 运行目录与控制账本分离

- 代码构造 `candidate-runtime/<window>/<a|b>/<attempt>`，0700；data/logs/home/tmp 归同一租约。
  窗口及 attempt 必须随机 UUID，父目录/租约/日志路径拒 symlink、escape、hardlink 和目录复用。
- 所有权、分配 claim、日志扫描和清理回执在候选不可写的控制区；记录 device/inode/uid 和完整身份。
  正式租约必须绑定真实 attempt claim；公开 startup probe 使用 migration 独立账本，不创建正式 attempt。
- A/B 共用同一目录合同。子进程网络策略保持不变；在现有文件拒绝规则上收紧为仅自身租约可写，
  拒读其它候选租约及正式控制账本。formal-windows 全目录继续拒读写，不为日志开洞。
- 停止精确 ownership 匹配的进程后，扫描最终日志；私有题面/expected/title/filename/id/path/body 等
  原文与 JSON 转义形式只在 OPS 内作为 needle。无日志、无 needle、读取/身份异常、命中均硬失败；
  命中不写 candidate complete/result，coordinator 必须 cleanup→after 后 INVALID。回执只有计数/布尔。
- 日志在 OPS 私有控制区归档后，按完整 window/candidate/attempt 和 inode 精确删除运行租约，验证0。
  不按 root 前缀清理另一窗口；不删除旧 data/claim/attempt/window/审计历史。

### 新断点门禁

先归档旧源码及失败证据，部署独立 source commit；新 migration 重做 source-bound 公开 synthetic
隐私验收（正常/鉴权错误日志、Langfuse/OTEL、loopback model、egress/socket），不读取私有 holdout。
主窗口 policy readiness 后、before 前，主 root 用同一 policy 与租约代码真实启动 A 三库、B 三库
及三个 sidecar；只用公开配置，不执行任何检索/评分。startup 的源码、策略、租约、扫描、cleanup
回执进入 freeze 的必需绑定。新 before、新随机 freeze/verify，再独立无 Popen precheck。
正式 attempt/arm/exposure/query/result/after 必须仍0，旧 A attempt1 不变。到 READY_TO_RUN 即停。

### 验证入口

- [运行目录与硬门测试](../../tests/test_rt055_candidate_workspace.py)
- [行为破坏测试](../../scripts/rt055_candidate_workspace_qa.py)
- [主运行策略真实 startup](../../scripts/rt055_candidate_startup.py)
- [旧路径公开 synthetic 根因证据](evidence/candidate-workspace-root-cause.json)

### Amendment 6 补充 — JVM 启动前日志

首次新源码的 OPS synthetic 在 JVM ergonomics 阶段被拒绝：发行包 `jvm.options` 中的相对
GC 日志目标仍指向只读发行目录。该失败与旧正式窗口的应用日志冲突属于不同启动层，失败回执保留，
公开 synthetic 已精确 cleanup；未打开 holdout，未创建新正式窗口/attempt。

JVM 会在逐项解析 `-Xlog` 时立即打开目标，后置 override 不能撤销前一次失败。修复在租约 data 内
独占复制原始 OpenSearch config，仅将 JVM GC/错误/heapdump 目的地改到本租约 logs/data；不改原配置、
heap 或其它选项。每次 spawn 重算该副本，config 原始字节进入 freeze 依赖指纹。

### Amendment 6 补充 — Bash here-string 与预启动清理归属

第二次公开 synthetic 已越过 JVM GC 初始化，但发行版 Bash 3.2 启动器的两个 here-string
依赖临时文件行为，被严格路径策略拒绝。本地真实 syscall 已证明：原 here-string 失败，同一策略下
显式 owned stdin 文件成功。只在执行参数中使用源码重算的启动器文本，保留 `$0` 与原命令解析；
把两个空 keystore 密码 here-string 替换为本租约独占、内容严格为换行的 stdin 文件。不修改发行包，
不授权共享 `/tmp` 或无路径 fd 写入。原 launcher 字节同 config 一并绑定 freeze。

预 before 的 synthetic startup 资源记录不属于正式 attempt，正式 cleanup 必须忽略其已终止的
审计记录；进程回执增加随机后缀并独占写入，避免 PID 再用时覆盖历史。

### Amendment 6 补充 — 仅本租约祖先 metadata

第三次公开 synthetic 已启动主 JVM，但 Lucene/keystore 的 canonical-path 检查逐级读取租约
父目录 metadata，被全 runtime 的读拒绝拦住。本地真实 `realpath(strict=True)` 已重现。
只向本租约已知的三个父目录授予 literal `file-read-metadata`，不授予目录枚举、文件内容或
其它候选访问；formal-windows 与其它账本仍完全拒读写。两种策略下 realpath 成功、父目录
listing 与 sibling 内容读取仍拒绝，另有断开 metadata 接线的行为破坏测试。

### Amendment 6 补充 — native config 与兜底日志

第四次公开 synthetic 已证实 A 启动/检索成功、B sidecar 启动成功；B native 因工作目录改变而
找不到公开 `config/config.yaml`。按官方 loader 的相对路径规则，将原始公开 config 逐字节复制到
每个 B 数据目录，native cwd 也落在该目录；原 WeKnora core/config 不改，配置字节进入 freeze。
新配置副本在 spawn 前重算，新增/篡改/escape 均拒绝。数据库、对象数据和相对缓存不再落回源码树。

日志扫描与留存同时覆盖租约内所有 `logs/` 子目录及 `.log` 文件，防止 native 兜底日志躲在数据
目录；数据库与文档内容不是日志，不参与该扫描，也不归档成日志。该接线有真实命中拒绝测试。

### Amendment 6 补充 — 公开 SQLite schema 依赖

native 的官方 migration loader 还依赖 cwd 下的 `migrations/sqlite`。第五次 synthetic 的
HTTP 初始化失败及清理回执保留；把这份**公开应用 schema 文件**与 config 一起逐字节复制、
spawn 前核对并纳入 freeze。它不是执行器/曝光控制账本，后者的隔离不变。
资源采集仍统计数据库、WAL、对象文件与缓存；仅排除新复制的、字节核对过的公开 config/schema
静态依赖，避免把程序支持文件误计为索引数据。候选检索、scorer 和质量门不变。
