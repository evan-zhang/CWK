# RT-052：知识库发现端点 + OPS 盘点面

> Spec-Lite v2（2026-09-07）。Evan 授权立项 + 独立 Codex 评审门。
> v1 经 Codex 评审 GO-WITH-CHANGES（[codex-review.md](codex-review.md)），本稿并入全部 16 条必须改。

## 背景与问题

RT-051 后多 Agent 接入成为现实（docs/cwk-kb-access-setup.md）。缺口：
1. 接入方无法发现自己有什么——库清单写死在接入文档里，库一变就过期。
2. 管理员全景盘点靠 SSH 手拼三处数据（挂载面 / tokens.json / 各库 _system）。

## ① 网关发现端点 `GET /v2/kb/libraries`（无目标库请求）

### 鉴权流（独立于既有 per-kb 分发）

现有分发把不带 `kb` 的请求按主库鉴权——本端点必须有独立路径：

- **admin token**：直接返回全部挂载库，**不读登记表**。
- **绑定 token**：**单次**读取登记表扫描该 token 记录（有效性 + 完整 kb_ids），再与 `self.mounts` 求交集。**禁止逐库 `decide()`**（重复读登记表、竞态、与挂载数相关的侧信道）。
- 网关未配登记表（admin-only 网关）→ 401。

### 状态码

- token 未知/过期/吊销、登记表不可读 → **401**（与既有 unauthorized 形状一致）。
- token 有效但授权域∩挂载面为空 → **200 + `libraries: []`**（无目标库请求不存在「越权目标」，不套 403）。

### 每库字段（固定语义）

| 字段 | 语义 |
|---|---|
| `kb_id` | 挂载键（NAS prefix） |
| `display_name` | 权威源 `kb.json.display_name`；读取失败 → 回落 `kb_id`，`display_source: "kb_id"\|"kb.json"` |
| `total` | raw-index 条目总数 |
| `readable_total` | 复用现有 `_v2_readable` 判定；placeholder 计入（与 list 行为一致，`reason` 判定不变） |
| `lexical_status` | `ready / stale / missing / corrupt / unknown`——**不复用 lexical_modes**（那是程序能力词表，语义不同） |
| `error` | 可选，脱敏状态码（如 `source_unavailable`），无错为 null |

顶层：`schema: cwk.kb.libraries.v2`、`gateway_version`（一次，不每库重复）、`complete: true|false`、`libraries[]` 按 `kb_id` 稳定排序、`Cache-Control: no-store`。

### 词法可用性不加载全量索引（评审第 5 条）

`_v2_load_lexical` 会全量下载词法索引（15MB+），单线程网关上三库串行会阻塞所有查询。改为 **builder 原子发布的小型 readiness 投影**：

- builder `publish` 时增写 `_system/lexical-readiness.json`（仅 `generation / corpus_digest / engine / coverage_complete / excluded_counts`，KB 级小文件）。
- 网关 libraries 端点只读 raw-index + readiness 小文件；用 corpus_digest 与当前 raw-index 资格域投影比对得 ready/stale。
- readiness 文件缺失（旧代已发布但 builder 未升级）→ `lexical_status: "unknown"`，不触发读全量。
- 兼容升级：对既有已发布库，重跑 builder publish（同 generation）补写 readiness；publish 增量幂等。

### 授权域先过滤、后读 NAS

绑定 token 请求只对**交集内**的库做任何 I/O（raw-index / kb.json / readiness）。隐藏库的 backend 一律不碰——测试用「隐藏 backend 被读即抛错」的 trap 证明。

### 单库失败聚合

某挂载库 raw-index 读失败：该库行保留，`total/readable_total/lexical_status = null`，`error: "source_unavailable"`，顶层 `complete: false`；不回 NAS 路径或原始异常，其余库照常。

### 发现链路补齐（不是只加一个 handler）

- `GATEWAY_VERSION` → **1.3.0**；capabilities `supported_operations` 与启动路由卡同步声明 `libraries`。
- `kb_gateway_client.py`：EXPECTED_SCHEMAS 增 `libraries`（`cwk.kb.libraries.v2`）。
- `kb_wizard.py`：**无 `--kb` 的 targetless `libraries` 动词**。
- `skills/cwk-kb-query/SKILL.md` + `docs/cwk-kb-access-setup.md` 阶段 B 增第 0 步「先 libraries 发现授权库，再选库查询」——摆脱写死清单这才算交付。

## ② OPS 盘点脚本 `scripts/kb_ops.py`（只读）

### `kb_ops.py status [--json] [--kb <id>]...`

- **库清单不是第二份硬编码事实源**：默认盘点全部挂载库（`--kb` 可重复指定子集）；脚本支持 `--mounts <a,b,c>` 显式声明预期挂载面并做差异检查——「已挂载未盘点」「配置存在但未挂载」都显式告警。
- 每库：total / readable_total / 词法代 `generation` 尾 8 + `up_to_date` 判定（读 readiness 投影；缺失标 `unknown`）。
- **token 授权账目**（读 `~/CWK/ops/tokens.json`，直接读取按白名单投影，不经 kb_token list）：
  - 字段白名单：`token_id_suffix`（**尾 8**）、`agent_binding_id`（HMAC 派生不可逆标识——登记表本就不存原始 agent-id，如实标注）、`kb_ids`、`created_at`、`expires_at`、`remaining_days`、`status`（active/expired/revoked）。
  - 明确禁止出现：`token_sha256` 全文、`owner_ref`、`owner_ref_salt`、`identity_probe`、receipts、`actor/reason`、登记表原文；**异常路径同样做递归泄漏扫描**（测试断言）。
- **夜间 refresh 摘要**：按文件名中的合法日期取**最新**一份 `kb-refresh-<date>.json`（不假设当天）；只投影每库 `ok`、运行时间；无任何日志 → `never_run`；只有旧日志 → 最后日期 + `stale`；最新文件损坏 → `error`（不炸）。
- **降级矩阵与退出码**：tokens.json 缺失/不可读/schema 坏 → `registry_unavailable` **非零退出**（绝不冒充 0 token）；单库 raw-index 坏 → 该库标错、继续他库、命令仍可成功（exit 0 + complete:false 语义）；词法 missing/stale/corrupt/unknown 分开报；active/expired/revoked 是账目状态不使命令失败。退出码合同同仓库惯例 0/1/2。
- **只读可证伪**：WriteTrap backend 验证（文本与 --json × 成功与损坏输入四象限）；执行前后比对 tokens/log/fixture 内容、mtime、目录树；静态检查禁止 `write/mkdir/remove/save_registry`/管理子进程/shell 调用。

## ③ cwk-ops-deploy skill 扩展（独立部署步骤）

工作区侧 skill 增「库与授权盘点」节：SSH 跑 `kb_ops.py status` 回报。**此更新是独立部署步骤与验收项**，不在 feature 分支内冒充已交付。

## 非目标

不做网页管理界面；网关不暴露 token 账目/挂载面全集/管理动词；不动签发吊销 CLI；不做跨网关聚合。

## 验收（脱敏行为断言）

- libraries：admin 全集不读登记表；绑定 token 单读登记表取 kb_ids∩mounts；空交集 200+[]；无效 token 401；隐藏库 backend 零 I/O（trap）；词法 readiness 缺失 → unknown 而非全量下载（I/O 计量断言）；单库坏 → 行保留 null 计数 + complete:false；WriteTrap 零写；no-store；capabilities/客户端/wizard 链路全声明。
- kb_ops：白名单字段外零出现（含异常路径递归扫描）；registry 坏 → 非零退出；refresh 降级矩阵四态；WriteTrap 四象限；--mounts 差异告警；--json 可解析。
- 既有回归全绿 + 双门禁 + CI（governance：kb_ops.py **创建时**登记 ownership——文件创建前登记触发 GA-STALE-RULE，评审已验证）。

## 风险与边界（含可执行阈值）

- 当前 3 库：记录 libraries 端点 NAS 请求数（每库 ≤3 次小读）/下载字节/总耗时进验收工件；**阈值**：挂载库 >10 或端点 P95 >2s 时改分页或快照，不在单线程网关内并发复用 FileStation session 掩盖线性增长。
- readiness 投影随 builder publish 演进；旧库补一次幂等 publish。
