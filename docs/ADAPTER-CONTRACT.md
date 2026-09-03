# 源适配器契约（v1，RT-041）

> 2026-09-03 23:20 Evan 立项；设计稿存 `docs/drafts/rt041-source-adapter/CONTRACT.md`，本文件是入仓定稿版。
> 接口实现与注册表在 `adapters/base.py`；1 号适配器（CWork 包装式）在 `adapters/gwork.py`。

## 目标

采集层解耦：任何数据源只要实现本契约即可接入镜像；下游（promote / compile / refine / query）零改动。

## 统一接口（每个源一个适配器，目录 `adapters/`）

- `discover(app_key, start_date, end_date) -> [SourceItem]`：增量发现，游标由适配器自管（CWork=日期分页；DocDB=目录+修改时间；Gmail=historyId；Drive=修改时间）
- `fetch(item, app_key) -> NormalizedDoc`：拉取并归一化
- `dedupe_key(item) -> str`：全局唯一键 = `<源前缀>-<原ID>`（`gwork-` / `docdb-` / `gmail-` / `drive-`），跨源永不撞号
- `watch(app_key, baseline, start_date, end_date) -> ([SourceItem], fresh)`：变化检测——RT-040 ord2 reply-state 机制的泛化，每源自选指纹（回复数 / 修改时间 / 版本号）。首见行（fresh）一并返回供调用方回填基线

凭据按调用传参（`app_key` 只在内存中经过适配器，不落盘、不回显、不进日志）；适配器实例本身不保存凭据。

## NormalizedDoc（统一出口 = 现有 raw frontmatter 契约）

`id（带前缀）/ native_id / title / author / participants（相关者列表）/ created / source_type / body_markdown`

`body_markdown` 是完整 raw 文件文本（frontmatter + 正文），与现有 `write_markdown` 产物同构——下游只认这份文本，不感知源。

## ord1 实施注记（CWork 包装式）

- **1 号适配器 `adapters/gwork.py` 是包装**：`from cwk_backfill_range import source_rows / fetch_one`、`from reply_refresh import detect_changes`，**scripts/ 封闭命名空间零改动**，行为零变化。等价性由 `tests/test_rt041_gwork_adapter.py` 兜底：同一测试窗口内适配器采出的 id 集合 = 现有 backfill 采出的 id 集合；`fetch` 产物与现有 `fetch_one` 落盘文件字节一致。
- **落盘通道不变**：ord1 的 gwork 走现有 staging→promote 通道，raw 文件名/目录保持现有契约（frontmatter `report_id` 不带前缀）。带前缀的 `NormalizedDoc.id` 存在于适配器 API 层，用于跨源去重与未来多源索引；落盘布局迁移到 `raw/<source>/<YYYY-MM>/<日期>/` 属后续演进，须另立 RT 评审（涉及 promote 与既有 1500+ raw 的兼容策略）。
- **participants 语义**（按源由适配器吐出）：CWork = `writer` frontmatter + 角色行（汇报人/收件人/抄送…，与编译器 refine-scope 同一角色词表，只认带角色标签的行，正文自由文本永不匹配）；邮件 = 收发件人；文件 = 共享者。

## 不变量（红线，全源适用）

1. raw 只增不改；变化一律 `-v2` 副本 + 重编译（复用 RT-040 ord2 机制）
2. 下游管线零改动；适配器不得绕过统一落盘（promote 通道）
3. 每源凭据独立（.env 键前缀 = 源名），永不回显
4. 精编相关性只看 `participants`

## 实施顺序

- **ord1**（本 RT）：契约定稿 + `adapters/` 目录与治理认领 + CWork 包装为 1 号适配器（等价测试兜底）
- **ord2**：DocDB 2 号适配器（cms-docdb skill + appKey 现成）——待拍板
- **ord3+**：Gmail / Google Drive（前置：Google OAuth 应用，摩擦在配置不在代码）

## 治理

`adapters/` 由 code-ownership-manifest 的 `R-runtime-adapters` 前缀规则认领（owner RT-041）。前缀而非 exact：新增适配器是被鼓励的扩展行为，与 `tests/` 同理；每个适配器必须带 `tests/test_<rt>_*_adapter.py` 或同等级测试。scripts/ 封闭命名空间不碰（包装式接入）。
