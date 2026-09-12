# Amendment 9 — 准备门执行中，尚非 READY_TO_RUN

仅修复公开 synthetic B 并发导入问题并建立新准备窗口；正式 A/B 未授权、未运行。

公开根因已从 Am8 synthetic 日志只读确认：native `chunks.seq_id` 并发分配竞态导致 UNIQUE 冲突，多行 INSERT 可见；不属于内容上限或 timeout，不推断旧私有 B 子类，不复制长日志。

实现每文档一次 POST → 同 ID pending 轮询 → completed 后下一 POST；failed 立即停止，transport 独立拒绝提前/重复/重入，7200 秒总 deadline 包含串行等待。所有历史回执保留。

最终状态以本页随后追加的验收结果及机器可读回执为准；当前不是运行正式 A/B 的凭据。
