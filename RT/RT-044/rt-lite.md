# RT-044 建库向导 + 查询网关 v1（MVP）

- 目标：① kb CLI 向导动词（create/ingest/status/query）包装 kb_create + kb_ingest + 查询；② 只读网关进程（HTTP JSON）。
- 上游合同：references/CLI-SPEC.md v1.1、KB-PARAMETERS.md。
- MVP 裁剪：向导=参数式（--yes 非交互 + JSON 确认卡输出）；对话式 cwk-kb Skill 包装层在 RT-045 验收时提供。网关 v1 单进程单端口；鉴权=管理 Key 派生 token（SHA-256 + 常时比较）；只读动词；NAS 专用只读账号 _kbquery 列 v1.x（今日现账号 + 网关侧禁写兜底）。
- 交付：scripts/kb_wizard.py、scripts/kb_gateway.py + tests/test_kb_wizard.py、tests/test_kb_gateway.py。
- 判据（负例红）：
  - J1 向导目的地脏必拒（复用 kb_create 预检），零写入。
  - J2 网关写语义/未知动词一律 405/404；只暴露查询与引文。
  - J3 引文实时性：citation 动词现场从 NAS 拉字节，返回 sha256+节选，不读本地缓存。
  - J4 token 错→401；token 只能查，拿不到任何管理面。
  - J5 向导输出一律 JSON（CLI-SPEC 合同）。
- 红线：两进程宪法——factory 写面（向导/摄取）与 gateway 读面严格分离；网关进程不实现任何写动词。

## 实现记录（2026-09-05）

交付：`scripts/kb_wizard.py`（写面）、`scripts/kb_gateway.py`（读面）、
`tests/test_kb_wizard.py`、`tests/test_kb_gateway.py`。纯标准库，离线 fake 全覆盖。

判据落点：J1 `J1DirtyDestinationTests`（含快照比对的红法）、J2/J3/J4/J5 见
`tests/test_kb_gateway.py` 与 `tests/test_kb_wizard.py` 同名类。

实现期做的判断，逐条记理由：

- **两进程宪法落到 import 图上**：查询语义只有一份实现（`kb_gateway.query_index`），
  由向导 import 网关，方向单向。网关不 import `kb_create/kb_wizard/kb_migrate`，
  由测试解析源码断言；再加一个写陷阱后端跑遍所有路由，「不写」是被观测到的，不是被声明的。
- **`--yes` 是写闸**：不给 `--yes` 时 `create`/`ingest` 只出确认卡、零副作用。
  MVP 是参数式向导，这一步就是对话里「确认吗」那一回合的落点。
- **退出码用 0/1/2**，和 RT-042 的 `kb_*` 家族一致；CLI-SPEC §四 的 4/5/6/7 是管理 API
  的码位，本 RT 不建那个面，语义类别改走 JSON 体的 `error.kind`，Skill 层照样能转译。
- **route 缺省按源提议**（cwork=timeline、docdb=classify），确认卡显式回显，
  `--route-mode` 可覆盖——DOCDB-INGEST-DESIGN v1.3 明确不写平台级默认。
- **摄取脚本用 `KB_INGEST_BIN` 定位**：RT-043 并行开发，本分支可能没有
  `scripts/kb_ingest.py`；找不到就给干净 JSON 错误并 exit 2，不崩。子进程命令行集中在
  `ingest_argv()` 一个函数里，RT-043 的 flag 名若与预期不同，只改这一处。
- **`/health` 不带身份**：它是唯一免鉴权的面，回 kb_code（128 位防枚举）或库名等于
  把防枚举白做了，因此只回版本 + 后端可达性，并由测试断言不泄漏。
- **引文不带 path**：CLI-SPEC §三 规定引文钉 `(lineage_id, version)`，路径只是缓存。
  返回的 `sha256` 一律由本次读到的字节现算，并与索引记录的摘要对比给出 `matches_index`，
  raw 被手工改过时这里就会露出来。
- **绑定时不反查 DNS**：`HTTPServer.server_bind` 会调 `socket.getfqdn()` 填一个只有 CGI
  会读的字段；本机反查超时让启动卡了 35 秒。`KbGatewayServer` 跳过它，配回归测试。

治理登记：`code-ownership-manifest.json` 的 `R-runtime-rt044-kb-wizard-gateway`
（exact_set，scripts/ 是 exact-only 区）、`modules-index.yaml` 的 `kb-wizard-gateway`、
`tests/test_governance_audit.py` 的 `_REPRESENTATIVE_TRACKED`。

遗留（不在本 RT 范围）：NAS 专用只读账号 `_kbquery` 列 v1.x，v1 用现账号 + 网关侧禁写兜底；
对话式 `cwk-kb` Skill 包装层在 RT-045 交付。
