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
7. 前 4 个阶段不读取任何真实 CWork 内容、不写 DocDB、不启用 AI、不创建 cron 或任何
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
ls -d /workspace/CWK /workspace/skills/cwk-mirror-workflow 2>/dev/null
```

要求 Python 3.10+（建议 3.11）。最后一条应当没有输出；若有输出说明已经装过，
**停下问我**，不要动它。任何一项不满足都停下汇报，不要自行安装或修复。

**暂停，等我说继续。**

## 阶段 2：安装

```bash
git clone https://github.com/evan-zhang/CWK.git /workspace/CWK
cd /workspace/CWK
PYTHON=python3.11 ./install.sh --install-skill --skills-dir /workspace/skills
```

`--skills-dir /workspace/skills` 不能省：默认目标在 `$HOME` 下，通常不跨会话保留。

这个脚本只做本地事情：本地检查、缺失时从模板创建 `.env` 与
`cwk-mirror.local.json`、编译脚本、跑脱敏 smoke、建立 Skill 软链接。它不读 CWork、
不写 DocDB、不建 cron、不改 Agent 配置。若它报“拒绝覆盖”，照它说的停下并汇报，
不要绕开。

**暂停，等我说继续。**

## 阶段 3：验证安装

```bash
ls -l /workspace/skills/cwk-mirror-workflow
test -f /workspace/skills/cwk-mirror-workflow/SKILL.md && echo skill ok
ls runs/ci-smoke/digest-human-v4.md runs/ci-smoke/digest-human-v4.html
```

链接应指向 `/workspace/CWK/skill`。smoke 用的是极小的脱敏样例，只验证命令与渲染
连通；它报 `overall_pass=false` 属正常，不要因此重装或改配置。

若这台机器的运行时不是从 `/workspace/skills` 发现 Skill，**只汇报现象并问我**，
不要改全局配置。

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

- `cms_cwork_workflow`、`cms_auth_skills` 必须 `ok`。失败时按上面的规则只补路径类
  变量，或请我先安装公司 Skill——不要碰凭据。
- `live_auth_configured` 只读当前 shell 的环境变量，不会去读 `.env`。Key 只写在
  `.env` 里时它会显示 `missing`，**这是预期的**：试跑用的
  `cwk_nightly_pipeline.py` 会自己加载项目 `.env`。不要为了让它变绿而去加载 `.env`，
  也不要把 Key 放进命令行；如果我想让它变绿，我会自己在终端里导出后重跑。

汇报时只写检查名和通过/失败，不要粘贴任何变量值。

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
