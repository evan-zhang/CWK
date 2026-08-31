# CWK Cloud Wiki — 可立即试用测试用例

> 更新：2026-08-04
> 当前阶段：以运行时 manifest 为准；本文件不绑定任何个人镜像规模。
> 可用性结论：**可以用自然语言检索并生成 raw 已验证证据包；最终答案仍由 OpenClaw 基于证据生成。**

## 现在能测什么

### 已可用
- 打开 `wiki/summaries/*.md` 看单篇摘要 + 证据引用
- 打开 `wiki/topics/*.md` 看跨汇报主题页
- 打开 `wiki/entities/**/*.md` 看人/组织/产品/系统导航页
- 本地 `rg/grep` 关键词检索
- 个人知识库云端目录 `工作协同镜像/wiki/` 浏览（若 DocDB 已同步）
- `cwk_wiki_query.py` 自然语言检索、日期/发送人过滤、证据校验和拒答置信度

### 还不能当最终形态
- 还没有独立 Web 问答 UI；当前 OpenClaw 对话是问答入口
- 当前是词面 + Wiki 图导航基线，不宣称向量语义召回已上线
- topics/entities 目前是频次聚合，会有申请类高频页占前
- 旧 `history/events/entities` 不再是事实源，请以 `wiki/` 为准

## 本地根目录

```bash
CWK_HOME="$(pwd)"
WIKI="${CWK_MIRROR_ROOT:-$CWK_HOME/knowledge/工作协同镜像}/wiki"
```

## 0. 一键冒烟

```bash
python3.11 scripts/cwk_wiki_smoke_test.py --mirror-root "${WIKI%/wiki}"
python3.11 scripts/cwk_wiki_query.py --lint
```

预期：两项均 `PASS`

---

## 用例清单（请直接试）

### TC-01 规模门禁（P0）
```bash
ls "$WIKI/summaries"/*.md | wc -l
ls "$WIKI/topics"/*.md | wc -l
find "$WIKI/entities" -name '*.md' | wc -l
```
通过标准：数量达标。

### TC-02 主题页：云端虾申请（P0）
1. 打开：`$WIKI/topics/云端虾申请.md`
2. 检查是否有多条 report_id 引用
3. 点开其中 1 条 summary 回链

通过标准：主题页不是空话，能回到具体汇报。

### TC-03 产品实体：云端虾（P0）
1. 打开：`$WIKI/entities/products/云端虾.md`
2. 看是否聚合了多篇相关汇报
3. 任选 1 个 report_id 打开 summary

通过标准：实体页可当导航，不编造无来源结论。

### TC-04 人物实体：李文俏（P0）
1. 打开：`$WIKI/entities/people/李文俏.md`
2. 检查相关主题/汇报列表
3. 对照 1 篇 summary 是否真出现该人

通过标准：人物页能帮助“她最近参与过什么”。

### TC-05 费用主题（P0）
1. 打开：`$WIKI/topics/AI费用日报(生产环境).md`
2. 或本地搜：
```bash
rg -n "AI费用" "$WIKI/topics" "$WIKI/summaries" | head
```
通过标准：能定位到费用相关原文摘要，而不是只有标题。

### TC-06 关键词检索：OpenClaw（P1）
```bash
rg -n "OpenClaw" "$WIKI/entities" "$WIKI/summaries" | head -20
```
打开命中的 entity/summary，确认上下文合理。

### TC-07 单篇摘要证据链（P0）
随机打开 1 个 summary：
```bash
ls "$WIKI/summaries" | sed -n '10p'
```
检查：
- frontmatter 有 `type: SourceSummary` 和 `report_id`
- 有“关键事实”
- 事实下有 `证据：> ...` 原文摘录

### TC-08 负面用例：不存在的人（P0）
```bash
rg -n "张三丰张三丰" "$WIKI" || true
```
通过标准：无命中。系统不应“脑补”。

### TC-09 原文证据保持（P0）
```bash
python3.11 scripts/cwk_wiki_query.py "任一已知汇报标题" --top-k 1 --format json
```
通过标准：返回 `evidence_status=verified`，并能回链到工作协同原文；工作协同可读取内容按原文保留。

### TC-10 云端可浏览（P1）
在个人知识库打开：
- `工作协同镜像/wiki/index.md`
- `工作协同镜像/wiki/topics/云端虾申请.md`
- `工作协同镜像/wiki/entities/products/云端虾.md`

通过标准：云端看得到，内容与本地一致。

### TC-11 自然语言问答召回（P0）

```bash
python3 scripts/cwk_wiki_query.py "AI财务单据审核两周 Token 消耗 10.91 亿"
```

通过标准：首位为 `2077642842540343298`，且 `evidence=verified`。

### TC-12 无依据拒答（P0）

```bash
python3 scripts/cwk_wiki_query.py "霜蓝鲸鱼量子披萨项目进展" --format json
```

通过标准：`confidence=none` 且结果为空。

---

## 你可以直接发给我的试问（人工验收）

把下面任意一句发到本频道，我按 wiki 证据回答，并附 report_id：

1. 云端虾申请最近有哪些相关汇报？
2. AI 费用日报最近在说什么？
3. 李文俏近期出现在哪些事项里？
4. OpenClaw / 本地虾相关有哪些主题？
5. TBS 训战最近有什么动态？
6. 有没有“未闭环/待确认”痕迹较明显的主题？

回答约束：
- 必须给 report_id 或 wiki 路径
- 无证据就说不知道
- 不做审批/自动回复

---

## 自动化状态（给执行同学）

每日 22:30 cron：`CWK nightly mirror pipeline`
- 已修模型白名单：`newapi/BD-MiniMax`
- nightly 已支持：
  - `--wiki-compile`
  - `--wiki-topics-entities`
  - `--wiki-sync`
- cron 文案已要求跑 wiki 增量

本地手动等价：
```bash
python3.11 scripts/cwk_wiki_smoke_test.py --mirror-root "${WIKI%/wiki}"
python3.11 scripts/cwk_cloud_wiki_compile.py --limit 20
python3.11 scripts/cwk_cloud_wiki_topics_entities.py --min-topic-reports 2 --min-entity-reports 2
python3.11 scripts/cwk_sync_mirror_to_docdb.py --only-prefix wiki/
```
