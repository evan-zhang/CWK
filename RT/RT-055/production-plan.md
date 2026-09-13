# RT-055 生产化方案 — cwk-opensearch-dual-channel-v1

状态：方案已选定（RT-055 简化考试 A 三库全过线；B 公平性复核确认无考场不公，真实能力差距）。
本文是从实验代码到生产代码的施工方案与部署路线。实验证据见 `evidence/simplified-*`，B 复核见本地留档 `runs/rt055-simplified/b-fairness-audit.md`。

## 1. 范围

- **In**：检索服务生产化——标准包结构、索引构建、双通道查询、HTTP API、部署工件、测试、部署文档。
- **Out（需单独授权/窗口）**：生产切换执行、NAS/生产系统/OPS 主机改动。

## 2. 代码落点（标准方案）

新增正式包 `adapters/opensearch_retrieval/`（沿用仓库 adapters/ 惯例，实验代码 `scripts/kb_retrieval_candidates.py` 中的 OpenSearchCandidate 逻辑为起点重构）：

- `indexing/`：摄取管线（source projection → OpenSearch 文档）、索引模板（ICU analyzer、BM25、parent/child、doc collapse 结构）
- `query/`：双通道查询——exact resolver（编号/日期抽取 → term/AND 过滤）、lexical（ICU BM25）、合并 + collapse + parent 展开、no_answer 判定（零命中即拒答）
- `service/`：HTTP API（`POST /query`、`GET /healthz`、`GET /readyz`），对接 kb_gateway
- CLI：`build-index` / `smoke-test`
- `config/`：环境配置模板（端点、端口、库注册表、top_k）

部署工件：

- `deploy/docker-compose.yml`：OpenSearch（内置 ICU 插件安装）+ 检索服务，默认只绑 loopback
- `deploy/README.md`：Mac（launchd/compose）与 Linux（compose）两条部署路径

## 3. API 契约

- `POST /query {bank, query, top_k}` → `{hits: [{doc_id, score, channel}], no_answer, took_ms}`
- 错误语义：未注册库 404、后端不可用 503、参数错 400；不静默降级
- 真实语料内容永不进入代码/测试/文档；测试一律合成 fixtures

## 4. 测试与验收

- 单测：索引模板生成、exact resolver 解析、双通道合并、no_answer 判定（合成 fixtures）
- 契约测试：API schema 校验
- 冒烟：compose 起 OpenSearch → `build-index`（合成样例）→ `smoke-test` 断言通过
- 生产验收（阶段 1 之后）：shadow 并跑 ≥24h 零错误；灰度期 recall/exact/no_answer 不低于考试基线（recall ≥90% 等同门槛）

## 5. 部署路线（生产 + 其他环境）

1. **阶段 1 shadow**：目标机部署 OpenSearch + 检索服务，摄取库快照，与现有检索并跑、只对比不切流
2. **阶段 2 灰度**：部分查询切双通道，指标对账
3. **阶段 3 切换**：全量；旧检索保留一个版本作为回滚路径

可移植性约束：

- 环境差异全部进配置文件/环境变量；docker-compose 一键起；无硬编码路径与凭据（凭据只走环境变量）
- 服务默认只绑 loopback；对外暴露必须显式配置
- 数据边界（不变）：NAS 原文只读；日志/代码/测试不含真实语料内容；索引重建幂等可重复

## 6. 风险与回滚

- OpenSearch 需预留内存（建议 1–2GB heap，目标机部署前确认余量）
- ICU analyzer 插件必须随镜像/安装步骤内置，不可依赖运行时联网安装
- 回滚：旧检索代码路径保留一版 + 摄取幂等，切回即可
