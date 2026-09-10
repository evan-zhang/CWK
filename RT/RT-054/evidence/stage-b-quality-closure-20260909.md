# RT-054 阶段 B staged 质量闭环

## 唯一裁决

**NO-GO / analyzer=None / mapping=None；停止调参，不进入阶段 C。**

本轮严格 holdout 只裁决 lexical analyzer/mapping，不是外部人工语义 gold，也不构成产品语义验收。候选 ICU v2 在三库都不低于同 corpus legacy，no-answer 与串库门也全过；但 cwork-3m Recall@10=`0.88<0.90`，docdb-touqian exact=`0.80<1.00`，因此硬门失败。legacy 仅作比较基线，不要求自身 Recall>=0.90。

机器权威：`stage-b-ops-quality-20260909.json`，schema `cwk.rt054.ops-quality-closure.v1`，随机 run id `847fd0b8-0ff1-47f9-95a8-5a05ccde5068`。历史存储 benchmark `stage-b-ops-benchmark-20260909.json` 未改写，acceptance SHA-256 仍为 `69c8b6db3058487ad29ba2025b850780b09a5fb0f25d90da557a1b75bcd12a48`。

## split 与冻结合同

- 固定语料为每库 42 题、总计 126；六类分别为 title/filename 10、exact identifier/date 8、body-only rare phrase 10、table row 4、no-answer mutation 5、near-neighbour 5。
- 抽样版本 `ops-known-item-stratified-v2`，split 版本 `ops-category-ordinal-calibration-holdout-v1`。每库每类别先按稳定 ordinal 分配 split，再派生 query；每类 calibration/holdout 两边都有 case。
- 每库 calibration 14、holdout 28；三库 holdout 共 84。calibration 只导出匿名 `category + subtype + count` 聚合，不含 case、query、doc、正文或任何内容派生摘要。
- calibration 完成后，mapping/query SHA 只写入 OPS 私有 freeze 文件；冻结确认后执行一次 holdout。三次前置失败都发生在 calibration 聚合阶段，没有发出 holdout 查询。正式 holdout 后停止调参，不再重跑。
- 全量口径只由 calibration 与 holdout 两个不相交结果在内存中合并诊断，没有第二次执行 holdout，也不参与最终 gate。

## ICU v2 通用规则

- mapping：`cwk-child-mapping-v2-icu-body-excluded` 候选，正文排除 `_source`。
- exact identifier/date：索引值与 query 同时做 NFKC、大小写与日期格式规范化，覆盖大小写、全半角和中/短横线日期边界。
- title/filename：显式写入 normalized keyword exact 字段并走 exact term boost。
- 普通 title/body：统一 ICU `match_phrase` + `operator=and`，使用通用 title/section/body boost；table 与 near-neighbour 使用同一规则。
- 没有 failure ordinal、库名、expected doc 或特定 case 分支；SmartCN 未构建、未查询。

## 逐库 holdout 质量与存储

### cwork-3m

- holdout 28；可命中题 25、no-answer 3。
- legacy：Recall@10 `16/25=0.64`，exact `5/5=1.00`，no-answer `3/3=1.00`，leak `0`。
- ICU v2：Recall@10 `22/25=0.88`，exact `5/5=1.00`，no-answer `3/3=1.00`，leak `0`。
- ICU win/tie/loss `14/10/1`；匿名失败：body-only rare phrase 2、table row 1，subtype 均为 `expected_not_top10`。
- 本轮 primary store：legacy `107,453,166` bytes，ICU v2 `80,719,031` bytes；相对已验收旧 lexical `1,495,220,459` bytes 缩小 `94.602%`，存储门 PASS。

### docdb-touqian

- holdout 28；可命中题 25、no-answer 3。
- legacy：Recall@10 `17/25=0.68`，exact `4/5=0.80`，no-answer `3/3=1.00`，leak `0`。
- ICU v2：Recall@10 `23/25=0.92`，exact `4/5=0.80`，no-answer `3/3=1.00`，leak `0`。
- ICU win/tie/loss `13/11/1`；匿名失败：exact identifier/date 1、near-neighbour 1，subtype 均为 `expected_not_top10`。
- 本轮 primary store：legacy `3,398,686` bytes，ICU v2 `1,861,772` bytes；相对已验收旧 lexical `46,513,952` bytes 缩小 `95.997%`，存储门 PASS。

### spbp-2027

- holdout 28；可命中题 25、no-answer 3。
- legacy：Recall@10 `11/25=0.44`，exact `5/5=1.00`，no-answer `3/3=1.00`，leak `0`。
- ICU v2：Recall@10 `24/25=0.96`，exact `5/5=1.00`，no-answer `3/3=1.00`，leak `0`。
- ICU win/tie/loss `17/7/1`；匿名失败：body-only rare phrase 1，subtype `expected_not_top10`。
- 本轮 primary store：legacy `13,409,511` bytes，ICU v2 `7,756,447` bytes；相对已验收旧 lexical `264,466,313` bytes 缩小 `97.067%`，存储门 PASS。

## 安全、清理与不变性

- OpenSearch `3.3.2`、Lucene `10.3.1`、官方 `analysis-icu=3.3.2`；临时服务只绑定 OPS `127.0.0.1`。
- SSH 凭据只由本机 `/Users/evan/.openclaw/gateways/life/.env` 经 Python 解析后放入子进程 `SSHPASS` 环境；命令、日志和 evidence 未打印凭据。
- NAS、旧索引和 3 个生产 Gateway 全程只读；未修改 Gateway、NAS、旧索引或生产配置。
- 前后 Gateway PID count 均为 3，聚合指纹一致；三库旧 metadata 聚合指纹前后一致。
- finally 后私有 case/query/freeze 文件、临时索引、容器、派生镜像、私有 workdir 全部为 0，cleanup failures=0。
- 最终 JSON 不含 case/query/title/filename/path/doc/content/locator，也不含它们的派生 hash、case-set 摘要或内容派生摘要；只保留逐库 holdout 聚合、primary store、匿名失败聚合、随机 run id 与安全/清理指标。

## 证据边界

A 层仓库合成题与本轮 B 层 confidential known-item 都不是外部人工真实用户语义 gold。本结果只能回答：在固定真实 corpus、固定 lexical case 合同下，ICU v2 是否可替换 legacy analyzer/mapping。它不能证明开放式意图理解、跨段推理、最终回答正确性、excerpt/locator 可读性、Gateway/token 鉴权撤权、增量索引或生产可用性。文档与裁决不得把 Stage B lexical 结果扩张为产品语义验收。
