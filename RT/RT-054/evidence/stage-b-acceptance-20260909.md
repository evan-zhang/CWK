# RT-054 阶段 B OpenSearch 整改验收

> 本文保留仓库 50-doc fixture 的历史收口。OPS 三库存储 benchmark 见 `stage-b-ops-acceptance-20260909.md`，acceptance SHA 保持为 `69c8b6db3058487ad29ba2025b850780b09a5fb0f25d90da557a1b75bcd12a48`；严格 calibration/holdout lexical 质量闭环见 `stage-b-quality-closure-20260909.md`。三库存储门仍 PASS，但 holdout 中 cwork ICU Recall@10=0.88、docdb ICU exact=0.80，最终 NO-GO。legacy 只作同 corpus 基线，不要求自身达到 0.90；本裁决不构成产品语义验收。

## 裁决

**NO-GO。OpenSearch 3.3.2、官方 ICU/SmartCN 与 scope 统计边界均已实测，但阶段 B 完成门仍未达到；不进入阶段 C，不关闭 RT-054。**

三个硬失败：

1. shared-filter、body 排除 `_source` 时，ICU/SmartCN 相对 legacy 的 primary store 只缩小 **16.047% / 16.384%**，远低于 ≥80%。
2. SmartCN macro document Recall@10 为 **52/53 = 0.9811**，虽高于 0.90，却低于 legacy/ICU 的 53/53，漏掉 G05「对齐」，因此“不低于 legacy”失败。
3. 仓库 fixture 是 28-doc `rt051_a11` + 22-doc `rt054_extension`，不是 cwork-3m、docdb-touqian、spbp-2027 的三库等价脱敏语料，不能证明逐库主索引门。

## 独立复核 FAIL 闭环

### 1. 离线 scoped BM25

`kb_stage_b_poc.search_scores` 先固定 `kb_id + doc_id_prefixes` 内的 children，再只用该集合计算 `n_docs`、`avgdl`、每个 query term 的 `df`、chunk 分数和文档排序。scope 不再是候选末端过滤。

行为判据覆盖三种本地 analyzer：

- G20「甲乙丙丁」在未限定的 liba 合并域会命中 `synthetic:801/802`；
- 指定 `rt051_a11` 的 `docdb:` scope 后，legacy、ICU probe、SmartCN probe 均返回 honest no evidence；
- 向 scope 外新增、删除或把同词词频放大到上千次，scope 内文档的分数和顺序逐项不变。

本地 ICU/SmartCN 仍明确标为等价 probe，不冒充官方插件；真实插件证据来自下述 OpenSearch lane。

### 2. shared-filter 与 isolated-scope 分 lane

机器权威：`stage-b-opensearch-benchmark-20260909.json`（schema `cwk.rt054.stage-b-opensearch-benchmark.v2`；SHA-256 `3fd0721be395db6d8bf58768ed176dfccca91c55b305bf54f00ccb8625129e36`）。

- **shared-filter lane**：一个物理索引含全部 50 docs；`kb_id + fixture_scope` term filter 只限制候选。Lucene 的 BM25 field/term statistics 仍来自整个物理索引，报告禁止称为 scoped IDF。
- **isolated-scope lane**：`rt051_a11` 与 `rt054_extension` 分别建立独立物理索引，只用于比较原 fixture 评审域；其 Lucene 统计是各自物理索引统计，不能冒充 shared-index 生产模型。
- `_termvectors` 证据直接记录两 lane 的 `field_statistics` 和 `term_statistics`。例如 ICU body 的 shared field `doc_count=50`，`rt051_a11` isolated 为 28；legacy 因 CJK-only body analyzer 只对有词项文档计数，shared/isolated 分别为 44/22。两者都符合 Lucene 的“有该字段词项的文档数”口径，而不是业务文档总数。

## 运行环境与镜像边界

- 服务：single-node、security disabled，仅绑定 `127.0.0.1:19200`；1 primary shard、0 replica。
- 实测 OpenSearch：**3.3.2**，build `6564992150e26aaa62d4522a220dfff5188aeb88`，Lucene 10.3.1。
- 官方插件：`analysis-icu=3.3.2`、`analysis-smartcn=3.3.2`，均与 runtime 匹配。
- 官方 base：`opensearchproject/opensearch:3.3.2`，RepoDigest `sha256:798cf28e226a32f5c928dd1ed9478dd3a33d2212176aad3679020088ad3afa1a`；在临时派生镜像中仅安装上述两个官方插件。
- 失败会话留下的 `cwk-opensearch-bench:3.3.2` / `sha256:e2c9f4fe...` 被拒用：其 OCI label、`opensearch --version` 和两插件都实际为 3.2.0。机器结果同时保留这条 rejected-image 证据，不把错标签算作 3.3.2 实测。
- JVM heap max/committed 为 1,073,741,824 bytes；最终证据点 heap used 600,651,992 bytes、non-heap 252,874,064 bytes。
- 容器 `/proc/1` VmRSS/VmHWM 为 1,556,148,224 bytes。这是 Docker Linux 容器中的 Java 进程 RSS，不是 JVM heap，也不是 macOS 宿主进程 RSS。
- macOS 宿主物理内存 25,769,803,776 bytes；OpenSearch `os.mem` 看到约 8.22GB Docker VM/cgroup 边界，不冒充宿主内存。

## shared-filter 50-doc 实测

全部运行都使用相同 50 docs / 50 Parents / 50 Children；refresh 后 force merge 到一个 segment，再读取 primary store。

### body 排除 `_source`

- legacy 1/2/3-gram：66,436 bytes；字段 unique term 合计 1,219；build/index/refresh/force-merge = 25.415/11.507/13.905/14.492ms；查询 P50/P95/P99 = 1.905/4.512/8.854ms。
- ICU：55,775 bytes；term 合计 440；20.472/10.381/10.086/18.164ms；1.788/4.142/5.941ms；相对 legacy 缩小 16.047%。
- SmartCN：55,551 bytes；term 合计 426；20.689/12.327/8.356/14.847ms；1.511/3.110/4.201ms；相对 legacy 缩小 16.384%。

### body 进入 `_source`

- legacy：62,228 bytes；build/index/refresh/force-merge = 26.971/17.249/9.712/14.956ms。
- ICU：51,572 bytes；24.866/16.236/8.624/12.841ms；相对 legacy 缩小 17.124%。
- SmartCN：51,348 bytes；16.836/9.794/7.038/12.102ms；相对 legacy 缩小 17.484%。

50-doc 小索引中，body 进入 `_source` 的 primary store 反而比排除方案小约 4KB。这是固定开销与小段压缩噪声主导的真实结果，不能解释成“存正文更省磁盘”。

## 质量、精确编号、权限与取回

每个 shared run 消费 72 题：62 题实测，10 个旧 Gateway/token/read/index-fault 行为题结构化 SKIP并排除分母。

- legacy：53/53，Recall@10=1.00；ICU：53/53，1.00；SmartCN：52/53，0.9811，失败为 G05「对齐」。
- 精确编号：三者均 5/5，Recall@10=1.00；`017` 不误命中 `AB-017`。
- 无答案：三者均 7/7；G20 按 `rt051_a11` scope 返回 honest no evidence。
- 权限/串库：G32 unauthorized-scope leak=0；G33 admin 请求 libb 能命中 `docdb:701`。这是 detached OpenSearch 的 `kb_id + fixture_scope` filter 证据，不是 token/Gateway 鉴权证据。
- source 排除时取回 50 条约 36.1–36.3KB、body 字段 0；source 进入时约 39.7–39.9KB、body 字段 50。取回延迟约 2.4–3.7ms，只是 loopback 单节点微基准。
- isolated-scope lane 的 primary store 因每个小索引各自固定开销，不与 shared lane 做生产容量比较；其用途仅是原评审域的 per-index BM25 对照。

## 测试与清理

专项测试覆盖：REST HTTP/transport/non-JSON 错误、loopback URL 限制、mapping/analyzer、strict mapping、bulk 投影、fixture_scope、日期/编号路由、source 两模型、plugin/runtime、metric schema、shared vs isolated Lucene 统计、逐题质量、权限探针和 cleanup。缺服务时 integration 以带原因的 SKIP 退出；本机最终使用真实 3.3.2 服务运行，未走 SKIP。

- 真实服务相关回归：RT-051 A11/acceptance + RT-054 Stage A/B 共 **54 tests PASS**，integration 使用真实 3.3.2 服务且未 SKIP。
- 清理后的全量 RT-054 专项：**83 tests PASS，3 个结构化 SKIP**；其中 2 个只在固定 pure-local child 内运行，1 个因服务已按要求删除而 SKIP。前一条 54-test 记录已证明真实 integration 通过。
- benchmark 自身 finally 删除 12 个唯一前缀临时索引；`_cat/indices/<prefix>-*` 复核为空。
- 临时容器和派生镜像在最终真实服务测试后删除；没有连接 Gateway、NAS/OPS、PostgreSQL 或生产。
- `rt-guard --rt RT-054`：13 PASS / 0 error / 1 non-blocking WARN（仓库 pre-commit hook 未安装）。
- 9 个 RT-054 JSON 全部可解析；benchmark v2 validator 通过；3 个 contract 均声明 JSON Schema Draft 2020-12，Stage A contract tests 通过。
- `make aodw-check`：PASS（framework fixture 79/0、RT-028..054 全过）；仅提示宿主 `handover-pack` skill 未安装，不阻断。
- `make governance-audit`：PASS，771 个 tracked files 全部有 ownership/evolution path。
- staged + unstaged `git diff --check`：PASS；15 个 staged 文件凭据模式扫描 clean；`docs/handover/` 未暂存且未修改。

## 阶段 B 完成门

- Parent/Child 投影：**PASS（PoC）**。
- scoped 本地 BM25 统计与 G20 污染闭环：**PASS**。
- OpenSearch 3.3.2 + 官方 ICU/SmartCN：**PASS**。
- shared-filter 与 isolated-scope 证据边界：**PASS**。
- body `_source` 两模型：**PASS（50-doc 微基准）**。
- candidate primary store 缩小 ≥80%：**FAIL**，ICU/SmartCN 仅 16.047%/16.384%。
- macro Recall@10 ≥0.90 且不低于 legacy：ICU **PASS**；SmartCN **FAIL（低于 legacy）**。
- 精确编号 Recall@10=1.00：**PASS**。
- detached filter 无串库且授权域可命中：**PASS**；Gateway/token 权限不属于本阶段证据。
- 三库等价语料逐库门：**FAIL / 未测**。
- 固定生产 analyzer/mapping：**FAIL**。

## 下一步

保持 RT-054 `in-progress`，不进入阶段 C。只有取得三库等价脱敏语料并逐库复测 primary store、质量、构建、取回与资源边界，才可重新裁决。若仍不能缩小 ≥80%，按 RT 停止条件回方案门评估原版 WeKnora；不恢复大 JSON，也不进入控制面/Worker 开发。
