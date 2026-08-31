# RT-002 Architecture Spec — 交互式工作协同办理中心

## 1. 分层架构

```text
CWK read-only collection + RT-001 AI understanding
  -> responsibility classifier
  -> action-card builder
  -> interactive action center
  -> confirmation transaction
  -> CWork write adapter
  -> read-back verifier
  -> audit ledger + knowledge mirror
```

知识库继续保存每日简报、事件历史和审计摘要，不承担直接写操作。交互工作台使用独立后端和短时操作令牌。

## 2. 责任分类

每个 `report_id` 生成：

```json
{
  "primary_type": "decision_todo|advice_todo|reactivated_todo|update_notice|inbox_awareness|historical",
  "role": "decision_maker|advisor|recipient|observer|unknown",
  "mandatory": true,
  "source_labels": [],
  "new_since_last_seen": [],
  "confidence": 0.0,
  "evidence_refs": []
}
```

责任分类必须优先使用 CWork 结构化角色、节点和待办状态；AI 仅用于解释和补充，不得凭正文猜测后直接触发写操作。

## 3. 操作卡片

卡片必须包含：

- `report_id` / `todo_id` / 当前节点 ID
- 主类型、角色、必办标识和辅助标签
- 摘要、历史背景、本次增量
- AI 建议、理由、风险和证据
- 建议提交文本
- 允许的操作集合
- 原文入口
- 当前服务端状态版本

## 4. 操作状态机

```text
draft
  -> previewed
  -> awaiting_final_confirmation
  -> executing
  -> succeeded_verified
  -> failed_retryable | failed_terminal | stale_conflict
```

第一次点击只生成操作预览。最终确认请求必须携带目标、正文、后果、幂等键和预期状态版本。执行前再次读取 CWork；状态已变化则进入 `stale_conflict`，禁止盲写。

## 5. 支持的操作

### 待办

- 提交建议
- 同意
- 有条件同意
- 不同意
- 忽略/无需处理（必须明确是否结束原待办）
- 转办
- 暂缓

### 收件与更新通知

- 本地已知悉
- 针对事项发表意见
- 针对本次更新发表意见
- 加入跟踪
- 稍后再看

“本地已知悉”和“CWork 标已读/结束待办”是不同操作，UI 和 API 不得混用。

## 6. 安全控制

- Shadow Mode 禁止加载任何 mutating adapter。
- 每个真实操作均需当次、逐项、最终确认。
- 操作令牌短时有效、单次使用并绑定用户、report_id、todo_id、action、payload hash。
- 幂等键防止重复提交。
- 执行后必须 read-back 验证。
- 审计记录保存建议、用户修改、最终 payload、目标、返回值、验证结果和时间。
- AI 不能自行调用写接口，也不能把建议结论当作用户授权。
- 批量办理属于后续独立验收，不在首个 production pilot 中启用。

## 7. 阶段

### Phase 1 — Shadow Mode

- 构建责任分类和交互卡片。
- 生成建议、按钮和确认预览。
- 不接入 CWork 写接口。

### Phase 2 — 单项真实办理

- 先接通“发表意见”。
- 再接通一种审批场景。
- 每项执行必须二次确认、幂等和回读验证。

### Phase 3 — 完整办理能力

- 扩展决策、建议、转办、暂缓和完成待办。
- 支持更新通知的增量互动。

### Phase 4 — 生产试点

- 小范围用户试点。
- 验证误分类率、草稿采纳率、写回成功率、重复提交率和审计完整性。

## 8. 验收指标

- 主类型唯一展示率 100%。
- 待办角色有结构化证据率 100%。
- Shadow Mode mutating command 调用数 0。
- 未经最终确认的写操作数 0。
- 已确认操作的幂等重复写入数 0。
- 写回后 read-back 验证覆盖率 100%。
- 审计记录完整率 100%。
- 用户可在卡片内编辑 AI 草稿后再确认提交。
