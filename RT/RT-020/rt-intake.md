# RT-020 rt-intake：Holdout 预览、Router 与决策日志

- 状态：planned（仅契约；尚未实现、测试或独立验收）
- Profile：Spec-Standard
- 依赖：RT-015、RT-019；RT-019 必须独立 PASS。
- 实现 Agent：`agent-rt020-impl`
- 独立验收 Agent：`agent-rt020-verify`

## 1. 目标

实现 RT-019 `PreviewProviderV1`，只用联合 manifest 的 holdout 展示 Profile 效果；在用户确认与 receipt 全绑定后激活 Profile；用确定性规则、confirmed entity、AI 的固定优先级生成可审计的三态、多标签 RouteDecision，并支持幂等重路由和不可变 diff。

## 2. 必须交付

- 实际 `PreviewProviderV1` 实现（ABI 与 preview receipt schema 的 owner 是 RT-019，RT-020 零漂移消费、不重定义同名 receipt）、RT-020 自有的 holdout preview **result set**、activation receipt。
- RouteDecision v2 schema + semantic validator；v1 历史不修改。
- Router、route log、review/archive metadata、reroute trigger/diff。
- 无边界歧义的 domain-separated `route_job_key/projection_key`。
- profile activation/rollback 与 route replay 的可恢复协调。
- 正常、异常、恶意、并发、回滚测试。
- 零漂移消费 neutral PilotAdmission ABI；Profile workflow provider 构造绑定
  `profile_workflow`，仅 `pilot` 在 preview/activation/route 持久化边界重验。

## 3. RouteDecision v2 冻结决策

v1 没有把 `evidence_refs` 设为 required，也无法表达跨字段条件；RT-020 不静默改 v1，而新增 `cwk.route_decision.v2`：

- `evidence_refs` required 且至少 1 个；
- `disposition="index"` 时 `space_ids` 至少 1 个，且每个属于当前 active Profile 的有效 opaque space；
- `archive_no_index/review` 的 committed `space_ids` 必须为空；review 候选空间只写独立 review metadata，不形成 membership；
- reason codes、profile version/SHA、canonical SHA、decision ID/job key、decided_at 必填；
- schema 之后再跑语义 validator，拒绝 inactive/unknown space、错误 evidence、profile/grant 快照漂移。

## 4. 幂等键冻结决策

禁止计划中的裸字符串 `+`。统一：

`base32` 固定为 RFC 4648 小写、去 padding、字符集 `[a-z2-7]`。

```text
LP(x) = uint32_be(len(UTF8(NFC(x)))) || UTF8(NFC(x))
route_job_key = "rj_" + base32(sha256(
  b"cwk-route-job-v1\0" || LP(tenant_id) || LP(report_key) ||
  LP(canonical_sha256) || LP(profile_sha256)))[0:26]
projection_key = "pj_" + base32(sha256(
  b"cwk-projection-job-v1\0" || LP(route_job_key) ||
  LP(space_id) || LP(projector_version)))[0:26]
```

## 5. 非目标

- 不创建 Wiki、索引或 durable space registry（RT-021）。
- 不删除 raw、grant、Profile 历史或 route log。
- RT-020 v1 不读取 RT-017 staged event body；只能把 opaque event-change signal 作为 reroute/review 触发，避免未冻结的跨安全边界读取。
- 不实现 Query Broker、沙箱 transport、生产 cron 或部署。
- 不实现 PilotAdmission production adapter，不读 RT-026 release/allowlist 私有配置。

## 6. 进入 RT-021 的门禁

必须通过独立验收，证明 holdout 未泄漏到 proposal、activation 三方绑定、
PilotAdmission 重验、路由优先级、AI fail-to-review、多空间、冷归档、重放幂等、
profile rollback 与恶意输入安全。未独立 PASS 不得进入 RT-021。
