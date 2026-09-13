---
name: "cwk-kb-authorize"
description: "知识库授权与 RT-055 新栈 per-Agent token 设计草案；灰度阶段实施"
status: draft-pending-gray-gate
date: 2026-09-14
diff_from_v2: "保留旧网关单库共享授权流程；新增 /query、/answer、/read 的 per-Agent token 中间件设计，当前不启用"
---

# cwk-kb-authorize v3（草稿待灰度拍板）

**灰度阶段实施，不激活。** 当前 RT-055 新服务仍是 OPS loopback 无鉴权面；本稿是把
RT-047/053 的 per-Agent token 机制移植到三个端点的设计，不是生产配置变更。

## 目标与边界

- `/query`、`/answer`、`/read` 在同一个服务边界接入统一 bearer 校验；`/healthz` 可继续免鉴权，
  但不得由健康探针读取正文。
- token 只代表 Agent 身份与 bank scope，不把管理 Key 下发给 Agent；registry 仍在 OPS。
- 不改变旧网关授权流程；旧 8787 的单库共享授权文件、绑定 token、管理通道在双轨期继续有效。
- 不自动发送授权文件、不读取 owner_ref、不操作真实 IM、不启动/修改 Gateway。

## 设计草案

中间件放在 HTTP 请求解析之后、进入 query/answer/read handler 之前，且早于 bank/doc_id
解析和任何 source/index 读取。它只读取 `Authorization: Bearer <token>`，失败响应不回显
token、URL、bank 挂载面或本地路径。建议统一响应：

- 缺失、格式错误、过期或 registry 吊销：401；
- token 有效但 bank 不在 scope，或 doc_id 不属于 scope：403；
- endpoint 不存在仍为 404；请求范围错误仍为 400/416；后端不可达仍为 503。

`/read` 必须按 token scope 先授权 doc_id，再让 `DocResolver` 做 index、路径 containment、
大小上限和 UTF-8 校验；不能用“已知 doc_id”绕过 scope。`/answer` 的 bank 既要授权，也要
传给检索器；不得只保护 query 而漏掉 bank。`/query` 的 bank 同理。

## 签发、轮换、吊销

沿用 `scripts/kb_token.py`：操作者在有业务 Key 的机器上以 env 传入 verify key，按
`agent-id` 与单个或明确的 bank scope `issue`；明文只在签发回执出现一次，随后写入 0600
文件。registry 在 OPS，Gateway/新服务每请求重读或使用有明确 TTL 的安全缓存；吊销必须在
验收中证明在下一请求生效。rotate 产生新代并使旧代失效；revoke 按 token_id 整体撤销。

命令参数、日志、异常、回执都不打印 secret。禁止把 token 放进 URL、skill、测试 fixture、
Git 或对话。管理 Key 派生必须使用 `printf '%s'`，不可把尾换行带入 SHA；绑定 token 不派生。

## 灰度验收门

1. 正常 token 能按 scope 调用三端点；跨 bank 的 query/answer/read 全部 403。
2. 缺失、过期、吊销 token 为 401，且吊销在下一请求生效；无 token 的旧流程仍只走旧网关。
3. `/read` 未授权的 doc_id 在任何 source/index 读取前被拒，响应不泄露存在性以外信息。
4. registry 不可达、格式损坏、重复 token_id fail-closed 为 503/管理错误，不默认放行。
5. 审计日志只留 agent_id、token_id（非 secret）、bank、endpoint、结果和耗时；不留 query、
   answer、原文、Authorization header。
6. 通过合成 fixture 的 replay、并发、轮换、吊销、路径穿越和日志泄露测试，再由 Evan 拍板
   灰度启用。未过门前，新栈仍标无鉴权，仅限 loopback/隧道。

## 当前旧流程保留

旧 `kb_access_file.py export/rotate/revoke` 仍是单库共享授权入口：目录 0700、文件 0600、
同库已有有效 token 时 export 必须 conflict；成功只回报 kb_id/token_id/generation/mode，
授权文件由用户自行分发。401/403 语义及旧 Gateway v2 读链不因本草案改变。
