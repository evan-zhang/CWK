# RT-054 阶段 B 离线 PoC 验收

## 裁决

**NO-GO。阶段 B 的可替换投影、离线 benchmark 和判据已完成，但阶段 B 完成门未达到。**

原因有三项，均不可用推算替代：

1. 同一合成语料上，ICU 等价探针和 SmartCN 等价探针相对 legacy PoC 只减少 43.951% / 47.453%，未达到 80%。
2. 本机没有实际执行 `analysis-icu`、`analysis-smartcn` 或 OpenSearch；`index_bytes` 是确定性序列化 PoC 大小，不是 OpenSearch primary store。
3. 阶段 A 的仓库内脱敏 fixture 只形成 liba/libb 两个合成库，不能证明三个目标库的等价语料门。

因此本提交可作为阶段 B PoC commit 保留，但不能固定生产 analyzer/mapping，也不能进入阶段 C。

## 实现范围

- `scripts/kb_stage_b_poc.py`：完全离线的 Parent/Child 投影、模板去重、精确字段投影、三 analyzer 对照、BM25、库过滤、72 题执行器和资源计数。
- Parent：800–2000 tokens，目标 1200；短于 800 的源允许形成显式 `underfilled_short_source`。
- Child：200–500 tokens，硬上限 700，目标 350；默认零重叠。仅超长无安全边界句强制切分，并使用 40 tokens 重叠（限定在 30–50）。
- 精确字段：编号、日期、公司、人名、文件名、英文缩写分别投影；正文是否进入 `_source` 作为两种候选模型。
- 未连接 Gateway，未读取或修改 NAS/OPS，未部署或连接 PostgreSQL/OpenSearch，未发网络请求。

## Analyzer 证据等级

- `legacy_123gram`：**实测**。执行本地 legacy 1/2/3-gram 等价算法。
- `icu_equivalent_probe`：**等价模拟**。本地 NFKC + CJK unigram 探针；不是 ICU plugin 实测。
- `smartcn_equivalent_probe`：**等价模拟**。本地 NFKC + CJK bigram 探针；不是 SmartCN plugin 实测。
- `analysis-icu` OpenSearch plugin：**SKIP**，本机未安装/未执行，本阶段禁止部署服务。
- `analysis-smartcn` OpenSearch plugin：**SKIP**，本机未安装/未执行，本阶段禁止部署服务。

## Benchmark 摘要

口径：50 个仓库内合成文档、50 Parents、50 Children；模板去重识别 4 个模板，删除 1,632 个重复行实例。由于去重后全部源都短于 800 tokens，实测最大 Parent/Child 都是 44 tokens；长文边界和强制切分由独立行为测试覆盖，不拿短 fixture 冒充尺寸压力测试。

### body 排除 `_source`

- legacy：179,146 bytes；1,197 terms；1,978 postings；build 4.028 ms；peak RSS 51,527,680 bytes。
- ICU 等价探针：100,410 bytes；522 terms；958 postings；build 2.834 ms；peak RSS 50,708,480 bytes；相对 legacy 减少 43.951%。
- SmartCN 等价探针：94,135 bytes；581 terms；844 postings；build 3.381 ms；peak RSS 51,134,464 bytes；相对 legacy 减少 47.453%。

### body 进入 `_source`

- legacy：182,846 bytes；正文 `_source` 比排除方案增加 3,700 bytes。
- ICU 等价探针：104,110 bytes；增加 3,700 bytes。
- SmartCN 等价探针：97,835 bytes；增加 3,700 bytes。
- postings、terms 与质量结果在两种 `_source` 模型间逐项相同；该数字只代表本合成语料序列化载荷，不代表 OpenSearch 磁盘段压缩或取回延迟。

## 72 题结果

每个 analyzer 都消费 72 题并输出逐题结果：62 题执行、10 题结构化 SKIP。

- 质量分母：53 个 `hits` 题；三者 Recall@10 都为 53/53 = 1.00。
- 精确编号分母：5 个正例；三者 Recall@10 都为 5/5 = 1.00。
- 类别：body 6/6、short_cjk 6/6、title 6/6、version 6/6、company_person 8/8、date 8/8、table 8/8、exact_identifier 5/5。
- 无答案：SmartCN 等价探针 7/7；legacy 与 ICU 等价探针 6/7。失败题是 G20「甲乙丙丁」：阶段 B 合并的新增公司 fixture 出现“合同甲方/乙方”，与阶段 A 继承题的“全库无这些字”理由冲突。这是合并 gold 的真实交叉污染，不作美化。
- 权限/串库：本地 `kb_id` 过滤探针泄漏 0；G32 与 G33 实测。其余 token、撤权、句柄和 index-fault 旧行为题不属于 detached Stage B 投影，逐题 SKIP 并排除质量分母。
- SKIP：G31、G34–G42（其中 G33 已执行；实际列表为 G31、G34、G35、G36、G37、G38、G39、G40、G41、G42）。每条理由已写入 JSON。

完整机器可读结果：`stage-b-benchmark-20260909.json`。

## 判据三格

### 工程判据

`tests/test_rt054_stage_b_poc.py` 共 10 条：

- 真正构造超长无句界文本，断言 Parent/Child 上限和 30–50 token 强制重叠；删掉切分或过滤接线会失败。
- 正常结构文本断言零重叠。
- 三文档重复模板断言跨文档去重，并保留独有正文。
- 六类精确字段分别断言。
- 对同一投影分别建立 body 进入/排除 `_source` 模型，断言 postings 不变、source bytes 真变化。
- 在同一合并索引中用 liba/libb 查询 `基线`，断言去掉 `kb_id` 过滤会发生可观察失败。已做真实断线破坏实验：临时移除候选集的库过滤后，该用例按预期变红并返回 `docdb:701`；还原后复跑转绿。
- 断言三 analyzer 的证据标签、72 题逐题覆盖、SKIP 理由、必出指标和 NO-GO 防冒充。

### 独立 AI 评审

本阶段没有调用外部或独立 AI reviewer：当前任务禁止外连，且本地没有额外审查运行时被授权进入本 RT。此格明确缺失，不以自审冒充独立评审。

### 读真实产出

人工读取 benchmark 摘要和失败题，确认：

- 80% 尺寸门真实失败；
- G20 出现合并 fixture 交叉污染；
- 10 个旧行为题为逐题 SKIP，没有进入质量分母；
- ICU/SmartCN 均标为等价模拟，OpenSearch primary store 明确未测。

## 阶段 B 完成门

- 结构化 Parent/Child 投影：**PASS（PoC）**。
- legacy / ICU / SmartCN 比较：**PARTIAL**；legacy 实测，ICU/SmartCN 仅等价模拟，真实插件 SKIP。
- body `_source` 候选比较：**PASS（逻辑载荷）**；真实 OpenSearch 磁盘和取回代价未测。
- 三库等价语料主索引缩小 ≥80%：**FAIL**；两库合成 PoC 只减少 43.951% / 47.453%，且不是 primary store。
- gold macro Recall@10 ≥0.90 且不低于 legacy：**PASS（合成 hits 分母）**；三者均 1.00。
- 精确编号 Recall@10 = 1.00：**PASS（5 个正例）**。
- 无串库：**PASS（本地 kb filter 探针）**；Gateway/token 权限不是本阶段证据。
- 固定 chunker/analyzer/mapping v1：**FAIL**；chunker/mapping 只形成 PoC v1，analyzer 不能在未实测插件、尺寸门失败时冻结为生产 v1。

## 下一步

保持 RT-054 `in-progress`，不进入阶段 C。要推翻 NO-GO，需要在明确批准的隔离环境中准备三个目标库的等价脱敏语料，运行版本匹配的 OpenSearch + ICU/SmartCN plugin，采集 primary store、真实 build/RSS/取回代价，并先修复 G20 的跨 fixture gold 冲突或明确重审其适用域。
