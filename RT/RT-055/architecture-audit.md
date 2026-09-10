# RT-055 代码与架构审计

## 基线和材料边界

- 工作树基线已由 `git rev-parse HEAD` 核验为 `f34918bb2158099f03eba99ffcc95e59714a15f2`，启动时无本地改动。
- 已完整读取根 `AGENTS.md`、AODW 宪章/交互/RT/Git/测试规范、项目 overview、`RT/index.yaml`，以及 `RT/RT-054/` 下全部合同、证据、机器 JSON、元数据和最终方案。
- 仓库对原版 WeKnora 的固定证据只有 RT-054 对官方 `github.com/Tencent/WeKnora` commit `8d7298fb5d759973cb1e481cadc5ecdf16dca599` 的源码审查结论；仓库没有 vendored WeKnora 源码或独立 source manifest。RT-055 不把摘要冒充源码。部署实验必须从该固定官方 upstream 取得精确 commit、验证 HEAD 后运行原生 pipeline，仍不得改 core。

## 当前代码事实

1. **SearchBackend 尚未实现。** `SearchBackend` 只出现在 RT-054 目标合同；当前 `kb_gateway.py` 直接装载 legacy `lexical-index.json`，`kb_gateway_client.py` 是 v2 薄客户端，未存在可替换的 backend 接口。
2. **Parent/Child 是离线 PoC，不是生产路径。** `kb_stage_b_poc.py` 定义 `Parent`/`Child`、稳定 ID、结构化切块和本地 BM25；OpenSearch 与 OPS runner 只用于 benchmark，没有接入 Gateway、控制面、增量 Worker 或生产索引。
3. **RT-054 已证明历史存储投影成立。** 三库 ICU/body excluded 主索引相对旧 lexical JSON 缩小 `94.602% / 95.997% / 97.067%`。这是 RT-054 当时 Parent/Child、取消全量 1/2/3-gram 与正文不进 `_source` 的历史投影证据；不是完整候选 A 的实测结论，A 必须在 RT-055 新 holdout/临时索引复测。
4. **RT-054 没有证明 OpenSearch 失败。** 最终 NO-GO 的 `quality_gate_scope` 明确是 `lexical_analyzer_and_mapping_selection_only`：ICU v2 在 cwork Recall@10 为 `0.88`，docdb exact 为 `0.80`，因此当前 mapping/query 未过门；OpenSearch 的存储门、隔离门和清理门均通过。
5. **缺口具有可分解机制。** exact 编号/日期/文件名不该继续依赖 analyzer 排名；它们可由规范化元数据上的确定性解析器解决。剩余少量正文/表格 lexical miss 才交给 ICU BM25，并先做文档折叠与 Parent 展开，避免长文 Child 霸榜。
6. **不得直接复用 RT-054 holdout。** 该集合已经用于 ICU v2 裁决，继续拿它开发会产生反馈污染；它只作为历史证据，不进入 RT-055 的 query、expected 或抽样池。

## 候选 A 的最小实现接缝

```text
Gateway / Query API
  -> 授权固定 tenant + kb + epoch
  -> QueryClassifier（只识别编号、日期、文件名的通用形态）
     -> ExactResolver（规范化 keyword/date/file metadata；确定性排序）
     -> ICU BM25（title/section/body）
  -> union + exact-first policy
  -> collapse doc_id（每文档有界 Child）
  -> batch Parent expand（有界上下文）
  -> response authorization recheck
```

- exact resolver 使用摄取期生成的 normalized keyword/date/filename 字段；不包含库名、case ordinal、expected doc 或 holdout 特判。
- 普通文本保留 RT-054 已验存储投影，不恢复 1/2/3-gram，不把 Parent 正文复制进 `_source`。
- rerank 不进入主候选。只有在 A 无 rerank 的冻结结果完成后，才能作为独立 ablation；必须预先写明模型/版本、Recall 增量和 P95 代价，失败不改变 A 的基础配置。
- `SearchBackend` 实现需覆盖 health/search/bulk-index/invalidate-version/stats；本冲刺只冻结接口和实验合同，不在未授权环境假造 OpenSearch 连接。

## 候选 B 的边界

- 唯一身份：官方 `github.com/Tencent/WeKnora` 原版 commit `8d7298fb5d759973cb1e481cadc5ecdf16dca599`；repository id、commit、receipt id 都冻结为协议常量，不接受任意 fork 标签。
- 聚合回执无自由文本旁路：角色、环境、快照和 receipt 标签全为常量，只有 OPS 受控 runner 生成的 run UUID 与候选构建 artifact SHA-256 可变；A/B freeze 分别绑定候选且关键 artifact 不得全部同值。
- 质量计数按 answerable/exact/no-answer 分区记录系统错误，timeout 只是 system error 子集；受影响类别的成功数上限扣除错误数，错误不能伪装成满分。
- 使用其原生 ingestion/retrieval pipeline；不 fork、不修改检索 core、不把 CWK exact resolver 塞入 B。
- 只允许外部薄适配器完成同 corpus 导入、知识库隔离、Top-10 结果转成 OPS 私有评分输入和资源测量。
- WeKnora 的产品控制面、数据库/缓存/存储和升级链均计入运维复杂度；不能只测其底层 OpenSearch/ParadeDB 查询而免除系统成本。

## 独立判断

**推荐 A，进入实现；B 是有硬边界的淘汰赛对照，不是并行建设路线。**

理由：RT-054 的历史投影显示约 95% 存储缩减，失败集中在可由确定性元数据解析消除的 exact miss 和少量 lexical miss；完整 A 尚须在 RT-055 复测，不能把 95% 外推成它的既成成绩。A 可复用 Parent/Child、ICU projection、权限模型、v2 read 和未来 Query API 设计，只新增可测 exact 通道及生产 backend；这不是继续调 analyzer。转向 WeKnora 可能涉及摄取、控制面、任务、鉴权、来源定位和 Gateway 合同迁移，但迁移成本当前是待实验证据，不能自报为已测事实。只有固定 B 的公平实验可决定这笔迁移是否值得。

只有 OPS 新 holdout 出现以下结果才改选 B：A 未过任一硬质量/安全门而 B 全过；或两者全过时，B 的机械运维向量逐项不高于 A，P95 逐库不恶化且须逐库改善 20% 才记一胜，index/build/RSS 三库总量改善 20% 且逐库恶化不超过 10%，四维至少三胜。两者全过时默认 A 是事前批准的 incumbency/迁移决策效用，不是无偏性能结论。除此之外实施 A，不维持双栈。
