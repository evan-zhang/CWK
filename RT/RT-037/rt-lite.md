# RT-037：回填源切 3.1 收件箱（秒级时间戳）

## 决策

2026-09-03 拍板：`cwk_backfill_range.py` 的 source_rows 从 6.16 searchPage
（search-list，毫秒窗口）切到 3.1 收件箱接口。实测证据（2026-09-03 16:2x，
同窗口对照）：search-list 毫秒窗口 0 篇、inbox 秒级窗口 38 篇；上游文档明确
收件箱接口禁止传 13 位毫秒时间戳。完整性回填的源端总数自此以收件箱为准。

## 变更（scripts/cwk_backfill_range.py，script-evolution-v2 ord1）

- source_rows 默认走 inbox：懒加载 cms-cwork-workflow 的 CWorkClient 直连
  （app_key 只在进程内传递，不落 argv），beginTime/endTime 为 UTC+8 固定的
  秒级时间戳；epoch_seconds() 带量级护栏（10^9 ≤ v < 10^12，毫秒级即抛错）
- `--source inbox|search-list`（默认 inbox）：search-list 原实现整体保留为
  search_list_source_rows 回退通道，行为零变化
- inbox 行补齐：reportEventVO.time → 秒级 reportTime（enrich_inbox_row，
  不覆盖服务端字段）；writer / reportTime fallback 对齐 live collector，
  cwk_source_coverage_audit.py 不改而日桶继续可用
- 翻页完整性契约不变：去重后条数必须等于服务端 total，否则报错；
  manifest 新增 source_mode
- raw 落盘 source_scopes：inbox 通道记 inbox_range（search-list 保留
  date_range_search）

## 边界

- 只读查询：不触碰已读/回复/审批/删除类接口；不改 raw/ 原文
- 调用方零改动：cwk_source_coverage_audit.py 与 cwk_nightly_pipeline.py
  按默认参数即获得新数据源
- 不在 v2 高风险名单（模型清单/默认模型字面量一概未动）

## 验证

- tests/test_rt037_inbox_seconds_source.py（秒级换算金样例/量级护栏/UTC+8
  固定/分页聚合/跨页去重/total 校验/错误包裹/分发路由/行级 fallback，17 例）
- 线上只读探针（2026-09-03）：秒级窗口 total=3（当日）；inbox 行 id 与
  full-content-for-ai --report-record-id、node-detail --report-id 完全兼容
- make aodw-check + python3 -m pytest tests/test_governance_audit.py
