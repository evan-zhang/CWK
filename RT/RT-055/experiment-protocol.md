# RT-055 三库 confidential holdout 实验协议

## 1. 目的与隔离

本实验只回答：候选 A 或 B 中哪一个应成为 CWK 下一条生产实现路线。它不是 RT-054 analyzer/mapping 的续调。

OPS owner 在 **RT-054 最终 holdout 已消费之后**，从当时未进入 RT-054 case pool 的当前三库源快照重新抽样。以下材料永远留在 OPS 0700 私有目录，不提交、不复制到开发机、不进入日志或返回 JSON：

- query 文本；
- expected document / no-answer 标注；
- source 原文、标题、文件名、路径、locator、命中片段；
- 上述内容的 hash、case-set 摘要和失败样例。

仓库只接收符合 `contracts/aggregate-report.schema.json` 的聚合 JSON。`kb_retrieval_decision.py` 二次扫描敏感 key/路径/多行文本并 fail closed。

## 2. 新 holdout 生成与冻结

1. OPS owner 固定三个 corpus snapshot：`cwork-3m`、`docdb-touqian`、`spbp-2027`；记录仅在 OPS 的 snapshot manifest。
2. 从未进入 RT-054 的源项中逐库分层抽样。每库至少覆盖：编号/日期/文件名 exact、标题、正文稀有短语、表格行、近邻干扰、no-answer mutation。exact 与普通文本必须分开报告，不能只回 macro。
3. 两名角色分离：case builder 读取源并生成 query/expected；verifier 独立回读源验证 expected 与 no-answer。候选实现不得参与抽样或查询派生。
4. 在运行任何候选前冻结私有 case manifest、候选配置、corpus snapshot 和运行顺序；随机化 A/B 顺序，防缓存顺序偏差。冻结值不导出仓库。
5. holdout 单次消费。任何候选代码、mapping、权重或 parser 因结果而修改，都必须废弃本轮并重新建立新的独立 holdout；不得重跑同一集合取得 PASS。

## 3. 固定候选

### A — `cwk-opensearch-dual-channel-v1`

- deterministic exact resolver：统一 NFKC、大小写、连字符、日期格式；只查摄取期建立的 identifier/date/filename keyword 字段。
- 普通文本：官方 ICU BM25；沿用 RT-054 Parent/Child 与 body excluded `_source` 的存储投影。
- 候选集合先按 `doc_id` collapse，每文档/Parent 的 Child 数有界，再批量展开 Parent 上下文。
- 主候选不含 rerank。若资源允许，rerank 只能在基础 A 完成且冻结后作为独立 ablation，单列 Recall delta 和 P95 delta，不得混入 A 主结果。

### B — `weknora-native-8d7298fb5d759973cb1e481cadc5ecdf16dca599`

- checkout 后必须验证 HEAD 为 `8d7298fb5d759973cb1e481cadc5ecdf16dca599`。
- 使用原版 WeKnora 原生 ingestion/retrieval pipeline；`core_modified=false`。
- 仅允许实验外壳做数据导入、库隔离映射、Top-10 评分和资源采集；不得重写检索核心或把 A 的 exact resolver 加给 B。

## 4. 公平运行合同

- 同一 corpus snapshot、同一 query/expected、同一 top_k=10、同一超时预算、同级硬件和冷/暖口径。
- 两候选均从空临时数据面完整构建；构建时间从开始接收规范化输入到全部可查询为止。
- `index_bytes` 统计候选完整可服务检索数据，不用压缩备份或删字段半成品；WeKnora 同时报告其数据库/缓存/对象层，但 schema 中的 index 指原生检索数据面。
- `peak_rss_bytes` 覆盖候选为本实验启动的所有服务进程/容器之和；记录采样间隔于 OPS 私有 runbook。
- `p95_ms` 为服务端收到请求到返回 Top-10 的端到端检索时间，不含 LLM；先做固定预热，A/B 预热次数相同。
- 运维复杂度由预先冻结的机械量组成：组件数、升级步骤数、备份恢复步骤数，加预先冻结 rubric 的 `complexity_score`；不得跑完后为偏好候选改权重。
- 公网 Gateway 接入按四个布尔能力验收：HTTPS Query API、每 Gateway 身份、服务端 KB grants、Gateway 不持 NAS/搜索凭据。任一缺失视为该候选硬门失败。

## 5. 硬门与决策规则

逐库、逐候选全部满足：

- document Recall@10 `>= 0.90`；
- exact `= 1.00`；
- no-answer `= 1.00`；
- `leak_count = 0`；
- 公网 Gateway 四项能力全部为 true。

P95、索引体积、构建时间、RSS 与运维复杂度是通过硬门后的选择指标，不得用总平均抵销任何库的硬门失败。

决策固定：

1. A 通过而 B 未通过：选 A。
2. B 通过而 A 未通过：选 B，停止 A 产品化。
3. 两者都未通过：NO-GO，不选折中或拼装第三候选。
4. 两者都通过：默认选 A；只有 B 的运维复杂度不高于 A，且 P95、索引体积、构建时间、RSS 四项三库总量中至少三项优于 A `>=20%`，才改选 B。

该规则体现当前独立判断：A 已复用经验证的约 95% 压缩投影和 CWK 权限/来源合同；B 要承担迁移控制面、摄取、任务、鉴权、来源定位及 Gateway 适配的成本，必须以显著实测优势推翻 A。

## 6. OPS 授权边界（下一道门）

未经单独授权，当前提交不会连接 OPS、NAS 或生产。下一次所需授权精确限定为：

1. 在 OPS 建一个 RT-055 专属 0700 临时工作目录，读取三个已授权知识库的冻结快照；源与现有索引只读。
2. 建立 **新的** 私有 holdout；query/expected/source 永留 OPS，只导出 schema 白名单聚合。
3. 启动 loopback-only 临时 OpenSearch 索引运行 A；不得改现有 index/alias/Gateway/config/service。
4. checkout 并验证固定 WeKnora commit，启动仅 loopback 的临时原生依赖栈运行 B；不得 fork/patch core，不接生产入口。
5. 采集同硬件级别质量、P95、index bytes、build time、RSS、机械运维复杂度与 Gateway readiness。
6. finally 删除 RT-055 临时索引、服务、容器和临时导入副本；保留 OPS 私有 holdout/freeze 供审计；复核 NAS、Gateway、现有索引和生产配置不变。
7. 仅将聚合 JSON 带回仓库运行 decision harness。任何生产切流、持久服务、端口开放、NAS 写入、旧索引删除都不在本授权内。

## 7. 风险与清理合同

- **污染风险**：RT-054 holdout 或失败样例进入开发即废弃 RT-055 轮次。
- **实现不对称**：给 A 加 exact、却给 B 改 core 会破坏候选定义；两者都必须由 identity gate 拒绝漂移。
- **缓存/顺序偏差**：冻结并随机化运行顺序，报告同冷暖口径。
- **资源漏算**：WeKnora 全部原生依赖与 A 全部实验服务都计入 RSS/运维，不只量搜索进程。
- **假 no-answer**：verifier 必须扫描完整库确认 mutation 不存在；候选零结果和系统错误分开。
- **泄漏**：聚合输出遇敏感 key、私有路径、多行内容即拒绝；失败时也不回 case detail。
- **清理误伤**：所有临时资源带随机 RT-055 run prefix；cleanup 只允许该 prefix。清理失败使总裁决失败，不能以质量 PASS 覆盖。
