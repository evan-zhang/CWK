# RT-035：assert_safe_ai_agent 兼容 OpenClaw 2026.8 的 agents 配置结构

## 决策与根因

2026-09-03 08:02 Evan 要求排查本机 CWK 服务的 AI 精编循环失败。根因：OpenClaw
2026.8 起没有 `config get agents.list` 配置路径（改为 `agents.entries`），预检命令
退出码 1 → `assert_safe_ai_agent` 必然抛 "could not inspect OpenClaw agent policy"
→ 847 轮精编里 780 轮全灭（网关升级后）。修复：双路径预检（新结构优先，旧命令
回退）。模型允许清单与零工具策略校验未动（高风险成员要求点名：本 RT 同样不触碰
allowed_cwk_models）。

## 验证

- tests/test_ai_contracts.py 41/41、tests/test_rt034_ai_exec_transport.py 8/8
- 本机真实上下文验证：PROJECT=projects/CWK 时 `assert_safe_ai_agent("cwk-ai-reviewer")`
  通过（AGENT_POLICY_OK）
- governance-audit 退出 0、test_governance_audit 全绿、test_pr001_release_gate_validation
  DelegatedFamilyCanonicalReuseTests 9/9

## 回滚

git revert；或 CWK_AI_TRANSPORT=exec 绕过该预检。
