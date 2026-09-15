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
CI 在 `make ci` 之前还单独跑一遍 `env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C make rt054-pure-local`
（RT-054 纯本地有界读验收，靠 `env -i` 剥掉调用者的 shell/PATH/解释器，只认系统 `/usr/bin/python3`）。

macOS 本地跑法（原因写在 `Makefile` 顶部注释）：

```bash
cd /private/tmp/<checkout> && env -u CWORK_APP_KEY make test TEST_TMPDIR=/private/tmp
```

`/tmp → /private/tmp` 符号链接会让 VGA 实例根链 fail-closed；默认 TMPDIR 路径过长会撞
rt032 socket 夹具的 AF_UNIX 104 字符上限。

单测单文件 / 单用例——122 个测试文件全部用 `Path(__file__)` 自举，把仓库根 / `scripts/` /
`adapters/` 插进 `sys.path`，所以两种跑法都行；`make test` 用的是第一种：

```bash
cd tests && python3 -m unittest test_rt041_gwork_adapter
cd tests && python3 -m unittest test_rt055_auth.AuthDecisionTests -v
python3 -m unittest tests.test_rt056_kb_admin        # 仓库根，包路径形式
```

脱敏 smoke 都走 `tests/smoke/raw` 夹具、`--no-publish-mirror`，产物落 `runs/ci-smoke*`：
`make smoke`（规则层）、`make smoke-ai`（AI 编排 dry-run，断言 `degraded=false`）、
`make smoke-ai-degraded`（模型全失败时仍出日报，断言 `degraded=true`）。

本机起一套知识库检索栈（合成夹具，不碰真实语料，步骤与端口见 `deploy/README.md`）：

```bash
cp adapters/opensearch_retrieval/config/example.env .env.retrieval   # 不提交
docker compose -f deploy/docker-compose.yml up -d --build
python3 -m adapters.opensearch_retrieval build-index --input tests/fixtures/rt055_retrieval_synthetic.json
python3 -m adapters.opensearch_retrieval smoke-test --expect-doc-id synthetic-rt055-001
```

真实 CWork 采集、真实语料摄取、DocDB 同步和真实模型调用涉及受保护数据与费用：没有明确授权
时一律用 `--source-dir tests/smoke/raw`、`--no-publish-mirror`、合成夹具、dry-run。

## 架构：两条主链

### 一、CWork 镜像主链（原有）

**CWork 只读采集 → staging → promote 进 `raw/YYYY-MM/YYYY-MM-DD/`（唯一事实源）
→ 规则层派生（提取、事件、实体、日报 md/html、办理中心）→ Wiki 精编（受约束摘要 + 主题/实体页）
→ 可信问答**。查询回读 raw 逐条核验引文，只交出 `evidence_status=verified` 的证据包，不产出散文答案。
每次运行的产物与 `nightly-pipeline-manifest.json` 落 `runs/<run-name>/`；`knowledge/工作协同镜像/`
是本地镜像；DocDB 只收派生物（Wiki、索引、回执、日报），raw 永不上云。生产画像是 Local-First，
`cloud`/`shadow` 查询与 Cloud-First 持久化是暂停实验路径，需要额外显式解锁（见 `docs/RUNTIME_STATUS.md`）。

两半架构（RT-039 定档，`docs/DESIGN.md`）：维护侧（采集/晋升/编译/精编/同步，只写镜像）与
应用侧（`cwk_wiki_query.py` + `skill-query/` 薄壳，只读镜像）的接口是数据不是代码，
边界就是 `工作协同镜像/`。任何一侧重写，只要数据契约不变，另一侧无感。

- `scripts/cwk_nightly_pipeline.py` 是编排器而非算法：每个阶段 `subprocess` 调用同目录的一个脚本，
  把 `{step, returncode, degraded, skipped}` 累进 manifest。要弄清「夜间到底做了什么、哪一步可降级」，
  读它 `steps.append(...)` 的先后顺序，比读任何单个脚本都快。
- `scripts/` 是确定性执行层（132 个文件，一脚本一职责，无包结构，同目录扁平 import）：
  采集 `cwk_collect_live` / `cwk_backfill_range` / `cwk_raw_store`；派生 `cwk_human_digest` /
  `cwk_daily_html` / `cwk_action_center` / `cwk_entity_catalog`；精编 `cwk_cloud_wiki_compile` /
  `cwk_cloud_wiki_topics_entities`；检索 `cwk_wiki_query` / `cwk_wiki_search_index`；
  同步 `cwk_sync_mirror_to_docdb` / `cwk_docdb_cloud`；多租户 `cwk_pr001_*` / `cwk_tenant_*` /
  `cwk_instance` / `cwk_agent_binding` / `cwk_credential_broker` / `cwk_access_ledger`；
  激活 `activation_*`；知识库 `kb_*` / `rt05*`（见下）。
- AI 层是受限辅助：`cwk-ai-reviewer` 为零工具 JSON transformer（`deny=["*"]`、`skills=[]`、
  `sandbox.mode=off`），模型白名单只有 `newapi/BD-MiniMax` 与 `newapi/BD-glm`，其它 ID 启动即拒；
  每个 AI 阶段都必须能降级，AI 产物与规则日报 side-by-side，不替代它。策略见 `docs/AI-PILOT.md`。
- PR-001 多租户是已冻结契约，未部署、未启用；碰 `cwk_pr001_*` / `cwk_tenant_*` / VGA 相关文件
  等于动安全门，先读 `PR/PR-001-multitenant-knowledge-spaces/STATUS.md`。

### 二、知识库平台（RT-042 → RT-056，当前主要在建面）

多库（bank）知识库：建库 → 摄取 → 索引 → 局域网网关检索/问答/读原文，按 Agent 实例发 token。
当前三个 bank：`cwork-3m`（工作协同近 3 月）、`docdb-touqian`（投前资料）、`spbp-2027`（2027 SP&BP）。

- **建库与存储（`scripts/kb_*`）**：`kb_create`（库 id 是 128 bit 随机 `kb_code`，显示名不进路径）、
  `kb_ingest`（`plan/run/status/reconcile`，每个子命令 stdout 只吐一个 JSON 对象）、
  `kb_storage`（`StorageBackend` 协议 + LocalFS/Memory/NAS 三后端，写入原子且路径重锚，符号链接逃不出根）、
  `kb_ledger`（哈希账本写后读回比对 + 采集游标 + 向导状态机，「存量不变」是硬失败）、
  `kb_doctor`（`verify`）、`kb_migrate`、`kb_wizard`（唯一允许写的工厂面）、`kb_ops`（只读状态投影）。
- **授权（`scripts/kb_token.py` / `kb_access_file.py`，`adapters/kb_auth.py`）**：per-Agent token，
  按 bank 发 scope；registry 是 `cwk.kb.token-registry.v1`，只存 `token_sha256`。
  运行期由 `RAG_AUTH_ENABLED` / `RAG_AUTH_REGISTRY` 控制，registry 故障 fail-closed。
  **401 = token 缺失/过期/吊销；403 = token 有效但 bank 不在 scope（跨库隔离正常生效，不是 bug）。**
- **检索/问答服务（`adapters/`，RT-055 新栈，2026-09-14 灰度上线）**：
  `opensearch_retrieval` 提供 `POST /query`（exact + lexical 双通道，编号/日期走 exact 不怕中文分词拆号）、
  `GET /healthz|/readyz`，默认 8787；`rag_answer` 提供 `POST /answer`（检索 → doc_id → 只读原文 → 本地
  OpenAI-compatible 模型，约 25–45s，客户端给 ≥120s）与 `POST /read`（字符级 offset/length 分页，
  越界 416），默认 8790。两者都只用标准库，服务本身不落原文和答案正文。
- **部署（`deploy/`）**：compose 起 OpenSearch 2.15 单节点（构建期装 `analysis-icu`）+ retrieval + rag-answer；
  端口默认只发布到 `127.0.0.1`，`KB_BIND_ADDR` 才对外。发布路径是 shadow → 灰度 → 切换三段，
  指标门槛写在 `deploy/README.md`，代码交付本身不执行切换。
- **Agent 入口（`skills/`）**：`cwk-kb-query`（检索/问答/读原文）、`cwk-kb-create`（建库向导）、
  `cwk-kb-authorize`（签发/轮换/撤销 token，只给有 OPS registry 写权限的管理 Agent）。
  使用者侧的接入 runbook 是 `docs/cwk-kb-access-setup.md`。
- **管理台（`scripts/kb_admin.py`，RT-056）**：默认关闭，默认 `127.0.0.1:8791`，只做脱敏概览、
  服务探测、审计读取；写动词即使开了开关也只记审计并返回 501 占位。见 `docs/KB-ADMIN.md`。
- **已停运但保留的回退路径**：旧 `kb_gateway.py`（v2 读链 8787-8789，`search→resolve→read/continue`）
  2026-09-14 停运，代码与测试仍在仓库里作回滚材料；新工作一律走新栈，别往旧网关加功能。
- **快照语义**：新栈语料是**快照**，没有 lineage / raw_sha / `full_sha_verified`，
  不得声称做了 SHA 现场复核；深度原文核验仍属旧 v2 链的能力。

### `adapters/` 里住着两种东西

RT-041 的源适配器（`base.py` / `gwork.py`）和 RT-055 的检索/问答服务包，共用一个目录但互不相干：

- 源适配器是接新数据源的地方，`scripts/` 不动：四个操作 `discover / fetch / dedupe_key / watch`，
  出口统一为 `NormalizedDoc`（即现有 raw frontmatter 契约），全局键 `<源前缀>-<原ID>`。
  1 号 `gwork.py` 是对现有 CWork 通道的包装，行为零变化、字节等价由测试锁定。契约见 `docs/ADAPTER-CONTRACT.md`。
- 导入风格要分清：`scripts/` 是扁平同目录 import（`import kb_ledger`），`adapters/` 下的服务是包导入
  （`python3 -m adapters.opensearch_retrieval`、`from adapters.kb_auth import authorize`）。

## 加文件、改文件前必看的两道门

- **`make governance-audit`（代码层）**：判据面是 `git ls-files` 全集（一千一百余个文件，会随提交增长）——每个受跟踪
  文件必须被 `.aodw-next/06-project/governance/code-ownership-manifest.json` 里**恰好一条**规则认领。
  孤儿文件是硬失败，匹配 0 个文件的失效规则同样硬失败。`exact_only_zones`（仓库根、`scripts/`、
  `config/`、`references/`、`skill/`、`.github/`、`.aodw-next/06-project/`）里禁止前缀规则：
  往这些目录新放文件，必须同时补一条 exact 规则，否则门当场红。因此 `scripts/` 是封闭命名空间，
  新脚本不在声明集合内即为孤儿；已有脚本的演化走回执链（v1 槽位用尽的才叠加
  `script-evolution-v2.json`，尚有余量却开 v2 槽位会被判失败）。
  `adapters/`、`deploy/`、`tests/`、`docs/`、`RT/`、`PR/` 是前缀规则，属被鼓励的扩展面，
  但每个新适配器 / 新服务要带对应测试。
  **`skills/` 是个陷阱**：它不在 `exact_only_zones` 里，却也没有任何前缀规则——四个文件全靠
  RT-045 / RT-053 的 exact 路径认领，所以新增 skill 文件同样要补 exact 规则。
  `runs/` 虽被 gitignore，但 `runs/rt055-auth/status.md` 是受跟踪的显式例外。
- **`make aodw-check`（方法层）**：RT 门禁只覆盖 RT-028 及之后（作用域在 `.aodw-next/project.yaml`
  的 `rt_gate_scope`）；存量 RT 在 `RT/index.yaml` 带 `backfill: aodw-adoption`，只作证据、不倒改。
  新 RT 编号取 `RT/` 目录与 `RT/index.yaml` 条目并集的最大序号 +1（只扫目录会撞号）。
  花名册一致性是双向的：**建了 `RT/RT-0xx/` 目录就必须同时补 `index.yaml` 条目**，
  只建目录不登记会直接把门跑红。注意这跟宪章的「占号」步骤是硬碰硬：宪章要求先在
  `main` 上只提交 `meta.yaml` 占号，而门要求目录一出现就有条目——所以**占号和登记
  必须同一步做完**（`status: created`，收口时再改 `done`），否则从占号那一刻起门就是红的。
  RT-056 和 RT-057 都实测踩过这一条。

## 红线

只读 CWork：不标已读、不回复、不办待办、不删除，任何 CWork 写操作都要单独授权。
raw 不被摘要、事件、实体或模型输出回写；内容变化一律新写 `-v2` 副本并重编译，原件不可变。
AI 与 Agent 不写正式 summary/manifest，只有宿主验证后落盘。
事实性回答必须绑定 citation 或 `/read` 原文页；`no_answer` 先换 2–3 个同义/变体再下结论，
一次零命中不等于「全库没有」。
知识库 token 不写日志、不放命令行、不进仓库，只以环境变量名（不是值）传递；401/403 是隔离
生效，不要靠放宽 scope 绕过。服务默认只绑 `127.0.0.1`，改绑定地址或端口映射等于对外暴露，需授权。
凭据只来自环境变量或私有配置，不进代码、日志、RT、测试、提交或外部消息；
`.env`、`cwk-mirror.local.json`、`runs/`、`knowledge/`、`raw/`、`collected-raw/`、`state/` 已 gitignore，
不要提交，也不要复制他人的这些目录。
