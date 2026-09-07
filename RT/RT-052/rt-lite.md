# RT-052：知识库发现端点 + OPS 盘点面

> Spec-Lite。2026-09-07 由 Evan 授权立项（「按你的建议执行，最好先做一下 codex 评审」）。
> 评审门：方案稿经独立 Codex 评审后才动产品代码。

## 背景与问题

RT-051 后多 Agent 接入成为现实（cwk-kb-access-setup.md 接入流）。当前缺口：

1. **接入方无法发现自己有什么**：Agent 拿到 token 后不知道能查哪些库、每库什么状态（件数/词法代可用性），只能靠接入文档里写死的清单，库一变就过期。
2. **管理员全景盘点靠手拼**：三库健康、词法代新旧、发出去的 token 都给谁（agent-id/库/TTL）、夜间 refresh 结果——数据分散在网关挂载面、tokens.json、各库 `_system/`，每次 SSH 手拼。

## 目标（两件，受众分离）

### ① 网关发现端点 `GET /v2/kb/libraries`

- **授权域过滤**：绑定 token 只见 `kb_ids ∩ 网关挂载面` 的库；admin token 见全部挂载库。403/401 语义与其他 v2 路由一致。
- 每库返回：`kb_id`、显示名（raw-index 侧无 display name 时如实给 kb_id）、件数（total）、可读件数、词法可用性（`lexical_modes`：有无可用词法代——只报能力不报 generation 细节）、网关版本。
- **红线（延续现有姿态）**：
  - GET-only、零写、Cache-Control no-store；
  - **不回送登记表内容**——token 授权明细（谁持有什么 token）是管理面信息，绝不出现在查询网关的网络面；本端点只暴露「调用者自己被授权的库」这一投影；
  - 未挂载的库对绑定 token 不可见（不能用它探测挂载面全集）。
- 响应 schema：`cwk.kb.libraries.v2`，`ok/kb 无关全局字段 + libraries[]`。
- 挂载面信息源：网关自身的 `self.mounts`（进程内事实，无新增 I/O）；每库件数/词法可用性轻量读取（list 走 raw-index，词法可用性走 `_v2_load_lexical` 判空，NAS 后端各一次读）。

### ② OPS 盘点脚本 `scripts/kb_ops.py`（只读）

- `kb_ops.py status [--json]`：聚合输出——
  - 三库（cwork-3m / docdb-touqian / spbp-2027）：total/可读件数/词法代 generation 尾 8 位与 up-to-date 判定；
  - token 授权账目（读 `~/CWK/ops/tokens.json`）：每条 token_id 尾 4、agent-id、kb_ids、签发/过期时间、剩余天数、状态（active/expired/revoked）——**只输出元数据，绝不输出 token 指纹全文或明文**（登记表本身就只存 sha256，脚本维持这一姿态）；
  - 夜间 refresh 摘要：`ops/logs/kb-refresh-<date>.json` 最近一次的 per-lib ok。
- 默认人读格式（短行），`--json` 给机器。
- **只读**：不写任何文件、不调任何管理动作（签发/吊销仍走 kb_token.py 既有 CLI，不并入）。

### ③ cwk-ops-deploy skill 扩展（工作区侧）

- skill 增「库与授权盘点」节：叫我查时 SSH 跑 `kb_ops.py status` 回报；不改 skill 的部署/回滚既有内容。

## 非目标

- 不做网页管理界面（RT-052 原设想的「人读管理页」继续搁置；库/管理员规模未到）。
- 不在网关暴露 token 账目、挂载面全量、任何管理动词。
- 不动签发/吊销流程（kb_token.py 原样）。
- 不做跨网关聚合（就这一个网关）。

## 验收（脱敏行为断言）

- libraries：绑定 token 只见授权库（多 token 交叉验证）、admin 见全集、未挂载库不可见、词法代摘掉后 `lexical_modes` 如实降空、零写（WriteTrap）、no-store。
- kb_ops：输出含三库+全部 token 元数据+refresh 摘要；token 明文/全文指纹零出现（输出扫描断言）；--json 可解析；对缺失文件（如当日无 refresh 日志）优雅降级不炸。
- 既有回归全绿 + 双门禁 + CI。

## 风险与边界

- libraries 的每库词法可用性在 NAS 后端上各加一次读（轻量；不做每请求缓存，保持读面无状态姿态）。挂载库多时（未来）响应时间线性增长——当前 3 库可接受，记为已知边界。
- tokens.json 属管理面：kb_ops.py 仅在 OPS 本机运行（skill 经 SSH），不落任何网络面。
