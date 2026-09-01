# CWK 云端 sandbox 自助安装与配置引导

适用于把 CWK 装进一台**持久 sandbox**（`/workspace` 跨会话保留）里的 OpenClaw Agent。
本文是该场景下的权威步骤；本机安装（非 sandbox）见
[内部小团队上手](INTERNAL_DISTRIBUTION.md)，两者不要混着照做。

要让云端 Agent 自己按本文执行，把
[sandbox 引导提示词](../prompts/OPENCLAW_SANDBOX_BOOTSTRAP.md)整段发给它。

全流程分两段，**中间有一道人工闸门**：

- **第 1–5 步：安装与本地自检**。不读取任何真实 CWork 内容、不写 DocDB、不启用
  cron 或 AI，可以放心执行。
- **第 6 步：个人只读试跑**。会用使用者自己的 Key 读取获授权的 CWork 内容，
  **只有使用者明确确认后才执行**。

## 0. 前提

- 使用者本人持有 CWork 读取授权和自己的工作协同 Key；CWK 不共享 Evan 或他人的
  镜像、配置与授权。
- sandbox 内有 `python3.11`（最低 3.10）、`git`、`make`；项目只用 Python 标准库，
  不需要 `pip install`。
- 真实试跑还需要 sandbox 内已有公司 Skill `cms-cwork-workflow` 与 `cms-auth-skills`。
- `/workspace` 可写且跨会话持久。CWK 不需要、也不应尝试安装 `openclaw` CLI。
- Skill 根（如 `/workspace/skills`）在云端 sandbox 里**通常是只读保护挂载**，且公共
  镜像被多个 Agent 共用。这不影响核心安装：核心程序装在 `/workspace/CWK`，接入方式
  由第 2 步显式选择。不要为了“装上 Skill”去改挂载、权限或安全策略。

## 1. 安装前的低风险检查

只做只读检查，任何一项不满足就先停下问使用者，不要自行修复：

```bash
python3.11 --version
git --version
make --version
test -w /workspace && echo "/workspace writable"
test -w /workspace/skills && echo "skills writable" || echo "skills read-only or absent"
ls -d /workspace/CWK 2>/dev/null
```

最后一条**应当没有输出**。若已存在，说明这台 sandbox 已经装过 CWK：停下并交给
使用者决定，不要覆盖、删除或重装。

倒数第二条决定接入模式：**云端 sandbox 的 Skill 根通常是只读保护挂载**。是只读就
按第 2 步用 `router` 模式，不要尝试往里写。

## 2. 安装（核心程序与 OpenClaw 接入分开）

先装核心程序。这一步在所有环境都一样，且不需要 `openclaw` CLI：

```bash
git clone https://github.com/evan-zhang/CWK.git /workspace/CWK
cd /workspace/CWK
PYTHON=python3.11 ./install.sh
```

成功时最后一行输出 `CWK_CORE_READY`。默认**不做任何 OpenClaw 接入**，也不会
静默改 `AGENTS.md` 或写 Skill 目录。

`install.sh` 只做这些事：跑一次不含凭据的本地检查、缺失时从模板创建 `.env` 与
`cwk-mirror.local.json`（已存在则内容和权限都原样保留，新建时为 `0600`）、编译脚本、
跑脱敏 smoke（产物在 `runs/ci-smoke`）。它不读取 CWork、不写 DocDB、不创建 cron、
不修改 Agent、Gateway 或宿主配置。

然后**显式选择一种**接入模式：

```bash
# 自助 sandbox（Skill 根只读时的推荐做法）：在 Workspace 的 AGENTS.md 里维护路由块
PYTHON=python3.11 ./install.sh --integration router --workspace /workspace

# 仅当 /workspace/skills 确实可写时，才安装正式 Skill
PYTHON=python3.11 ./install.sh --integration workspace-skill --workspace /workspace

# Skill 根受保护、需要运维在宿主控制面为指定 Agent 注册时
PYTHON=python3.11 ./install.sh --integration host-skill --workspace /workspace
```

- `router` 只改指定的 `AGENTS.md`，在固定起止标记之间维护一个 CWK 路由块；重复执行
  不会重复追加，标记外的原有内容一律保留。缺半边标记或出现多个块时会拒绝修改并报
  `AGENTS_ROUTER_CONFLICT`，交给使用者手工处理。模板渲染后若不是恰好一对标记，报
  `AGENTS_ROUTER_TEMPLATE_INVALID`；`AGENTS.md` 不是合法 UTF-8 时报
  `AGENTS_ROUTER_UNREADABLE` 并原样保留文件（不猜编码、不改写）。`AGENTS.md` 是会话
  启动上下文，安装器会输出 `AGENTS_ROUTER_ACTIVATION=NEXT_SESSION`；当前会话不会自动
  重载该路由。
- `workspace-skill` 只复制公开的 `skill/` 目录，**绝不复制** `.env`、
  `cwk-mirror.local.json`、`runs/`、`raw/`、`knowledge/`。目标必须留在所选 Workspace
  内，且不能是软链接；目标已存在时默认拒绝覆盖。安装采用同盘暂存目录 + 原子改名，
  就位后的 Skill 目录保持与仓库里公开 `skill/` 一致的可读可进入权限（`0755`），
  以便以其他账号运行的 Agent 也能发现它；失败或中断时暂存目录会被清理。
- `--workspace` 不接受文件系统根 `/`，也不接受 CWK 项目自身的检出目录。
- `host-skill` 不写任何 Skill 根，只输出 `SKILL_REGISTRATION_REQUIRES_HOST_ADMIN`、
  当前执行环境里的源路径，以及可计算时的
  `CWK_SKILL_SOURCE_RELATIVE_TO_AGENT_WORKSPACE=CWK/skill`。`/workspace/...` 是容器路径，
  **不能原样当作宿主路径**；运维应把相对路径映射到该 Agent 的宿主 Workspace，再用与
  Gateway 同版本的宿主控制面只给指定 Agent 注册。sandbox 内不发起任何控制面调用。

一个 Agent 只启用一种模式，避免正式 Skill 与路由重复触发。**这条由安装器强制执行**：
同一 Workspace 已有路由块时装 `workspace-skill`、或已有正式 Skill 时装 `router`，都会
输出 `OPENCLAW_INTEGRATION_CONFLICT` 和 `CWK_EXISTING_INTEGRATION=...` 并以退出码 3
停下，不写任何文件。`--force` 只用于替换同一种模式下已存在的目标，**不会**放行混装，
也不会替你拆掉另一种模式。脚本报“拒绝覆盖”时照它说的停下，这是保护，不要绕开。

宿主运维收到 `host-skill` 交接后，可在**宿主终端**（不是 sandbox）把相对路径映射到
指定 Agent 的真实 Workspace，再执行当前 OpenClaw 支持的本地目录安装并核验（下面两条
已对照本机安装的 OpenClaw 2026.8.1 `skills` CLI 文档核实，未使用未记载的参数）：

```bash
openclaw skills install "<宿主 Agent Workspace>/CWK/skill" \
  --as cwk-mirror-workflow --agent "<Agent ID>"
openclaw skills info cwk-mirror-workflow --agent "<Agent ID>"
```

这两条是运维动作，不属于 sandbox 自助流程；若宿主 CLI 与 Gateway 版本不一致，先停下
处理版本一致性，不要把 Gateway token 或 Docker 权限下放给 sandbox。

## 3. 验证安装

```bash
ls runs/ci-smoke/digest-human-v4.md runs/ci-smoke/digest-human-v4.html
```

smoke 只验证命令与渲染是否连通；它使用极小的脱敏 fixture，`overall_pass` 为 `false`
也属正常。

按所选模式再验证接入：

```bash
# router 模式：确认路由块存在且只有一个；随后开启新会话验证触发
grep -c "BEGIN CWK ROUTER" /workspace/AGENTS.md

# workspace-skill 模式：
test -f /workspace/skills/cwk-mirror-workflow/SKILL.md && echo skill ok
```

安装器只能证明“文件已就位”，**不能**证明这台 sandbox 的运行时真的发现并加载了
Skill，所以它输出 `OPENCLAW_DISCOVERY=UNVERIFIED`。要确认是否真的被加载，请在 Agent
里实际验证；若发现不了，交给使用者决定怎么接，不要擅自改全局配置或安全策略。

## 4. 个人私有配置

由使用者自己填写 `/workspace/CWK/.env`，Agent 不需要看到、也不应回显其内容：

```dotenv
CWORK_APP_KEY=自己的工作协同Key
# 建议填写，供日报关系标签使用：
CWK_OWNER_EMP_ID=自己的员工ID
CWK_OWNER_NAME=自己的姓名

# 个人只读试跑保持为空；只有已批准的团队 DocDB 目标才填写：
CWK_DOCDB_PROJECT_ID=
CWK_DOCDB_ROOT_FILE_ID=
```

Key 不要贴进聊天记录、命令行参数或提交。`.env` 与 `cwk-mirror.local.json` 都已
gitignore。

只有当公司 Skill 不在默认位置时，才在同一份 `.env` 里补**路径类**变量
`CMS_CWORK_WORKFLOW_DIR`、`CMS_AUTH_SKILL_DIR`（计划发布 DocDB 时再加
`CMS_DOCDB_SKILL_DIR`）。这些是目录路径，不是凭据。

## 5. 本地自检

```bash
cd /workspace/CWK
python3.11 scripts/cwk_doctor.py --require-live
```

`doctor` 只检查本机条件，不读取 CWork、不写 DocDB。看这几件事：

- `cms_cwork_workflow`、`cms_auth_skills`：路径类检查，必须为 `ok`。`doctor` 会在多个
  Skill 根里查找（`/workspace/skills`、`/workspace/.agents/skills`、
  `/workspace/.openclaw/sandbox-skills/skills` 这个当前 OpenClaw 只读物化根，以及
  `$HOME/.openclaw/skills`、`$HOME/.agents/skills`），`HOME=/` 也能正常工作。
  仍找不到时，补上第 4 步的路径变量，或用 `CWK_SKILL_ROOTS` 指定额外的根，
  或请使用者先装公司 Skill。
- `live_auth_configured`：`doctor` 会用最小 dotenv 解析读取项目 `.env`，因此 Key 只写在
  `.env` 里也会显示 `configured`，不必再 `source .env`。它**只报告 configured /
  missing**，永远不打印值、前缀、哈希或任何可逆片段。当前 shell 已导出的变量优先于
  `.env`，`doctor` 也不会把 `.env` 的值写回进程环境。`.env` 只支持 `KEY=value`；
  `export KEY=value` 会被夜间流水线和 `doctor` 同样忽略，所以自检结论和实跑一致。
- `env_file`：只报告 `.env` 是否存在，不读出内容。
- `openclaw_integration`：报告检测到的接入方式（`NONE` / `FORMAL_SKILL` /
  `AGENTS_ROUTER`）。它只报告，不会替使用者选择或自动切换模式；同时检测到两种时会给出
  “一个 Agent 只该启用一种”的警告。

**不要**执行 `source .env`、`. ./.env`、`cat .env` 或任何转储凭据的命令；`doctor`
已经安全地读过它了。

只有计划发布到已批准的 DocDB 目标时，才额外执行：

```bash
python3.11 scripts/cwk_doctor.py --require-live --require-docdb
```

到这一步为止，全部是安装与本地检查，没有读取任何真实 CWork 内容。

## 6. 个人只读试跑（需使用者明确确认）

确认后再执行；它会按使用者的 Key 读取获授权的 CWork 内容，并只在本机生成
`runs/`、`raw/`、`knowledge/`，不发布 DocDB：

```bash
cd /workspace/CWK
python3.11 scripts/cwk_nightly_pipeline.py \
  --config cwk-mirror.local.json \
  --run-name pilot-$(date +%Y%m%d-%H%M) \
  --date $(date +%F)
```

验收：`runs/pilot-*/ACCEPTANCE-RESULT.md` 中 `overall_pass=true`，并检查同目录下的
`digest-human-v4.md` 与 `digest-human-v4.html`。

仅当目标是已批准的个人或团队 DocDB、且第 5 步的 DocDB 检查已通过时，才在同一命令
末尾加 `--sync-docdb`。本引导流程内不启用 cron、AI 试点或任何生产默认。

## 7. 持久化与隔离

- 跨会话保留：`/workspace/CWK`（含私有 `.env`、`cwk-mirror.local.json`）；
  `router` 模式还依赖 `/workspace/AGENTS.md`，`workspace-skill` 模式依赖
  `/workspace/skills`。受保护的只读 Skill 挂载由宿主管理，不在本流程的持久化范围内。
- 私有数据：`.env`、`cwk-mirror.local.json`、`runs/`、`raw/`、`knowledge/`、`state/`
  都不提交，也绝不复制他人的同名文件或历史镜像。
- 每台 sandbox、每个使用者用自己的 Key 与数据目录。

## 边界

- CWK 对 CWork 只读：不会回复、审批、完成、删除或标记事项。
- 原始 CWork 内容的安全处理由进入 CWK 前的上游授权链路负责；CWK 不做二次内容判定、
  扫描、脱敏或过滤。
- 历史 RT/PR、设计与验收材料仅供追溯，不是安装操作指令。
