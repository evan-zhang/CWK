# RT-001: AI 增强理解与质量复核 Pipeline

## 需求来源

当前 CWK nightly pipeline 已能稳定只读采集工作协同内容，生成每日 Markdown/HTML 简报并同步到个人知识库 `工作协同镜像`。最近几天运行结果显示基础质量合格：每轮处理 60 条，A1-A8 全部 PASS，未调用 CWork 变更命令。

但当前 pipeline 主要依赖 Python 规则和启发式逻辑，存在以下质量上限：

- 跨天重复事项容易连续出现在“今天优先看”。
- 摘要中偶尔出现 `fileName`、JSON 片段、`暂无可读摘要` 等机器噪声。
- `PC`、`交流`、`双周会` 等泛化锚点会降低事件归并质量。
- 时间格式不统一，部分为 UTC ISO 字符串，部分为本地时间。
- 强合并长期为 0，关系层保守但可能导致事件库碎片化。

Evan 明确提出：如果不考虑 token 消耗、只以质量为准，希望在 pipeline 层引入 AI，让 AI 阅读每篇汇报内容并参与处理，同时增加质量复核层。

## 目标

在不破坏现有只读、安全、可追溯 pipeline 的前提下，新增 AI 增强层：

1. 对每篇工作协同汇报进行 AI 理解，产出结构化 JSON。
2. 对同一批次内的结构化结果进行 AI 跨篇归并，识别同一事项、新事项、历史事项进展。
3. 对最终日报进行 AI 质量复核，输出可读性更高、重复更少、锚点更准的管理版简报。
4. 保留现有规则版产物作为 baseline 与失败兜底，支持同日对比。

## 范围

### 本 RT 做

- 新增 AI 单篇理解 stage：`ai_record_understanding`
- 新增 AI 跨篇归并 stage：`ai_event_clustering`
- 新增 AI 质量复核 stage：`ai_quality_review`
- 新增 AI 增强版 digest：`digest-ai-enhanced.md/html`
- 新增机器可读质量报告：`quality-review.json` 与 `quality-review.md`
- 新增环境变量配置：
  - `CWK_AI_ENABLED`
  - `CWK_AI_RECORD_MODEL`
  - `CWK_AI_CLUSTER_MODEL`
  - `CWK_AI_QUALITY_MODEL`
  - `CWK_AI_MAX_PARALLEL`
- README / operations 文档补充 AI 增强运行方式。
- CI 增加 dry-run 或 fixture 测试，保证 AI 功能关闭时不影响现有 pipeline。

### 本 RT 不做

- 不修改 CWork 数据状态，不标已读、不回复、不审批、不完成待办。
- 不把真实 raw 原文上传 GitHub。
- 不强制替换现有规则版 digest。
- 不在没有用户授权的情况下启用高成本 AI 模型。
- 不做前端 UI。
- 不做公司级 GA 发布。

## 用户价值

- 日报更像“管理者该看的 5-10 件事”，而不是规则摘要。
- 同一事项跨天重复出现时可以自动降权或标记“延续事项”。
- 长会议纪要、请示、日报周报能被更准确拆成：背景、结论、行动项、责任人、截止时间、风险。
- 每条 AI 判断保留证据引用和原始 report_id，便于追溯。
- 可并行输出规则版和 AI 增强版，先试运行、再决定是否切换。

## 验收标准

1. `CWK_AI_ENABLED=false` 时，现有 `make test` 和 nightly pipeline 行为不变。
2. AI 增强开启后，每条 raw 汇报生成一个 `ai-understanding/*.json`，字段符合 schema。
3. 每个 AI JSON 至少包含 `report_id`、`title`、`summary`、`event_anchor`、`entities`、`action_items`、`risks`、`evidence_refs`。
4. AI 输出不得丢失证据：每条 `summary` / `action_item` 至少能追溯到原文片段或 report_id。
5. 生成 `digest-ai-enhanced.md` 和 `digest-ai-enhanced.html`。
6. 生成 `quality-review.json` 和 `quality-review.md`，包含质量分、主要问题、修正建议。
7. 同一批次中明显重复/延续事项在 AI 增强版中被合并或降权展示。
8. 时间统一展示为 Asia/Shanghai。
9. manifest 中记录 AI stage、模型名、耗时、输入输出文件，但不得记录 appKey 或 API token。
10. AI stage 失败时，pipeline 不整体失败；回退到规则版 digest，并在 manifest 中标记 degraded。

## 成功口径

试运行 3 个夜跑批次后：

- Evan 主观评分 AI 增强版高于规则版。
- AI 增强版“今天优先看”中重复旧事项数量明显下降。
- `暂无可读摘要` 和明显 JSON/fileName 噪声减少。
- 未发生 CWork 变更命令调用。
- 未泄露密钥到 manifest、日志或 GitHub。

## 预估

- 设计与 schema：0.5 天
- 单篇理解 stage：0.5-1 天
- 跨篇归并 stage：0.5 天
- 质量复核与增强 digest：0.5-1 天
- 文档、测试、试运行：0.5 天

合计：2-3 天，取决于 AI 调用方式和模型可用性。
