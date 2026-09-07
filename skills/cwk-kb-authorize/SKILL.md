---
name: "cwk-kb-authorize"
description: "知识库授权：生成/轮换/撤销单库共享授权文件；仅供具备 OPS token registry 写权限的管理 Agent"
---

# cwk-kb-authorize — 单库共享授权

为一个既有知识库生成可分发的共享授权文件，或整体轮换、撤销该文件的所有副本。

## 启用边界

- 只在**已具备 OPS token registry 写权限**的管理 Agent 上启用；能写登记表就是 MVP 的管理员边界。
- 不读取或判断 `kb.json.owner_ref`，不声称验证了创建者。生产三库的 `owner_ref=pending` 保持不变。
- 不操作真实 IM，不替用户发送附件；只在本机生成文件并回报路径、库 ID、token_id 和代际。
- 不启动、不修改查询 Gateway。Gateway 继续只有 GET 只读面。
- 不在消息、日志、命令参数或回执中显示 token；授权文件本身含明文 bearer secret，按敏感附件处理。

## 生成

确认目标 `kb_id`、Gateway URL 和本机输出路径后运行：

```bash
cd <CWK仓库>
python3 scripts/kb_access_file.py export \
  --registry <OPS登记表路径> \
  --kb-id <单个kb_id> \
  --gateway-url <Gateway基础URL> \
  --out <私有输出目录>/<文件名>.cwk-access.json \
  --reason <授权理由>
```

成功只回报非秘密字段。文件为 0600，父目录为 0700。一份文件只含一个 `kb_id`。
若该库已有有效共享 token，`export` 必须失败；不能通过重复导出找回旧明文。

把生成路径交给用户，由用户自行决定是否、向谁、通过何种 IM 分发。不要自动发送。

## 轮换

授权文件疑似泄露、丢失，或需要重新分发时：

```bash
python3 scripts/kb_access_file.py rotate \
  --registry <OPS登记表路径> \
  --kb-id <单个kb_id> \
  --gateway-url <Gateway基础URL> \
  --out <新的私有输出路径> \
  --reason <轮换理由>
```

若覆盖本机已有输出文件，额外加 `--replace`。成功后旧代全部在登记表中吊销；Gateway 每请求重读登记表，旧副本下一次查询返回 401。新文件仍由用户自行分发。

## 撤销

按非秘密句柄 `token_id` 整体撤销该共享授权：

```bash
python3 scripts/kb_access_file.py revoke \
  --registry <OPS登记表路径> \
  --token-id <tok-...> \
  --reason <撤销理由>
```

撤销后所有该 token 的文件副本同时失效。MVP 不支持按接收人单独撤销。

## 回执判定

- 成功：JSON `ok=true`，并核对 `kb_id`、`token_id`、`generation`、`mode=0600`。
- `conflict`：已有有效共享 token；需要新文件时改用 `rotate`，不要绕过。
- `unsafe_path` / `invalid_access_file`：停止，不放宽权限或 schema。
- 任何输出若意外出现 bearer token，停止分发并立即按 `token_id` 撤销；不要把泄露值复制到对话。
