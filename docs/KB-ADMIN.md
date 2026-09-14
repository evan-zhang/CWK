# RT-056 知识库管理台 MVP

管理台是独立的内网管理面，默认关闭，默认绑定 `127.0.0.1:8791`。它只提供脱敏概览、服务探测、审计读取和写操作占位，不改变知识库内容，也不调用创建/摄取脚本。

## 运行

```bash
KB_ADMIN_ENABLED=true \
KB_ADMIN_KEY_ENV=KB_ADMIN_KEY \
KB_ADMIN_KEY='只存在于受控环境的密钥' \
python3 scripts/kb_admin.py
```

`KB_ADMIN_KEY_ENV` 和 `--key-env` 只能是环境变量名，不能放密钥值；不要把密钥写入命令行参数、仓库或日志。启动时不设置 `KB_ADMIN_ENABLED=true`，管理 API 仍统一返回 `401 unauthorized`，不读取库配置；仅 `/healthz` 返回禁用状态。

可选配置：

- `KB_LOCAL_LIBRARY_ROOT`：本地库根目录；默认 `~/CWK/libraries`。
- `KB_REGISTRY_PATH`：token registry 路径；默认 `~/CWK/ops/tokens.json`。
- `KB_ADMIN_AUDIT_PATH`：管理审计 JSONL；默认 `~/CWK/ops/admin-audit.jsonl`，与 KB 内容树分离。
- `KB_GATEWAY_URL` / `KB_OPS_URL`：8787/8790 健康探测地址。
- `KB_ADMIN_SERVICE_TIMEOUT`：探测超时，最大 3 秒。
- `KB_ADMIN_WRITE_ENABLED=true`：仅允许记录 create/ingest 的受控 `501 not_implemented` 占位；不启用时返回 `403`。两种情况下都不修改 KB。审计文件目录按 `0700`、文件按 `0600` 创建。

## API 与安全边界

除 `/` 和 `/healthz` 外，API 均须在 `X-KB-Admin-Key` 头提供管理密钥（兼容 `X-KB-Token`）。无密钥或错误密钥统一返回 `401`。`/healthz` 只返回启用状态；界面不会保存密钥。

- `GET /api/overview`：复用 `kb_ops` 的只读状态投影，返回库计数、词法就绪状态和 token 的脱敏 id、scope、状态与时间。
- `GET /api/services`：有界超时探测 8787/8790，只返回地址、健康状态和 HTTP 状态。
- `GET /api/audit`：读取最近受控审计事件，仅返回时间、动作、结果、状态。
- `POST /api/jobs/create`、`POST /api/jobs/ingest`：默认拒绝；显式打开写开关后也只记审计并返回 501。

响应不会返回 token 摘要、owner 引用/盐、token 明文、路径、`CWORK_APP_KEY` 或 SSH 信息。错误响应使用固定错误码，不回显异常文本。

## 验证

```bash
python3 -m unittest tests.test_rt056_kb_admin
python3 -m py_compile scripts/kb_admin.py
```
