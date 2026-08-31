# CWK Wiki TEST_CASES

## Smoke Commands

Set `CWK_HOME` to this clone and leave `CWK_MIRROR_ROOT` empty for the default
relative mirror location:

```bash
export CWK_HOME="$(pwd)"
export CWK_MIRROR_ROOT="${CWK_MIRROR_ROOT:-$CWK_HOME/knowledge/工作协同镜像}"
```

1. 本地计数与样例页检查
```bash
python3.11 scripts/cwk_wiki_smoke_test.py --mirror-root "$CWK_MIRROR_ROOT" --min-summaries 1 --min-topics 1 --min-entities 1
```
预期：当前镜像的实际计数满足设置的门槛，且 `overall=PASS`。

2. 直接查看样例页面
```bash
find "$CWK_MIRROR_ROOT/wiki/topics" -name '*.md' ! -name index.md | head -1 | xargs -I{} sed -n '1,80p' '{}'
find "$CWK_MIRROR_ROOT/wiki/entities" -name '*.md' ! -name index.md | head -1 | xargs -I{} sed -n '1,80p' '{}'
```
预期：页面正文包含 `report_id` 链接或反引号 ID，且有“证据：”或“来源：”字样。

3. 检查 manifest 关键字段
```bash
python3 - <<'PY2'
from pathlib import Path
import json
p = Path(__import__('os').environ['CWK_MIRROR_ROOT']) / 'wiki' / '_system' / 'manifest.json'
m = json.loads(p.read_text())
print('source_count=', m.get('source_count'))
print('compiled_count=', len(m.get('compiled_report_ids', [])))
print('topic_page_count=', m.get('topic_page_count'))
print('entity_page_count=', m.get('entity_page_count'))
print('failure_queue=', len(m.get('failure_queue', [])))
PY2
```
预期：`source_count` 与 `compiled_count` 一致，且 `failure_queue` 为 0。

4. 如需验证 DocDB 搜索路径（本机已具备 `cms-docdb` 与 `XG_BIZ_API_KEY` 时）
```bash
PROJECT_ID=$(python3 ~/.agents/skills/cms-docdb/scripts/browse/get-personal-project-id.py | tail -1 | python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["data"]["projectId"])')
python3 ~/.agents/skills/cms-docdb/scripts/query/search.py 云端虾申请 --project-id "$PROJECT_ID"
```
预期：结果里能看到 `工作协同镜像/wiki/topics/云端虾申请.md` 或同名文件。

## 中文测试问题

1. 问题：`云端虾申请现在有哪些共性动作和审批线索？`
期望证据：回答必须引用至少 2 个 `report_id`，并指向 `wiki/topics/云端虾申请.md` 或 `wiki/topics/申请云端虾.md`。

2. 问题：`云端虾权限申请和开通申请有什么区别？`
期望证据：回答要同时引用两个 topic 页面，列出各自来源 `report_id`，不能只给口语总结。

3. 问题：`AI费用最近都在追哪些异常？`
期望证据：引用 `wiki/topics/AI费用日报(生产环境).md`、`wiki/topics/AI费用（ai-billing）.md` 等页面，并给出对应 `report_id`。

4. 问题：`李文俏最近在工作协同里出现在哪些事项里？`
期望证据：回答应落到 `wiki/entities/people/李文俏.md`，列出最近关联事项标题和 `report_id`。

5. 问题：`BP流程和云端虾待办集成目前有哪些未闭环项？`
期望证据：引用 `wiki/topics/BP流程与云端虾待办集成.md` 中的“相关行动项”或“相关风险”，并带来源 `report_id`。

6. 问题：`知识库（kb）这个主题最近有哪些决策和风险？`
期望证据：回答必须区分“相关决策”和“相关风险”两类证据，分别附 `report_id`。

7. 问题：`OpenClaw App V3 消息页优化牵涉到哪些人和系统？`
期望证据：引用 topic 页以及关联 entity 页，至少给出 1 个 people/entity 页面路径和 `report_id`。

8. 问题：`学币系统最近一个月有哪些变化趋势或异常？`
期望证据：引用 `wiki/topics/学币系统.md`、`wiki/topics/平台失效学币回收.md` 或相关 summary，附 `report_id`。

## 验收口径

- 回答必须能回链到 wiki 页面或 source summary 页面。
- 每条关键事实最好带 `report_id`，最低要求是能定位到页面里的证据段。
- 如果某问题只找到单一来源，回答里必须明确“当前仅见 1 条来源”。
