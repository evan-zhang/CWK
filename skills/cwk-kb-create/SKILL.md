---
name: "cwk-kb-create"
description: "AI 提议、用户拍板的知识库建库向导；RT-055 新栈注册步骤草案"
status: draft-pending-gray-gate
date: 2026-09-14
diff_from_v2: "原有 kb_wizard/kb_ingest/NAS 体检流程保留；摄取后增加 rag-sources、rag-index.json 与检索索引注册"
---

# cwk-kb-create v3（草稿待灰度拍板）

本 skill 是对话皮，不替用户拍板。库名、prefix、数据源、时间窗口、路由和是否注册新栈都
先给建议，再等用户确认；建库与摄取由 CWK 引擎执行。新栈注册是快照发布，不把原文上传到
第三方模型或 NAS 以外的服务；新栈仍仅供本机/隧道读取。

## 1. 收集并拍板

给出库名与 prefix 建议；确认来源（cwork mirror、DocDB 或本地目录）、窗口、route mode、
可见性，以及是否进入 RT-055 三库 bank。凭据只来自受控 env，不回显、不进转录。先运行
`kb_ingest.py plan`，把件数、expected_status_counts、unidentified 念给用户；计划未获用户
确认不得 run。目的地不干净、raw 非只增不改或存在未知文件时停止。

## 2. 既有 CWK 建库与摄取

按旧流程用 `kb_wizard.py create` 建库，随后 `kb_ingest.py plan → 用户确认 → run`。大库可
后台执行但要记录进度；完成后检查 status、reconcile 和 `kb_doctor.py verify --all`。
counts.failed 必须为 0，raw/manifest/collection-state/changed-paths/tree 全绿。refresh 仍
是快照增量入口：同字节 unchanged，变更升版本，源侧消失只报告不删库；计划为 0 或数量
异常暴增必须人工确认。不要改旧网关或直连 NAS 读全文。

## 3. 新栈注册（新增，灰度前只允许 dry-run/合成验证）

在 CWK 摄取和 doctor 全绿后，追加以下注册检查；任何一步失败都不标“新栈已就绪”：

1. 将已批准、可读的派生文本快照放入对应 `rag-sources/`；原文仍受本地快照权限保护，禁止
   把真实语料放进测试、Git、日志或模型提示。
2. 生成 `rag-index.json`，每个 doc_id 只映射到快照根目录下的相对路径；拒绝绝对路径、`..`
   段、重复 doc_id、越界 symlink、非 UTF-8、超过 resolver 大小上限的文件。
3. 在 OpenSearch 建立该 bank 的检索索引，确认 exact 通道覆盖编号/日期类查询，lexical
   通道覆盖普通正文词；记录索引代、件数和构建时间，不记录正文。
4. 通过 loopback `/query` 做合成或脱敏 smoke；命中结果的 doc_id 必须能被 `/read` 分页读出，
   页片段拼接等于快照全文，eof/total_chars 正确；`/answer` 传 bank 并验证零命中体面拒答。
5. 注册 bank 与快照索引元数据到 OPS 登记表；当前不注册 token，不激活新路由。把注册回执、
   索引代和 doctor 结果交给用户拍板灰度。

## 4. 失败与回滚

`/query` 不通、索引缺失或 `/read` 404/503 时，新 bank 不得对外宣称可用；保留已完成的 CWK
库和旧网关读链，修复后重跑注册检查。新栈是 snapshot-backed read，当前没有 NAS-backed
SHA chain；需要 lineage/version/full SHA 校验的场景走旧 v2。新栈注册失败不删除源文件、不
回滚或改动旧库，避免把快照故障误当摄取故障。

## 5. 建库完成回执

分别回报：CWK doctor 是否全绿；新栈 bank、快照索引代、检索 smoke、`/read` 分页 smoke、
`/answer` 零命中 smoke；未注册/未激活项明确写出。只有用户拍板灰度后才进入授权移植和运行
配置变更；本稿阶段不得部署、激活或关闭旧网关。
