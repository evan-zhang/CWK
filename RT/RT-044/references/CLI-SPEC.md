# KB CLI 规格（CLI-SPEC v1.1）

- 日期：2026-09-04（v1.1：与 DOCDB-INGEST-DESIGN v1.3 同步——lineage 寻址/doctor 层/route 参数）

- 日期：2026-09-04
- 定调：Evan 16:22——两个验证库都必须走「标准 CLI ← Skill」建库；CLI 是平台的操作面合同。
- 关系：CLI 与 Skill 同构（Skill=对话壳，每回合调一次 CLI）；CLI 与管理 API 同构（CLI=API 的壳）。参数名三处一致。

## 一、颗粒度三原则

1. **一动词 = 向导一个决策点**。Skill 对话的每个用户确认动作对应恰好一条 CLI 命令；不设计"一条命令干完三步"的组合动词（向导节奏由 Skill 控制），也不拆出比决策点更碎的命令。
2. **输出一律 JSON（默认）/ --pretty（人读）**。所有 ID 必须回显：kb_code、job_id、token_id、taxonomy version——Skill 靠它们续话。
3. **凭据永不进命令行**。Key 走 `--key-stdin` 或 `--key-env <VAR_NAME>`（传变量名不传值）；token 走 `--token-env <VAR_NAME>`。

## 二、命令清单（建库组 = 向导逐步）

| 向导步 | 命令 | 关键输入 | 输出（JSON） |
|---|---|---|---|
| 1 起名 | `kb draft create --name --type` | display_name | {kb_code, draft_expires_at} |
| 3 验 Key | `kb verify-key --kb <code> --key-stdin` | Key（stdin） | {owner_ref, owner_name, auth_handle} |
| 4A 配源 | `kb source set --kb <code> --source cwork --lanes inbox,outbox --window auto-3m [--route classify\|timeline]` | 窗口/通道/路由 | {filters_applied[], route_mode}（预留字段→exit 4） |
| 4B 配源 | `kb source set --kb <code> --source docdb --root <fileId> [--no-recursive] [--include-types] [--route classify\|timeline]` | 根目录 | {root_path, recursive, types, route_mode} |
| 4B 选目录 | `kb browse docdb --parent <fileId>` | 父目录 | {items[{id,name,type,child_count}]} |
| 4A/4B 预查 | `kb preview --kb <code>` | — | cwork: {count, window}; docdb: {file_count, type_histogram} |
| 4 拉取 | `kb ingest --kb <code>` | — | {job_id} |
| 进度 | `kb job status <job_id>` | — | {phase: collect|refine, done, total, eta} |
| 5 建议 | `kb taxonomy propose --kb <code>` | — | {dimensions[], entity_candidates[], topic_candidates[]} |
| 5 确认 | `kb taxonomy confirm --kb <code> [--file tax.json] [--focus-note ...]` | 定稿 | {version} |
| 6 频率 | `kb schedule set --kb <code> --daily@22:00 --tz Asia/Shanghai` | 频率 | {schedule} |
| 8 发token | `kb activate --kb <code>` | — | {token（一次性显示）, token_id, expires_at} |
| 补发 | `kb token issue --kb <code> --device <name>` | 设备名 | 同上 |

## 三、维护组 / 查询组（摘）

- 维护：`kb update --name` / `kb source patch` / `kb rotate-key` / `kb schedule get` / `kb doctor --kb <code> [--layer raw|manifest|collection-state|originals-changes|taxonomy|index|provenance|coverage] [--apply]`（index 层默认只读差异清单） / `kb rebuild --layer ...`（高危层需 `--authorize`）/ `kb token revoke|reissue|list` / `kb archive --mode freeze|purge` / `kb restore` / `kb member add|remove`（v2）/ `kb list` / `kb status`
- 查询（token 鉴权，打网关）：`kb query --token-env KB_TOKEN --q "..." [--kb <code>]` → {answer, citations[{lineage_id, version, quote}]}（**无 path 字段**，路径由 locate 即时解析）；`kb read --lineage <id> [--version N]` → 原文；`kb browse wiki --kb <code> [--category|--topic|--entity]` → 结构化目录树；`kb locate <lineage_id>` → 当前路径

## 四、退出码约定

0 成功 / 4 参数被拒（预留字段 400 语义）/ 5 鉴权失败（Key/token 无效或越权）/ 6 状态机拒绝（非法跃迁、draft 过期）/ 7 依赖不可用（NAS/源 5xx，含 retry 耗尽）

## 五、Skill 使用契约（cwk-kb）

- Skill 对话回合 = 一次 CLI 调用；Skill 解析 JSON 续话；错误码 4/5/6 转译为人话追问
- 两个验证库（Evan 工作协同库 + 投前流程库）必须全程走 Skill→CLI 建，RT-045 验收项
