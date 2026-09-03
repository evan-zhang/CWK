# RT-041：源适配器契约与 CWork 包装

## 决策

2026-09-03 23:20 Evan 立项（排在 RT-040 之后）。目标：采集层解耦——任何
数据源只要实现本契约即可接入镜像；下游（promote / compile / refine /
query）零改动。设计稿（Evan 案头版）存
`docs/drafts/rt041-source-adapter/CONTRACT.md`，本 RT 将其定稿入仓为
`docs/ADAPTER-CONTRACT.md`。

## ord1 做了什么

- **契约入仓**：`docs/ADAPTER-CONTRACT.md`（v1）——四接口
  （discover/fetch/dedupe_key/watch）、NormalizedDoc（统一出口 = 现有
  raw frontmatter 契约）、红线四条（raw 只增不改、下游零改动、凭据
  每源独立、精编只看 participants）、实施顺序（ord2 DocDB 待拍板）
- **`adapters/base.py`**：SourceItem / NormalizedDoc dataclass +
  SourceAdapter Protocol + register/get_adapter 注册表（懒导入，
  CI 无需凭据/网络）
- **`adapters/gwork.py`（1 号适配器，包装式）**：
  - discover → `cwk_backfill_range.source_rows(source="dual")`（RT-040
    ord1 双通道默认）
  - fetch → `cwk_backfill_range.fetch_one`（与 nightly 同一条落盘
    代码，产物字节一致）
  - dedupe_key → `gwork-<report_id>`（带源前缀，跨源永不撞号）
  - watch → inbox+outbox 行 + `reply_refresh.detect_changes`（RT-040
    ord2 基线机制的泛化；fresh 首见行一并返回）
  - participants = writer frontmatter + 角色行（与编译器
    refine-scope 同一角色词表；正文自由文本永不匹配）
- **治理**：code-ownership-manifest 新增 `R-runtime-adapters` 前缀规则
  （owner RT-041）；tests/test_governance_audit.py 合成基线补
  adapters/ 与 docs/ADAPTER-CONTRACT.md 代表文件
- **RT 登记**：RT/index.yaml + RT/RT-041/{rt-lite.md, meta.yaml}

## 边界

- scripts/ 封闭命名空间零改动（包装式：只 import，不修改）
- ord1 落盘通道不变：gwork 走现有 staging→promote，raw 文件名/目录
  契约保持现状（frontmatter report_id 不带前缀）；带前缀的
  NormalizedDoc.id 存在于 API 层。`raw/<source>/<YYYY-MM>/` 布局迁移
  属后续演进，须另立 RT
- 凭据只在内存（app_key 走调用参数，不落盘不回显）；对工作协同只读
- CC-2 不破：ci recipe 未动

## 验证

- tests/test_rt041_gwork_adapter.py 23 例全绿：四接口可用、dedupe_key
  带前缀、注册表、participants 角色行语义（正文不算）、等价性两支柱
  （同一测试窗口内适配器采出 id 集合 = 现有 dual source_rows 采出 id
  集合；fetch 产物与 fetch_one 落盘文件字节一致）+ 治理断言
- make governance-audit 通过（652 个受跟踪文件全归属，+5）
- tests/test_governance_audit.py 62+2 例全绿
- make aodw-check / make ci（快车道）见推送后 CI

## 下一步（ord2 待拍板）

DocDB 2 号适配器（cms-docdb skill + appKey 现成）。落盘布局迁移
（raw/<source>/ 子目录）与 watch 接入 nightly 也列在候选。
