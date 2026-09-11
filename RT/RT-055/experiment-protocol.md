# RT-055 三库 confidential holdout 实验协议

## 1. 目的与隔离

本实验只回答候选 A 或 B 哪一个成为 CWK 下一条生产实现路线，不是 RT-054 analyzer/mapping 续调。OPS owner 在 RT-054 最终 holdout 消费后，从三库当前快照中未命中 §2.1 排除权威 R 的来源项重新抽样；R 是本轮协议定义的权威依据，不是历史实际题池的精确副本。query、expected/no-answer 标注、原文、标题、文件名、路径、locator、命中片段及这些材料的 hash、case-set 摘要、失败样例永远留在 OPS 0700 私有目录，不提交、不复制到开发机、不进入日志或返回 JSON。

仓库只接收 `contracts/aggregate-report.schema.json` v2 聚合 JSON。所有非度量字符串均冻结为协议常量或严格枚举；唯一动态字符串是 canonical run UUID，以及由 OPS 受控 runner 对候选非 confidential 构建 artifact 生成的 `sha256:` digest。禁止人工填充、私有 case/corpus/query/expected/source hash 或任意 opaque token。harness 对字段闭集、固定常量、artifact digest、严格 UUID、分母/分子和比率二次校验并 fail closed。INVALID 与质量 NO-GO 分开：格式、冻结、未测量或核验失败返回 INVALID；合法结果未过质量门返回 NO-GO。

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
