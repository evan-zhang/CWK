---
name: "cwk-kb-authorize"
description: "知识库授权：为 RT-055 新网关签发/轮换/撤销 per-Agent token（按库 scope）；仅供具备 OPS registry 写权限的管理 Agent"
status: active
date: 2026-09-14
diff_from_v2: "新增 RT-055 新网关 per-Agent token 签发（已上线强制鉴权）；旧网关单库共享授权文件流程降级为回退附录"
---

# cwk-kb-authorize — 知识库授权（RT-055 新网关 token）

为新网关（8787 检索 / 8790 问答+读原文）签发、轮换、撤销 per-Agent 访问 token。
**启用边界**：只在具备 OPS token registry 写权限的管理 Agent 上使用；不操作真实 IM、
不替用户发送附件；token 明文不打印、不写日志、不进转录。

## 新网关 token（当前强制鉴权面）

- registry：OPS `/Users/xgstudio/rt055-production/auth/rt055-tokens.json`（每请求现读，吊销即刻生效）
- 签发（在 OPS 上，凭据走 env，不进命令行）：

```bash
cd /Users/xgstudio/rt055-production
export CWORK_APP_KEY="$(cat auth/key.env)"   # 业务 Key，仅用于派生 owner_ref
python3 scripts/kb_token.py issue \
  --registry auth/rt055-tokens.json --verify-env CWORK_APP_KEY \
  --agent-id <Agent实例标识> --actor <操作者> --reason "<理由>" \
  --kb-id cwork-3m [--kb-id docdb-touqian] [--kb-id spbp-2027] [--ttl-days 90]
```

- `--kb-id` 可重复 = 多库 scope；scope 决定 403（token 有效但库不在授权面）
- 回执含明文 token（唯一一次）；交给对应 Agent 后即从会话消失
- 吊销：`python3 scripts/kb_token.py revoke --registry auth/rt055-tokens.json --token-id tok-<hex>`
- 列表：`... list --registry auth/rt055-tokens.json`
- 内部服务 token（rt055-internal-rag，三库 scope）已签发，勿撤销——撤销会打断 /answer 内部调用

## 验证与用户交付

- 签发后用新 token 对授权 bank 各做一次 /query 冒烟（期望 200），对非授权 bank 期望 403
- 交付给用户/Agent 的只是 token 明文或按其本机习惯落地的 0600 文件；说明 401/403 语义

## 附录：旧网关单库共享授权文件（回退用，已停运）

旧 kb_gateway 已停运；其 `.cwk-access.json` 共享授权文件流程（export/import/revoke/rotate，
`kb_access_file.py`）保留作回退参考。恢复旧网关按
`/Users/xgstudio/rt055-production/auth/old-gateway-rollback.txt` 档案操作。
