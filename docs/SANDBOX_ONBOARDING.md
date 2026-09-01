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

## 1. 安装前的低风险检查

只做只读检查，任何一项不满足就先停下问使用者，不要自行修复：

```bash
python3.11 --version
git --version
make --version
test -w /workspace && echo "/workspace writable"
ls -d /workspace/CWK /workspace/skills/cwk-mirror-workflow 2>/dev/null
```

最后一条**应当没有输出**。若已存在，说明这台 sandbox 已经装过 CWK 或已有同名
Skill：停下并交给使用者决定，不要覆盖、删除或重装。

## 2. 安装

```bash
git clone https://github.com/evan-zhang/CWK.git /workspace/CWK
cd /workspace/CWK
PYTHON=python3.11 ./install.sh --install-skill --skills-dir /workspace/skills
```

`--skills-dir /workspace/skills` 是这个场景的关键：默认目标是
`$HOME/.openclaw/skills`，在很多 sandbox 里不跨会话保留。

`install.sh` 实际只做这些事：跑一次不含凭据的本地检查、缺失时从模板创建 `.env` 与
`cwk-mirror.local.json`（已存在则原样保留）、编译脚本、跑脱敏 smoke（产物在
`runs/ci-smoke`），最后把 `skill/` 软链到指定 skills 目录。它不读取 CWork、不写
DocDB、不创建 cron、不修改 Agent 配置。

已存在同名非链接目录、或链接指向别处时，脚本会拒绝覆盖并报错——这是保护，不要绕开。

## 3. 验证安装

```bash
ls -l /workspace/skills/cwk-mirror-workflow
test -f /workspace/skills/cwk-mirror-workflow/SKILL.md && echo skill ok
ls runs/ci-smoke/digest-human-v4.md runs/ci-smoke/digest-human-v4.html
```

链接应指向 `/workspace/CWK/skill`。smoke 只验证命令与渲染是否连通；它使用极小的
脱敏 fixture，`overall_pass` 为 `false` 也属正常。

若这台 sandbox 的运行时不是从 `/workspace/skills` 发现 Skill，交给使用者决定怎么接，
不要擅自改动全局配置或安全策略。

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

`doctor` 只检查本机条件，不读取 CWork、不写 DocDB。看三件事：

- `cms_cwork_workflow`、`cms_auth_skills`：路径类检查，必须为 `ok`，否则补上第 4 步的
  路径变量或请使用者先装公司 Skill。
- `live_auth_configured`：只读取**当前 shell 的环境变量**，不会去读 `.env`。因此
  Key 只写进 `.env` 时这一条会显示 `missing`，这是预期的：第 6 步的
  `cwk_nightly_pipeline.py` 会自动加载项目 `.env`，且不覆盖已有环境变量。使用者若要
  让这条也变 `ok`，请自行在终端里导出 Key 后重跑同一条命令。

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

- 跨会话保留：`/workspace/CWK`（含私有 `.env`、`cwk-mirror.local.json`）与
  `/workspace/skills`。
- 私有数据：`.env`、`cwk-mirror.local.json`、`runs/`、`raw/`、`knowledge/`、`state/`
  都不提交，也绝不复制他人的同名文件或历史镜像。
- 每台 sandbox、每个使用者用自己的 Key 与数据目录。

## 边界

- CWK 对 CWork 只读：不会回复、审批、完成、删除或标记事项。
- 原始 CWork 内容的安全处理由进入 CWK 前的上游授权链路负责；CWK 不做二次内容判定、
  扫描、脱敏或过滤。
- 历史 RT/PR、设计与验收材料仅供追溯，不是安装操作指令。
