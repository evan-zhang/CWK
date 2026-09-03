# RT-040 收口复盘

## 结果

两个 ordinal 全部落地，main 绿（a98ba80 CI success）。

- ord1：`--source dual`（inbox + outbox 同秒级窗口、report_id 去重）成为
  backfill 默认；自发汇报盲区关闭。legacy 成员 cwk_backfill_range.py 第二次
  演化（v2 回执 ord2 链：779f475→4eb6ea8）。
- ord2：`scripts/reply_refresh.py`（移出 cwk_ 封闭命名空间）——reply-state
  基线对比 → 重拉详情 → `<id>-v2-标题.md` 新 raw（原文件一字不动）→
  重编译触发；附编译器 `_version_of` tie-break（高风险成员点名演化，
  27932db→472e9fc），修复「重编译永远选中原快照」缺陷（排序使
  `-v2-` 排在 CJK 前，last-wins 落在原文上；线上实测复现并修复验证）。

## 实证

- 今日（2026-09-03）真实补齐：4 篇全采（此前 0）；8-27/9-1/9-2 追补
  4 篇（其中 2 篇是 live collector 漏采、timeline 有快照但 raw 缺失的），
  raw 1539 → 1545。
- outbox 通道线上验证：2026-08-27 窗口 outbox-only 1 篇（自发汇报），
  dual 合并 total = inbox 7 + 增量 1 = 8。
- 回复刷新线上 E2E：统计月报（replyCount 0→1）v2 快照落地、原文件
  sha256 前后一致、manifest 双注册、编译器选择验证指向 v2。
- 门禁：aodw-check / governance-audit / rt-guard RT-040 / make test
  （1666 tests）全绿；CI a98ba80 success。

## 教训

- 本地 make test 失败两次都是环境性（/tmp 符号链接 + TMPDIR 长度 +
  CWORK_APP_KEY 残留），Makefile 注释里早写了正确跑法
  （cd /private/tmp/... && env -u CWORK_APP_KEY）——先读注释再排查。
- CI 干净树上测试夹具必须真实落盘：mock 掉 write_markdown 又断言
  remaining_missing=0，本地靠残留目录侥幸通过，CI 必炸。
- 新脚本进 cwk_ 命名空间会被两道独立门禁拦（governance-audit 孤儿 +
  rt032 namespace 声明断言）；RT-032 先例是移出命名空间，照做。

## 遗留

- 回复巡检尚未接入 nightly（当前是手动 CLI；接入属 RT-041 候选）。
- 今日 AI 编译失败的两篇在 failure_queue（attempts=2），随 nightly 重试。
