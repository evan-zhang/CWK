# CWK 内部小团队上手

适用于公司内部已获授权的员工：在自己的 OpenClaw Agent 上，用自己的工作协同 Key
建立个人镜像，或向已批准的团队 DocDB 目标发布派生产物。CWK 不是共享 Evan 现有镜像、
配置或授权的安装包。

## 1. 前置条件

- 私有仓库访问权；Python 3.10+（建议 3.11）。项目当前只使用 Python 标准库，无需
  `pip install`、lockfile 或包发布体系。
- 本机 OpenClaw 可发现公司 `cms-cwork-workflow` 与 `cms-auth-skills`。选择 DocDB
  发布时，另需 `cms-docdb` 和该个人/团队目标的写入权限。
- 安装者本人持有 CWork 读取授权和自己的 Key。团队目标还需事先批准目标范围、项目和根目录。

## 2. 安装

```bash
git clone https://github.com/evan-zhang/CWK.git
cd CWK
PYTHON=python3.11 ./install.sh --install-skill
```

安装会创建 gitignored 的 `.env` 和 `cwk-mirror.local.json`、运行脱敏 smoke，并把
`skill/` 链接到当前用户的 OpenClaw skills 目录。不会读取 CWork、写入 DocDB、创建
cron 或修改 Agent 配置。若当前 Agent 的 skills 目录不同，追加
`--skills-dir /path/to/openclaw/skills`。

## 3. 只配置自己的本机文件

编辑新建的 `.env`，只填自己的值；不要把 Key 粘贴到命令行、提交或聊天记录。

```dotenv
CWORK_APP_KEY=自己的工作协同Key
# 建议填写，供日报关系标签使用：
CWK_OWNER_EMP_ID=自己的员工ID
CWK_OWNER_NAME=自己的姓名

# 个人本地试跑保持为空；只有已批准的团队 DocDB 目标才填写：
CWK_DOCDB_PROJECT_ID=
CWK_DOCDB_ROOT_FILE_ID=
```

默认路径找不到公司 Skills 时，才在同一 `.env` 里设置对应的
`CMS_CWORK_WORKFLOW_DIR`、`CMS_AUTH_SKILL_DIR` 或 `CMS_DOCDB_SKILL_DIR`。
每个人都使用自己的 `.env`、`cwk-mirror.local.json`、本机 Agent 和数据目录；绝不复制
Evan 或其他同事的 `.env`、`knowledge/`、`raw/`、`runs/`、`state/` 或历史镜像。

## 4. 本地自检

`doctor` 只读取当前 shell 的环境变量，因此先导入刚才的私有 `.env`，再检查 CWork 与
认证前置条件：

```bash
set -a; . ./.env; set +a
python3.11 scripts/cwk_doctor.py --require-live
```

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
才在同一命令末尾加 `--sync-docdb`。不要在这次上手流程中启用 cron、AI 试点或任何生产默认。

## 边界

- CWK 对 CWork 只读：不会回复、审批、完成、删除或标记事项。
- 原始 CWork 内容的安全处理由进入 CWK 前的上游授权链路负责；CWK 不做二次内容判定、扫描、
  脱敏或过滤。
- 历史 RT/PR、设计和验收材料仅供追溯，不是安装操作指令。
