# CWK 内部小团队上手

适用于公司内部已获授权的员工：在自己的 OpenClaw Agent 上，用自己的工作协同 Key
建立个人镜像，或向已批准的团队 DocDB 目标发布派生产物。CWK 不是共享 Evan 现有镜像、
配置或授权的安装包。

本文覆盖**本机安装**。若要装进 `/workspace` 持久的云端 sandbox，改看
[云端 sandbox 上手](SANDBOX_ONBOARDING.md)；要把整件事交给云端 OpenClaw Agent 自助
完成，把[sandbox 引导提示词](../prompts/OPENCLAW_SANDBOX_BOOTSTRAP.md)整段发给它。
两条路径共用同一套安装、自检与试跑命令，不要混着照做。

## 1. 前置条件

- 私有仓库访问权；Python 3.10+（建议 3.11）。项目当前只使用 Python 标准库，无需
  `pip install`、lockfile 或包发布体系。
- 本机 OpenClaw 可发现公司 `cms-cwork-workflow` 与 `cms-auth-skills`。选择 DocDB
  发布时，另需 `cms-docdb` 和该个人/团队目标的写入权限。
- 安装者本人持有 CWork 读取授权和自己的 Key。团队目标还需事先批准目标范围、项目和根目录。

## 2. 安装

核心程序安装与 OpenClaw 接入是两个动作。先装程序：

```bash
git clone https://github.com/evan-zhang/CWK.git
cd CWK
PYTHON=python3.11 ./install.sh
```

安装会创建 gitignored 的 `.env` 和 `cwk-mirror.local.json`（新建时权限 `0600`；已存在
则内容与权限都原样保留）、编译脚本并运行脱敏 smoke，成功时输出 `CWK_CORE_READY`。
它不会读取 CWork、写入 DocDB、创建定时任务，也不会修改 Agent、Gateway 或宿主配置，
更不需要 `openclaw` CLI。

紧随其后还有一行 `CWK_ACTIVATION=NOT_STARTED`：安装既不做发现，也不创建私有激活
状态，夜间自动化默认关闭。要打开它得另走第 6 步。

再**显式选择一种** OpenClaw 接入模式（默认什么都不接入，不会静默改任何目录）：

```bash
# 本机可写的 Workspace：安装正式 Skill（只复制公开的 skill/ 目录）
PYTHON=python3.11 ./install.sh --integration workspace-skill --workspace ~/my-workspace

# Skill 根受保护、由运维在宿主控制面为指定 Agent 注册
PYTHON=python3.11 ./install.sh --integration host-skill

# 只在 AGENTS.md 里挂一个路由块（自助 sandbox 用）
PYTHON=python3.11 ./install.sh --integration router --workspace ~/my-workspace

# 只要程序、不接 OpenClaw
PYTHON=python3.11 ./install.sh --integration none
```

`--skills-dir <workspace 内路径>` 可覆盖默认的 `<workspace>/skills`；出于隔离要求，
新接入模式会拒绝 Workspace 外路径和现有叶子软链接。`--workspace` 也会拒绝文件系统根
`/` 和 CWK 项目自身的检出目录：前者会让“Workspace 内”这个边界失去意义，后者不是
Agent Workspace。目标已存在时默认拒绝覆盖，确认要替换再加 `--force`。`router` 修改
的是会话启动上下文，输出 `AGENTS_ROUTER_ACTIVATION=NEXT_SESSION` 后要在新会话验证，
当前会话不会自动重载。

**一个 Agent 只启用一种模式，这条由安装器强制执行。** 同一个 Workspace 里若已存在
`AGENTS.md` 路由块而你去装 `workspace-skill`，或已存在正式 Skill 而你去装 `router`，
安装器都会输出 `OPENCLAW_INTEGRATION_CONFLICT` 与 `CWK_EXISTING_INTEGRATION=...`
并以退出码 3 停下，不做任何写入。`--force` 只用于“替换同一种模式下已存在的目标”，
不会放行混装，也不会自动拆掉另一种模式——要换模式请自己先移除旧的接入方式。

`router` 在写入前会校验渲染出来的块恰好含一对起止标记，模板异常时输出
`AGENTS_ROUTER_TEMPLATE_INVALID`；目标 `AGENTS.md` 不是合法 UTF-8 时输出
`AGENTS_ROUTER_UNREADABLE` 并原样保留文件，不会用猜测的编码改写它。以上失败都不留
临时文件，中断（Ctrl-C 等）也会清理暂存目录。

安装器会输出 `OPENCLAW_DISCOVERY=UNVERIFIED`：它只保证文件到位，不能证明你的 OpenClaw
运行时真的发现并加载了这个 Skill，请在 Agent 里实际确认。`host-skill` 输出的
`CWK_SKILL_SOURCE` 只属于当前执行环境；若它来自 sandbox 的 `/workspace/...`，宿主运维
必须用 `CWK_SKILL_SOURCE_RELATIVE_TO_AGENT_WORKSPACE` 映射到该 Agent 的真实宿主
Workspace，不能把容器路径原样交给宿主 CLI。

### 从旧版 `--install-skill` 迁移

`--install-skill` 仍然可用，行为不变（在默认或 `--skills-dir` 指定的目录建立指向
`skill/` 的软链接，并保留原有的“拒绝覆盖非链接目录 / 拒绝替换指向别处的链接”保护），
不会静默删除或覆盖你已有的链接。它现在会额外打印 `CWK_INSTALL_SKILL_DEPRECATED`
和迁移提示。新装请改用上面的显式模式；已装的可以继续用，或删掉旧链接后改用
`--integration workspace-skill`。

## 3. 只配置自己的本机文件

把 `CWORK_APP_KEY` 直接发给 Agent（定制客户端通路内允许且推荐），Agent 用
`python3.11 scripts/setup_app_key.py`（Key 经 stdin 传入）原子写入并回执
`configured`；或自行编辑新建的 `.env`。无论哪种方式，Key 都不要放进命令行参数
或提交。

```dotenv
CWORK_APP_KEY=自己的工作协同Key
# 建议填写，供日报关系标签使用：
CWK_OWNER_EMP_ID=自己的员工ID
CWK_OWNER_NAME=自己的姓名

# 个人本地试跑保持为空；只有已批准的团队 DocDB 目标才填写：
CWK_DOCDB_PROJECT_ID=
CWK_DOCDB_ROOT_FILE_ID=
```

如果 CWK 仓库不在所选 Agent Workspace 里面，在同一 `.env` 里把
`CWK_WORKSPACE_DIR` 设为安装命令使用的 Workspace；这是路径提示，不是凭据，供
`doctor` 找正式 Skill 或路由块。默认路径找不到公司 Skills 时，再设置对应的
`CMS_CWORK_WORKFLOW_DIR`、`CMS_AUTH_SKILL_DIR`、`CMS_DOCDB_SKILL_DIR`，或补充
`CWK_SKILL_ROOTS`。
每个人都使用自己的 `.env`、`cwk-mirror.local.json`、本机 Agent 和数据目录；绝不复制
Evan 或其他同事的 `.env`、`knowledge/`、`raw/`、`runs/`、`state/` 或历史镜像。

## 4. 本地自检

`doctor` 会用最小 dotenv 解析安全读取项目 `.env`，**不需要**先 `source .env`：

```bash
python3.11 scripts/cwk_doctor.py --require-live
```

它只报告凭据“已配置 / 缺失”（`live_auth_configured` 为 `configured` 或 `missing`），
永远不打印值、前缀、哈希或任何可逆片段，也不会执行 `.env` 里的内容。当前 shell 已经
导出的变量优先于 `.env`。`.env` 只支持 `KEY=value` 这一种写法：`export KEY=value`
会被夜间流水线忽略，`doctor` 也照样忽略，因此不会出现“自检说已配置、实跑却读不到”
的分歧。公司 Skill 会在多个根里查找（`$HOME/.openclaw/skills`、
`$HOME/.agents/skills`、`<workspace>/skills`，以及 sandbox 当前的只读物化根
`<workspace>/.openclaw/sandbox-skills/skills`），必要时用 `CWK_SKILL_ROOTS` 补充额外的根。

`doctor` 还会报一项 `activation`，取值是激活状态机的枚举（刚装完是 `not_started`），
只报枚举、状态名和下一步，不打印路径、哈希或任何业务内容。私有激活状态存在却校验
不过时它报 `unreadable` 并给出告警，但不会让安装类检查失败——这样才有可能重装修复。

只有计划发布到已批准的 DocDB 目标时，额外执行：

```bash
python3.11 scripts/cwk_doctor.py --require-live --require-docdb
```

两个命令都只检查本机条件，不读取 CWork 或写入 DocDB。

## 5. 一次试跑

先做个人本地只读试跑；它会按你的 Key 读取获授权的 CWork 内容，并只在本机生成
`runs/`、`raw/` 与 `knowledge/`，不会发布到 DocDB：

```bash
python3.11 scripts/cwk_nightly_pipeline.py \
  --config cwk-mirror.local.json \
  --run-name pilot-$(date +%Y%m%d-%H%M) \
  --date $(date +%F)
```

确认本次 `runs/pilot-*/ACCEPTANCE-RESULT.md` 中 `overall_pass=true`，并检查生成的日报
Markdown/HTML。仅当目标为已批准的个人或团队 DocDB、且第 4 步的 DocDB 检查已通过时，
才在同一命令末尾加 `--sync-docdb`。这一步是手动试跑，不启用 AI 试点、不排期、也不
建任何定时任务。

## 6. 激活（可选，且独立于安装）

到这里程序装好了，但没有任何东西被授权每晚自动跑。**装好 ≠ 授权**：安装从不创建
私有激活状态，也从不建定时任务。

激活是一段有状态的对话，判定由 `scripts/activation_wizard.py` 独占——它决定状态
机、两道人工确认、每日执行合同及其漂移、试跑门禁和调度交接：

```bash
python3.11 scripts/activation_wizard.py status
```

按它给出的唯一下一步走。这条路径上有**两次相互独立的确认**：先授权只读发现；很久
之后、在你看过一次真实只读试跑的评分结果之后，再单独问一次是否允许排期。中途改了
配置或画像，已有确认自动作废并要求重走，这是设计如此。

定时任务由**宿主**创建，不由本仓库创建。仓库只产出一份交接单（节奏、本地运行时刻、
时区、完整 argv、需要的环境变量**名**、前置条件），不含凭据值，也不含宿主绝对路径；
配置用项目内相对定位符加一份「宿主自己解析项目根」的约定来表述，配置若在项目外则
直接拒绝出单，而不是退化成塞一个宿主路径进去。你用自己的宿主机制建好任务后，把宿主
分配的标识回填给 `record-schedule`。

完整的分状态话术、命令与失败处理见[激活对话参考](../skill/references/activation.md)。
无论哪种状态，四条红线都成立：`CWORK_APP_KEY` 之外的凭据不进对话（这把 Key 本身允许在定制客户端通路里发送，且只经 `scripts/setup_app_key.py` 落盘）；不把「现在能调用工具」当成授权；
不展示 raw 原文；不创建、不修改、不删除任何定时任务。

## 边界

- CWK 对 CWork 只读：不会回复、审批、完成、删除或标记事项。
- 原始 CWork 内容的安全处理由进入 CWK 前的上游授权链路负责；CWK 不做二次内容判定、扫描、
  脱敏或过滤。
- 历史 RT/PR、设计和验收材料仅供追溯，不是安装操作指令。
