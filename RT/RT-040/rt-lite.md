# RT-040：采集完整性——发件箱双通道 + 回复动态刷新

## 决策

2026-09-03 22:17 Evan 确认整个 RT 现在做完（ord1+ord2 连续，默认版本方案
已批准）。ord1 解决「自发汇报采不进来」：RT-037 切到 3.1 收件箱后，收件箱
只覆盖「别人发给我」的汇报，本账号自己发出的汇报在源端总数里就是零——
夜里 `nightly-20260903-2230` 的 source 失败（11 个采集查询失败）与当日
完整性缺口正是这个盲区的实证。ord2 解决「回复变了但 raw 不知道」：汇报
发出后 replyCount 会增长，而 raw 只存首采快照，回复动态没有刷新通道。

## ord1 变更（scripts/cwk_backfill_range.py，script-evolution-v2 ord1）

- `--source` 新增 `dual`（新默认）：inbox 分页后再分页 outbox
  （client.get_outbox_list，同为秒级时间戳，同一 UTC+8 固定窗口），按
  report_id 去重后走现有 staging→promote；inbox-only 行为零变化，
  search-list 回退通道原样保留
- 新增 `_paged_list`（共享分页骨架）与 `outbox_source_rows`（outbox 行
  走与 inbox 完全相同的 enrich 秒级补齐与 total 完整性契约）
- dual 的 source_total = inbox_total + outbox_only 去重增量（服务端两个
  total 直接相加会把重叠行计两次，去重增量才是真总数）
- raw 落盘 source_scopes：dual 记 `inbox_range,outbox_range`
- nightly 调用方零改动：cwk_nightly_pipeline.py 走 CLI 默认值即获得
  双通道；cwk_source_coverage_audit.py 走函数默认值同样受益

## ord2 变更（scripts/cwk_reply_refresh.py，新增非封闭命名空间文件）

- `wiki/_system/reply-state.json` 基线：report_id → replyCount/hasNewReply
  快照 + checked_at
- 巡检（只读 API）：backfill 的 list 行 replyCount 与基线对比 → 变化的
  report 重拉详情（full-content-for-ai / record-simple-info / node-detail）
  → 写 `<id>-v2-<标题>.md` 新 raw（原文件一字不动；走直接写文件 + 索引
  注册通道，不进 promote 的按 id 去重）→ 触发该篇重编译 → 更新基线
- 首次运行自动建基线（无基线 = 全量建立，不触发重拉）；`--dry-run`
  只对比不写入

## 边界

- 只读查询：不触碰已读/回复/审批/删除类接口
- 原 raw 一字不动：ord2 的 v2 是新文件，promote 通道对既有 id 的
  unchanged 分支不触发任何重写
- 凭据只在内存传递（app_key 不进 argv 的 client 直连路径，与 RT-037 同）
- CC-2 不破：ci recipe 仍含 governance-audit；两车道结构未动
- cwk_backfill_range.py 不在 v2 高风险名单（模型清单/默认模型未动）

## 验证

- tests/test_rt040_outbox_dual_channel.py（dual 合并去重/outbox 分页
  完整性/秒级窗口契约/错误包裹/scopes 落盘/CLI 默认值，13 例）
- tests/test_rt040_reply_refresh.py（检测/v2 写入/原文件不动断言/基线
  更新/重编译触发/dry-run，12 例）
- make aodw-check + make governance-audit +
  pytest tests/test_governance_audit.py tests/test_rt040*.py 全绿
- 线上真跑（2026-09-03）：`--source dual --start-date 2026-09-03
  --end-date 2026-09-03` 补齐当日自发汇报；回复巡检只读跑通
