# PR-001 开发计划扩展整改记录（r1-plan）

> 日期：2026-08-19
> 依据：`开发计划审核-r1.md`
> 范围：仅修改 PR-001 文档，不修改代码、cron、Cloud、DocDB、生产镜像或 Git 历史。

## 1. 整改结论

第一轮文档整改形成的 8 个大 RT 仍把多项安全边界绑在一起，无法做到“一个实现 Agent + 一个独立验收 Agent”闭环。现已重拆为 RT-011～RT-026 共 16 个 RT，并增加 VG-A～VG-E 五个波次集成门禁。

本轮不改变 PRD 的目标架构，只改变开发颗粒度、依赖、代码所有权和量化指标冻结时点。

## 2. 旧计划到新计划映射

- 旧 RT-011A/011B → 新 RT-011（外部契约探针）+ RT-012（实例布局）+ RT-013（身份/凭据）。
- 旧 RT-012（租户运行时大包）→ 新 RT-012 + RT-013 + RT-015。
- 旧 RT-013（共享证据与迁移）→ 新 RT-014（共享不可变证据）+ RT-016（legacy 影子迁移）。
- 旧 RT-014（采集+ACL+调度）→ 新 RT-015（ACL/撤权）+ RT-017（Collector）+ RT-018（Scheduler）。
- 旧 RT-015（Profile）→ 新 RT-019（提案/确认）+ RT-020（Holdout/Router 激活凭据）。
- 旧 RT-016（Router+Projector）→ 新 RT-020 + RT-021。
- 旧 RT-017（Broker+沙箱传输）→ 新 RT-022（授权核心）+ RT-023（传输/客户端）。
- 旧 RT-018（运维+恢复+试点）→ 新 RT-024（审计/容量）+ RT-025（备份/恢复）+ RT-026（试点准备/回滚）。

## 3. 主要设计修正

1. **P95 冻结时点**：RT-011 只验证外部契约，不具备真实 Broker 性能；PRD 与 DESIGN 已统一为 RT-024 测量并冻结，RT-026 消费该基线做 go/no-go。
2. **Legacy 哈希语义**：`legacy_source_sha256` 与 `canonical_sha256` 分开保存，通过 crosswalk/原始字节对象对账，不宣称两种序列化哈希相等。
3. **可信身份与传输分层**：RT-013 只负责可信 `AgentContext` 和绑定；RT-022 只负责 Broker 授权核心；RT-023 才接 OpenClaw Tool/UDS 和真实沙箱。
4. **ACL 与清理分层**：RT-015 提供权威 Access Ledger、撤权和消费者契约；RT-021、RT-022 分别实现派生物清理和查询拒绝。
5. **Profile 与路由分层**：RT-019 生成、确认并版本化 proposal；RT-020 用独立 holdout 预览、产出路由决定并生成激活 receipt。
6. **试点不是开发 RT 的替代验收**：RT-026 只交付试点工具，2～3 人 14 天及 5～10 人 30 天是后续独立发布活动。

## 4. 五个波次门禁

- VG-A：Canonical + grant/view + allow/deny。
- VG-B：fake CWork + Collector + Scheduler 故障隔离。
- VG-C：Profile + Holdout/Route + Multi-space + 撤权清理。
- VG-D：真实沙箱身份 + Broker 双 ACL + SHA 证据回读。
- VG-E：Clean-room restore + allow/deny/query 与撤权一致性。

每个门禁只产生集成 receipt，不新建跨边界的“巨型 RT”。

## 5. 代码所有权调整

- `cwk_collect_live.py`：RT-017 独占。
- `cwk_nightly_pipeline.py`、安装入口、统一 feature flag：RT-026 独占。
- `cwk_entity_catalog.py`、`cwk_wiki_search_index.py`、Projector 适配：RT-021 独占。
- `cwk_wiki_query.py` 的 Broker/raw-loader 适配：RT-022 独占。
- RT-016/017 只能调用 RT-014/015 公共 writer，不互改其内部实现。

## 6. 外部未知与生产边界

下列事项允许在文档阶段保持 `conservative_unknown`，但在真实试点前必须关闭：report ID 全局性、双用户 reply/node/attachment 差异、权威撤权事件、Gateway 可信 Agent 身份、可用沙箱传输、Secret backend。未知时必须使用 tenant overlay 或拒绝服务，不得放宽权限。

本轮没有启用生产 tenant、没有迁移真实密钥、没有修改 cron、没有写 Cloud/DocDB，也没有开始 RT-011 编码。

## 7. 自检

> 本节最初记录的是 16-RT 草案的局部自检，随后第二轮审核发现三轴映射、迁移双哈希、RT-024/025 指标归属等仍有冲突。以下条目以 2026-08-19 第二轮整改后的现行文档为准；详细闭环见 `整改记录-r2.md`。

- 现行计划含 RT-011～RT-026 全部 16 个标题。
- 现行计划含 VG-A～VG-E 全部 5 个门禁。
- 每个 RT 均列出目标、依赖、输出/范围、非目标、验收门禁、回滚和独立 Agent。
- PRD/DESIGN 已把 P50/P95、容量、配额归 RT-024，把 RTO/RPO 归 RT-025；RT-026 仅消费两类基线。
- DESIGN §21 与开发计划已统一 16 RT、G/VG/M 三轴 crosswalk、依赖 DAG 和唯一代码所有权，不再把采集/ACL/调度或 Broker/传输绑在同一 RT。
- 迁移使用 `legacy_source_sha256`、`canonical_sha256` 与确定性 crosswalk，不再宣称不同序列化哈希相等。
- 现行文档已等待第三轮独立审核；在取得无 blocker 的 verdict 前，本自检不构成进入 RT-011 的批准。
