# RT-056 知识库管理台 MVP

管理台是独立的内网管理面，默认关闭，默认绑定 `127.0.0.1:8791`。它只提供脱敏概览、服务探测、审计读取和写操作占位，不改变知识库内容，也不调用创建/摄取脚本。

## 运行

```bash
export KB_ADMIN_ENABLED=true
export KB_ADMIN_KEY_ENV=KB_ADMIN_KEY
# 由受控环境/密钥管理器注入，不要在命令行或仓库中写出值
export KB_ADMIN_KEY
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

**一个进程只开一个面**（`--face` / `KB_ADMIN_FACE`，默认 `admin`）：

- `admin`：管理面。保持回环绑定、要管理密钥。这里没有 `/register`，请求它一律 `404`。
- `register`：注册面。只有 `/healthz`、`/register`、`/api/register`、`/api/session`、`/api/logout`，
  **没有任何管理接口**——`/console`、`/api/overview`、`/api/audit`、`/api/services` 在这个面上都是 `404`。
  注册面可以绑局域网，管理面不必跟着出去。配置写错（面名拼错）时退回 `admin`，不会误开公开面。

管理面除 `/` 和 `/healthz` 外的管理 API 均须在 `X-KB-Admin-Key` 头提供管理密钥（兼容 `X-KB-Token`）。无密钥或错误密钥统一返回 `401`。`/healthz` 只返回启用状态；界面不会保存密钥。

- `GET /api/overview`：复用 `kb_ops` 的只读状态投影，返回库计数、词法就绪状态和 token 的脱敏 id、scope、状态与时间。
- `GET /api/services`：有界超时探测 8787/8790，只返回地址、健康状态和 HTTP 状态。
- `GET /api/audit`：读取最近受控审计事件，仅返回时间、动作、结果、状态。
- `POST /api/jobs/create`、`POST /api/jobs/ingest`：默认拒绝；显式打开写开关后也只记审计并返回 501。
- `POST /api/register` / `GET /api/session` / `POST /api/logout`：自助注册与薄会话（RT-065）。

响应不会返回 token 摘要、owner 引用/盐、token 明文、路径、`CWORK_APP_KEY` 或 SSH 信息。错误响应使用固定错误码，不回显异常文本。

## 验证

```bash
python3 -m unittest tests.test_rt056_kb_admin tests.test_rt065_register tests.test_rt065_face_and_tls
python3 -m py_compile scripts/kb_admin.py
```

## 用户自助注册（RT-065）

注册面提供 `/register` 页面：用户粘贴本人工作协同 Key，服务经玄关核实后写入人员目录，并设置短时会话 Cookie。Key 不落盘、不进审计正文。

必配环境变量：

- `KB_ADMIN_FACE=register`
- `KB_REGISTER_ENABLED=true`
- `KB_AUTHZ_STORE`：人员/成员授权表路径
- `KB_REGISTER_SESSION_SECRET`：至少 16 字符的会话签名密钥

### 加密是硬条件

这一页要用户交出本人的 Key，所以它**必须跑在 https 上**，判定只认两种证据：

1. **本连接就是 TLS**——给 `--tls-cert` / `--tls-key`（或 `KB_TLS_CERT` / `KB_TLS_KEY`），服务自己起 https，不需要反代。
   注册面缺证书时**启动即失败**，不会悄悄以明文跑起来。
2. **前面确实有反代**——只有显式设了 `KB_REGISTER_TRUST_PROXY=true` 才采信 `X-Forwarded-Proto`。
   不设的话这个头一律忽略：它是客户端自己发的，没有反代覆盖时谁都能写 `https`。

明文访问 `/register` 返回 `403` 和一张**没有输入框**的说明页。这一点是有意的：
如果先给表单、等用户填完提交再拒绝，Key 已经明文过了一次网络，闸就白装了。

`KB_REGISTER_ALLOW_HTTP=true` 只用于本机联调，生产不要开。

### 成员管理页面（RT-068）

登录后打开 `/members`：看某个库有谁、加人、改角色、移除。改动约十秒生效，不用重发令牌。

- **没有共享密码**。操作者就是会话里那个人，能管什么完全按 RT-061 的角色规则：
  所有者管自己的库，管理员管所有库。管理面的 `X-KB-Admin-Key` 与这一页无关。
- **刻意不放在明文控制台上**：那里局域网可达、共享密码，给它写权限等于谁知道密码就能给自己开库。
- 规则在服务端强制，页面只是不给按钮：所有者不能降级其他所有者（只能降级自己），
  服务身份只有管理员能动，库至少保留一位所有者。
- 并发保护：页面带着读到的版本号提交，期间别人改过就拒绝并刷新。
  **两种 409 分得很清楚**——`version_conflict` 重试有用，`conflict` 是规则不允许，重试无用，
  页面照原样显示服务端给的原因。
- 写接口要求页面自带的 `X-CWK-Members` 头；跨站表单加不上它（会触发预检）。

### 自助令牌页面（RT-069）

登录后打开 `/tokens`：看自己的令牌、签一支新的、吊销。

**只能签给自己，而且要当场再证明一次身份。** 签发同时要求两件事：会话说你是谁，
并且你当场粘贴的 Key 解析出同一个人。于是会话被盗签不出令牌（没有 Key），
拿着别人的 Key 也签不出（会话对不上）。管理员能看全部令牌的元数据、能紧急吊销，
但**签不出别人的令牌**——这是 RT-047 铁律的延续，主人只能由当场核实的 Key 推导。

- 明文只在签发那一次返回，页面提示"只出现这一次"；登记表只存摘要。
- 列表只给元数据，连 `owner_ref`、`agent_binding_id` 这类派生标识都去掉了。
- 签出的令牌是 `authz=grants`：能查哪些库跟随成员表，加减权限不用重签。
  `kb_ids` 快照取签发当时的成员资格，只用于回滚。
- 还不是任何库的成员时拒绝签发，并直接告诉他"先请管理员把你加进一个库"。
- 沿用既有约束：同一个用途标识同时只能有一支有效令牌；每人有效令牌数有上限。
- 吊销：自己的随时可撤；别人的只有管理员能撤。立即生效。

### 谁是管理员

`admins` 列表只能在服务器上用命令行改——"谁能管所有库"这种权力不从网页发出去：

```bash
python3 scripts/kb_authz.py admin-add    --store $S --principal person:<组织ID>:<人员ID>
python3 scripts/kb_authz.py admin-remove --store $S --principal person:<组织ID>:<人员ID>
```

对象必须是已报到的人（服务不行）。每次改动都留回执。

### 防滥用

注册接口会把收到的字符串拿去问玄关，因此按来源限速（`KB_REGISTER_RATE_LIMIT`，默认每分钟 5 次，
超出返回 `429` 并带 `Retry-After`）。没有这道闸，它就是一个「这把 Key 有效吗」的免费验证器，
也能被用来借道压玄关。

### 生产启动示例（OPS）

```bash
# 证书一次性生成（自签，含服务器 IP），私钥 0600，不进仓库
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -subj "/CN=192.168.91.72" -addext "subjectAltName=IP:192.168.91.72" \
  -keyout auth/tls/register-key.pem -out auth/tls/register-cert.pem

KB_ADMIN_FACE=register KB_REGISTER_ENABLED=true \
KB_AUTHZ_STORE=auth/registry/kb-authz.json \
KB_REGISTER_SESSION_SECRET="$(cat auth/register-session.secret)" \
python3 scripts/kb_admin.py --face register --host 0.0.0.0 --port 8793 \
  --tls-cert auth/tls/register-cert.pem --tls-key auth/tls/register-key.pem
```

自签证书意味着同事首次打开会看到浏览器安全警告，需要手动继续或导入证书——
这是自签方案的固有代价，加密本身是真的。

门户导航的注册入口只认 `KB_PORTAL_REGISTER_URL`，而且**必须是 `https://` 开头**；
没配或配成明文地址就不显示入口（门户上出现一个明文的「填 Key」链接，比没有链接更危险）。
