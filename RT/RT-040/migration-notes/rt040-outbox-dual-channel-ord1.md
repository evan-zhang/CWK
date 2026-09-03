# RT-040 ord1：cwk_backfill_range.py 双通道（inbox + outbox）

- from_sha256（原 pin，RT-037 链尖）：779f475321ea83956f85e50faf96993ec965c43c85dd65b4509eeeee9dc21eb3
- to_sha256（新 pin）：4eb6ea8c603a289570e65ba131b410d0d9f1e3a9336ebcb5a5512840652799d2

## 行为差异

`--source` 新增 `dual` 并成为默认：inbox 分页完成后同样分页 outbox
（`client.get_outbox_list`，同一 UTC+8 固定的秒级时间窗），按 report_id
去重后进现有 staging→promote 流程。`--source inbox` 行为零变化（仅收
件箱），`--source search-list` 原样保留为回退通道。

新增 `_paged_list` 共享分页骨架与 `outbox_source_rows`：outbox 行走与
inbox 完全相同的 enrich（reportEventVO.time → 秒级 reportTime，不覆盖
服务端字段）与「去重后条数必须等于服务端 total」完整性契约；错误包裹为
`outbox page N failed`。dual 的 manifest source_total = inbox_total +
outbox_only 去重增量。raw 落盘 source_scopes 变为
`inbox_range,outbox_range`（仅 dual；inbox/search-list 通道的 scopes
不变）。CLI 其余参数、退出码语义不变。

nightly 调用方（cwk_nightly_pipeline.py 走 CLI、
cwk_source_coverage_audit.py 走函数默认）零改动获得双通道。

## 回滚

`--source inbox` 即回到 RT-037 行为（无需回滚代码）；需要完全恢复旧默认
时 revert 本提交即可。

## 风险控制

app_key 仅进程内传递（client 直连，不经 argv）；仅调用只读列表/详情接口，
不触碰已读、回复、审批类操作；promote 的按 id 去重保证已存在 raw 不会被
outbox 通道重写（unchanged 分支只做 timeline 幂等采集）。
