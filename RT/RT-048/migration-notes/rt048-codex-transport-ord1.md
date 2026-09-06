# RT-048 ord1：cwk_ai_common.py 新增 codex transport（AI 精编通道）

- from_sha256（原 pin）：47a6161fdcc62accd50ea43986888629b67639fe8d043d89971a0ee4e23d8eae
- to_sha256（新 pin）：08d70dc75bea8f421bd9cdec2cd88574d2628e941f3c09c19cb320a67b7ff634

## 行为差异

`CWK_AI_TRANSPORT` 合法值从 `agent|exec` 扩为 `agent|exec|codex`。默认
transport（agent）与既有 exec 通道行为零变化。codex 通道 shell 出宿主
codex CLI 的一次性无头回合：prompt 走 stdin（避开 argv 上限）、最终回复
落 `-o` 文件（stdout 是流式过程噪声，仅作回退）、`--sandbox read-only`、
模型 ID 剥 CWK provider 前缀（codex 用宿主 ChatGPT 账号）、prompt 与
last-message 临时文件 finally 清理。JSON 合约解析（extract_json_object）
与重试/超时语义不变。

动机：OPS（生产机）宿主 OpenClaw 停在 2026.4.15，`agent exec` 无
`--model` 旗标，升级会动 OPS 上运行的 gateway 服务；codex CLI 0.153.4
已在 OPS 登录 ChatGPT，可直接充当带凭据的模型调用通道。模型口径：
lulu/5.6-codex/5.6-codex-mini 被 ChatGPT 账号拒绝、newapi 无 5.6 系
中转（双路确认不可用）；批跑白名单中的 openai/gpt-5.6-terra 实测可用。

## 回滚

`CWK_AI_TRANSPORT` 不设为 codex 即回到原通道（无需回滚代码）；完全恢复
旧行为时 revert 本提交即可。

## 风险控制

模型允许清单未动（CWK_ALLOWED_MODELS 4 项 + TEMPORARY_GPT56_BATCH
时间盒批次），codex 通道下模型仍须 assert_cwk_model 通过；read-only
沙箱使 reviewer 无法写盘；sanitized_ai_environment 使 AI 子进程不继承
业务凭据（CWORK_APP_KEY 等）；codex 二进制路径可用 CWK_CODEX_BIN 钉住。
