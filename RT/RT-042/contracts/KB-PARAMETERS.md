# AI 知识库参数全集（KB-PARAMETERS v1.1）

- 日期：2026-09-04（v1.1：三引擎评审修复，仅采纳经复核确认项；不采纳项见文末）
- 原则：能提炼成参数的全部提炼；安全红线明确列为「不可参数化」
- 同构：参数名 = CLI flag = API 字段（三入口一致）

## A. 建库参数

### A1 库身份组

| 参数 | 必填 | 缺省 | CLI | API |
|---|---|---|---|---|
| display_name | ✅ | — | `--name` | POST /api/kb/draft |
| kb_type | 🔶 | personal | `--type` | 同上 |
| kb_code | ❌ | 系统生成（**128 位随机**，防枚举） | — | 系统生成 |
| owner_ref | ❌ | Key 派生（稳定工号标识；**username 一词废弃**） | — | verify-key 派生 |
| visibility | 🔶 | private | `--visibility` | PATCH |
| draft_ttl | ❌ | 30min | — | 系统常量（草稿鉴权前仅驻内存） |

### A2 源配置组（数组；v1 支持 cwork + docdb）

**cwork：**

| 参数 | 必填 | 缺省 | CLI | API |
|---|---|---|---|---|
| source_type=cwork | ✅ | — | `--source cwork` | PATCH /sources |
| key_ref | ✅ | — | `.env` 引用（**禁命令行明文**） | verify-key 内存通道 |
| lanes | 🔶 | [inbox,outbox] | `--lanes` | 同上 |
| window.mode | 🔶 | auto-3m | `--window` | 同上 |
| window.start / window.end | 🔶 | — | `--start-date --end-date` | 同上 |
| keywords / senders / recipients / project_tags / min_relevance | **v1 拒收** | — | （v2 开放） | **收到即 400，不落盘** |

**docdb：**

| 参数 | 必填 | 缺省 | CLI | API |
|---|---|---|---|---|
| source_type=docdb | ✅ | — | `--source docdb` | PATCH /sources |
| key_ref | ✅ | 复用同一把 | 复用 | 复用 |
| root_folder | ✅ | — | `--docdb-root` | 同上 |
| recursive | 🔶 | true | `--no-recursive` | 同上 |
| include_types | 🔶 | 全部 | `--include-types` | 同上 |

### A3 调度组

| 参数 | 必填 | 缺省 | CLI | API |
|---|---|---|---|---|
| fetch.frequency | 🔶 | daily | `--schedule daily@22:00\|every6h\|weekly@Mon` | PATCH /schedule |
| fetch.timezone | 🔶 | Asia/Shanghai | `--tz` | 同上 |
| fetch.reply_refresh（仅 cwork 有意义） | 🔶 | true | `--no-reply-refresh` | 同上 |
| refine.batch_size | ❌ | 平台值 | `--refine-batch` | 管理员 |

**调度模型**：单 launchd 心跳（30 分钟）+ 工厂按 schedule.json 判到期（不按库生成 launchd）。

### A4 分类体系组

| 参数 | 必填 | 缺省 | CLI | API |
|---|---|---|---|---|
| dimensions[].name | ✅ | [内容类型,项目] | `--taxonomy-file` | POST /taxonomy/propose→confirm |
| dimensions[].values | 🔶 | 动态枚举 | 同上 | 同上 |
| dimensions[].dynamic | 🔶 | — | 同上 | 同上 |
| entity_candidates[] / topic_candidates[] | ❌ | AI 提议 | （确认制，非手设） | propose 载荷，confirm 确认 |
| focus_note | ❌ | 空 | `--focus-note` | PATCH /taxonomy |
| version | ❌ | 1（**focus_note 变更也 bump**） | — | 系统 |

### A5 精编组（管理员）

refine_model / compounding_scope（默认 15）/ lang —— 同 v1.0。

### A6 token 组（v1.1 新增）

| 参数 | 缺省 | 说明 |
|---|---|---|
| token.ttl | 90d | 过期自动失效 |
| token.scope | query-only | **token 调管理 API 一律 403** |
| token.max_active | 3 | 每设备独立 token，上限可调 |
| token.generation | 系统 | reissue 原子递增，旧代全失效 |

### A7 告警组（v1.1 新增）

| 参数 | 缺省 | 说明 |
|---|---|---|
| alert.channel | discord-webhook | 告警渠道 |
| alert.target | 平台管理员 | 接收者 |
| alert.on[] | [nightly_fail, manifest_mismatch, audit_gap] | 触发事件 |

## B. 库目录树规范（23 + 3 = **26 项**；#2b/#24/#25 为 v1.1 新增；含「适用源」标注，纯 docdb 库对 cwork-only 项豁免）

| # | 文件/目录 | 建库 | 更新 | 维护命令 | 适用源 |
|---|---|---|---|---|---|
| 1 | kb.json | ✅（只存 A1 身份+子配置引用；**子配置为唯一权威**） | 引用变更时 | patch | 通用 |
| 2 | raw/YYYY-MM/*.md（cwork） | 初灌 | 新增收发件箱追加 | `rebuild --raw`（需授权） | cwork |
| 2b | **raw/docdb/<fileId>@<rev>.md**（v1.1 新增） | 初灌 | 变更检测（mtime/hash/revision）追加新 rev | `rebuild --docdb-raw`（需授权） | docdb |
| 3 | raw/_system/raw-manifest.json | ✅ | 每次写 raw 后 | `verify --raw` | 通用 |
| 4 | timelines/{id}/snapshots/ | 初灌基线 | 新回复追加快照 | `verify --timelines` | cwork |
| 5 | wiki/AGENTS.md | ✅ | taxonomy **或 focus_note** 变更时重生成 | `rebuild --schema` | 通用 |
| 6 | wiki/index.md | ✅ | 每页增改同步 | `rebuild --index` | 通用 |
| 7 | wiki/log.md | ✅开账 | 每次运行追加（幂等断言豁免项） | —（只追加） | 通用 |
| 8 | wiki/summaries/{id}.md | 初灌 | 新内容 + 回复变化重编译 + **taxonomy 变更全量重打标** | `rebuild --summaries` | 通用（docdb 键=<fileId>） |
| 9–10 | wiki/entities/ + topics/ | 初灌 | 复利更新 | `rebuild --entities/--topics` | 通用 |
| 11 | wiki/categories/ | taxonomy 定稿后 | 打标变化更新 | `rebuild --categories` | 通用 |
| 12–13 | wiki/daily/ + sources/ | ✅ | 每晚/分卷增长 | `rebuild --daily/--sources` | 通用 |
| 14 | _system/taxonomy.json | 默认版 | confirm/patch 时 version+1 | patch | 通用 |
| 15–17 | entity-catalog / search-index / query-contract | ✅ | **contract 含 contract_version，平台可统一升级（不接受定制≠不接受升级）** | `rebuild --search` 等 | 通用 |
| 18 | reply-state.json | ✅ | v2 刷新（指向当前权威 snapshot） | `verify --reply-state` | cwork |
| 19 | root-manifest.json | ✅ | 每写操作后对账 | `verify --manifest` | 通用 |
| 20–21 | source.json / schedule.json | ✅ | patch 时（**唯一权威**） | patch | 通用 |
| 22 | audit.jsonl | ✅开账 | **由工厂审计接收器唯一写入**（哈希链） | `audit --query` | 通用 |
| 23 | kb_members.json | ✅ | 成员变更（OPS 集中索引为权威，此为审计副本） | `member add/remove` | 通用 |
| 24 | **_system/collection_state.json**（v1.1 新增） | ✅ | 每轮采集后（**每源每 lane 游标+断点**） | `verify --collection-state` | 通用 |
| 25 | **_system/changed_paths_manifest.json**（v1.1 新增） | ✅ | 增量变更记录 | `verify --changed-paths` | 通用 |

**summaries 冻结模型（v1.1）**：raw 永不改；回复只追加 snapshot；reply-state 指权威；summaries frontmatter 声明 raw_ref + snapshot_id；查询回读权威 snapshot；doctor 三方一致。

### B2 缓存（无参数）+ B3 OPS 运维产物

- B2 不变（4 项缓存）
- B3（v1.1 补 2 项 = 6 项）：runs/、logs/、tokens.json（**只存 HMAC 摘要**）、**secrets/<kb>.enc**（用户 Key 加密存储：AES-256-GCM；主密钥存 OPS macOS Keychain；rotate-key 原地重加密）、**audit-receiver/**（网关事件落盘+哈希链）、备份索引

## C. 维护动词（v1.2：14→18，补 `kb preview` / `kb ingest` / `kb taxonomy propose|confirm` / `kb list`）

原 11 个 + 新增 `verify`（各账本体检）、`audit --query`、`restore`（归档恢复）。
高危动作定义：`rebuild --raw/--docdb-raw`、`archive --mode purge` 需**离线审批对象**
（工单 id + 文件清单哈希先落盘再执行）+ `--authorize <admin>`。

## D. 查询参数

token（host 侧配置槽）/ ops_endpoint（常量）/ query.q / query.scope（v2）。

## E. 三入口同构

A 组每参数均有 CLI + API 两列（v1.1 已补齐）；C 表动词与 SKILL-ARCHITECTURE 端点一一对应；
实现前做一次交叉矩阵自检（空格 = 未闭合，禁止开工）。

## F. 不可参数化红线（7 条不变 + 措辞精确化）

1. raw 只增不改（授权清理除外：工单+清单哈希先落盘）
2. owner 结构化字段不进精编 prompt（focus_note 为用户自述文本，允许）
3. 拒答纪律 4. 单写者（工厂写；审计接收器是唯一例外入口，属工厂进程）
5. NAS 凭据不出 OPS（**「不落盘」= 不落 Agent 盘/不进 NAS 库/不进聊天记录；OPS 加密存储除外**）
6. 审计追加式（哈希链防篡改）7. query-contract 模板固定（含版本号可升级）

## G. 参数总量（v1.1 重算）

- A 组：身份 6 + cwork 8 + docdb 5 + 调度 4 + taxonomy 6 + 精编 3 + **token 4 + 告警 3 = 39 项**
  （v1 生效 28 项；预留 5 项且**拒收**；系统生成/派生 6 项）
- B 表 26 项 + B2 缓存 4 + B3 运维 8 = 38 项落盘物闭合（B3 补 alerts.json、members-index.json=OPS 集中 kb_members 权威索引、audit-anchor/ 每日链头哈希）
- C 动词 18 个（v1.2 补 preview/ingest/propose/list——CLI 续跑判据全链路）；F 红线 7 条

## 附：复核后不采纳（留档）

- OAuth 授权替代 Key（v2 考虑，内网 v1 成本不匹配）
- mTLS（v2；v1 用 TLS+证书指纹固定）
- display_name 禁入关联表（已折中：关联表存 owner_ref，display_name 仅 kb.json）
- 缩窗查询层窗口（v1 语义定死：缩窗只影响未来采集，查询可见全部 raw，手册明示）


### A6/A7 落盘归属与列补齐（v1.2）

- A6 token 组 → OPS `tokens.json`（B3，只存 HMAC 摘要）；A7 告警组 → OPS `alerts.json`（B3）。
- A5/A6/A7 的 CLI/API 列语义同 A1–A4（管理员组经 `kb admin set` 与 `PATCH /api/kb/{id}/admin`）；
  实现前交叉矩阵自检覆盖全部 39 项（任一格空 = 未闭合，禁止开工）。
