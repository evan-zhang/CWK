# RT-055 三库 confidential holdout 实验协议

## 1. 目的与隔离

本实验只回答候选 A 或 B 哪一个成为 CWK 下一条生产实现路线，不是 RT-054 analyzer/mapping 续调。OPS owner 在 RT-054 最终 holdout 消费后，从从未进入 RT-054 case pool 的三库当前快照重新抽样。query、expected/no-answer 标注、原文、标题、文件名、路径、locator、命中片段及这些材料的 hash、case-set 摘要、失败样例永远留在 OPS 0700 私有目录，不提交、不复制到开发机、不进入日志或返回 JSON。

仓库只接收 `contracts/aggregate-report.schema.json` v2 聚合 JSON。所有非度量字符串均冻结为协议常量或严格枚举；唯一动态字符串是 canonical run UUID，以及由 OPS 受控 runner 对候选非 confidential 构建 artifact 生成的 `sha256:` digest。禁止人工填充、私有 case/corpus/query/expected/source hash 或任意 opaque token。harness 对字段闭集、固定常量、artifact digest、严格 UUID、分母/分子和比率二次校验并 fail closed。INVALID 与质量 NO-GO 分开：格式、冻结、未测量或核验失败返回 INVALID；合法结果未过质量门返回 NO-GO。

## 2. 新 holdout 与运行前冻结

1. OPS builder 固定 `cwork-3m`、`docdb-touqian`、`spbp-2027` 三个 corpus snapshot，私有 manifest 留 OPS。
2. 从未进入 RT-054 的源项逐库分层抽样。每库必须有非零 answerable、exact、no-answer 分母，并覆盖标题、正文稀有短语、表格行和近邻干扰。exact 是 answerable 子集；`total_count = answerable_count + no_answer_count`。
3. builder 读取材料建立 case；独立 verifier 回读完整库验证 expected/no-answer、类别覆盖和与 RT-054 pool 不复用。候选实现人员不得参与抽样或 query 派生。
4. 在任何候选运行前，冻结私有 case manifest、corpus snapshot、随机 A/B 顺序，以及两个候选的代码、镜像、配置、mapping、query plan、依赖 digests。聚合报告只带 OPS 受控 runner 生成的候选 artifact `sha256:` digest；绝不导出 case/corpus/query/expected/source hash。receipt id、holdout version、硬件/快照标签和角色 id 全部使用 schema 固定常量，不能承载数据。
5. OPS verifier 必须针对真实文件、镜像、checkout 与运行配置核验 freeze receipt，不能只检查报告中字符串。builder、verifier、candidate implementer 三个固定角色必须互不相同。A/B receipt id 必须各自固定、互异并绑定 candidate id，且 code/image/config/mapping/query-plan 五项不能全部相同。A/B 都要求 `frozen_before_run=true`、`ops_artifacts_verified=true`。B 还须核验固定 commit 可从官方 `github.com/Tencent/WeKnora` 到达、HEAD 精确匹配、tree clean、native config、core 未改；repository、receipt 与 commit 均是协议常量。
6. holdout 单次消费。候选代码、mapping、权重、parser 或配置因结果修改，立即废弃本轮并建立另一套独立 holdout，不得重跑取 PASS。

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

## 6. OPS 授权边界（当前停止点）

未经单独授权，本提交不连接 OPS/NAS/生产。下一次授权只包括：在 OPS 建 RT-055 0700 临时目录并只读三库冻结快照；新建私有 holdout；创建 loopback-only A 临时索引；checkout/核验固定 B commit 并启动 loopback 原生依赖；冻结和核验真实 artifact；采集聚合质量/资源/机械复杂度/Gateway 能力；finally 删除 RT-055 临时索引、服务、容器和导入副本，保留私有 holdout/freeze 审计材料；复核 NAS、Gateway、现有索引和生产配置不变；仅带回 v2 聚合 JSON。

生产切流、持久服务、端口开放、NAS 写入、现有 alias/index/config 修改或删除均不在授权内。

## 7. 独立判断、风险与清理

**主推荐 A，B 是淘汰赛对照，不并行建设。** RT-054 只证明 ICU/body-excluded 历史投影在当时三库相对旧 lexical JSON 缩小 `94.602% / 95.997% / 97.067%`；这约 95% 是历史投影，不是完整 A 的已验证结果，完整 A 必须在新 holdout 和新临时索引复测。RT-054 的 NO-GO 只限当时 analyzer/mapping，不是 OpenSearch 架构失败。A 从机制上把 exact miss 移出 analyzer 排名，并保留已验证存储机制；少量 lexical miss 再由 ICU + collapse + Parent expand 处理。

B 的控制面、摄取、任务、鉴权、来源定位和 Gateway 迁移成本尚待实测。若 B 按上述公平门和事前效用胜出，就停止自研并迁移；否则实施 A，不维持双栈。

污染、候选漂移、缓存偏差、资源漏算、假 no-answer、泄漏或 cleanup 失败都使轮次 INVALID。所有临时资源使用随机 RT-055 prefix；cleanup 只允许该 prefix，且不得触碰现有资源。
