# CWK AI System Overview

> 本文件记录每次开发都需要的 CWK 事实。代码、已批准合同和 `README.md` 冲突时，
> 以当前代码与已批准合同为准。真实工作协同原文、密钥和生产运行产物不在本仓库内。

## 1. 项目定位

CWK 是只读的 CWork／工作协同知识镜像与问答工作流。它把经过授权读取的工作汇报、
待办、已办事项和回复链，组织成可追溯的本地事实镜像与派生产物：

- `raw/` 是唯一事实源；
- 规则层产出结构化提取、事件、实体、日报 Markdown/HTML；
- Wiki 摘要、主题与实体页只用于导航和召回；
- 查询必须回读 raw，并仅把已核验的证据交给对话 Agent；
- AI 精编是受限的辅助层，不能替代原文、改变采集事实或直接对 CWork 写入。

本仓库是代码与脱敏样例仓库；实际生产镜像、原始汇报、模型提示和运行产物均在受控的
本地运行环境中，且不得提交或复制到设计文档、测试夹具或外部服务。

## 2. 当前权威入口

- `scripts/cwk_nightly_pipeline.py`：只读采集、规则处理、日报与可选派生同步；
- `scripts/cwk_cloud_wiki_compile.py`：对单篇 raw 生成受约束的 Wiki 摘要，宿主负责
  JSON、SHA、引文与 manifest；
- `scripts/cwk_wiki_query.py`：本地检索并回读原文核验；
- `skill/`：面向 OpenClaw 的安装入口、配置模板和使用说明；
- `docs/AI-PILOT.md`：AI reviewer 的安全边界与降级规则；
- `docs/RUNTIME_STATUS.md`：当前 Local-First 运行状态。

生产问答默认使用本地镜像。云端/影子查询是暂停实验路径，必须有额外显式解锁；
DocDB 仅保存派生 Wiki、索引、回执和日报，不上传 raw。

## 3. AI 与 Work Agent 边界

当前 `cwk-ai-reviewer` 是零工具 JSON transformer：无 Skills、工具全拒绝、不能搜索、
不能读写项目、不能委派 subagent。调用失败由宿主的有界 repair/fallback 处理。

Work Agent 只是尚未实施的备用运行时设计，见
`docs/design/cwk-work-agent-runtime-integration.md`。在新 RT 完成合同、隔离、样本、
成本和引用验收前，任何 Codex、Claude、OMP 或 xAI runtime 都不得进入正式精编路由。

## 4. 主要模块与不可破坏约定

- `skill/`：Agent 入口和安装配置；
- `scripts/`：确定性采集、规范化、精编、检索、同步和运行验证；
- `tests/`：脱敏 fixture、单元测试和回归合同；
- `docs/`：设计、运行与迁移说明；
- `RT/`：历史与未来研发追踪；新工作按 AODW 处理，历史记录只作追溯证据；
- `PR/`：多租户知识空间等产品级计划及合同；
- `runs/`、`knowledge/`、`raw/`、`collected-raw/`：运行/数据路径，默认不提交。

不可破坏的约定：

- 只读 CWork；未经单独授权，不标已读、不回复、不处理待办、不删除、不做任何 CWork
  写操作；
- raw 不由摘要、事件、实体、模型或人工派生产物回写；
- 事实回答只基于 `evidence_status=verified` 的证据包；
- AI 或 Work Agent 不能写正式 summary/manifest，只有宿主可以在验证后落盘；
- 凭据只来自受控环境或私有配置，不能写入代码、日志、RT、测试、Git 或外部消息；
- 任何模型、Agent、并发、路由或数据权限改动都属于可审计的运行合同变更，需匹配测试。

## 5. 常用验证

```bash
make doctor
make test
python3 scripts/cwk_wiki_query.py --lint
python3 scripts/cwk_cloud_wiki_compile.py --help
```

真实采集、DocDB 同步和真实模型调用可能处理受保护数据或产生费用；没有明确需要时，优先
使用 `tests/`、dry-run 与本地只读检查。

## 6. AODW 接入边界

本项目于 2026-08-31 从 `bd-eval-loop` 提取 AODW v0.6.1 的通用框架。只复制方法层，
不继承投前项目的业务代码、运行时、案例、数据、密钥、历史 RT 或审计结论。

既有 CWK RT 与 `.spec-workflow/` 是接入前的历史工作记录，不能因安装 AODW 被倒推为
“已通过 AODW 门禁”。新建或重新启动的研发任务按 `.aodw-next/`、根目录 `AGENTS.md`
和相应 RT 执行。
