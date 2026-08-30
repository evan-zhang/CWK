# AODW

版本 0.6.1。最高规则：`01-core/aodw-constitution.md`。

AI 是主工程师：先调查、先规划、范围内自己做完。用户是产品负责人：听人话方案、拍板代价、在收口时确认合并。

## 现行文件

| 文件 | 作用 |
|------|------|
| `01-core/aodw-constitution.md` | 职责、目标、授权、思考规划、工作流 |
| `01-core/ai-interaction-rules.md` | 对人怎么说话 |
| `02-workflow/rt-manager.md` | 查重、编号、建 RT、遗留事项台账 |
| `02-workflow/spec-lite-profile.md` | 批准后如何自己做完 |
| `01-core/git-discipline.md` | 分支、worktree、合并 |
| `06-project/` | 本项目概览 |
| 仓库根目录 `AGENTS.md` | 怎么写代码、怎么讲方案 |
| `tools/rt-guard.sh` | 关闭 RT 时的机械检查 |

每条规则只写一次。授权边界只在宪法里。

## 新项目

拷贝 `.aodw-next/`，重写 `06-project/ai-overview.md` 和 `modules-index.yaml`，在 `AGENTS.md` / `CLAUDE.md` 引用宪法。
