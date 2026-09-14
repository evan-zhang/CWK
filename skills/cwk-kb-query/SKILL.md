---
name: "cwk-kb-query"
description: "知识库检索、AI 问答与原文读取：RT-055 新网关三端点（8787/8790，X-KB-Token 鉴权），旧网关已停、回退路径见附录"
status: active
date: 2026-09-14
diff_from_v2: "主入口迁至 8787 /query、/answer、/read（强制 token 鉴权）；旧网关 v2 读链降级为回退附录（已停运）"
---

# cwk-kb-query — 知识库检索 / 问答 / 原文读取（RT-055 新网关，已激活）

本 skill 只做知识库读取与回答，不写 CWork、不标已读、不发送消息。铁律不变：**每个事实性
回答必须绑定 citation 或 /read 原文页**；没有命中就直说没有，不许拿记忆或猜测作答。

## 入口与鉴权（2026-09-14 起强制）

| 服务 | 正式入口（局域网） | OPS 本机 | 鉴权 |
|---|---|---|---|
| 检索 | `http://192.168.91.72:8787/query` | `127.0.0.1:18887/query` | X-KB-Token 必须 |
| AI 问答 | `http://192.168.91.72:8790/answer` | `127.0.0.1:8790/answer` | X-KB-Token 必须 |
| 原文读取 | `http://192.168.91.72:8790/read` | `127.0.0.1:8790/read` | X-KB-Token 必须 |
| 健康检查 | `http://192.168.91.72:8787/healthz` | 同左 | 免鉴权 |

- 所有业务请求必须带 `X-KB-Token: <token>` 头；token 按 Agent 实例签发、按库授权 scope。
- **领 token**：已有 token 优先复用（Mac mini 会话直接用 gateway `.env` 的 `CWK_KB_TOKEN`，180 天三库 scope，禁止重复签发）；确需新签才走 cwk-kb-authorize，token 明文只在签发回执出现一次。管理面有每用户 5 token 上限。
- 401 = token 缺失/过期/被吊销；403 = token 有效但目标 bank 不在其 scope（跨库隔离，正常）。

## 能力矩阵与路由

| 需求 | 入口 | 关键规则 |
|---|---|---|
| 找文档/候选 | `POST /query` `{bank, query, top_k}` | 返回 hits(doc_id,score,channel)、no_answer、took_ms |
| AI 结论 | `POST /answer` `{query, top_k, bank?}` | 约 24–40s（BD-deepseek-v4-flash）；零命中体面拒答 |
| 读原文 | `POST /read` `{doc_id, offset?, length?}` | 字符分页；返回 text/eof/total_chars；越界 416 |

bank 三选一：`cwork-3m`（工作协同近 3 月）、`docdb-touqian`（投前资料）、`spbp-2027`（2027 SP&BP）。
编号/日期/短标识类查询看 `channel=exact`（精确通道，不怕中文分词拆编号）；自然语言概念看
`channel=lexical`。`no_answer=true` 或零命中时先换 2–3 个同义/变体再下"库里没有"的结论。

## 操作顺序

1. 明确 bank 与问题；不跨 bank 猜测。
2. `/query` 找候选，记录 doc_id / score / channel。
3. 要 AI 归纳 → `/answer`（同一 bank；超时给 ≥120s；零命中如实说"未找到"）。
4. 要原文核验 → `/read` 分页（下一页 offset = 上一页 offset + 返回 text 长度，直到 eof=true）。
5. 结论必须带 citation 或原文页；自检不过就标"未验证"。

## 错误与边界

- 400 请求不合法；401 token 缺失/失效（找 authorize 重签）；403 跨库越权（换有 scope 的 token）
- 404 /read 的 doc_id 不存在；416 offset 越界（按 total_chars 重算）；503 检索后端/模型/源文件不可用
- 语料是**快照**（非 NAS 实时链）：没有 lineage/raw_sha/full_sha_verified，不得声称做了 SHA 现场复核
- 任何 token 不写日志、不放命令行、不回显

## 附录：旧网关回退路径（已停运，仅供恢复）

旧 kb_gateway（8787-8789 v2 读链）已于 2026-09-14 08:55 停运。停运档案与回滚材料：
OPS `/Users/xgstudio/rt055-production/auth/old-gateway-rollback.txt` 与 `old-gateway-plists/`
（含原进程命令行、cwd、launchd plist 备份）。需要回滚时按档案即时恢复 launchd 任务。
历史参考：旧入口 `8787 /v2/kb/*`（search→resolve→read/continue，document_ref 15 分钟 TTL，
full_sha_verified=true）；旧错误速查 401=过期/撤销、403=跨库、405=写动词被拒、503=词法代不可用。
SHA 派生管理 token 时严禁 `printenv` 带尾换行（用 `printf '%s'`），否则伪装成 401。
