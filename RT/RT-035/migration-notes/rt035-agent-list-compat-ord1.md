# Migration note: scripts/cwk_ai_common.py agent-list compatibility (RT-035 ord 1)

- from: 8b1b610893feea984824423480c5dfe5808ce9093c23e9e005c042bfbb2b459f
- to:   47a6161fdcc62accd50ea43986888629b67639fe8d043d89971a0ee4e23d8eae

## 行为变化

`assert_safe_ai_agent` 的预检命令从 `openclaw config get agents.list --json`（OpenClaw
2026.8 起该配置路径已不存在，退出码 1）改为双路径：优先 `openclaw config get agents
--json` 读 `entries`（按 id 键出完整策略），失败时回退旧命令。旧网关行为不变；
2026.8+ 网关上 AI 通道预检从必败恢复为可用。策略校验（workspace/零工具/无 skills）
逐字节未动。

## 回滚方式

`git revert` 本提交；或运行时改用 `CWK_AI_TRANSPORT=exec`（完全不经过该预检）。
