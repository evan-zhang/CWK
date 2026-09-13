---
name: "cwk-kb-query"
description: "知识库检索、问答与原文读取；RT-055 新栈三端点草稿，待灰度拍板"
status: draft-pending-gray-gate
date: 2026-09-14
diff_from_v2: "主路径从 8787 v2 读链迁至 loopback /query、/answer、/read；保留旧网关回退附录"
---

# cwk-kb-query v3（草稿待灰度拍板）

本 skill 只做知识库读取与回答，不写 CWork、不标已读、不发送消息。RT-055 新栈是本机
或隧道可达的 OPS loopback：检索 `127.0.0.1:18887/query`，问答
`127.0.0.1:8790/answer`，原文 `127.0.0.1:8790/read`。新栈当前**无鉴权**，只可在
本机/受控隧道使用；语料是快照，不保证实时。

## 能力矩阵与路由

| 需求 | 入口 | 关键规则 |
|---|---|---|
| 找文档/候选 | `POST /query` | `{bank, query, top_k}`；返回 hits、score、channel、no_answer、took_ms |
| 形成 AI 结论 | `POST /answer` | `{query, top_k, bank?}`；当前模型通常约 38s；零命中必须原样体面拒答 |
| 读原文 | `POST /read` | `{doc_id, offset?, length?}`，默认 offset=0、length=65536；分页单位是字符，返回 eof、total_chars |

bank 只能使用已登记库：`cwork-3m`、`docdb-touqian`、`spbp-2027`。编号、日期、带连字符
的短标识等查询优先看 `channel=exact`：它避免中文词法分词把编号拆散；普通自然语言和正文
概念看 `channel=lexical`。`no_answer=true` 只表示本次快照检索没有命中，不能凭一次零命中
断言库里没有；先换 2–3 个同义词、变体或日期格式。

## 新栈操作顺序

1. 明确 bank 与问题；不要跨 bank 猜测或把默认库当作授权证明。
2. 调 `/query` 找候选；记录 `doc_id`、score、channel 与 `no_answer`。连接拒绝表示 OPS 不通，
   不直接读取 NAS。
3. 需要 AI 归纳时调用 `/answer`，传同一 bank；零命中时说“知识库中未找到相关内容。”，
   不用记忆补答案。当前约 38s，调用方应设置足够超时并避免重复轰炸。
4. 需要原文核验时调用 `/read` 分页：下一页 offset = 上一页 offset + 返回文本长度，直到
   `eof=true`。按 `total_chars` 校验没有跳页或重复；服务返回 416 就修正 offset，不静默前移。
5. 每个事实结论都要绑定 citation 或 `/read` 原文页。引文自检不通过就标“未验证”，不得把
   模型答案当作证据。

## 错误、降级与边界

- 400：请求结构、bank/query、offset/length 不合法；修正请求，不重试原请求。
- 401/403：新栈当前没有鉴权面；迁移后的对应语义分别是“未来 token 缺失/失效”和“未来
  token 无该 bank scope”。旧网关仍按 401=过期/撤销/换代、403=跨库隔离处理。
- 404：新 `/read` 的 doc_id 不存在；不要改写成“文档内容为空”。旧网关 unknown_kb 也是
  404，但不能回落主库。
- 416：`/read` offset 越界或范围不合法；重新以 `total_chars` 计算分页。
- 503：检索/模型不可达或索引/源文件不可用；词法不可用时不能静默降级。新栈应先报告
  loopback 不通/快照不可用；旧网关 lexical_unavailable 可显式 `allow_degraded=metadata`。
- 新快照非 NAS 实时链：没有 lineage/raw_sha/full_sha_verified，不得声称做了旧 v2 的
  SHA 现场复核。
- SHA 派生旧授权 token 时严禁 `printenv` 带尾换行：用 `printf '%s'` 后再 sha256；否则会
  伪装成 401。任何 token 不写日志、不放命令行、不回显。
- placeholder 或不可读源只能如实说明“只存档未转正文”；不假装读过。

## 过渡期判定规则

- 默认新栈：三库候选检索、常规问答、可接受快照延迟的短原文查看。
- 暂走旧网关 v2：深度原文校验、需要 lineage/version/raw_sha256、`full_sha_verified=true`
  或 candidate_spans 精确 byte 范围的场景；也包括新 `/read` 404/503 且不是明确新栈数据缺失的
  故障排查。旧网关只读，不改其配置或服务。
- 新旧结果冲突时，以能提供现场 SHA 链的旧 v2 为校验依据，并把差异记录为迁移缺口；不把
  两套结果拼成一个无来源结论。
- 退出双轨前必须完成本文“迁移决策点清单”中的读端点、鉴权、回退和一致性验收；未满足时
  不得关闭旧网关。

## 附录：旧网关 v2 回退路径

旧入口为 8787 `/v2/kb/*`，先用 `/v2/kb/libraries` discovery，再带绑定 token 与 `kb`。
典型链路是 `search → resolve → read/continue`；document_ref 有 TTL，续读必须用
`continue`，不能把 `read+cursor` 当作替代。read 返回 identity、lineage/version、
`full_sha_verified=true`；citation 可取实时 SHA 与 excerpt。byte range 落在 UTF-8 码点中间
必须接受 416，不静默前推；历史 version 非当前版返回 409；非法 UTF-8 返回 422。

旧网关错误速查：401 token 过期/撤销/换代；403 scope/跨库；405 写动词被拒；503 lexical
index 不可用；连接拒绝表示 OPS 不通。只有 OPS 不可达且本机确有 CWK 凭据时，才按旧 runbook
临时起本机网关；Agent 不直连 NAS。旧融合检索在词法代未建库上可显式 metadata 降级，不能
静默降级；高频词可能超过 60s，超时按 300s 设计。
