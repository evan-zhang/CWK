# PR-001 Security Gates SG-00～SG-10：机器权威

> 本目录的三个 JSON 文件是 SG-00～SG-10 的**唯一机器权威**。
> 中央计划 §5.1 的 Markdown 矩阵是**派生文档**，永远不是事实来源。
> 任何冲突以本目录为准。

## 1. 文件

| 文件 | 角色 |
| --- | --- |
| `security_gate_registry_v1.json` | 冻结的 registry：十个生产 RT、各自唯一 receipt 路径、`receipt_kind`、拥有的 SG、冻结的 claim ID 集合、文件系统策略、生产任务、生产/消费**阶段** |
| `security_gate_registry_v1.schema.json` | 证明 registry 是 **config-not-state**：`additionalProperties:false`、`entries` 恰 10 条、禁止一切可变状态字段 |
| `security_gate_receipt_v1.schema.json` | 单一冻结 receipt schema `cwk.pr001.security_gate_receipt.v1` |

可执行校验：`tests/test_pr001_security_gate_contracts.py`。

## 2. registry 是配置，不是状态

registry 中**不存在**任何 `status`、`conclusion`、`verdict`、`result`、`passed`、
`failed`、`state`、`last_run`、`executed`、`satisfied`、`closed` 字段，schema 的
`forbiddenKeys` 强制这一点。它只声明"谁负责产出什么、放在哪、必须证明哪些 claim"。

状态推导规则（`status_resolution_rule`）：

- `receipt_path` **不存在** ⇒ 该 SG 条目为 `NOT_RUN`。这是当前全部十个条目的状态。
- `receipt_path` 存在 ⇒ 状态**恰好**等于该 receipt 自身的 `status` / `conclusion`，
  且该 receipt 必须完全通过校验，否则 fail closed。
- 一个 RT 的缺失**永远不能**由另一个 RT 覆盖了同一个 SG 来满足。
- `security-receipts/` 下出现任何**未声明**的额外文件是硬失败，不是加分项。
- **精确成员关系（闭合判定）**：判定闭合在 `receipt_root` **整棵子树**上，而不只是 registry 列出的条目。递归枚举 `security-receipts/` 后，每一个 `*.json` 都必须**恰好**落在某个已声明的 `receipt_path` 上；任何位置出现的 symlink、以 `.` 开头的隐藏项、非普通文件（FIFO / 设备节点 / socket）、嵌套的额外 receipt、或紧邻已声明 receipt 的垃圾 JSON，都是**硬 NO_GO**。
  理由：这类条目不携带 `producer_rt`，也不携带 `sequence`，因此任何"按条目校验"或"按链校验"的规则都永远看不见它们——只有对目录整体的闭合枚举能看见。

因此"未来才会产生的 receipt"不需要现在存在：缺席即 `NOT_RUN`，RT-026 只能 `NO_GO`。

### 2.1 Security owner scope v2

`owner_scope_tree_sha256` 对安全 receipt 使用独立的
`cwk-owner-scope-tree-v2\0` 模型；release/capability 家族继续使用其既有 v1
算法，互不迁移。v2 同时绑定**选择器清单**与选择出的 Git blob：

- `owner_code_path_prefixes` 中以 `/` 结尾的是递归目录选择器，receipt 存在时必须
  非空；不以 `/` 结尾的是一个精确文件，绝不是字符串前缀。
- `owner_test_file_prefixes` 是 `tests/test_rt0NN_` 根目录文件前缀；其余部分只允许
  `[a-z0-9_]+.py`，禁止子目录、`.py.bak` 和跨 RT 引用。
  `required_security_test_files` 至少冻结该 RT 的 `paths` 与 `security` 测试。
- exact file、目录内容、全部匹配测试、必需测试、evolution stage、共享 ABI 依赖和
  evidence exclusions 都进入 selector manifest；添加或删除一个 selector 不能在空
  digest 中隐身。
- `managed_script_inventory` 把所有 `scripts/cwk_[a-z0-9_]+.py` 与 `install.sh`
  闭合为三类：精确 RT owner、central shared ABI、或冻结 path/mode/SHA 的 legacy。
  candidate 出现任何未声明 managed path、跨类重叠或 legacy 漂移都会失败；唯一双 owner
  是 tenant CLI 的 stage 3 → stage 7。
- 每个 `specs/` 在 subject 中只能含 `需求契约.md`、`技术方案.md`，每个 `tasks/`
  只能含 `开发任务.md`。目录选择器和测试 namespace 保留 Git mode/kind，selected
  symlink、gitlink、特殊 mode、`.bak`、子目录或 nested basename lookalike 都会失败。
- receipt 存在时，所有选择器必须在 `tested_subject_commit` 中解析为普通 Git blob；
  同一 immutable scope 在显式 evaluation candidate 必须逐记录相等。receipt 之后再改
  script/schema/test/spec 会立即使旧 receipt 失效。
- `reports/`、`receipts/` 及四类中央 receipt 根永远是 evidence output，既不进入 owner
  code digest，也不能单独满足 "subject touched owner code"。
- refs 必须同时绑定 subject blob、evaluation blob 与 fail-closed 当前读取字节，并在
  那一份 AST 中通过 canonical acceptance validator：直接 `unittest.TestCase`、唯一
  direct `test_` 方法、无 decorator/skip flag/delete/rebind/duplicate overwrite，且有
  `self.assert*`/`self.fail`；继承 skip、alias skip、class-level skip 和非 test helper
  全部拒绝。
- manifest 绑定 policy path/SHA 及每个 stage 的 `stage_index/owner_rt/target_path/`
  `receipt_path/ordinal`。evolution replay 还验证 companion pins，并对 tenant CLI 调用
  完整 slot + AST + comment guard；只改注释冒充 stage 3/7 不成立。完整 policy 对每个
  SG receipt 都会 replay，因此已完成的 RT-012 stage 9 与 RT-013 stage 10
  receipt，以及 `scripts/cwk_instance.py` 与 `scripts/cwk_agent_binding.py` 的最终 tip，
  都不能被缺失、回退或伪造，即使 RT-012/013 本身不属于 RT-017～026 的
  Security Gate producer 集。
- evaluation 前必须是 clean worktree；`assume-unchanged`/`skip-worktree` 一律拒绝。
  当前磁盘 selected bytes、目录闭包、测试闭包必须与显式 evaluation commit 完全一致，
  包括被 `.gitignore` 隐藏的未跟踪项。

唯一普通 overlap 是 `scripts/cwk_tenant_cli.py`：RT-019 只拥有 append-only stage 3，
RT-026 只拥有依赖 stage 3 的 stage 7；两者通过固定 SHA 的
`contracts/script-evolution/policy_v1.json` 和 receipt chain 复核，不能用普通字节相等
冒充。其他 exact/dir/test-prefix overlap 全部拒绝。

`scripts/cwk_instance.py` 与 `scripts/cwk_agent_binding.py` 分别是已完成的 RT-012
stage 9 和 RT-013 stage 10 兼容演进：policy/receipt chain 证明它们从冻结 genesis
迁移到当前 tip；managed inventory 再以最终 path/mode/SHA 将两者冻结。它们不是
任一 future SG entry 的 owner stage，也不能替 RT-017～026 满足 owner touch，但所有
future SG receipt 都必须成功 replay 这两条 bootstrap chain。

`PilotAdmissionProviderV1` 属于 neutral Wave-0 ABI，不归任何 RT owner。registry 的
`shared_abi_dependencies` 固定其 runtime/schema 路径与 SHA；消费 RT 的 subject 和
evaluation candidate 必须都含相同 blob，但它不能单独满足 owner touch。

RT-025 的 tracked clean-room fixture owner selector 精确为 `tests/fixtures/rt025/`：
它只允许合成、非生产、无真实凭据的测试数据，签 receipt 时必须非空并存在于 subject。
真实 restore target 仍必须由 `mktemp` 或等价安全 API 在仓库外创建，不能复用 fixture。

计划态允许 future selector 尚不存在，因为此时没有 receipt。receipt 一旦出现，缺
exact file、空 contracts 目录、零匹配测试、缺 required test、断裂 evolution chain 或
非空 `unresolved_owner_surface_requirements` 都 fail closed。RT-026 已在三份权威文档
冻结 `install.sh`、`scripts/cwk_doctor.py` 与
`scripts/cwk_go_no_go_launcher.py`，三者均为其 tested subject 的强制精确文件；launcher
只生产 OS-control evidence，不是只读 go/no-go evaluator。

## 3. 十个条目（当前全部 NOT_RUN）

| RT | receipt 路径 | kind | 生产任务 | 生产阶段 | 拥有的 SG | claim |
| --- | --- | --- | --- | --- | --- | --- |
| RT-017 | `security-receipts/RT-017/receipt.json` | rt-security | D-07 | rt_independent_acceptance | SG-00/01/03/05/06/08 | SGC-017-01..06 |
| RT-018 | `security-receipts/RT-018/receipt.json` | rt-security | D-05 | rt_independent_acceptance | SG-03/05/08 | SGC-018-01..03 |
| RT-019 | `security-receipts/RT-019/receipt.json` | rt-security | D-07 | rt_independent_acceptance | SG-03/07 | SGC-019-01..02 |
| RT-020 | `security-receipts/RT-020/receipt.json` | rt-security | D-06 | rt_independent_acceptance | SG-03 | SGC-020-01 |
| RT-021 | `security-receipts/RT-021/receipt.json` | rt-security | D-06 | rt_independent_acceptance | SG-03/06 | SGC-021-01..02 |
| RT-022 | `security-receipts/RT-022/receipt.json` | rt-security | T022-14 | rt_independent_acceptance | SG-02/03/04/06 | SGC-022-01..04 |
| RT-023 | `security-receipts/RT-023/receipt.json` | rt-security | T023-14 | rt_independent_acceptance | SG-00/02/03/07 | SGC-023-01..04 |
| RT-024 | `security-receipts/RT-024/receipt.json` | rt-security | T024-13 | rt_independent_acceptance | SG-03/04/06/08 | SGC-024-01..04 |
| RT-025 | `security-receipts/RT-025/receipt.json` | rt-security | T025-14 | rt_independent_acceptance | SG-03/05/09 | SGC-025-01..03 |
| RT-026 | `security-receipts/RT-026/receipt.json` | **preflight-security** | T026-14 | **preflight_after_candidate_freeze** | SG-03/10 | SGC-026-01..02 |

- 共 **31** 个冻结 claim ID，全局唯一，永不复用。
- **SG-03 的 owner 集合恰为 RT-017～RT-026 全部十个包**——没有任何包可以豁免。
- 每份 receipt 的 `claims` 必须与本表所属 RT 的集合**逐字相等**：多一个、少一个、
  改一个字都 fail closed。

## 4. 反循环：阶段序

```
phase_order = [
  rt_implementation,
  rt_independent_acceptance,        <- RT-017..025 在此签发
  preflight_after_candidate_freeze, <- RT-026 的 preflight receipt 在此签发
  go_no_go_evaluation,              <- RT-026 评估器在此只读消费
  rt026_independent_acceptance,
  final_acceptance,                 <- G6
]
write_phase_allowlist = [rt_independent_acceptance, preflight_after_candidate_freeze]
```

图无环当且仅当对每个条目的每个 consumer：
`index(producer_phase) < index(consumer_phase)`。

`go_no_go_evaluation` **被刻意排除在 `write_phase_allowlist` 之外**。这条排除加上身份重算，
给出的是**接口级署名排除**，**不是 OS 级写拒绝**：

- RT-026 自己的 SG-03/SG-10 receipt 由**独立预检验证者**
  （`independent_preflight_verifier`）在实现候选 commit 冻结之后、只读评估器运行
  之前签发；
- 评估器在**声明阶段**上不属于写阶段（`go_no_go_evaluation` 不在 `write_phase_allowlist`
  内），并且把自己的 `go_no_go_evaluator_identity` **注入**校验、重算署名排除性：任何
  `producer` 或 `verifier` 等于该身份的 receipt 一律 INVALID。因此评估器既不是这份证据的
  **声明阶段写者**，也不是它的**署名作者**，也不得据此代写、补写或推断任何 SG owner 证据；
- 因此 RT-026 不依赖自己署名的输出，同时十个条目仍然全部被精确声明。

**作用域诚实**：以上是**接口级**结论。阶段 allowlist 是**声明**检查，身份排除是**字符串
比对 + 重算**，两者都**不**证明内核/沙箱拒绝了该进程的写入。OS 级只读执行（可信启动器、
启动器签发的运行证明、前后精确成员清单、真实写拒绝证据）归口 **AC-026-11**
（任务 T026-15a～d），Wave-0 **未**证明。禁止把前者表述为后者。

对应任务：T026-14（签发，独立预检者）与 T026-15（只聚合不制造，含负向测试）。

## 5. 六类文件系统攻击（SG-03）

`path_traversal`、`symlink_component`、`symlink_leaf`、`hardlink`、`toctou`、
`special_file`。

**是六类不是四类**：`symlink` 被刻意拆成 component（路径中间目录是 symlink）与
leaf（末段是 symlink），因为 `O_NOFOLLOW` 只防末段；`special_file`（FIFO、设备节点、
socket）与 `hardlink` 分开，因为 `O_NOFOLLOW` 不拒绝 FIFO。

把六类塌缩成一句"已覆盖路径攻击"的 receipt **fail closed**。

- `filesystem_policy.mode = applicable`（九个 RT）：**不允许任何 N/A**，六类逐条
  必须绑定真实运行面与可执行用例引用。
- `filesystem_policy.mode = not_applicable_permitted`：**仅 RT-022**，且**仅**
  `SGC-022-02` 可标 N/A，必须同时给出 registry 允许的 `reason_code`
  （`no_filesystem_write_surface`、`read_only_injected_provider`、
  `in_memory_only`、`owned_by_other_rt_by_design`）、自然语言 reason 与静态证据。
  两种模式**不可混用**。

## 6. receipt 语义（摘要，以 schema 为准）

- 哈希 domain separator 为 `cwk-security-gate-receipt-v1\0`，与
  `cwk-verification-gate-receipt-v1\0`、`cwk-capability-activation-receipt-v1\0`
  **刻意不可互换**。
- `independent_security_verification_pass` 是**独立安全/预检结论**，
  **不等价于该 RT 的最终 PASS**，对 RT-026 尤其不表示 RT-026 已 PASS 或 G6 已签发。
- `verifier` 必须存在且**严格不等于** `producer`。
- `evaluator_identity_excluded` 在 `preflight-security` 上必填为 `true`，在
  `rt-security` 上禁止出现。它是一条**声明**，**不是证明**：校验方注入
  `go_no_go_evaluator_identity` 并**重算**排除性（`producer != 评估器身份` ∧
  `verifier != 评估器身份`），该声明必须与重算结果**一致**，否则 fail closed。
  自报 `true` 而 `producer` 恰为评估器身份的 receipt 一律 INVALID。
- `tests_run > 0`、`tests_failed = 0`、`skipped` 有界；`artifacts` 非空，逐条为
  仓库相对**普通文件**（拒绝绝对路径、`..`、任一分量 symlink、hardlink、目录、
  特殊文件）且 sha256 与磁盘匹配；`artifacts` 不得指向另一份安全门 receipt。
- `tested_subject_commit` 为 40 位 hex，是写入该 receipt 的 evidence-only commit 的
  **祖先**，并与 receipt 自身证据一致。这是**祖先规则，不是相等规则**：
  **不要求**等于 RT-026 的最终 HEAD，因为下游合并会合法推进 HEAD。
- `created_at` 为严格 RFC3339；`receipt_sha256` 由校验方重算比对。

## 7. 与其他 receipt 家族的关系

| 家族 | 目录 | separator | 消费者 |
| --- | --- | --- | --- |
| 验证门 VG-A～VG-E | `gate-receipts/` | `cwk-verification-gate-receipt-v1\0` | G3/G4/G5、RT-026 |
| 能力激活 | `capability-receipts/` | `cwk-capability-activation-receipt-v1\0` | 仅 RT-026 |
| 安全门 SG-00～SG-10 | `security-receipts/` | `cwk-security-gate-receipt-v1\0` | RT-026、G6 |

三者互不替代。特别地，RT-017 的 `SGC-017-01`（SG-00，权威探针 fail-closed 语义）
与 `cwork-authority-source` **能力激活 receipt** 是两个不同对象：前者是关于外部 API
语义的安全证据，后者是闭合 VG-A 缺口的能力激活记录。
