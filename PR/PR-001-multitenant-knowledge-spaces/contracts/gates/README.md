# PR-001 gate contracts（verification gates VG-A～VG-E + release gates G1～G7）

> 本目录冻结**两个互不替代的门禁族**：
>
> - **verification gates VG-A～VG-E**——wave 内集成门禁，receipt 落在 `gate-receipts/`；
> - **release gates G1～G7**——发布链门禁，receipt 落在**独立根** `release-gate-receipts/`。
>
> 两个根**永不合并**，理由见下文《两个 receipt 根为什么必须分开》。
> G1～G6 回答"这份东西被验证过了吗"，G7 回答"这份东西可以被部署吗"——不同问题、
> 不同信任根，因此不同 schema、不同文件名、不同 domain separator。

> 状态（截至 Wave-0）：VG-A 有机器 receipt，**synthetic PASS / conclusion=conservative_unknown**；
> VG-B～VG-E 此刻尚无 receipt 文件，因此按解析规则为 **NOT RUN**。两份 capability activation
> receipt（`capability-receipts/cwork-authority-source/`、
> `capability-receipts/gateway-identity-transport/`）此刻也**都不存在**，因此 VG-A 的能力缺口
> 为 **OPEN**，今天的正确评估是 **NO_GO**。本目录不代表真实 authority 或生产集成已就绪。
>
> 这段是**当前观测，只写在本 README / STATUS / 可行性文档里**，不写进任何机读契约：
> `gate_registry_v1.json` 与 `synthetic_closure_map_v1.json` 都不含状态字段，
> owner RT 独立验收 PASS 后可随时生成各自 receipt，契约测试会自动接管校验，
> 不需要修改本目录任何 JSON。

## 为什么需要本目录

RT-024 / RT-026 的契约声称"只消费 schema-valid VG receipt"，但此前 VG-A～VG-E 没有统一
schema，也没有确定路径——VG-A 只有叙述性 Markdown。消费者因此没有可机读输入，只能从
Markdown 猜结论。本目录冻结统一格式与精确路径，消除这一阻断。

## 文件

| 路径 | 作用 |
| --- | --- |
| `verification_gate_receipt_v1.schema.json` | `cwk.pr001.verification_gate_receipt.v1`——VG-A～E 唯一 receipt 格式 |
| `gate_registry_v1.json` | **冻结配置**：五个 gate 的 feeder RT、精确 receipt path、consumer allowlist、每 gate `allowed_prerequisite_ids` |
| `gate_registry_v1.schema.json` | registry 自身的 schema |
| `synthetic_closure_map_v1.json` | **冻结配置**：synthetic gate 的能力缺口如何闭合（closure_mode、required_capability_ids、rerun/归档规则） |
| `synthetic_closure_map_v1.schema.json` | closure map 自身的 schema（封闭 5 gate / 2 capability，禁状态与观测字段） |
| `capability_activation_receipt_v1.schema.json` | `cwk.pr001.capability_activation_receipt.v1`——闭合能力缺口的唯一 receipt 格式 |
| `release_gate_registry_v1.json` | **冻结配置**：G1～G7 七个条目的精确 receipt path、精确前置集、消费的 VG、允许的签发角色；G0 在 `bootstrap_gate` 内且**不是**条目 |
| `release_gate_registry_v1.schema.json` | release registry 自身的 schema（封闭 7 条目，`forbiddenEntryKeys` 含 `authorized`） |
| `release_gate_receipt_v1.schema.json` | `cwk.pr001.release_gate_receipt.v1`——**仅 G1～G6**，release **验证**格式；`gate_id` 枚举结构性排除 G7 |
| `release_authorization_receipt_v1.schema.json` | `cwk.pr001.release_authorization_receipt.v1`——**仅 G7**，release **授权**格式；无 status/conclusion/tests/artifacts 面 |

## 两个 receipt 根为什么必须分开

`gate-receipts/` 已经被断言为 **VG-A～VG-E 的精确闭合**：该根下每一个 `receipt.json`
都必须落在 VG registry 声明的路径上。若把 G1～G7 的 receipt 也放进去，它们会立刻被判为
undeclared extra，唯一的出路是**削弱那条已有的闭合断言**——而削弱一条已经生效的
fail-closed 检查，比多一个目录的代价大得多。

因此 release gate 使用独立根 `release-gate-receipts/`：

```
release-gate-receipts/G1..G6/receipt.json         # cwk.pr001.release_gate_receipt.v1
release-gate-receipts/G7/authorization.json       # cwk.pr001.release_authorization_receipt.v1
release-gate-receipts/G<n>/archive/<自身hash>.json # 被取代的历史件，append-only
```

两个根互不为对方的子树，任一家族都无法被塞进另一家族的证据集，两条闭合规则都不必放松。
`release-gate-receipts/` **当前不存在**，实现工作不得把它创建出来。

## 验证 ≠ 授权（G1～G6 vs G7）

| | G1～G6 | G7 |
| --- | --- | --- |
| 回答的问题 | 这份东西被验证过了吗 | 这份东西可以被部署吗 |
| 文件名 | `receipt.json` | `authorization.json` |
| schema `$id` | `cwk.pr001.release_gate_receipt.v1` | `cwk.pr001.release_authorization_receipt.v1` |
| domain separator | `cwk-release-gate-receipt-v1\0` | `cwk-release-authorization-receipt-v1\0` |
| 自哈希字段 | `receipt_sha256` | `authorization_sha256` |
| 签发者 | 项目内**独立**验收者（G6 须**新鲜**） | **外部信任根** + 人类 `authorizing_principal` |
| 机器前置 | 该 gate 的精确前置集 | **恰为 `{G6}`** |

两者的 separator 与自哈希字段名都不同，因此**跨族重放在哈希与字段名两处同时失败**：
一份验证 receipt 无法被当作授权使用，一份授权也无法被当作验证通过。
任何项目内 agent / 实现 agent / 验收 agent / go-no-go 评估者 / 测试签名者**都不得**签发 G7。

## release gate 的 DAG 与前置集（精确相等，非子集）

```
G0（引导，不在 registry 内）
 └─ G1 {G0, RT-011}
     └─ G2 {G1, RT-012, RT-013}
         └─ G3 {G2, RT-014, RT-015, RT-016, VG-A}      范围上限＝数据/迁移级对账
             └─ G4 {G3, RT-019, RT-020, RT-021, VG-C}
                 └─ G5 {G4, RT-022, RT-023, VG-D, CAP:gateway-identity-transport}
                     └─ G6 {G1..G5, RT-017..RT-026, VG-A..VG-E,
                            两份 CAP, 十份 SG:RT-017..026, RT-026-GO-NO-GO}
                         └─ G7 {G6}
```

- receipt 的 `prerequisite_refs[].ref_id` 多重集必须与 `required_prerequisite_ids`
  **精确相等**：少一个和多一个同样非法。子集检查会放过被削弱的 gate，超集检查会放过被灌水的 gate。
- **G6 重新绑定 G1～G5 全部五个**，而不是只引用 G5：链式传递会让一个已被撤销或过期的
  中段门禁悄悄留在结论里，逐个重算哈希才能发现。
- **G0 不是 registry 条目**。它是 RT 之前的**文档评审**门禁，证据是叙述性 Markdown，
  没有 feeder RT、没有测试证据、没有 artifact 哈希集、没有环境绑定；把它建模成第八个条目
  会强行给一次文档评审套上机器 receipt schema，等于让一份 Markdown 冒充可验证门禁。
  它只能作为 `G0` 这个 ref_id 出现在 G1 的前置集里。
- G0 的满足条件被冻结为 `bootstrap_gate.final_wave0_review_report_path`
  （`reviews/审核报告-wave0-final.md`），**该文件当前不存在**，因此 **G1 恒为 NOT_RUN**。
  `historical_narrative_ref` 指向的第四轮报告写在本轮契约整改之前，**明确不足以满足 G0**——
  信任一份已被取代的评审去放行 G1，正是这条规则要防的事。该报告只能由独立评审者产出，
  不得作为实现工作的副产物被创建。

## RT-012 Stage-09 / RT-013 Stage-10 重新验收

RT-012 的原始 `独立验收报告.md` 在 `cwk_instance.py` Stage-09 root-anchor
补救之前完成，因此不再是 G2 可消费的验收证据。registry 将它按原路径、
raw SHA-256、已验收 commit 和 Stage-09 编号收入封闭
`superseded_legacy_report`，只作不可变溯源；`rt_acceptance` family 同时把该旧路径
列入 `forbidden_paths`，即使引用的字节与旧 hash 完全一致也会拒绝。

RT-013 的原始 `独立验收报告.md` 同样早于 `cwk_agent_binding.py` Stage-10
concurrent-bind 补救。它以自己的原路径、raw SHA-256、已验收 commit 和 Stage-10
编号进入第二个精确、封闭的 `superseded_legacy_report`，旧路径同样被禁止；两个
provenance 对象不可互换，也不能挂到其他 RT。

G2 的前置集仍严格为 `{G1, RT-012, RT-013}`，没有新增一个伪 RT 或
Stage 前置。其中 `RT-012` 与 `RT-013` 现在分别只解析到
`RT/RT-012/reports/独立验收报告-stage09.md` 与
`RT/RT-013/reports/独立验收报告-stage10.md`；两个文件在候选 commit 冻结并完成
各自新的独立验收前必须缺席，任一缺席即 G2 `NOT_RUN`。实现工作不得预创建文件，
也不得写入 PASS。

所有采用 `cwk_acceptance_v1` 的 RT 报告（当前为 RT-012、RT-013 与 RT-017～026）
都必须带 `implementer_ids` 和 `reviewer_ids`；只有 G0 文档评审保留六字段
marker。旧 `legacy_frozen_hash` 的闭集缩为 RT-011 和 RT-014～016，
且只允许 ancestry + owner-scope 零漂移；不存在调用方自报的 fresh-rerun 绕过。

## 两条 consumer 关系（互不派生）

VG-A～VG-E 上存在**两条不同且不可互推**的消费关系，早期版本把它们混成一个字段，
读起来就是自相矛盾：registry 说"只有 RT-024/RT-026 消费 VG receipt"，
而计划说"G3 消费 VG-A、G4 消费 VG-C、G5 消费 VG-D"。两句话都对，但说的是**不同的关系**。

| 关系 | 字段 | 权威文件 | 值空间 |
| --- | --- | --- | --- |
| ① 直接 RT 消费者 | `consumers` | `gate_registry_v1.json` | 恰为 `{RT-024, RT-026}` |
| ② release gate 消费者 | `release_gate_consumers` | `gate_registry_v1.json` | 恰为 `{G3, G4, G5, G6}` |

关系 ② 的冻结值：`VG-A → {G3, G6}`、`VG-B → {G6}`、`VG-C → {G4, G6}`、
`VG-D → {G5, G6}`、`VG-E → {G6}`，且必须是 `release_gate_registry_v1.json` 中
`consumes_verification_gates` 的**精确逆关系**——两个文件双向对账，单边修改是可检测的矛盾
而不是静默漂移。

两条关系的值空间不相交：release gate 永远不得出现在 `consumers` 里，RT 永远不得出现在
`release_gate_consumers` 里。注意与 `allowed_prerequisite_ids` 的刻意不对称：
**消费某个 VG 的 release gate 按定义就不在该 VG 的上游**，所以 G3/G4/G5 分别被排除在
VG-A/VG-C/VG-D 的前置允许集之外，G6 消费全部五个因而不出现在任何允许集里。

## registry 是配置，不是状态

`gate_registry_v1.json` **不含**任何会随执行变化的字段。它没有 `status`、`conclusion`、
`verdict`、`last_run_at`，schema 的 `forbiddenEntryKeys` 也结构性地禁止它们回流。原因：
若 registry 携带状态，每跑一个 VG 都要改中央配置，等于把执行结果写进冻结契约。

**运行态状态解析规则**（`status_resolution_rule` 已写入 registry）：

1. `receipt_path` 不存在 ⇒ 该 gate 为 **NOT RUN**；
2. 存在 ⇒ 状态就是该 receipt 内部的 `status` 与 `conclusion`，别处均不作数。

消费者不得从 registry、`narrative_refs` 或任何 Markdown 推断状态。
每个条目的 `static_note` 只能陈述静态规则与范围，不得描述某次运行的结果。

## 所有权与生产/消费关系

- **格式 owner**：本目录（PR-001 中央契约）。任何 RT 都不得 fork、扩展、收窄或以同名
  schema 重定义；字段变更必须在此发新版本并附 migration note。
- **producer**：每个 gate 自己，且只能在其 feeder RT **独立验收 PASS 之后**运行。
  冻结映射：VG-A=RT-015、VG-B=RT-018、VG-C=RT-021、VG-D=RT-023、VG-E=RT-025。
- **consumer**：见上文《两条 consumer 关系》。直接 RT 消费者只有 RT-024 与 RT-026
  （字段 `consumers`）；release gate 消费者是 G3/G4/G5/G6（字段 `release_gate_consumers`）。
  两者都读 `receipt_path` 指向的 JSON，**不得**从 Markdown 叙述推断结论，
  **也不得创建或补写任何 receipt**。

## 反环不变式（结构性强制）

- `prerequisite_refs[].ref_id` 的 pattern 只允许 `RT-011～RT-025`、`G0～G5`、`VG-A～VG-E`。
  **`G6`、`G7`、`RT-026` 在语法层被排除**，无法作为任何 VG 的输入。
- `consumers` 枚举只有 `RT-024`/`RT-026`，VG 不能被上游 RT 反向消费；
  `release_gate_consumers` 枚举只有 `G3`/`G4`/`G5`/`G6`，且与 `allowed_prerequisite_ids`
  **互斥**——消费某个 VG 的 release gate 不可能同时是它的上游。
- receipt 不得自引用（`prerequisite_refs` 不含自身 `gate_id`）。
- **VG-A 的 receipt 必须在 Wave-0 就地生成**，绝不能挂到 RT-026 去转换：RT-026 要求
  VG-A machine receipt 作为前置输入，若由 RT-026 生成即构成 `VG-A → RT-026 → VG-A` 新环。

## 前置引用的秩约束（`allowed_prerequisite_ids`）

上面的 pattern 只界定 **id 空间**，不足以防环：它同样会放行 `VG-B → VG-C`、
`VG-A → RT-025` 或 `VG-A → G5` 这类**前向引用**。因此每个 gate 在 registry 里冻结一份
**穷举** `allowed_prerequisite_ids`，它是 `prerequisite_refs[].ref_id` 的唯一合法取值集，
由且仅由三条秩规则推导（见 registry 顶层 `prerequisite_rank_rule`）：

1. **RT 秩**：不得引用序号高于本 gate `feeder_rt` 的 RT。VG-A 最高到 RT-015，
   VG-B 到 RT-018，VG-C 到 RT-021，VG-D 到 RT-023，VG-E 到 RT-025。
2. **VG 秩**：只允许波次顺序 `VG-A < VG-B < VG-C < VG-D < VG-E` 中**严格更早**的 gate；
   自身与任何后续 gate 一律禁止。
3. **发布门禁秩**：只允许该时点**已在上游**的 `G0～Gn`。**消费本 gate 的 G 必须排除**——
   G3 消费 VG-A、G4 消费 VG-C、G5 消费 VG-D，因此这三对组合会构成环，被显式剔除。

| gate | RT 上限 | 允许的 G | 允许的 VG |
| --- | --- | --- | --- |
| VG-A | RT-015 | G0～G2（G3 消费本 gate） | 无 |
| VG-B | RT-018 | G0～G3 | VG-A |
| VG-C | RT-021 | G0～G3（G4 消费本 gate） | VG-A、VG-B |
| VG-D | RT-023 | G0～G4（G5 消费本 gate） | VG-A～VG-C |
| VG-E | RT-025 | G0～G5 | VG-A～VG-D |

另外，schema 的 `uniqueItems` 只对**整个对象**去重，`{ref_id: X, ref_sha256: a}` 与
`{ref_id: X, ref_sha256: b}` 会被当成两个合法条目。因此 **`ref_id` 在单份 receipt 内必须唯一**
是一条独立语义规则：同一 `ref_id` 出现两次即非法，无论 hash 是否相同。

以上四条都由 `tests/test_pr001_gate_contracts.py` 双向验证：正向证明冻结允许集与秩规则
独立推导的结果完全相等，反向用合成的未来 receipt 证明前向引用、自引用、超秩 RT、
消费方 G、重复 `ref_id` 全部 fail closed。

## 状态语义

- `status=not_run` → `conclusion=not_run` 且 `evidence.tests_run=0`。
- `status=implementation_done` 是实现 Agent 自记录，**不是 PASS**。
- `status ∈ {pass, fail}` 必须带 `verifier`，且 `verifier != producer`。
- `status=pass` 还要求 `feeder_rt_independent_pass=true` 且 `evidence.tests_failed=0`。
- `synthetic=true`（用到 fake authority/provider/identity）时必须给 `synthetic_reason`，
  且 `conclusion` **不得**为 `integration_verified`——只能是 `conservative_unknown` 或更弱。

## VG-A receipt 的来源与边界

`gate-receipts/VG-A/receipt.json` 在 Wave-0 依据两份**已完成独立验收**的叙述记录
（`VG-A-集成验证.md` 由 `agent-vga-impl-opus` 交付 `implementation_done`；
`VG-A-独立验收.md` 由独立复核者 `agent-vga-verify-opus` 判 PASS）以及一次新鲜的
VG-A 测试运行（75 tests, 0 failed, Python 3.11.14）生成。

- `status=pass` **仅表示 synthetic gate 通过**：RT-015 `FakeSigningAuthority` +
  test-only token 下的 host-chain 集成成立。
- `synthetic=true`、`conclusion=conservative_unknown` 是硬上限：**不代表**真实 authority、
  真实工作协同权限 API、真实 Gateway、沙箱传输、G3/M3 或生产集成可用。
- 两份 Markdown 与九个 VG-A 测试文件都以 `artifacts[].sha256` 绑定；任一文件漂移，
  `test_present_receipt_artifacts_exist_with_matching_hashes` 立即失败。
- `receipt_sha256 = sha256(b"cwk-verification-gate-receipt-v1\0" + JCS_UTF8(NFC(record_without_receipt_sha256)))`，
  由测试独立重算校验。

## 契约测试如何面向未来

`tests/test_pr001_gate_contracts.py` **不冻结"哪些 gate 已经跑过"**，只冻结规则：

- `test_vga_receipt_exists_and_every_present_receipt_is_registry_declared`：
  VG-A 必须现在就存在（RT-024/RT-026 的硬输入）；其余 receipt 可有可无，但
  `gate-receipts/**/receipt.json` 只要存在就必须落在 registry 声明的路径上，
  杜绝未声明/夹带的 receipt。
- 结构、语义、秩、hash、artifact 五组校验对**磁盘上实际存在**的 receipt 逐个执行，
  VG-B～VG-E 一旦由 owner RT 合法生成即自动纳入，无需改测试。
- `FutureReceiptRegressionTests` 在临时目录合成 VG-A～VG-E 的未来 receipt，
  正向证明合法 receipt 全部通过、反向证明每条反环/秩/唯一性规则 fail closed，
  因此这些规则今天就有回归覆盖，而不是等到 VG-E 真正产出才第一次被执行。

## synthetic gate 的闭合路径（`synthetic_closure_map_v1.json`）

VG-A 是**永久** synthetic。如果规则停在"任一 hard gate receipt 为 synthetic ⇒ 无条件
NO_GO"，那么即便 RT-017 / RT-023 后来交付了真实能力，`READY_FOR_G7_REVIEW` 也永远不可达
——那是设计死角，不是 fail-closed。closure map 在保持 fail-closed 的前提下把出口显式化。

**"永不升级"有三重含义，互不混淆：**

1. **不可变**：已签发的 receipt 永不就地编辑、打补丁或重签。
2. **不可重解释**：RT-024 / RT-026 / G6 / G7 都不得把 `synthetic=true` /
   `conservative_unknown` 读成比它本身更强的结论——**能力缺口闭合之后也不行**。
3. **不可重跑**：**仅**对 `rerun_allowed=false` 的 gate 成立。VG-A 验证的是
   `FakeSigningAuthority` 下的 RT-015 host chain，其 synthetic 范围是永久事实，
   因此 VG-A 的 receipt 永久冻结、永不轮换。

| gate | `synthetic_expected` | `rerun_allowed` | `closure_mode` | 需要的 capability |
| --- | --- | --- | --- | --- |
| VG-A | true | **false** | `capability_activation_receipts` | `cwork-authority-source` + `gateway-identity-transport`（两个都要） |
| VG-B～VG-E | false | true | `non_synthetic_rerun` | 无（不得借用 VG-A 的映射） |

**能力缺口只能由单独 owner 的 activation receipt 闭合**
（`cwk.pr001.capability_activation_receipt.v1`，与 SG-00 逐条对应）：

| capability | owner | 路径 |
| --- | --- | --- |
| `cwork-authority-source`（CWork 权威 ACL / 撤权源） | **RT-017** | `capability-receipts/cwork-authority-source/receipt.json` |
| `gateway-identity-transport`（Gateway 可信身份 / 受控传输） | **RT-023** | `capability-receipts/gateway-identity-transport/receipt.json` |

- 它们**不在** `gate-receipts/` 下，因此不会污染"VG receipt 路径必须由 registry 声明"这条不变式；
  domain separator 也不同（`cwk-capability-activation-receipt-v1\0`），两族 receipt 无法互换。
- 闭合是一个**合取**：`status=pass` ∧ `synthetic=false` ∧ `owner_rt_independent_pass=true`
  ∧ `conclusion=capability_activated` ∧ `receipt_sha256` 重算一致 ∧ 全部 `artifacts[].sha256`
  与磁盘一致。任一条不成立 ⇒ 缺口 **OPEN** ⇒ **NO_GO**。完整判定还要求：verifier 存在且
  ≠ `producer`；`consumers` 恰为该 receipt 声明的消费者数组（schema 中是数组，
  没有 `consumer_rt` 标量）；`closes_gate_ids` 恰为闭包图映射集合；
  `tests_run > 0` ∧ `tests_failed = 0` ∧ skipped 有界；`evidence_refs` 按 capability 的
  `required_evidence_roles` **非空且角色齐全**；每个 evidence/artifact 是**安全的仓库相对
  普通文件**（拒绝绝对路径、`..`、任一分量 symlink、末段 symlink、hardlink、目录、特殊文件）
  且 sha256 与磁盘一致；`created_at` 为严格 RFC3339；无多余字段、无深层禁用字段。
- **有界 TTL（`activation_validity_bound_rule`）**：`expires_at > created_at` 还不够——无上界的
  TTL 会让一次探测把真实外部能力认证一个世纪，等于悄悄重建死角。每个 capability 在闭包图里
  冻结 `max_validity_seconds`（`cwork-authority-source` 90 天 = 7776000s；
  `gateway-identity-transport` 30 天 = 2592000s，**刻意更短**，因为陈旧 trust-store 声明是
  静默认证绕过），校验器对链上**每一份** receipt 强制
  `0 < expires_at - created_at <= max_validity_seconds`。上界**闭区间**：正好等于上界有效，
  多一秒无效。该界只存在于冻结配置中，receipt 无法自行放宽自己的 TTL；该检查不依赖评估器时钟。
- **可续签、不可原地覆盖（`activation_history_rule`）**：activation receipt 与 VG 链同形——
  `sequence`、`previous_receipt_sha256`、
  `capability-receipts/<capability_id>/archive/<旧 receipt_sha256>.json` 追加归档。
  owner 只能在其 RT 验收仍然有效、且完成**一次新的真实探测**之后续签。RT-026 只读 current。
- **subject 绑定是祖先关系，不是相等关系（`activation_subject_binding_rule`）**：
  `tested_subject_commit` 为 40 位 hex，须是引入该 receipt 的 evidence-only commit 的**祖先**，
  且与该 receipt 自身的 evidence/artifacts 一致。它**不要求**等于 RT-026 的最终 HEAD——
  下游合并会在诚实证据产出之后合法推进 HEAD。
- 闭合之后 VG-A **仍然**以 scoped evidence + 显式生产 caveat 列在 go/no-go 报告里，
  只是不再单独构成永久 blocker。
- RT-026 **只聚合**：不得创建、补写、猜测任何 activation receipt，不得轮换/归档任何 gate
  receipt，也不得修改 closure map。文件不存在 = NOT RUN = fail closed，永远不是"已激活"。

**current vs archive（`receipt_history_rule` / `chain_rule`）**：`receipt_path` 永远是
**当前** receipt，也是消费者唯一读取的那一份。`rerun_allowed=true` 的 gate 重跑时，必须先把
被取代的 receipt 按字节原样归档到
`gate-receipts/VG-{X}/archive/{被取代的 receipt_sha256}.json`（append-only，文件名就是它
自己的域分隔 hash，篡改可由重算发现），再发布新的 current receipt。`rerun_allowed=false` 的
gate 不轮换，archive 目录恒为空。归档文件名不是 `receipt.json`，因此不会被当成 current
receipt 扫到——这一点有专门的负向测试。

**archive 目录的精确成员关系**：`gate-receipts/VG-{X}/archive/` 的合法成员**恰好**是链上被取代的那些 receipt，文件名恰为各自的 `receipt_sha256` 加 `.json`。枚举该目录后，任何多余条目——未出现在链上的 JSON、以 `.` 开头的隐藏项、任意位置的 symlink、hardlink、非普通文件、嵌套子目录——都必须判为**失败**，而不是忽略。仅靠 `sequence` 恰好占满 `1..N` **不足以**发现它们：这些条目根本不带`sequence`，链校验对其完全不可见。`rerun_allowed=false` 的 gate 其 archive 目录必须**缺席或为空**，"空"同样按上述精确成员关系判定。

**链是可机读遍历的，不是叙述**（VG-B～VG-E）：

- 每份 receipt 带单调整数 `sequence`（首跑 = 1）。archive 与 current 合起来**恰好**占满
  `1..N`，current 位于链尾（tip）。
- `supersedes_receipt_sha256` 把第 k 份链接到第 k-1 份；首跑此字段必须缺席。
- 校验器走**唯一一条前缀链**，拒绝 gap、fork、orphan 与 cycle；每个归档文件名必须等于
  它自身重算出的 receipt hash；current 必须链接到最新的归档项；全链 `gate_id` 必须一致。
- `created_at` 严格递增是**辅助**校验，**不是唯一排序证据**——单靠时间戳无法证明没有分叉。
- 空目录 / 无 current / 无 archive 一律是 `NOT_RUN` / `OPEN`，不是失败。

**VG-A 是 pinned legacy exception**：其当前 receipt 由冻结配置中的静态不可变哈希
`7058a91a1a5d48a8daca2967b77e7cb64cea578fb66ee3b69f3c12ff3e918657` 钉住；它**没有**
`sequence`、**没有** supersedes 链接，archive 必须保持缺席或为空。不得为了补新字段而改动
它的字节。任何"另一份 VG-A current receipt"或任何 VG-A 归档/轮换尝试都被直接拒绝。

closure map 与 registry 一样是**配置不是状态**：它不含 `status`/`conclusion`/`closed`，
也不含 `wave0_status`/`current_state_note` 这类**时点观测**——那些会在 activation receipt
出现的当天变陈旧。"今天是否已闭合"一律在评估时从磁盘上的 receipt 重算，人读的现状快照
只放在本 README、`STATUS.md` 与可行性文档里。

## 校验命令

```
python3.11 -m unittest tests.test_pr001_gate_contracts -v              # 211 tests（VG-A～VG-E）
python3.11 -m unittest tests.test_pr001_security_gate_contracts -v     # 206 tests（SG-00～SG-10）
python3.11 -m unittest tests.test_pr001_release_gate_contracts -v      # 115 tests（G1～G7 契约面）
python3.11 -m unittest tests.test_pr001_release_gate_validation -v     # 171 tests（G1～G7 可执行正/反例）
python3.11 -m unittest discover -s tests -p 'test_vga_*.py'            #  75 tests（VG-A 证据复现）
```

以上模块计数为 2026-08-21 实测值，非计划值。全库当前可收集 2059 tests；本轮完整
实跑尚未完成，因此这里不记录全量 PASS/skip。

release gate 的两个模块刻意分开：`..._release_gate_contracts` 断言四份契约文件**说了什么**
（$id、封闭性、枚举、DAG、两条 consumer 关系、深层禁用字段、语义规则文本）；
`..._release_gate_validation` 断言它们**做了什么**——内含一个自足的 evaluator，
结构校验由 schema JSON 自身驱动（schema 被削弱会被抓到），语义规则按冻结设计手写
（`semanticRules` 文本被削弱也会被抓到）。

`..._release_gate_validation` 的正例覆盖 G1～G6 各一份完整合法 receipt、一份 synthetic 且
被压到 `conservative_unknown` 的 receipt、一份 `not_run` 占位件，以及一份完整合法的 G7 授权——
没有正例的反例套件毫无价值：一个"全部拒绝"的校验器能trivially"拦下"所有攻击，同时也拦下所有合法证据。
反例每次只改一处并断言必须出现的精确违规码，覆盖：producer==verifier 自证、
verifier_role 与 registry 不符、G7 冒充验证 receipt、前置少一个/多一个/重复 ref_id/自引用/
前向引用/ref_kind 替换/哈希漂移/路径穿越、pass 但 feeder 未独立 PASS、pass 但有失败用例、
synthetic 冒称 verified、G6 结论被 wave gate 冒用、冻结 feeder/VG 集被改、授权字段与租户凭据
深层禁用、sequence=1 带 supersedes、断链/错链/gap/时间倒填、绑定自身引入 commit/无关 commit/
未触达 feeder 包、artifact 漂移/引用门禁证据/空 artifacts、任意字段改动破坏自哈希、多余顶层字段；
G6 新鲜性覆盖缺四件套、wave gate 误带 G6 字段、有历史 engagement、**自报新鲜但重算矛盾**、
attestation 出现 false、五类新鲜证据不全、永不过期、过期超 30 天、未重绑 G1～G5、无 RT 消费 G6；
G7 覆盖项目内/测试签名者、未知信任根、密钥撤销/过期、签名覆盖被篡改正文、缺签名、
指向不同 G6、G6 未 PASS、G6 已失效、越界 scope、非 M4、GA 动作、租户标识、验证字段、
非人类主体、可推断渠道、nonce 重放、预签未生效、已过期、永久授权、撤销 append-only、
撤销缺 revocation_ref、活件带 revocation_ref、不可撤销、受益者自记、主体自记、首签带前驱、
双向跨族重放，以及 **AUTHORIZATION IS NOT EXECUTION**（授权面上不存在任何执行字段，
且同一份授权在窗口关闭后必须重新失败）；闭合覆盖根不存在=NOT_RUN、只含声明件、
旁边多一份 JSON、G7 下出现 `receipt.json`、验证 gate 下出现 `authorization.json`、
末段 symlink、分量 symlink、dotfile、hardlink、FIFO、嵌套多余 receipt、归档件命名正确/错误。

`ClosureEvaluationRegressionTests` 在临时目录合成 activation receipt，**正向**证明两份齐备
且全部有效时缺口转为 CLOSED（即 `READY_FOR_G7_REVIEW` 可达，不是死角），**反向**证明缺失、
只有一份、synthetic、owner 不符、无独立 PASS、结论不到 `capability_activated`、hash 漂移、
artifact 漂移全部退回 NO_GO；同时对**真实仓库当前状态**断言 `OPEN ⇔ NO_GO`，
既验证今天确实是 NO_GO，又不把"今天没有 activation receipt"冻进测试。

`HistoryChecks` 另行覆盖 VG 链：首跑、三段重跑链、archive 无 current、缺失归档项、
错误归档文件名、`archive/receipt.json` 不会被当成 current、正文篡改、supersedes 断链/缺失/
非法、gap、fork、orphan、时间倒填、外来 gate_id，以及"另一份 VG-A"与"VG-A 带 archive /
带 supersedes"全部拒绝；activation 侧覆盖零测试、`tests_failed>0`、skipped 越界、空 artifacts、
缺 verifier、verifier==producer、错/多 consumer、错/多 `closes_gate_ids`、多余字段、深层禁用
字段、缺角色、绝对路径/穿越/目录/末段 symlink/分量 symlink/hardlink/FIFO artifact、绑定 gate
receipt、绑定自身路径、畸形时间戳，以及生命周期：当前已过期、**TTL 正好等于上界通过**、
上界 +1 秒拒绝、9999 年 TTL 拒绝、首次签发、续签链、缺归档、断链、缺探测引用、错归档文件名、
倒填续签、首签带链接、有 archive 无 current。

Security Gate（SG-00～SG-10）的机器权威在 `../security/`，说明见
`../security/README.md`。
