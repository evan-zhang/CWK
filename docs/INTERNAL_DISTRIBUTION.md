# CWK 内部可分发版

CWK 可以部署到公司内部其他 OpenClaw Agent，但每位使用者必须运行自己的实例；它不是共享 Evan 现有镜像、配置或授权的安装包。

## 分发前提

- 已获得私有仓库访问权。
- 使用 Python 3.10+；建议 Python 3.11。
- 本机可访问公司 `cms-cwork-workflow`、`cms-auth-skills`，以及需要云端同步时的 `cms-docdb`。
- 工作协同读取授权与 DocDB 写入权限均归安装者本人或明确指定的团队目标所有。

## 安装

```bash
git clone https://github.com/evan-zhang/CWK.git
cd CWK
PYTHON=python3.11 ./install.sh --install-skill
```

这一步只会：创建 gitignored 的本地配置模板、运行无数据 smoke，并将 `skill/` 链接到本机 OpenClaw skills 目录。它不会读取工作协同、写入 DocDB、创建 cron 或修改 Agent 配置。

如果 OpenClaw Skills 位于其他位置：

```bash
PYTHON=python3.11 ./install.sh --install-skill --skills-dir /path/to/openclaw/skills
```

## 配置与自检

在 `.env` 中只填写安装者自己的授权：

```bash
CWORK_APP_KEY=安装者自己的工作协同授权
# 留空时使用安装者个人 DocDB；仅团队镜像才显式填写：
CWK_DOCDB_PROJECT_ID=
CWK_DOCDB_ROOT_FILE_ID=
```

如果公司 Skills 不在默认目录，可设置：

```bash
CMS_CWORK_WORKFLOW_DIR=/path/to/cms-cwork-workflow
CMS_DOCDB_SKILL_DIR=/path/to/cms-docdb
CMS_AUTH_SKILL_DIR=/path/to/cms-auth-skills
```

在首次实采前执行：

```bash
python3.11 scripts/cwk_doctor.py --require-live --require-docdb
```

再运行一次只读试点：

```bash
python3.11 scripts/cwk_nightly_pipeline.py \
  --config cwk-mirror.local.json \
  --run-name pilot-$(date +%Y%m%d-%H%M) \
  --date $(date +%F) \
  --sync-docdb
```

仅在试点 `overall_pass=true` 后，才由安装者单独配置其 cron。

## 隔离规则

- 不复制任何既有 `knowledge/`、`raw/`、`runs/`、`state/`、`.env` 或 `cwk-mirror.local.json`。
- 默认写入安装者个人 DocDB；团队镜像必须使用明确批准的项目与根目录 ID。
- 多人团队镜像要先确定数据范围和写入负责人；默认不要混合个人原始汇报。
- CWK 对工作协同保持只读，不会回复、审批、完成或删除事项。

## 试点验收

1. `PYTHON=python3.11 make test` 通过。
2. `cwk_doctor.py --require-live --require-docdb` 通过。
3. 首次试点成功，且目标是安装者个人/已批准的团队 DocDB。
4. 确认无 Evan 本机路径、授权或历史镜像被复制。
