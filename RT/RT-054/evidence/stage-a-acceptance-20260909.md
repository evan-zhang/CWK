# RT-054 阶段 A 收口

## 裁决

**PASS，阶段 A 完成；允许进入阶段 B 的本地、离线 PoC。**

本裁决不批准部署 PostgreSQL/OpenSearch，不批准修改 OPS/NAS/生产，也不代表新检索质量或容量已经达标。

## 四项产出

1. 三库基线：`stage-a-baseline-20260909.json/.md`。冻结原文/JSON/词法大小、文档/chunk、已有查询分段与 RSS 证据；没有可靠证据的 terms、全量 build 和成功 P50/P95 保持 `null`，不得推算冒充实测。阶段 B 的新 builder 必须原生输出这些计数。
2. gold：`stage-a-gold-20260909.json`，72 题；48 题继承已独立复核的 RT-051 A11，24 题补公司/人名、日期、表格；新增题由 22 件仓库内 fixture 支撑，二次独立复核 PASS。
3. 合同：v2/v3 兼容说明，v3 request/success/error 三个 Draft 2020-12 JSON Schema。
4. 要求去留：保留/简化/删除/延后逐项冻结；明确 RT-051 v2 read 不被本 RT 静默改义。

## 可证伪性

- 删除或改错基线、题量、类别、fixture/证据绑定、权限断言、请求允许字段、响应证据字段或错误分类，`tests.test_rt054_stage_a_contracts` 会失败。
- 后续实际检索须输出逐题 rank、doc 命中、证据单元、locator、权限/错误断言；不能只报平均分。
- exact identifier Recall@10 必须为 1.00；permission leaks 必须为 0；table evidence 的查询原子必须在同一证据单元。
- 存储门使用 OpenSearch primary store；不允许拿 snapshot、删字段半成品或只报逻辑 payload 冒充。

## 已知证据空白

旧生产链没有三库完整、可重复的成功冷/热延迟和 builder 资源分段，也没有可安全复用的 real-library terms/postings 计数。由于重取 cwork-3m 的 1.495GB 旧对象只会重复已被否定的高风险路径，阶段 A 不为填表重跑；空白已在机器基线中显式记录，并转为阶段 B builder 的必出指标。这是证据边界，不是性能达标声明。

## 下一步边界

阶段 B 只做脱敏/批准数据的本地离线 Parent/Child 与 analyzer benchmark，不接 Gateway，不切流量，不部署生产。先比较旧 1/2/3-gram、ICU、SmartCN，再固定 chunker/analyzer/mapping v1；任一质量或存储硬门失败即回方案门。
