# OpenClaw 云端 sandbox 引导提示词

把下面分隔线之间的内容整段复制给运行在持久 sandbox 里的 OpenClaw Agent。
它对应的完整步骤说明是 [docs/SANDBOX_ONBOARDING.md](../docs/SANDBOX_ONBOARDING.md)；
命令以那份文档和仓库当前代码为准，两边不一致时以仓库为准。

---

你在一台持久 sandbox 里工作（`/workspace` 跨会话保留）。任务：为我安装 CWK，做完
本地自检，然后逐项引导我完成我自己的私有配置。全程用简体中文，先给结论再给证据。

## 硬性禁止（任何阶段都适用）

1. 不向我索取 Key、Token 或 `.env` 内容；不回显、不打印、不写进命令行参数、不贴进
   聊天记录、不写进任何文件或提交。
2. 不执行 `source .env` / `. ./.env`，也不用 `cat`、`env`、`echo` 等方式读取或转储
   `.env`。凭据由我自己填写，你只看命令的通过/失败结果。
3. 不依赖、不安装、不尝试调用 `openclaw` CLI。CWK 不需要它。
4. 不修改全局配置、shell 配置文件、安全策略、权限或沙箱设置。
5. 不覆盖、不删除、不重装已存在的 CWK 目录、已存在的 `.env` 或
   `cwk-mirror.local.json`、已存在的 workspace Skill 或 Skill 链接。遇到已存在就停下
   问我。
6. 安装前只做低风险只读检查（查版本、查目录是否存在、查是否可写），不做任何修复性
   改动。
7. 阶段 6 之前不读取任何真实 CWork 内容、不写 DocDB、不启用 AI、不创建 cron 或任何
   定时任务。
8. 只有我明确说“可以试跑”之后，才执行个人只读试跑。
9. 如果这台机器的公司 Skill 不在默认路径，只设置**路径类**环境变量
   `CMS_CWORK_WORKFLOW_DIR` / `CMS_AUTH_SKILL_DIR`（值是目录路径），绝不碰任何凭据类
   变量。

## 工作方式

分阶段执行。**每个阶段结束就停下**，先用一段话汇报：这一步做了什么、结果是通过还是
失败、我需要做什么决定。不要连着把多个阶段跑完，也不要替我决定。

## 阶段 1：环境体检（只读）

执行并汇报结果：

```bash
python3.11 --version
git --version
make --version
test -w /workspace && echo "/workspace writable"
test -w /workspace/skills && echo "skills writable" || echo "skills read-only or absent"
ls -d /workspace/CWK 2>/dev/null
```

要求 Python 3.10+（建议 3.11）。最后一条应当没有输出；若有输出说明已经装过，
**停下问我**，不要动它。任何一项不满足都停下汇报，不要自行安装或修复。

汇报时明确写出 Skill 根是可写还是只读——它决定阶段 3 用哪种接入模式。云端 sandbox 的
Skill 根通常是只读保护挂载，这是正常的，不要尝试改权限或挂载。

**暂停，等我说继续。**

## 阶段 2：安装核心程序

```bash
git clone https://github.com/evan-zhang/CWK.git /workspace/CWK
cd /workspace/CWK
PYTHON=python3.11 ./install.sh
```

成功时最后一行是 `CWK_CORE_READY`。这一步**只装程序，不接入 OpenClaw**，不会改
`AGENTS.md`，也不会写任何 Skill 目录。

这个脚本只做本地事情：本地检查、缺失时从模板创建 `.env` 与
`cwk-mirror.local.json`（新建时权限为 `0600`；已存在则内容和权限都不动）、编译脚本、
跑脱敏 smoke。它不读 CWork、不写 DocDB、不建 cron、不改 Agent、Gateway 或宿主配置。
若它报“拒绝覆盖”，照它说的停下并汇报，不要绕开。

**暂停，等我说继续。**

## 阶段 3：选择并执行一种 OpenClaw 接入模式

**先把选择权交给我**：用一句话说明阶段 1 测到的 Skill 根是否可写，并给出你的推荐，
然后等我拍板。不要自己替我选，也不要为了“装上”而改权限、挂载或安全策略。

```bash
cd /workspace/CWK

# A. Skill 根只读（sandbox 常见）→ 在 Workspace 的 AGENTS.md 里维护 CWK 路由块
PYTHON=python3.11 ./install.sh --integration router --workspace /workspace

# B. /workspace/skills 确实可写 → 安装正式 Skill（只复制公开的 skill/ 目录）
PYTHON=python3.11 ./install.sh --integration workspace-skill --workspace /workspace

# C. 需要运维在宿主控制面为指定 Agent 注册 → 只输出交接信息，不写任何 Skill 根
PYTHON=python3.11 ./install.sh --integration host-skill --workspace /workspace
```

- A 只改指定的 `AGENTS.md`：在固定起止标记之间维护一个 CWK 路由块，重复执行不会重复
  追加，标记之外的原有内容全部保留。若它报 `AGENTS_ROUTER_CONFLICT`（缺半边标记或有
  多个块）、`AGENTS_ROUTER_TEMPLATE_INVALID`（模板渲染后标记不成对）或
  `AGENTS_ROUTER_UNREADABLE`（`AGENTS.md` 不是合法 UTF-8），**停下汇报**，不要手工猜
  着改，也不要转换文件编码。它会输出 `AGENTS_ROUTER_ACTIVATION=NEXT_SESSION`；当前
  会话不会自动重载路由，后续要在新会话里验证。
- B 只复制公开的 `skill/`，绝不复制 `.env`、`cwk-mirror.local.json` 或任何运行数据；
  目标必须留在 `/workspace` 内且不能是软链接，已存在时会拒绝覆盖，此时停下问我。
- C 会输出 `SKILL_REGISTRATION_REQUIRES_HOST_ADMIN`、当前环境的 Skill 源路径，以及
  可计算时的 `CWK_SKILL_SOURCE_RELATIVE_TO_AGENT_WORKSPACE`。把这些非敏感交接状态转给
  我即可；明确提醒我 `/workspace/...` 是容器路径，运维要把相对路径映射到该 Agent 的
  宿主 Workspace。**不要**尝试调用任何控制面、Gateway 或 `openclaw` CLI。

一个 Agent 只启用一种模式，安装器会强制这一点：已有路由块时装 B、或已有正式 Skill 时
装 A，都会报 `OPENCLAW_INTEGRATION_CONFLICT` 并停下，不写任何文件。遇到它就把
`CWK_EXISTING_INTEGRATION=...` 转给我，**不要**加 `--force` 去绕（`--force` 只替换同
一种模式下的已有目标，不放行混装），也不要自作主张删掉另一种模式。安装器输出
`OPENCLAW_DISCOVERY=UNVERIFIED` 是如实说明：它只能保证文件到位，不能证明运行时真的
加载了 Skill。

## 阶段 3b：验证

```bash
cd /workspace/CWK
ls runs/ci-smoke/digest-human-v4.md runs/ci-smoke/digest-human-v4.html
# A 模式：应当输出 1
grep -c "BEGIN CWK ROUTER" /workspace/AGENTS.md
# B 模式：
test -f /workspace/skills/cwk-mirror-workflow/SKILL.md && echo skill ok
```

smoke 用的是极小的脱敏样例，只验证命令与渲染连通；它报 `overall_pass=false` 属正常，
不要因此重装或改配置。若运行时最终发现不了 CWK，**只汇报现象并问我**，不要改全局配置。

**暂停，等我说继续。**

## 阶段 4：引导我完成私有配置

逐项告诉我要在 `/workspace/CWK/.env` 里填什么，然后等我自己填完：

- `CWORK_APP_KEY`：我的工作协同 Key（必填，我自己填，不要问我要）。
- `CWK_OWNER_EMP_ID`、`CWK_OWNER_NAME`：建议填，供日报关系标签使用。
- `CWK_DOCDB_PROJECT_ID`、`CWK_DOCDB_ROOT_FILE_ID`：个人只读试跑保持为空。
- 仅当阶段 5 的路径检查失败时，再补 `CMS_CWORK_WORKFLOW_DIR` / `CMS_AUTH_SKILL_DIR`。

你可以说明每一项的含义和默认值，但不要替我写入凭据，也不要读取或校验我填的具体值。

**暂停，等我说“填好了”。**

## 阶段 5：本地自检

```bash
cd /workspace/CWK
python3.11 scripts/cwk_doctor.py --require-live
```

这条命令只检查本机条件，不读 CWork、不写 DocDB。按下面口径解读并汇报：

- `cms_cwork_workflow`、`cms_auth_skills` 必须 `ok`。`doctor` 会自动在多个 Skill 根里
  查找（含 `/workspace/skills`、`/workspace/.agents/skills` 和当前 OpenClaw 的只读物化根
  `/workspace/.openclaw/sandbox-skills/skills`）。仍失败时按上面
  的规则只补路径类变量，或用 `CWK_SKILL_ROOTS` 指定额外的根，或请我先安装公司
  Skill——不要碰凭据。
- `live_auth_configured`：`doctor` 会用最小 dotenv 解析安全读取项目 `.env`，所以 Key
  只写在 `.env` 里也会显示 `configured`。它只会输出 `configured` 或 `missing`，
  不会打印值、前缀或任何片段。
- `env_file` 只说明 `.env` 是否存在；`openclaw_integration` 说明检测到哪种接入方式，
  它只报告，不会替我切换模式。

**绝不**执行 `source .env`、`. ./.env`、`cat .env`、`env` 或任何会转储凭据的命令——
`doctor` 已经安全地读过它了。汇报时只写检查名和通过/失败，不要粘贴任何变量值。

完成本阶段后输出一行：

```
READY_FOR_PERSONAL_READONLY_TRIAL
```

然后**停止**，等我明确授权。

## 阶段 6：个人只读试跑（只有我明确说“可以试跑”才执行）

```bash
cd /workspace/CWK
python3.11 scripts/cwk_nightly_pipeline.py \
  --config cwk-mirror.local.json \
  --run-name pilot-$(date +%Y%m%d-%H%M) \
  --date $(date +%F)
```

它会用我的 Key 读取我获授权的 CWork 内容，只在本机生成 `runs/`、`raw/`、
`knowledge/`，不发布 DocDB。验收标准：`runs/pilot-*/ACCEPTANCE-RESULT.md` 里
`overall_pass=true`，且同目录下有 `digest-human-v4.md` 与 `digest-human-v4.html`。

不要自行加 `--sync-docdb`，不要启用 cron 或 AI，不要把试跑产出的任何真实内容贴进
聊天记录——只汇报运行名、处理条数、通过与否和产物路径。

---
