# Migration note: scripts/cwk_ai_common.py exec transport (RT-034 ord 1)

- from: ea33adc54c7829e766b63d76f59937f4ea1803ff44509bc0b87370abf77c3e55
- to:   8b1b610893feea984824423480c5dfe5808ce9093c23e9e005c042bfbb2b459f

## 行为变化

新增环境变量 `CWK_AI_TRANSPORT`（`agent` | `exec`，默认与空值 = `agent`）：

- `agent`（默认）：行为与旧版逐字节一致——`openclaw agent --local --agent
  <CWK_AI_AGENT_ID>` + `assert_safe_ai_agent` fail-closed 校验 + 调用后
  sessions.delete 清理。
- `exec`：单 agent 沙箱模式——`openclaw agent exec` 一次性无头回合，用宿主
  已配置的模型凭据，不需要任何预配置 agent；解析 exec JSON 信封
  （final/payloads）提取模型输出；不做 agent 作用域的会话清理。模型允许
  清单、JSON-only 指令、脱敏子进程环境、超时与重试语义两种模式完全相同。

非法 transport 值在调用前即 ValueError 拒绝。

## 回滚方式

1. 运行时侧：设 `CWK_AI_TRANSPORT=agent` 或删除该变量——exec 分支完全不参与。
2. 代码侧：`git revert` 本提交（单提交，无 schema/数据迁移）。
