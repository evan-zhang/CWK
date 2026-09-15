# CWK RT-055 知识库平台与 RT-056 管理台交接包

## 任务目标

本交接包整理当前 Discord「工作协同」会话中关于 CWK 知识库新栈、跨 gateway 接入、知识库管理台 MVP 的讨论、决策、实现、验证和遗留事项，供后续会话安全接续。

完成标准：
- RT-055 新检索/RAG 栈已上线并可供局域网 gateway 使用。
- `cwk-kb-query`、`cwk-kb-authorize`、`cwk-kb-create` v3 已在当前 chat-main-agent gateway 安装、可见并完成 spbp 端到端验证。
- 新 gateway 接入 runbook 已形成。
- RT-056 管理台 MVP 已在本地验收、合并、推送，并以独立进程部署到 OPS 预览。
- 所有关键事实、代码位置、运行状态和下一步可被接管者复核。

明确不包含：本包不执行 `/new`；不实现真实建库/摄取写入；不把 OPS 管理密钥、业务 Key、token、SSH 凭据或临时 Portal 凭证写入交接材料。

## 范围与边界

- 主项目是 CWK Git 仓库；RT-055 生产服务位于 OPS，现有 8787/8790 不得因管理台变更而停止、重启或改动。
- 管理台是 RT-056 独立新增服务，默认 loopback 8791；OPS 部署目录独立，不覆盖生产目录。
- 普通 Agent 通过 `X-KB-Token` 访问 8787 `/query`、8790 `/answer` 和 `/read`；token 按 Agent 实例和 bank scope 管理。
- 管理面（建库、摄取、签发/吊销授权）应集中在有 OPS registry 写权限的管理侧；不向普通 gateway 分发 `CWORK_APP_KEY` 或 OPS SSH 凭据。
- 语料、命中片段、原文、密钥、token 明文、盐、owner 引用和 SSH 信息不得进入仓库、日志或公共频道。
- 当前管理台是开发预览：OPS 上使用临时开发配置启动，正式环境前必须更换独立管理密钥并重新评估暴露面。

## 当前状态

`COMPLETED`（本阶段代码、skill 接入、RT-056 本地验收和 OPS 开发预览已完成；真实建库/摄取和正式安全收口未完成）。

本次现场核验时间：2026-09-15 16:32（Asia/Shanghai）。

## 已完成事项

1. RT-055 方案选择：OpenSearch 双通道方案 A 胜出；B 的 WeKnora 原生方案未通过预注册门限。该结论来自冻结试卷和只读公平性复核，详见历史运行记录与 `RT/RT-055/`。
2. RT-055 生产包已完成并合并 main；正式检索入口为 OPS 8787，RAG 问答/原文读取为 8790；OpenSearch 9200 仅 loopback。
3. 影子对账完成：历史记录为 828 轮、0 错误，平均检索耗时约 10.57ms；数据已存档 OPS。旧 gateway 已退役，回滚材料留存 OPS。
4. 鉴权已接线并验证：健康端点免鉴权；无 token 401；有效 token 200；越权 bank 403；吊销 token 401。测试 token 已清理，主力 token 活跃面仅保留内部/工作区 token（敏感值不记录）。
5. 三个 CWK skill v3 已从仓库同步至当前 chat-main-agent 的 workshop 安装位，并追加到 `agents.entries.chat-main-agent.skills` 白名单；CLI 显示 `Visible to model: yes`，独立新会话实测三者均可见。
6. 新会话使用 `cwk-kb-query` 查询 `spbp-2027` 已完成端到端验证：HTTP 200、3 条命中、约 5.8ms，token 来源为 gateway 环境变量 `CWK_KB_TOKEN`。
7. 新 gateway 接入文档已升级为 v3 两阶段 runbook，并推送：`docs/cwk-kb-access-setup.md`，提交 `ca64885`；随后补充 8790 LAN 入口说明，skill 提交 `34c6c02`，token 复用说明提交 `9cd8d53`。
8. RT-056 管理台 MVP 已实现并合并 main：提交 `11c99e0`。主要文件：`scripts/kb_admin.py`、`docs/KB-ADMIN.md`、`tests/test_rt056_kb_admin.py`、`RT/RT-056/rt-lite.md`、治理归属 manifest。
9. RT-056 独立分支验收和 main 复跑均为 5/5；`py_compile`、`git diff --check`、治理审计通过。管理台默认关闭、默认 loopback 8791；overview/services/audit 脱敏；建库/摄取默认 403，开启后也仅返回 501 占位，不执行写入。
10. RT-056 已以加法方式部署到 OPS：`/Users/xgstudio/rt056-kb-admin-mvp`，当前 PID 62285，`127.0.0.1:8791`，健康状态 `ok/enabled=true`；现有 8787/8790 仍正常。通过本机 SSH 隧道和 Portal 临时入口供 Evan 预览；临时入口不写入本包。
11. 当前管理台 MVP 的页面按钮可查看：库概览、服务健康、审计记录；页面请求会提示管理密钥。建库/摄取仍是安全占位。

## 进行中事项

- 无必须立即继续的代码动作。RT-056 预览进程正在 OPS 持续运行，属于可恢复的开发预览后台任务；后续若不再预览，可由管理员按 PID 62285 停止，不能误停 8787/8790。
- 若 Evan 要继续体验界面，需复用现有本机 SSH 隧道/Portal 会话或重新建立隧道；临时 Portal URL 未记录在包内。

## 未开始事项

按优先级：

1. P0：把管理台从“开发预览临时密钥”改成受控的独立管理密钥，并决定正式访问方式（仍 loopback+隧道，或内网绑定）。
2. P0：管理台首次真实试用后收集 Evan 的界面调整意见，补 UI 导航、筛选和详情页。
3. P1：实现真实建库流程：输入库名、语料来源、用途/授权范围，经用户确认后调用现有 CWK 摄取与 RT-055 注册流程。
4. P1：实现真实摄取任务队列、进度、失败重试和任务审计；当前接口明确不执行写入。
5. P1：管理台接入正式授权签发/轮换/吊销流程；当前授权列表只做脱敏投影。
6. P1：为第一个外部 gateway 按 runbook 实战接入，校验文档的跨机器路径和 token 交付流程。
7. P2：发布 `v0.3.0` 正式版；当前已有 beta release，正式发布时机尚待观察/确认。
8. P2：New API 中转密钥轮换（此前曾有测试脚本误打印风险，需 Evan 在后台完成轮换）。
9. P2：单独讨论动态知识库自动更新管道。
10. P2：清理或修订其他仍指向旧 CWK gateway 的历史 skill/文档（尤其 `cwk-query`、`cwk-ops-deploy` 等），需另行核验，不能本包直接假定完成。

## 关键决策

- 采用 OpenSearch 双通道方案 A：检索质量、exact/no_answer 和延迟达到预注册门限；底座为开源 OpenSearch，检索设计和代码为 CWK 自研。
- 采用单实例多 bank：新增知识库通过摄取+注册一行接入，8787/8790 服务无需为每个库复制部署。
- 读能力分布到所有 gateway，管理能力集中在管理侧：普通 gateway 只需 `cwk-kb-query` 和按 scope 的 token；建库、摄取、token 管理需 OPS registry 管理权限。
- token 采用 per-agent、per-bank scope；最小授权、可单独吊销；每 owner 跨 Agent 实例活跃 token 上限为 5。
- 管理台先做轻量独立 MVP，不修改 8787/8790；真实建库/摄取第一版先占位，避免在 UI 尚未评审时产生不可逆写入。
- 这次 Evan 明确同意部署管理台并要求开发阶段先看 MVP，因此 OPS 预览以开发模式开启；正式化前必须重新配置凭据和访问边界。

## 阻塞与风险

- **当前关键风险：** RT-056 管理台使用的是临时开发密钥/开发预览配置。它仅用于本阶段观看，不应作为正式部署方案；解除条件是配置受控管理密钥并决定访问网络范围。
- **未阻塞但需注意：** 管理台依赖从 CWK scripts 复制到 OPS 独立目录；后续版本发布应改成明确的部署包或容器，避免手工漏依赖（本次曾先后漏掉 `kb_ops`、`kb_gateway` 依赖）。
- **历史运维风险：** macOS Docker bind mount 对 registry 原子替换存在秒级传播延迟；签发/吊销后应等待至少 10 秒再验证。
- **文档风险：** `docs/cwk-kb-access-setup.md` 已升级 v3，但 skill references 目录仍保留历史 `v2-read-chain.md`；v3 主 skill 不引用它，接管者不要按该旧文件操作。
- **本地工作区风险：** `CLAUDE.md` 有一处已有修改，未由本任务创建，禁止覆盖或回退；`docs/handover/` 是交接包目录/未跟踪产物。
- **未确认：** 管理台正式管理密钥未进入受保护密钥库；不记录其值，也不在此包内补发。

## 关键文件与产物

| 绝对路径 | 存在 | 用途 | 所有权 |
|---|---:|---|---|
| `/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK` | 是 | 本机 CWK 主仓库 | task/repository |
| `/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK/scripts/kb_admin.py` | 是 | RT-056 管理 API/UI | task |
| `/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK/docs/KB-ADMIN.md` | 是 | 管理台启动/安全说明 | task |
| `/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK/tests/test_rt056_kb_admin.py` | 是 | RT-056 合成契约测试 | task |
| `/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK/docs/cwk-kb-access-setup.md` | 是 | 新 gateway v3 接入 runbook | task/repository |
| `/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK/skills/cwk-kb-query/SKILL.md` | 是 | 查询/问答/读原文 skill v3 | task/repository |
| `/Users/xgstudio/rt056-kb-admin-mvp` | 是 | OPS 管理台独立运行副本 | OPS runtime |
| `/Users/evan/.openclaw/gateways/life/openclaw.json` | 是 | 当前 gateway agent skills 白名单所在配置 | gateway config |
| `/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK/docs/handoffs/20260915-163210-cwk-knowledge-platform-rt055-rt056` | 是 | 本交接包 | handoff |

敏感文件只记录用途，不记录内容：Life gateway `.env`、OPS `auth/key.env`、token registry、管理台临时 key、SSH 凭据、Portal token URL。

## Git 与工作区状态

### CWK 主仓库（本次现场核验）

- 根目录：`/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK`
- remote：`https://github.com/evan-zhang/CWK.git`
- branch：`main`
- HEAD：`11c99e0ce8a6d87eed451a554c328172ebbd6fbf`
- 最近交付 commit：`11c99e0 merge: RT-056 knowledge base admin MVP`
- GitHub 已推送：历史核验显示 `main` 已推送到 `11c99e0`。
- 当前状态：`CLAUDE.md` 有 1 个已有修改；`docs/handover/` 未跟踪（本交接包及既有 handover 目录）。二者均不可擅自清理。
- 保护分支/历史参考：`feature/RT-056-kb-admin-mvp`=`f396c0e`，已合并；RT-055 相关 feature/release 分支保留，详见 `git branch`。

### OPS 运行副本

- 目录：`/Users/xgstudio/rt056-kb-admin-mvp`
- 不是 git 主仓库部署，不得覆盖 `/Users/xgstudio/rt055-production`。
- 当前由独立后台 Python 进程运行，使用只读状态依赖副本；管理台默认开发配置。

## 进程与后台任务

- `PID 62285`：OPS RT-056 管理台，状态 RUNNING；用途：开发预览；本次核验 2026-09-15 16:32；恢复/停止：通过 OPS SSH 查看 `/tmp/rt056-kb-admin.pid`，只操作该 PID，勿触碰 8787/8790。
- 本机 `PID 30600 -> 30601`：SSH 隧道，状态 RUNNING（本次现场进程核验曾见）；用途：本机 `18791` 转发至 OPS `127.0.0.1:8791`；恢复：按既有 Life gateway `.env` 使用 password-only SSH 重新建立；停止前确认不再预览。
- 活跃子 Agent：无。
- 其他后台任务：无（除上述 SSH 隧道和 OPS 管理台）。
- 管理台不由当前前台会话直接承载，可独立恢复；本交接包不擅自停止它。

## 验证证据

以下区分历史验证与本次现场核验：

### 本次现场核验（2026-09-15 16:32）

- **PASS**：CWK workspace 路径和 git root 确认为 `/Users/evan/.../projects/CWK`。
- **PASS**：主线 branch=`main`、HEAD=`11c99e0...`、remote 为 CWK GitHub。
- **PASS**：RT-056 关键文件存在。
- **PASS**：OPS 管理台 PID 62285 仍运行；8791 为 loopback listener；8787/8790 仍有 listener；`/healthz` 返回 `enabled=true,status=ok`。
- **PASS**：本地 SSH 隧道 PID 30600/30601 仍在现场进程列表中。
- **NOT_RUN**：本次没有重复访问真实语料、token registry 明文或 OPS 生产索引；原因是交接仅需状态核验，避免扩大敏感数据暴露。

### 历史验证（原日期注明）

- **PASS（2026-09-15 14:40 左右）**：RT-056 分支独立测试 5/5、编译、diff check 通过。
- **PASS（2026-09-15 14:40 左右）**：合并到 main 后主线测试 5/5、编译、diff check 通过；合并 commit `11c99e0` 并 push。
- **PASS（2026-09-15 11:35 左右）**：三个 skill 白名单追加后 CLI 显示 `Visible to model: yes`；新子代理三项可见均为 true。
- **PASS（2026-09-15 15:18 左右）**：新会话自行读取 `cwk-kb-query`，用 `env CWK_KB_TOKEN` 查询 `spbp-2027`，HTTP 200、3 hits、5.799ms。
- **PASS（2026-09-15 15:20 左右）**：LAN `/answer` 通过 192.168.91.72:8790，HTTP 200、约 23.5s、3 citations。
- **PASS（2026-09-15 16:01 左右）**：OPS 管理台开发模式开启；健康 200、无 key API 401、开发 key API 200；该开发 key 值不记录。
- **PASS（2026-09-15 16:32 左右）**：OPS 管理台独立目录和依赖副本补齐后进程存活；当前现场核验重复确认。
- **NOT_RUN**：RT-056 真实建库、真实摄取、生产管理密钥轮换、正式公网/内网暴露验证；这些属于未开始或后续决策。

## 下一步

唯一首要动作：**由接管者先在本机通过现有 SSH 隧道打开 RT-056 页面，做一次只读 MVP 体验验收，并记录 Evan 的界面调整意见；不要先实现真实建库/摄取。**

前置条件：
1. OPS PID 62285 仍运行，或按 `/tmp/rt056-kb-admin.pid` 恢复独立管理台进程。
2. 本机 SSH 隧道 `18791 -> OPS 127.0.0.1:8791` 可用；若已断，重新建立 password-only 隧道。
3. 只操作 RT-056 独立目录和 8791，不改 8787/8790。

预期证据：页面打开；点击“库概览”“服务健康”“审计记录”均能显示 MVP 数据；管理台服务健康为 ok；Git/OPS 生产服务无变化。失败处理：只读记录错误并检查依赖/进程日志，不重启生产服务、不写入真实库；若需要变更代码，另开明确任务并保留本包只读。

## 未知项

- 管理台临时开发密钥的持久化位置和最终轮换方案：未知；需 Evan/管理员决定并通过受控密钥机制配置。
- Portal 临时 URL 的当前有效期：未知；接管者需重新建立本机隧道/Portal，不能依赖本包外部链接。
- `quant-orchestrator`、`workbuddy-clone` 是否需要三项 CWK skill：未授权/未验证；当前仅 chat-main-agent 已配置。
- OPS 防火墙规则的完整内容：本次未取得权限；但 8787/8790 已有跨机 LAN 正向验证，8791 当前 loopback。
- RT-055 当前正式 release 是否已切为 v0.3.0 stable：未知/未发布；已有 beta 记录。

## /new 安全结论

**是，但有条件。** 本交接包三份文件和封存清单完成后，当前唯一后台依赖是可追踪、可恢复的 OPS 管理台与 SSH 隧道；没有活跃子 Agent 或未完成前台命令。新会话应先读取本包并复核 PID/端口/Git 状态，再继续唯一下一步。管理台临时预览仍在运行，不应把“可安全 /new”理解为“可删除或停止后台进程”。

## 会话与历史指针

- sourceSession.sessionKey：`agent:chat-main-agent:discord:channel:1524382903184789685`
- sourceSession.sessionId：`71ad48f7-a187-4ddb-89f3-38f3940e8067`
- channel：Discord；目标频道：`#工作协同`，channel id `1524382903184789685`
- 源会话创建时间：本包只记录已知运行上下文：当前会话持续约 7 天 25 分钟；精确创建时间未在本次状态输出中直接给出。
- compactions：2；本次 `session_status` 现场快照：context 224k/272k，约 82%，gateway uptime 5h23m。
- 详细历史不要全量回捞；若需核对，使用 `sessions_history` 以本 sessionKey 和已知 message/session 锚点有界读取，或使用 `sessions_search` 搜索 `RT-055`、`RT-056`、`管理台`、`交接`，每次限制结果量。

关键 anchors：
- 2026-09-14 15:16：确认其他会话通过 `cwk-kb-query` 访问 `spbp-2027` 的话术和端点。
- 2026-09-15 00:00：明确多 gateway 共享 OPS 服务，读分布式接入、建库管理集中化。
- 2026-09-15 00:13：v3 gateway 接入 runbook 写入并推送。
- 2026-09-15 00:24–00:29：提出并同意 AI 知识库管理台 MVP。
- 2026-09-15 14:40：RT-056 分支独立验收后合并 main、测试 5/5。
- 2026-09-15 16:00–16:02：OPS 管理台开启开发预览，8791 loopback + SSH/Portal 访问。

## 接管提示

本包只固化状态和事实，不自动执行旧建议。接管者必须先复核可变现实状态；若现实与本包冲突，以现场为准，并创建 supersedes 修订包，不覆盖本包。
