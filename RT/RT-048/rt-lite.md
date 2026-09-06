# RT-048：AI 精编通道接 codex exec（OPS 生产可用）

## 决策

2026-09-06 07:54 拍板：OPS 上 AI 精编不再依赖宿主 OpenClaw（2026.4.15
版 `agent exec` 无 `--model` 旗标，升级会动 OPS 上运行的 gateway 服务），
改接宿主已登录的 codex CLI（`codex exec` 一次性无头回合，ChatGPT 账号
凭据）。模型结论：`5.6 lulu` 双路确认不可用（ChatGPT 账号硬拒 lulu/
5.6-codex/5.6-codex-mini；newapi 30 个中转模型无 lulu 无 5.6 系），
批跑白名单中的 `openai/gpt-5.6-terra` 实测可用（OPS codex exec 直调
返回合约 JSON）。

## 变更（scripts/cwk_ai_common.py，script-evolution-v2 ord1）

- `CWK_AI_TRANSPORT` 新增合法值 `codex`：走 `codex exec --sandbox
  read-only --skip-git-repo-check -C <workspace> -m <model> -o <file> -`
- prompt 走 stdin（长文安全，避开 argv 上限）；最终回复落 `-o` 文件，
  `_parse_codex_result` 优先读文件、缺失时退回 stdout 再走
  extract_json_object
- 模型 ID 剥 CWK provider 前缀；codex 二进制可用 `CWK_CODEX_BIN` 覆盖
  （默认 `codex`）
- 沙箱 read-only：reviewer 调用是纯文本→JSON 变换，没有理由写盘；
  prompt 与 last-message 临时文件 finally 清理，不进 gateway 会话索引
- transport=agent / exec 行为零变化

## 边界

- 模型允许清单未动（CWK_ALLOWED_MODELS 4 项 + TEMPORARY_GPT56_BATCH
  时间盒批次不变）；codex 通道下模型仍须过 assert_cwk_model
- sanitized_ai_environment 照旧：AI 子进程不继承业务凭据
- 治理联动：registry 指纹重钉 + overlay inherits/current_pin 级联 +
  manifest upstream/pin 级联（照 RT-035 先例）

## 验证

- tests/test_ai_contracts.py 新增 CodexTransportTests 4 例（命令构建与
  read-only 沙箱断言、-o 缺失回退 stdout、非法 transport 值拒绝、临时
  文件清理）
- OPS 实测（2026-09-06）：codex-cli 0.153.4 已登录 ChatGPT；
  gpt-5.6-terra exec 直调返回合约 JSON
- make governance-audit + python3 -m pytest tests/test_governance_audit.py
