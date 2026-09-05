# RT-042 rt-lite：存储层 NAS 化 + 建库基座

- meta: RT-042 | in_progress | 2026-09-04 | owner 雷锋(BD-glm) | worker Claude Code
- 合同：docs/drafts/rt042-cloud-primary/{EXECUTION-PLAN,KB-PARAMETERS,SKILL-ARCHITECTURE}.md（v1.1+v1.2 补丁已含）
- 基线 commit：44d8fe4

## 1. 目标（一句话）

个人 KB 平台的存储地基：storage 抽象（本地 FS + NAS FileStation 双后端）、
建库 CLI（26 项目录树）、账本/游标/状态机数据模型、存量镜像迁移——全部纯标准库。

## 2. 范围（7 项）

1. `scripts/kb_storage.py`：StorageBackend 协议 + LocalFSBackend + FileStationBackend
   （create/write/read/list/mkdir/exists/sha256；FileStation 走 HTTPS API，凭据只从环境变量
   `CWK_NAS_KB_*` 读，禁硬编码禁命令行明文；重试 + 幂等）
2. `scripts/kb_create.py`：建库 CLI——display_name + 128 位随机 kb_code +
   26 项目录树生成（按 KB-PARAMETERS B 表逐项；cwork-only 项按源裁剪）+
   kb.json（身份+引用）/ source.json / schedule.json（子配置唯一权威）+
   kb_members 副本（OPS 集中索引 members-index 稍后 RT-043，先留接口）
3. `scripts/kb_ledger.py`：root-manifest 哈希账本（写后对账、全量 sha256 校验、
   断言存量不变）+ collection_state 游标模型 + 向导状态机数据模型
   （draft→verified→sourced→previewed→ingesting→taxonomy→active；draft TTL 30min）
4. `scripts/kb_migrate.py`：镜像→NAS 迁移（路径映射表 + 内容哈希双向配对 +
   允许新增/重命名清单；退役 4 目录 entities/events/history/_index 不迁）
5. `scripts/kb_doctor.py`（或并入 ledger）：verify 子命令族
   （--raw/--manifest/--collection-state/--changed-paths）
6. tests/test_kb_storage.py + test_kb_create.py + test_kb_ledger.py + test_kb_migrate.py
7. 新脚本回执 + 登记（**落点按仓库实况调整，理由如下**）
   - 回执：`RT/RT-042/receipts/new-script/*.json`（from=none），**不是**
     `receipts/script-evolution-v2/`。governance-audit 的 GA-V2-RECEIPT 会硬失败在
     「回执指向的 target 既不是续演槽位、也不是 legacy 成员」——v2 叠加层按设计只为
     v1 已登记且槽位用尽的存量文件续命（GA-V2-SLOT 同理），全新文件进不去。
     同一目录下 `README.md` 记录该判定与复验方法。
   - 登记：本仓库没有 `scripts/index.yaml` 这个文件。等价的两处真实登记是
     ①`.aodw-next/06-project/governance/code-ownership-manifest.json` 的
     `R-runtime-rt042-kb-platform`（exact_set，五个脚本逐条认领，受 governance-audit
     闭包判据约束）②`.aodw-next/06-project/modules-index.yaml` 的 `kb-platform-storage`
     模块条目。另在 `.env.example` 补 `CWK_NAS_KB_*` 四项（J7 操作入口）。

## 3. 非目标（防扩散）

- 不做采集/精编/管理 API/网关/token（RT-043/044）
- 不改既有脚本（scripts/ 封闭命名空间，触碰老脚本必须 legacy 路径，本 RT 禁止）
- 不做真实 NAS 大规模迁移执行（迁移工具就绪 + 小样本真验即可，全量迁移在 RT-045）

## 4. 成功判据（判据纪律：每条必须能红）

- J1 双后端一致性：同一操作序列跑 LocalFS 与 FakeFS（契约后端），排除时间戳类文件
  （log/audit/manifest 版本号），终态逐文件 sha256 零差异；排除集做结构等价断言
  【红法：故意让 FileStationBackend 写偏一个字节→diff 非空】
- J2 反空转：换 NoOpBackend（不真写）→ 全部写路径测试必须红
  【红法验证：临时注入 NoOp 应见 FAIL，不许静默跳过】
- J3 越权路径：`../`、绝对路径、symlink 逃逸写入必须拒绝（函数级断言）
- J4 账本锁死：写操作后 manifest 全量校验通过；人为改一个已存在文件内容→校验必须红
- J5 游标续采：模拟中断（写到一半抛异常）→ 重跑从游标续，无重复无丢失（确定性断言）
- J6 迁移对账：小样本（≥20 文件）双向配对零未配对；人为漏迁一个→未配对清单非空
- J7 真机冒烟：FileStationBackend 连真实 NAS（.env 凭据）建探针目录→写→读→sha256→删，
  全链路通过（本机执行，不进 CI；CI 无凭据自动跳过并标注 SKIP-reason）
- J8 状态机：draft TTL 过期后所有动词拒绝；非法跃迁拒绝

## 5. 收口门

- make aodw-check / make governance-audit / python3 -m pytest 全绿
- 新脚本全部 cwk_ 前缀 + index.yaml 登记 + 回执齐全
- CI（GitHub Actions 快车道）绿
- Codex 安全评审 + Grok 红队各一轮（针对 storage/账本/越权面），发现按复核-采纳流程
