# ADR-0001：Query Broker 传输通道与可信 Agent 身份

> 状态：Accepted（RT-011 G1 冻结）
> 日期：2026-08-19
> 关联：PRD §7 FR-17；DESIGN §5 C-13；`references/安全威胁模型.md` §3 T-01/T-07；`plans/开发计划.md` §3、§18、§25。
> 范围：本 ADR 冻结政策、能力探针与安全默认；真实运行时合规由 RT-023 + VG-D + G5 验证。

## 1. 背景

沙箱 Agent 需要一条受控通道向宿主机 Query Broker 提交查询。若传输通道允许沙箱声明 `agent_id` 或 `tenant_id`，任何提示注入、恶意脚本或错配沙箱就能越权访问其他租户；因此必须由传输层强制注入不可伪造的 Agent 身份。

## 2. 决策

### 2.1 首选（政策强制）

- **通道**：OpenClaw 受控 Tool 调用。
- **身份来源**：Gateway 在 Tool 调用元数据中直接注入 `agent_id`；请求体中的 `agent_id`/`tenant_id`/`credential_ref`/路径字段**必须**被 Broker 拒绝。
- **审计**：Gateway 与宿主机双侧记录 Tool 调用元数据摘要（HMAC 后的 `agent_id_hash`），不落原始 `agent_id`。

### 2.2 后备（受限本机实现）

仅在 OpenClaw Tool 通道不可用时启用：

- **通道**：Unix Domain Socket（`runtime/query-broker.sock`，权限 `0600`）。
- **身份来源**：`SO_PEERCRED`（Linux）或等价的 peer credential（macOS 使用 `LOCAL_PEERCRED`）。
- **请求体身份字段**：仍然禁止，与首选一致。
- **约束**：peer uid/pid 必须映射到宿主机 secret backend 中登记的可信 Agent 身份，否则拒绝。

### 2.3 明确禁止

以下方案任何时候都**不满足** G1，即使加签名也不接受：

- **loopback HTTP + 请求体自报 `agent_id`**；
- 共享共享 raw 目录挂载到沙箱；
- 任何依赖沙箱输入声明租户/路径/凭据的方案。

## 3. 理由

1. **不可伪造身份**：Tool 元数据由 Gateway 在受控层注入，超出沙箱进程权限范围；UDS peer credential 由 kernel 提供，无法由用户空间伪造。
2. **零信任请求体**：沙箱运行时可能被提示注入；请求体中的任何字符串都是不可信数据，不得决定授权。
3. **审计与轮换**：`agent_id_hash` 仅由宿主机 secret backend 计算；binding secret 轮换会一次性推进所有 tenant `auth_epoch`，让旧缓存/in-flight 请求同步失效。
4. **保守默认**：G1 阶段能力探针默认返回 `conservative_unknown`；真实运行时是否具备不可伪造 Tool 元数据、可用 peer credential、限流与 SLA，均由 RT-023 在 Gateway 控制环境实测后升级为 `verified`。

## 4. 后果

- Broker 代码永远不会解析请求体中的身份字段。RT-022/RT-023 若违反此契约，独立验收必须拒绝合并。
- Broker 与 Skill 客户端需要能够在缺失可信身份时 **fail closed** 并阻塞试点；不允许降级为自报身份。
- 兼容性：现有 legacy 单用户 CLI 不使用 Broker，不受本 ADR 影响。

## 5. 探针与升级路径

| Probe ID | 默认结果 | 升级为 `verified` 条件 |
|---|---|---|
| `trusted_agent_identity_openclaw_tool` | `conservative_unknown` | 提供 Gateway 在真实控制环境中不可伪造注入 `agent_id` 的 receipt（RT-023） |
| `trusted_agent_identity_uds_peercred` | `conservative_unknown` | 提供 socket 权限、peer credential 与 secret backend 映射的 receipt（RT-023） |
| `sandbox_transport_openclaw_tool` | `conservative_unknown` | Gateway 端到端 receipt |
| `sandbox_transport_uds` | `conservative_unknown` | UDS receipt |
| `sandbox_transport_loopback_http_self_reported` | `conservative_unknown` | **永远无法升级**（政策禁止） |

## 6. 相关引用

- PRD FR-03、FR-17；
- DESIGN C-03、C-13；
- 安全威胁模型 T-01、T-07；
- `security_defaults.json` 中的 `transport_and_identity`。
