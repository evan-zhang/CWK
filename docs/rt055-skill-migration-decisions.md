# RT-055 知识库三 skill v3 迁移决策点清单（草稿待拍板）

日期：2026-09-14。本文只记录决策，不代表已部署或激活。

## 建议拍板顺序

1. **读端点是否进入 v0.3.x 正式版：建议进入 v0.3.x，但先以 loopback-only、snapshot-backed 标准发布。**
   - 必须确认：分页契约（字符 offset/length、默认 65536）、416/404 语义、路径安全、大小上限、合成夹具绿。
   - 暂不承诺 NAS-backed SHA chain；深度原文核验继续走旧 v2。
2. **per-Agent token 移植：建议先灰度，不与读端点同批强制启用。**
   - 验收：三端点 scope 隔离；缺失/过期/吊销=401；越权 bank/doc=403；吊销下一请求生效；registry 故障 fail-closed；日志无 token/query/原文。
3. **混合期退出：建议把旧网关关停设为独立收口门。**
   - 三库新 `/query` exact+lexical 命中率和错误预算达标；`/answer` citation 可回读；`/read` 分页覆盖主要客户端；NAS SHA chain 或等价现场完整性证据补齐；token 灰度完成；连续一个完整 refresh 周期无未解释差异；回退演练通过。

## 其他设计决策

- `/read` 的 offset/length 是否长期定义为“Unicode 字符”而非 byte：当前实现建议字符，因响应使用 `total_chars` 且不拆 UTF-8；若客户端必须 byte range，应另立 v0.4 合同，不在迁移中隐式改变。
- 快照新鲜度：是否给 `rag-index.json` 增加生成时间、索引代和 source snapshot id，并在 `/query`/`/answer` 响应暴露？建议在正式版前补元数据，否则用户容易把快照当实时库。
- bank 注册与授权 registry 是否同源：建议 bank allowlist、token scope、索引代由 OPS 单一登记面管理，避免“能检索但不能读原文”。
- 约 38s 的 `/answer` 超时与并发上限：建议在灰度前定出客户端 timeout、重试上限和熔断；禁止对模型慢请求无界重试。
- 零命中语义：新栈“拒答即真没有”只适用于已确认 bank、索引健康且查询变体已尝试的流程；接口层不能把一次 `no_answer` 包装成全库事实。
- 关闭旧网关的责任人与观察窗口：需明确谁看回退指标、保留多久审计证据，以及异常时是否允许只读重开。
