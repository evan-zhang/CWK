---
name: cwk-query
description: 工作协同知识库查询。当用户问到「我的工作汇报里…」「上次某某的汇报说…」「某事项的行动项/风险/结论」等已入镜的工作协同内容时使用。返回带 report_id 引用的证据包并据此回答；无证据时拒答，不凭记忆补答。
---

# CWK Query — 工作协同知识库查询（只读）

查询一个已建好的「工作协同镜像」知识库。本 Skill 只读：不采集、不精编、不同步、不写镜像内任何文件。

## 定位依赖（按序，找不到就问用户要路径，不要猜）

1. 环境变量 `CWK_PROJECT_DIR`：CWK 仓库检出（含 `scripts/cwk_wiki_query.py`）
2. 环境变量 `CWK_QUERY_MIRROR`：镜像根（含 `raw/` 与 `wiki/` 的「工作协同镜像」目录）
3. 常见布局回退：镜像位于 `CWK_PROJECT_DIR` 同级的 `../CWK-*/knowledge/工作协同镜像`

## 查询

```bash
python3 "$CWK_PROJECT_DIR/scripts/cwk_wiki_query.py" \
  "<问题：带人名/系统名/事由等具体词，命中率更高>" \
  --mirror-root "$CWK_QUERY_MIRROR" --top-k 5
```

输出为「证据包」：命中篇目（标题/写人/日期/质量 ai_refined|fallback）、逐条引文、report_id、置信度与回答策略。

## 回答规矩（强制）

1. 只基于证据包作答，每条事实附 report_id；细节存疑时顺着证据包里的 raw 路径回原文核对
2. 工具弃权（无实体范围/无证据）→ 如实告知，建议换更具体的词（人名/系统/事项），不得凭记忆补答
3. fallback 页只有导航信息，细节必须回读其链接的原文
4. 只读红线：绝不修改 `raw/`、`wiki/` 或镜像内任何文件

## 适用与不适用

- 适用：查已入镜的工作汇报事实、行动项、决策、风险、来龙去脉
- 不适用：实时收件/待办处理、写操作、镜像构建与维护（那是维护侧 cwk-mirror-workflow 的事）
