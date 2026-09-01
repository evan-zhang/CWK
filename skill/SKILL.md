---
name: cwk-mirror-workflow
description: Use when asked to build, migrate, operate, or troubleshoot a read-only CWork/工作协同 knowledge mirror with daily Markdown and HTML digests, event/entity linking, DocDB sync, or nightly cron.
---

# CWK Mirror Workflow

Use this skill to create or operate a portable `工作协同镜像` for a person or team.

首次内部安装请先按仓库的 `docs/INTERNAL_DISTRIBUTION.md` 完成前置条件、个人配置和
本地自检。只使用当前用户自己的 `.env`、CWork Key、本机 Agent 与数据目录；不得复制
其他人的 `.env`、`knowledge/`、`raw/`、`runs/`、`state/` 或历史镜像。历史 RT/PR
材料只用于追溯，不是操作指令。

## 定位项目根目录

这份 Skill 可能是被复制到 Skill 根下的副本，其中**不含**脚本包。所有命令都要在 CWK
项目根目录里执行。按顺序确定它：

1. 环境变量 `CWK_PROJECT_DIR`；
2. 云端 sandbox 的约定位置 `/workspace/CWK`；
3. 使用者告知的克隆位置。

判据是该目录下存在 `scripts/cwk_doctor.py`。找不到就停下问使用者，不要猜测或新建。

```bash
cd "${CWK_PROJECT_DIR:-/workspace/CWK}"
python3 scripts/cwk_doctor.py --require-live
```

`doctor` 会安全读取项目 `.env`（最小 dotenv 解析，不执行 shell），只输出
`configured` / `missing`，绝不打印凭据值或片段。不要执行 `source .env`、`cat .env`
或任何转储凭据的命令。

## OpenClaw 接入模式

核心程序安装与 OpenClaw 接入是分开的，接入必须由使用者显式选择一种，安装器不会静默
选择：`workspace-skill`（可写 Workspace 里的正式 Skill）、`host-skill`（受保护 Skill
根，交由运维在宿主控制面为指定 Agent 注册）、`router`（在 Workspace 的 `AGENTS.md`
里维护带标记的路由块）、`none`（只装程序）。一个 Agent 只启用一种。云端 sandbox 的
Skill 根通常是只读保护挂载——不要尝试写入、改权限或改挂载。`router` 写入后只对
后续新会话生效，当前会话不会自动重载 `AGENTS.md`。

## Safety Boundary

Allowed:
- Read CWork records in read-only modes.
- Write local run artifacts and knowledge mirror files.
- Optionally publish derived Wiki and daily Markdown/HTML files into the configured knowledge-base folder.
- Schedule cron after a live read-only run succeeds.

Forbidden unless the user explicitly asks for that separate action:
- Mark CWork items as read.
- Reply, approve, reject, delete, or complete CWork tasks.
- Send CWork messages.
- Mix one user's private raw evidence into another user's mirror.

## Required Inputs

Before running live collection, identify:
- target user/team and whether this is a personal mirror or team mirror
- CWork auth source: `CWORK_APP_KEY` preferred
- optional derived-page publishing target: default personal knowledge base, unless a team/shared `docdb_project_id` and `docdb_root_file_id` is explicitly required
- local script package path

For migration, read `references/migration.md`. For operation and failures, read `references/operations.md`.

## Standard Workflow

1. Locate the script package and inspect the local config.
2. Create a private config from `templates/CONFIG.example.json`.
3. Run a no-publish smoke test.
4. Run one live read-only Local-First pass; add `--sync-docdb` only when derived Wiki/HTML publishing is desired.
5. Inspect `run.json`, `ACCEPTANCE-RESULT.md`, and `incremental-link-preview-v1.md`.
6. Enable or update cron only after the live pass succeeds.
7. Report concise results: run name, processed count, pass/fail, MD/HTML paths, link statistics, and sync status.

## Commands

Smoke test:

```bash
python3 scripts/cwk_nightly_pipeline.py \
  --config cwk-mirror.local.json \
  --run-name nightly-smoke-$(date +%Y%m%d-%H%M) \
  --date $(date +%F) \
  --source-dir tests/smoke/raw \
  --no-publish-mirror
```

Live read-only run:

```bash
python3 scripts/cwk_nightly_pipeline.py \
  --config cwk-mirror.local.json \
  --run-name nightly-$(date +%Y%m%d-%H%M) \
  --date $(date +%F) \
  --no-cloud-first \
  --no-publish-cloud-query-catalog
```

To publish only derived Wiki/HTML copies, add `--sync-docdb --sync-wiki`. Raw
evidence remains local and authoritative.

## Quality Review

Warning signs:
- `overall_pass=false` on live runs
- missing daily HTML
- too many suspected links
- any CWork mutating command appears in manifests
- raw evidence from different users appears in a shared/team mirror without approval

Markdown is the durable source; HTML is the human reading surface.
