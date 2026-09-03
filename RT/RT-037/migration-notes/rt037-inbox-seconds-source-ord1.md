# RT-037 ord1：cwk_backfill_range.py 源端切 3.1 收件箱（秒级）

- from_sha256（原 pin）：b859f456ce1beaab60c581df86c97830c99082535f8c0c7930593dd5e71519ce
- to_sha256（新 pin）：779f475321ea83956f85e50faf96993ec965c43c85dd65b4509eeeee9dc21eb3

## 行为差异

source_rows 默认数据源从 6.16 searchPage 改为 3.1 收件箱接口；时间窗从
毫秒改为 UTC+8 固定的秒级时间戳（上游文档禁止 13 位毫秒；2026-09-03 实测
同窗口毫秒 0 篇、秒级 38 篇）。`--source search-list` 完整保留旧实现作为
回退，旧行为零变化。CLI 其余参数、manifest 字段、退出码语义不变（manifest
仅新增 source_mode）。

## 回滚

`--source search-list` 即回到旧数据源（无需回滚代码）；需要完全恢复旧默认
时 revert 本提交即可。

## 风险控制

app_key 仅进程内传递（直连 client，不经 argv）；仅调用只读列表/详情接口，
不触碰已读、回复、审批类操作；raw/ 原文不修改。
