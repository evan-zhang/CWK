---
name: cwk-mirror-workflow
description: Use when asked to build, activate, migrate, operate, or troubleshoot a read-only CWork/工作协同 knowledge mirror with daily Markdown and HTML digests, event/entity linking, DocDB sync, or scheduled nightly runs.
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

## 激活：装好 ≠ 授权

装好程序和「授权它每晚读这个人的工作」是两件事。安装器不做发现、不读 CWork、
不建私有激活状态、也不建任何定时任务；它只在末尾打印 `CWK_ACTIVATION=<状态>`。
`scripts/cwk_doctor.py` 会把同一个状态作为 `activation` 检查项报出来（只报枚举，
不报路径、哈希或业务内容）。

激活由你主动发起对话来推进，但**判定不归你管**：状态机、两道人工确认、每日执行
合同与其漂移、试跑门禁、调度交接，全部由 `scripts/activation_wizard.py`
决定。你读它的 JSON 并解释，不替它下结论。

```bash
cd "${CWK_PROJECT_DIR:-/workspace/CWK}"
python3 scripts/activation_wizard.py status
```

按返回的 `next_step` 决定这一轮谈什么，不要问用户「我们走到哪一步了」。顺序是：
说清边界 → 取得只读发现授权（第一道确认）→ 只用既有回执做发现 → 提出有证据支撑
的业务画像草案 → 用户认领画像 → 逐条讲清每日执行合同 → 跑并核验一次只读试跑 →
**单独再问一次**是否允许排期（第二道确认）→ 出交接单交给宿主建任务 → 回填宿主给
的外部任务标识。

四条红线，任何状态下都成立：`CWORK_APP_KEY` 之外的凭据不进对话（这把 Key 本身允许在定制客户端通路里发送，且只经 `scripts/setup_app_key.py` 落盘）；不把「现在能调用工具」当成授权；
不展示 raw 原文；不创建、不修改、不删除任何定时任务，也不假设存在 OpenClaw 调度
API。完整的分状态话术、命令与失败处理见 `references/activation.md`。

## Safety Boundary

Allowed:
- Read CWork records in read-only modes.
- Write local run artifacts and knowledge mirror files.
- Optionally publish derived Wiki and daily Markdown/HTML files into the configured knowledge-base folder.
- Emit a scheduler handoff after a live read-only pilot passes and the user has given the second confirmation.

Forbidden unless the user explicitly asks for that separate action:
- Mark CWork items as read.
- Reply, approve, reject, delete, or complete CWork tasks.
- Send CWork messages.
- Mix one user's private raw evidence into another user's mirror.

Forbidden outright, with or without a request:
- Collect a credential inside the conversation.
- Create, modify, or delete a scheduled task, or claim this repository did.

## Required Inputs

Before running live collection, identify:
- target user/team and whether this is a personal mirror or team mirror
- CWork auth source: `CWORK_APP_KEY` preferred
- optional derived-page publishing target: default personal knowledge base, unless a team/shared `docdb_project_id` and `docdb_root_file_id` is explicitly required
- local script package path

For activation, read `references/activation.md`. For migration, read
`references/migration.md`. For operation and failures, read
`references/operations.md`.

## Standard Workflow

1. Locate the script package and inspect the local config.
2. Create a private config from `templates/CONFIG.example.json`.
3. Run a no-publish smoke test.
4. Run one live read-only Local-First pass; add `--sync-docdb` only when derived Wiki/HTML publishing is desired.
5. Inspect `run.json`, `ACCEPTANCE-RESULT.md`, and `incremental-link-preview-v1.md`.
6. Drive the activation dialogue from `references/activation.md`. Scheduling
   needs a passing pilot **and** a second explicit confirmation, and the task
   itself is created by the host, never here.
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
- `doctor` reports `activation: unreadable`, or the wizard reports
  `healthy: false` — the private activation record cannot be validated, so any
  scheduled run must be treated as untrusted until it is resolved. Do not
  delete the state and re-`init` over it.

Markdown is the durable source; HTML is the human reading surface.
