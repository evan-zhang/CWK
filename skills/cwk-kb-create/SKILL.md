---
name: "cwk-kb-create"
description: "AI 提议、用户拍板的知识库建库向导：CWK 摄取 + RT-055 新栈注册（快照/索引/token），注册完成即接入正式网关"
status: active
date: 2026-09-14
diff_from_v2: "保留 kb_wizard/kb_ingest/NAS 体检流程；新增 RT-055 注册段（rag-sources/rag-index.json/检索索引/per-Agent token），注册后新库立即可经 8787/8790 访问"
---

# cwk-kb-create — AI 知识库建库向导（含 RT-055 注册，已激活）

本 skill 是对话皮，不替用户拍板。库名、prefix、数据源、时间窗口、是否注册新栈都先给建议，
再等用户确认；建库与摄取由 CWK 引擎执行。新栈注册是快照发布：不把原文上传到第三方模型或
NAS 以外的服务。

## 1. 收集并拍板

给出库名与 prefix 建议；确认来源（cwork mirror、DocDB 或本地目录）、窗口、route mode、
可见性，以及是否注册进 RT-055（成为第 4 个 bank）。凭据只来自受控 env，不回显、不进转录。
先跑 `kb_ingest.py plan`，把件数、expected_status_counts、unidentified 念给用户；计划未获
确认不得 run。目的地不干净、raw 非只增不改或存在未知文件时停止。

## 2. CWK 建库与摄取（既有流程）

`kb_wizard.py create` 建库 → `kb_ingest.py plan → 用户确认 → run` → status/reconcile →
`kb_doctor.py verify --all`。counts.failed 必须为 0，raw/manifest/collection-state/
changed-paths/tree 全绿。refresh 是快照增量入口：同字节 unchanged、变更升版本、源侧消失只
报告不删库；计划为 0 或数量异常暴增必须人工确认。不直连 NAS 读全文。

## 3. RT-055 新栈注册（正式流程，2026-09-14 灰度已过）

CWK doctor 全绿后追加以下注册；任何一步失败都不标"新栈已就绪"：

1. **快照落位**：把已批准、可读的派生文本快照放入 `/Users/xgstudio/rt055-production/rag-sources/`
   （按 bank 前缀组织）；真实语料禁止进入测试、Git、日志或模型提示。
2. **索引映射**：生成 `rag-index.json`——每个 doc_id 只映射快照根下相对路径；拒绝绝对路径、
   `..` 段、重复 doc_id、越界 symlink、非 UTF-8、超 resolver 大小上限文件。
3. **检索索引**：在 OpenSearch（rt055-production-opensearch-1）建该 bank 的检索索引；确认
   exact 通道覆盖编号/日期、lexical 覆盖正文词；记录索引代、件数、构建时间（不记正文）。
4. **服务注册**：新 bank 加入 `CWK_RETRIEVAL_BANKS`（deploy/.env），重建 retrieval 容器。
5. **smoke（带 token）**：/query 三通道冒烟、/read 分页拼接等于快照全文、/answer 零命中体面
   拒答；全部通过才算注册成功。
6. **授权**：为需要访问的 Agent 用 `kb_token.py issue` 签发含新 bank 的 token（见
   cwk-kb-authorize）；无 token 的 Agent 会 403（scope 隔离，属正常）。
7. **回执**：向用户交付 bank 名、索引代、smoke 结果、token 签发回执（明文只出现一次）。

## 4. 失败与回滚

/query 不通、索引缺失或 /read 404/503 时，新 bank 不得对外宣称可用；已完成步骤保留待修复
重跑。新栈是 snapshot-backed read（无 NAS-backed SHA chain）；需要 lineage/version/full SHA
校验的场景目前无替代（旧 v2 已于 2026-09-14 停运，回滚材料在 OPS
`rt055-production/auth/old-gateway-rollback.txt`）。注册失败不删源文件、不动既有库。

## 5. 完成回执

回报：CWK doctor 是否全绿；新栈 bank、索引代、三端点 smoke 结果、token 签发记录；未完成项
明确写出。后续的 nightly 更新（快照增量）由独立管道负责，不在本 skill 范围。
