# RT-034：AI 通道 exec transport（单 agent 沙箱无需专用 reviewer Agent）

## 决策

2026-09-03 00:47 Evan 明确部署场景：沙箱 agent 环境，只有主 agent，没有任何
其他 agent。原实现把所有真实模型调用钉在 `openclaw agent --local --agent
cwk-ai-reviewer` 上（零工具 reviewer Agent + 配置级 fail-closed 校验），单
agent 沙箱没有这个 agent，部署就卡在"配置专用 cwk-ai-reviewer"。

平台路径已实测（2026-09-03 本机）：`openclaw agent exec` 提供一次性无头回合，
不需要任何预配置 agent，用宿主已配置的模型凭据，输出稳定 JSON 信封。

## 变更

- `scripts/cwk_ai_common.py`（legacy 高风险成员，点名说明见下）：
  - 新增 `CWK_AI_TRANSPORT`（`agent` | `exec`，默认与空值 = `agent`；非法值
    ValueError）
  - `exec` 模式：跳过 `assert_safe_ai_agent`，命令改为 `openclaw agent exec
    --message-file <tmp> --model <m> --thinking <t> --timeout <s> --json --cwd
    <.cwk-ai-runtime>`；输出经 `_extract_exec_envelope` / `_parse_exec_result`
    解析信封并提取模型 JSON；不执行 agent 作用域的 sessions.delete 清理
  - `agent` 模式行为完全不变（命令、校验、清理、重试逐字节保留）
- 文档：`.env.example`、`docs/AI-PILOT.md`（单 agent 沙箱一节）、`README.md`、
  `docs/OPERATIONS.md`、`docs/USER_GUIDE.md`
- 测试：`tests/test_rt034_ai_exec_transport.py`（8 用例）

## 为什么必须动这个 legacy 高风险成员（owner_assignment_rule 要求点名）

`invoke_openclaw_json` 是所有真实模型调用的唯一出口，transport 分支只能加在
这里。该文件被列为高风险是因为它承载模型允许清单（DI-002 的 dc96c28）：
**本 RT 没有触碰允许清单**——`assert_cwk_model` 与 `allowed_cwk_models`
逐字节未动，两种 transport 都仍然强制过同一道允许清单门。

## 隔离强度说明（诚实记录）

`agent` 模式：配置级零工具策略（deny-all，预配置校验）。
`exec` 模式：调用级隔离——一次性无头回合、无 deliver、无持久会话、workspace
钉在 `.cwk-ai-runtime`、JSON-only 指令、超时杀进程组、CWK 侧严格解析输出。
在"原文本来就只在这台用户自己的机器上转"的单租户沙箱威胁模型下强度匹配；
多 agent 宿主继续用默认 `agent` 模式不受影响。

## 验证

- `python3 tests/test_rt034_ai_exec_transport.py`：8/8
- `python3 tests/test_ai_contracts.py`：41/41
- `python3 tests/test_distribution.py`：通过
- `python3 .aodw-next/06-project/governance-audit.py --root .`：退出 0
- `python3 tests/test_governance_audit.py`：全部通过
- 实测：`openclaw agent exec --message-file - --model evan-openai/evanModel
  --thinking off --timeout 90 --json` 信封解析正确（2026-09-03）
- 推送 main 后由 GitHub CI（make ci）作为权威门禁兜底

## 回滚

单提交回退（git revert）；或运行时设 `CWK_AI_TRANSPORT=agent`，exec 分支
完全不参与。

## S-4 重钉与 CI 修复（2026-09-03 追加）

RT-033 的 CI 在 PR-001 安全面测试上红了（`missing v2 security owner scope for
RT-017`），两个根因在本 RT 一并收口：

1. RT-033 新增的 `scripts/setup_app_key.py`（原名 `cwk_key_set.py`）落进了
   PR-001 管控命名空间 `^scripts/cwk_*.py$`（封闭归属表）却没有登记，安全面
   快照 fail-closed。修复：更名为 `scripts/setup_app_key.py` 离开封闭命名
   空间，治理归属走 manifest 规则 R-runtime-rt033-key-setup（同类先例：
   `cwk_wiki_batch_driver.sh`），全仓 14 处引用同步更新。
2. 本 RT 修改 `cwk_ai_common.py` 后补执行了 authorized_change_procedure 的
   S-4：registry pin `ea33adc5` 重钉为 `8b1b6108`，并级联更新 overlay
   `inherits.registry_sha256` 与 `current_pin`、manifest
   `upstream_authorities[0].sha256` 与 overlay 规则 pin。S-1..S-3 证据
   （本 rt-lite、v2 回执、migration note）保持不变。
