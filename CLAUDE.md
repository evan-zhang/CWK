# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 流程入口（先读，不可跳过）

本仓库采用 AODW v0.6.1。开始任务前依次阅读：

1. `.aodw-next/01-core/aodw-constitution.md`
2. `.aodw-next/01-core/ai-interaction-rules.md`
3. `AGENTS.md`
4. `.aodw-next/06-project/ai-overview.md`

你是主工程师：先调查与规划，再用简体中文给出结论和建议；授权范围内连续实现并验证。
RT、实现和 Git 细则按需从 `.aodw-next/` 加载。读不到宪章时停止并说明，不用旧流程替代。

流程与授权规则以上述文件为准，本文件不复述；以下只写「跑什么」和「代码长什么样」。

## 常用命令

Python 3.10+（基线 3.11），只依赖标准库，没有 lockfile、没有第三方包、没有 lint 配置——
「语法检查」就是 `make test` 里那句 `python3 -m py_compile scripts/*.py`。

```bash
make ci        # CI 跑的就是这条 = make test + make aodw-check + make governance-audit
make ci-full   # 全量车道（加跑 PR-001 安全族单测，本地 70+ 分钟），发布或产品代码改动前过一次
make doctor    # 可移植安装自检，含 Python 版本闸
make wiki-lint # 本地 Wiki 证据完整性（数据检查，不是代码 lint）
```

`make test` 是快车道：排除 `test_pr001_*.py`，保留其余全部单测 + 三条脱敏 smoke，本地约 3 分钟。
「现在是不是绿的」以 `make ci` 为准；CI 的绿灯不能当本地证据，反之亦然。

macOS 本地跑法（原因写在 `Makefile` 顶部注释）：

```bash
cd /private/tmp/<checkout> && env -u CWORK_APP_KEY make test TEST_TMPDIR=/private/tmp
```

`/tmp → /private/tmp` 符号链接会让 VGA 实例根链 fail-closed；默认 TMPDIR 路径过长会撞
rt032 socket 夹具的 AF_UNIX 104 字符上限。

单测单文件 / 单用例——测试自己把 `scripts/`、`adapters/` 插进 `sys.path`，**必须在 `tests/` 里跑**：

```bash
cd tests && python3 -m unittest test_rt041_gwork_adapter
cd tests && python3 -m unittest test_rt041_gwork_adapter.RegistryTests.test_gwork_is_registered_by_source_type -v
```

脱敏 smoke 都走 `tests/smoke/raw` 夹具、`--no-publish-mirror`，产物落 `runs/ci-smoke*`：
`make smoke`（规则层）、`make smoke-ai`（AI 编排 dry-run，断言 `degraded=false`）、
`make smoke-ai-degraded`（模型全失败时仍出日报，断言 `degraded=true`）。

真实 CWork 采集、DocDB 同步和真实模型调用涉及受保护数据与费用：没有明确授权时一律用
`--source-dir tests/smoke/raw`、`--no-publish-mirror`、dry-run。

## 架构

主链一条：**CWork 只读采集 → staging → promote 进 `raw/YYYY-MM/YYYY-MM-DD/`（唯一事实源）
→ 规则层派生（提取、事件、实体、日报 md/html、办理中心）→ Wiki 精编（受约束摘要 + 主题/实体页）
→ 可信问答**。查询回读 raw 逐条核验引文，只交出 `evidence_status=verified` 的证据包，不产出散文答案。
每次运行的产物与 `nightly-pipeline-manifest.json` 落 `runs/<run-name>/`；`knowledge/工作协同镜像/`
是本地镜像；DocDB 只收派生物（Wiki、索引、回执、日报），raw 永不上云。生产画像是 Local-First，
`cloud`/`shadow` 查询与 Cloud-First 持久化是暂停实验路径，需要额外显式解锁。

- `scripts/cwk_nightly_pipeline.py` 是编排器而非算法：每个阶段 `subprocess` 调用同目录的一个脚本，
  把 `{step, returncode, degraded, skipped}` 累进 manifest。要弄清「夜间到底做了什么、哪一步可降级」，
  读它 `steps.append(...)` 的先后顺序，比读任何单个脚本都快。
- `scripts/` 是确定性执行层（约 43k 行，一脚本一职责，无包结构，同目录互相 import）：
  采集 `cwk_collect_live` / `cwk_backfill_range` / `cwk_raw_store`；派生 `cwk_human_digest` /
  `cwk_daily_html` / `cwk_action_center` / `cwk_entity_catalog`；精编 `cwk_cloud_wiki_compile` /
  `cwk_cloud_wiki_topics_entities`；检索 `cwk_wiki_query` / `cwk_wiki_search_index`；
  同步 `cwk_sync_mirror_to_docdb` / `cwk_docdb_cloud`；多租户 `cwk_pr001_*` / `cwk_tenant_*` /
  `cwk_instance` / `cwk_agent_binding` / `cwk_credential_broker` / `cwk_access_ledger`；
  激活 `activation_*`。
- `adapters/`（RT-041）是接新数据源的地方，`scripts/` 不动：四个操作
  `discover / fetch / dedupe_key / watch`，出口统一为 `NormalizedDoc`（即现有 raw frontmatter 契约），
  全局键 `<源前缀>-<原ID>`。1 号 `gwork.py` 是对现有 CWork 通道的包装，行为零变化、字节等价由测试锁定。
  契约见 `docs/ADAPTER-CONTRACT.md`。
- AI 层是受限辅助：`cwk-ai-reviewer` 为零工具 JSON transformer（`deny=["*"]`、`skills=[]`、
  `sandbox.mode=off`），模型白名单只有 `newapi/BD-MiniMax` 与 `newapi/BD-glm`，其它 ID 启动即拒；
  每个 AI 阶段都必须能降级，AI 产物与规则日报 side-by-side，不替代它。策略见 `docs/AI-PILOT.md`。
- PR-001 多租户是已冻结契约，未部署、未启用；碰 `cwk_pr001_*` / `cwk_tenant_*` / VGA 相关文件
  等于动安全门，先读 `PR/PR-001-multitenant-knowledge-spaces/STATUS.md`。

## 加文件、改文件前必看的两道门

- **`make governance-audit`（代码层）**：判据面是 `git ls-files` 全集——每个受跟踪文件必须被
  `.aodw-next/06-project/governance/code-ownership-manifest.json` 里**恰好一条**规则认领。
  孤儿文件是硬失败，匹配 0 个文件的失效规则同样硬失败。`exact_only_zones`（仓库根、`scripts/`、
  `config/`、`references/`、`skill/`、`.github/`、`.aodw-next/06-project/`）里禁止前缀规则：
  往这些目录新放文件，必须同时补一条 exact 规则，否则门当场红。因此 `scripts/` 是封闭命名空间，
  新脚本不在 PR-001 声明集合内即为孤儿；已有脚本的演化走回执链（v1 槽位用尽的才叠加
  `script-evolution-v2.json`，尚有余量却开 v2 槽位会被判失败）。`adapters/`、`tests/` 用前缀规则，
  是被鼓励的扩展面，但每个新适配器要带对应测试。
- **`make aodw-check`（方法层）**：RT 门禁只覆盖 RT-028 及之后（作用域在 `.aodw-next/project.yaml`
  的 `rt_gate_scope`）；存量 RT 在 `RT/index.yaml` 带 `backfill: aodw-adoption`，只作证据、不倒改。
  新 RT 编号取 `RT/` 目录与 `RT/index.yaml` 条目并集的最大序号 +1（只扫目录会撞号）。

## 红线

只读 CWork：不标已读、不回复、不办待办、不删除，任何 CWork 写操作都要单独授权。
raw 不被摘要、事件、实体或模型输出回写；内容变化一律新写 `-v2` 副本并重编译，原件不可变。
AI 与 Agent 不写正式 summary/manifest，只有宿主验证后落盘。
凭据只来自环境变量或私有配置，不进代码、日志、RT、测试、提交或外部消息；
`.env`、`cwk-mirror.local.json`、`runs/`、`knowledge/`、`raw/`、`collected-raw/`、`state/` 已 gitignore，
不要提交，也不要复制他人的这些目录。
