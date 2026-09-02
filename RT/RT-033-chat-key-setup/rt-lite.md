# RT-033：聊天直供 CWORK_APP_KEY（cwk_key_set.py）

## 决策

2026-09-02 18:54 Evan 拍板：CWK 部署在全定制服务端/客户端上，用户通路本身可信；
允许用户把 CWORK_APP_KEY 直接粘贴在聊天窗口里发给 Agent，由 Agent 调用脚本落盘，不再要求
用户手动上机编辑 .env。目标：降低安装门槛，消灭 export 前缀 / BOM / 引号 / 错路径
这一类格式工单（2026-09-02 用户实际卡在“确认画像没有读到内容”，根因即此类）。

## 变更

- 新增 `scripts/cwk_key_set.py`：stdin 读 Key（绝不进 argv），原子写 `.env`
  （0600），保留其他行，修复 export 前缀 / 成对引号 / BOM / 重复行；回执 JSON
  不含值。读取路径零改动（pipeline / doctor 现有 `.env` 解析即读取面）。
- 新增 `tests/test_key_set.py`。
- 文本红线改写（`prompts/OPENCLAW_SANDBOX_BOOTSTRAP.md` 硬性禁止 1/2 + 阶段 4、
  `docs/SANDBOX_ONBOARDING.md`、`docs/INTERNAL_DISTRIBUTION.md`、
  `docs/OPERATIONS.md`、`skill/SKILL.md`、`skill/references/activation.md`、
  `skill/references/operations.md`、`.env.example`、`README.md`）：
  「不在对话里收集凭据」→「仅 CWORK_APP_KEY 允许在定制客户端通路发送，且只经
  `scripts/cwk_key_set.py` 落盘；其余凭据照旧禁止」。
- 治理：code-ownership-manifest 新增 `R-runtime-rt033-key-setup`
  （`scripts/` 是 exact-only 区，PR-001 委派规则为封闭集合，新脚本必须由清单直管）。

## 不做

- 不改 `install.sh`：属 PR-001 managed_script_inventory 封闭集合，改它要走
  script-evolution 回执链，且其提示语并非错误。
- 不加 Key 在线有效性探测：另行立项（候选：collect 全线 auth 失败显式失败、
  doctor 报错说人话、discovery 全 0 软门）。
- 不引入 masked-entry / OpenClaw secrets 集成：按 2026-09-02 决策不需要。

## 验证

- `python3 tests/test_key_set.py`：8/8 通过。
- `python3 .aodw-next/06-project/governance-audit.py --root .`：退出 0。
- `python3 -m unittest discover -s tests -p 'test_governance_audit.py'`、
  `test_distribution.py`、`test_install_modes.py`：全部通过。
- 推送 main 后由 GitHub CI（make ci）作为权威门禁兜底。
