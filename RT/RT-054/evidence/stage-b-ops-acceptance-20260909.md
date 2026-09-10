# RT-054 阶段 B：OPS 三库原地 benchmark 收口

> 本文是不可改写的存储/投影历史收口，机器 evidence acceptance SHA 仍为 `69c8b6db3058487ad29ba2025b850780b09a5fb0f25d90da557a1b75bcd12a48`。后续严格 calibration/holdout lexical 闭环见 `stage-b-quality-closure-20260909.md` 与 `stage-b-ops-quality-20260909.json`：legacy 只作同 corpus 基线，候选 ICU v2 仍因 cwork Recall@10=0.88、docdb exact=0.80 而 NO-GO。该补测只裁决 analyzer/mapping，不是外部人工语义 gold，也不构成产品语义验收。

## 裁决

**NO-GO，不进入阶段 C；不固定 analyzer/mapping。**

三库真实正文投影后的存储门全部通过，shared physical index + `kb_id` filter 的 90 次取回探针未见串库；但 OPS 没有可安全识别并带 legacy 基线的人工固定 gold。真实 Recall@10、exact Top10 和 no-answer 都是 `UNKNOWN`，按硬门不能 PASS。仓库内合成 72 题没有代替真实 gold。

机器权威：`stage-b-ops-benchmark-20260909.json`，schema `cwk.rt054.ops-three-library-benchmark.v1`，SHA-256 `69c8b6db3058487ad29ba2025b850780b09a5fb0f25d90da557a1b75bcd12a48`。

## 环境与边界

- 远端运行时：OpenSearch `3.3.2`，Lucene `10.3.1`。
- 官方插件：`analysis-icu=3.3.2`、`analysis-smartcn=3.3.2`，均与 runtime 匹配。
- 官方 base image ID / RepoDigest 均为 `sha256:798cf28e226a32f5c928dd1ed9478dd3a33d2212176aad3679020088ad3afa1a`；OPS 两个官方 registry 暂不可用，因此经受控 SSH 流式导入本机已缓存的同一官方 digest，再在 OPS 通过 Docker Desktop CLI 安装两插件并验证实际 runtime，不以 tag 作为证据。
- 单节点、security disabled、1 shard、0 replica，只绑定 `127.0.0.1` 高位端口。未接 Gateway 流量，未修改 NAS、旧索引或生产配置。
- 三库正文只在 OPS 内存与临时索引中出现；机器结果不含 query、title、filename、doc_id、path、Parent/Child ID、正文或命中片段。

## 三库投影与主存储

口径：每库真实 eligible 文档；Parent 800–2000 tokens，Child 200–500、硬上限 700；默认零重叠，只在强制切分时 40 tokens；模板按库去重。分母只用该库旧 `_system/lexical-index.json` 字节数。下列均为 body 排除 `_source` 的独立物理索引、refresh 后 force merge 到 1 segment 的 primary store。

### cwork-3m

- 500 documents / 11,773 Parents / 47,803 Children；旧 lexical JSON 1,495,220,459 bytes。
- legacy 1/2/3-gram：107,394,880 bytes，缩小 92.817%；body postings 等价 `sum_doc_freq=14,634,844`；build 11,073.173ms。
- ICU：72,142,992 bytes，缩小 95.175%；`sum_doc_freq=5,011,693`；build 7,825.985ms。
- SmartCN：75,959,144 bytes，缩小 94.920%；`sum_doc_freq=5,598,076`；build 34,012.520ms。
- 10-hit filter 取回（body excluded）：legacy / ICU / SmartCN P50 3.860 / 3.191 / 2.994ms，P95 6.147 / 5.167 / 4.665ms；响应中位 8,016 / 7,996 / 8,036 bytes。

### docdb-touqian

- 114 documents / 275 Parents / 772 Children；旧 lexical JSON 46,513,952 bytes。
- legacy：3,398,686 bytes，缩小 92.693%；`sum_doc_freq=317,305`；build 387.706ms。
- ICU：1,718,687 bytes，缩小 96.305%；`sum_doc_freq=94,126`；build 268.248ms。
- SmartCN：1,734,015 bytes，缩小 96.272%；`sum_doc_freq=93,144`；build 555.214ms。
- 10-hit filter 取回：P50 3.668 / 3.173 / 3.019ms，P95 5.622 / 5.003 / 4.521ms；响应中位 11,007 / 10,987 / 11,027 bytes。

### spbp-2027

- 90 documents / 1,112 Parents / 3,561 Children；旧 lexical JSON 264,466,313 bytes。
- legacy：13,409,511 bytes，缩小 94.930%；`sum_doc_freq=1,910,340`；build 1,018.646ms。
- ICU：7,063,687 bytes，缩小 97.329%；`sum_doc_freq=476,026`；build 724.547ms。
- SmartCN：7,167,679 bytes，缩小 97.290%；`sum_doc_freq=483,626`；build 1,604.581ms。
- 10-hit filter 取回：P50 3.785 / 2.911 / 3.157ms，P95 5.574 / 4.854 / 4.243ms；响应中位 9,501 / 9,481 / 9,521 bytes。

## `_source` 与 shared physical index

三库合并为 52,136 Children 的 shared physical index，所有 probe 都带 `kb_id` filter。body excluded / included primary store：

- legacy：123,371,938 / 117,557,265 bytes；build 12,768.593 / 11,770.815ms。
- ICU：80,859,338 / 75,044,665 bytes；build 8,756.586 / 8,281.948ms。
- SmartCN：84,774,378 / 78,959,705 bytes；build 36,036.122 / 35,597.682ms。

body included 的 10-hit 响应约 20–22KB，body excluded 约 8–11KB。磁盘数值保留实测原样：本轮 body included 的 primary store 反而略小，不能从单次 one-segment 结果推导“存正文更省空间”；生产候选仍应依据最小响应面与质量门，不按这一反直觉差值拍板。

shared lane 的 JVM heap max 为 4,294,967,296 bytes；观测 heap used 1.232–2.292GB。容器 RSS HWM 4.999–5.190GB。它们是 OpenSearch 容器边界，不是 Gateway RSS 或 macOS 全机内存。

## Gold 与失败边界

- OPS 自动扫描到的候选机器文件没有形成可识别的目标库人工 gold；最终可计分 case=0、category=0。
- `real_gold_recall=UNKNOWN`，原因 `no_ops_manual_gold`。
- `exact_top10=UNKNOWN`，原因 `no_ops_manual_gold`。
- `no_answer=UNKNOWN`，原因 `no_ops_manual_gold`。
- 匿名失败 case ID：无（没有可计分 case，不生成或伪造 ID）。
- shared physical filter probe：3 analyzers × 2 source modes × 3 libraries × 5 repeats = 90，`cross_kb_leaks=0`。这只证明本轮 filter 结果，没有把缺失的真实权限/gold 行为包装成质量 PASS。

## 清理与不变性

最终 `finally` 验证：

- 临时索引前缀=0；临时容器=0；派生镜像=0；私有 workdir=0；cleanup failures=0。
- 3 个生产 Gateway 的 PID、端口和命令 SHA-256 基线未变。
- 三库 `kb.json/raw-index/lexical-index/lexical-readiness/manifest` 元数据摘要未变。
- 原文、规范化文本、Parent/Child、query、命中正文和内部路径未离开 OPS。
- 本地 evidence 仅保留模板数量与删除行实例数；内容派生的模板指纹清单已移除并由测试禁止。

## 唯一选择

**NO-GO / analyzer=None / mapping=None。**

存储和 filter 隔离已经足够推翻“真实三库必然过不了 80%”这一担忧，但不能填补真实 gold。只有补入 OPS 现有、人工确认、带旧实现基线的固定问题后，才能在同一 shared-index 映射上比较 legacy / ICU / SmartCN 并选择唯一 analyzer；在此之前不进入阶段 C。
