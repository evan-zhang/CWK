# RESUME_PROMPT

> 把本文件全文发给新会话即可接管；用户说“继续 cwk-knowledge-platform-rt055-rt056”时执行本协议。

## 接管协议

接管任务：CWK RT-055 知识库平台与 RT-056 管理台 MVP。目标是在不破坏现有 8787/8790 生产服务的前提下，继续完成管理台只读体验验收、记录修改意见，并按 `STATE.json.nextAction` 继续后续工作。

## 必须读取

按顺序完整读取：

1. `/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK/docs/handoffs/20260915-163210-cwk-knowledge-platform-rt055-rt056/HANDOFF.md`
2. `/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK/docs/handoffs/20260915-163210-cwk-knowledge-platform-rt055-rt056/STATE.json`
3. 本文件
4. `/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK/docs/KB-ADMIN.md`
5. `/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK/docs/cwk-kb-access-setup.md`
6. `/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK/scripts/kb_admin.py`

## 现实复核

接管后先只读核对：

- 当前 workspace、branch、HEAD、`git status --porcelain`、remote；不要覆盖已有 `CLAUDE.md` 修改或清理 `docs/handover/`。
- OPS `/Users/xgstudio/rt056-kb-admin-mvp` 是否存在；读取 `/tmp/rt056-kb-admin.pid`，确认管理台 PID 与 8791 listener。
- 确认 8787/8790 仍在监听，不能停止、重启或修改它们。
- 确认本机 SSH 隧道 `18791 -> OPS 127.0.0.1:8791` 是否仍存在；Portal URL 不可假定仍有效。
- 清点当前子 Agent、前台命令和后台任务；发现僵尸只报告，不擅自终止。

## 继续执行

执行 `STATE.json.nextAction`：在现有隧道上打开 RT-056 页面，完成只读 MVP 体验验收，点击库概览、服务健康、审计记录并记录 Evan 的界面修改意见。若隧道失效，先重新建立隧道；不得把管理台直接暴露到公网。真实建库、摄取和正式管理凭据不在这一步执行。

预期回执：页面/接口路径可访问、管理台 healthz 正常、三个只读视图能返回脱敏结果、8787/8790 无变化。失败时只读取独立目录日志和依赖状态，记录 BLOCKED/UNKNOWN，不能把旧历史 PASS 当成本次 PASS。

## 禁止事项

- 不覆盖、删除、reset、clean、stash 或回退用户已有修改；不覆盖旧交接包。
- 不停止/重启/修改 Life Gateway、OPS 8787/8790、OpenSearch、NAS、旧网关回滚档案。
- 不执行真实建库、摄取或任何不可逆数据写入，除非 Evan 另行明确授权并经过新一轮验收。
- 不读取、打印、提交或外发 token、API key、业务 Key、SSH 凭据、cookie、Portal token URL 或真实语料。
- 不把临时开发密钥当作正式凭据；正式化前重新配置并记录安全决策。
- 不根据本文件的旧命令自动重复外发、签发、部署或发布动作。

## 完成回执

完成只读体验验收后，向 Evan 报告：访问方式、三个视图的真实结果、是否修改了代码、验证时间、后台 PID 状态、仍未实现的真实建库/摄取与正式密钥事项。若要变更代码，创建独立修订并重新测试；若要更新本交接事实，创建 supersedes 本包的新交接包，不修改本包。

## 接管触发

用户说“继续 cwk-knowledge-platform-rt055-rt056”时，先读取本包三份文件和关键事实源，再做现实复核；不能仅凭本文件直接宣称完成或执行生产操作。

## 建议技能

- `task-handoff-package`
- `cwk-kb-query`
- `cwk-kb-authorize`
- `cwk-kb-create`
- `cwk-kb-access-setup`（若当前 gateway 已安装）

