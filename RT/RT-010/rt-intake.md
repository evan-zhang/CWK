# RT-010：本地检索实体域约束加固

## 背景

CWK 本地检索目前把 Wiki 的人物/系统/项目页面作为实体导航层，并直接使用它们
的 30 条汇报清单做候选召回。这带来两类稳定的失效：

1. **实体识别不完整**：同一实体在编译期就被拆分成多张页面（`TBS.md`、
   `TBS训战.md`、`TBS训战系统.md`、`TBS平台.md`、`SFE-TBS.md`、
   `projects/TBS.md`、`projects/TBS训战.md` 等），并且每张页面都被
   `REPORT_LIMIT=30` 截断。以 TBS 为例，本地镜像至少有 380+ 条汇报提到
   TBS，但没有任何一张页面能够单独承担完整的召回。
2. **无 scope 硬约束**：`query_mirror` 只用 BM25 分数与朴素的标题匹配，
   对“TBS 项目进展与下一步”这样的查询，即使前 8 条结果里一条 TBS 相关都
   没有，也会被打成 `high`；同时可能被“杨晶晶最近参与什么”里出现过的
   实体页短语拉高得分，导致 scope 精度失守。

之前提出的 RT-009 用一个大型固定基准题（数十道 TBS 相关查询）来兜底，
在评审中被判定为“对特定实体过拟合”，且违背“不写 TBS 特定代码/权重/固定
排名”的约束，因此被否决。RT-010 用通用规则从根源上重建实体域约束。

## 目标

- 在本地镜像内构建一份**完整、确定性、可校验**的机器实体目录
  （entity catalog / postings），并将它绑定到持久化搜索索引；
- `cwk_wiki_query` 在明确解析到实体时，对候选集执行 scope 硬过滤，
  确保 Top-K 中每条结果都能通过 scope 审计（`scope precision = 100%`）；
- 实体识别失败或 postings 空集时，返回结构化的“安全空结果”，
  不再回落到全局 BM25；
- 保证一切规则通用（不写 TBS 特定代码、不设固定目标排名、不引入固定
  基准题集），并可通过 property/synthetic 测试反复验证；
- 只影响本地检索路径，云端/影子模式保持暂停状态，不改动 cron 与
  DocDB 契约。

## 边界

- 实现文件范围：
  - `scripts/cwk_entity_catalog.py`：机器实体目录、别名家族与边级 provenance；
  - `scripts/cwk_wiki_search_index.py`：索引构建及 catalog 快照绑定；
  - `scripts/cwk_wiki_query.py`：实体解析、scope 硬过滤与证据闭包；
  - `scripts/cwk_sync_mirror_to_docdb.py`：明确排除 Local-First 实体派生物
    及镜像 registry 覆盖，含重试队列陈旧路径清理；
  - `config/entity-family-registry.json`：版本化、可审计的跨类型合并注册表
    唯一真源；
  - `tests/test_wiki_entity_scope.py`、`tests/test_rt010_registry_binding.py`、
    `tests/test_cloud_first_index_restore.py`：实体检索、registry 三处审计绑定、
    Local-First 排除与暂停 cloud 路径兼容回归；
  - `RT/RT-010/**/*.md`：需求、方案、任务与交付记录；
- `wiki/_system/entity-family-registry.json` 仅作为本地实验覆盖，被 sync
  排除、不进入 DocDB、也不被视为规范来源；
- 摘要（`wiki/summaries/*.md`）的 `## 候选实体` 段落是机器目录的**主要**来源；
  raw 原文作为**次要**来源，仅允许扩展已知规范/已批准别名的 postings（严格
  边界匹配，不生成新 surface / 家族）；
- `wiki/entities/`、`wiki/topics/`、`wiki/sources/` 都仅作为人类导航，
  不再参与机器 scope 判定；
- 不修改 `cwk_cloud_wiki_topics_entities.py` 的 30 条限制（这是给人看的），
  只把机器检索的 scope 层从它剥离；
- 不修改 nightly cron；只在 nightly 现有 `wiki_search_index` 步骤内部
  额外产出 catalog，并在现有同步器中增加本地派生物排除规则；
- `.serena/`、`.spec-workflow/` 不动；本次交付不写产线数据、不发布。

## 判定 USER DECISION

当查询能唯一确定实体，但其 postings/候选 scope 在应用显式过滤后为空时：

- 返回 `entity_resolution.status = "resolved_empty"`；
- `confidence = "none"`；
- `results = []`；
- `global_fallback_used = false`；
- `reason` 给出人类可读理由：识别出实体但没有匹配的汇报。

**永远不允许**在这种情况下回落到全局 BM25。
