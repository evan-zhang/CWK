# RT-Lite: RT-065 - 用户自助注册（填工作协同 Key）

> profile: Spec-Lite | execution_mode: collaborative

## 方案（给人看）

- 做什么：放开一个**注册页**。用户自己打开 → 填入本人的工作协同 Key → 点确认。
  系统拿这把 Key 去核实是谁，把人记进人员目录，并让浏览器记住「已登录的是此人」。
  不设用户名密码，不另建账号体系——Key 背后已经是具体的人。
- 为什么：没有这一步，只有管理员能以本人名义进系统；后面的成员管理、自助建库都走不动。
- 代价：注册页必须走**加密网址**（HTTPS）。Key 经明文网页传等于把钥匙亮在局域网上——
  这是唯一多出来的硬条件，页面本身保持一张表单那么简单。
- 这次故意不做什么：用户管理大后台、自助建库、改检索权限模型、邮箱/密码注册。
- 用户怎样算成功：同事打开注册页 → 填自己的 Key → 看到「已注册：张三」之类结果；
  假 Key 被拒绝；系统**不保存**那串 Key。
- 建议：**定论按这个简化版做。**

## 用户路径（就这一条）

1. 打开注册页（加密地址）。
2. 粘贴自己的工作协同 Key，提交。
3. 核实通过 → 注册成功，进入已登录状态。
4. 以后进控制台能认出是谁（同一套登录态）。

## 假设与现状

- 核实「Key → 哪个人」的能力已经有；缺的是给同事用的这一页，以及页必须加密。
- 管理员仍可用原来的运维入口；本 RT 不拆掉它。

## 实现备注

- `kb_admin`：`/register` 单页表单；`POST /api/register` 调 `kb_identity` 核实后 `kb_authz.upsert_person`；HMAC 会话 Cookie；`GET /api/session` / `POST /api/logout`。
- Key 只在请求内存中使用，不写 store、不进审计正文。
- 默认要求 HTTPS（`X-Forwarded-Proto`）；本地联调用 `KB_REGISTER_ALLOW_HTTP=true`。
- 门户导航增加「注册」链接（`KB_PORTAL_REGISTER_URL` 可覆盖）。

## 验证

- 单元：`python3 -m unittest tests.test_rt065_register`（7 例通过）。
- 假 Key / 空 Key / 未开注册 / 非 HTTPS → 对应 401/400/404/403。
- 真路径（夹具）：写入人员目录 + Set-Cookie；logout 清 Cookie 后 session 无 Cookie。
- 本机浏览器：`http://127.0.0.1:18791/register` 表单页可打开（ALLOW_HTTP 联调）。

## 变更记录

- 用户能感到什么：自己打开一页、填 Key，就能注册进来。
- 2026-09-18：实现注册页、会话与测试；文档写入 `docs/KB-ADMIN.md`。

## 遗留事项

- 成员管理页、按人判权切换、自助建库/上传——注册跑通后再做。
- 生产启用需 HTTPS 反代 + 正式 `KB_REGISTER_SESSION_SECRET` / `KB_AUTHZ_STORE`。
