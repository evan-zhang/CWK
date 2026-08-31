# CWK 遗留事项台账

> AODW 接入于 2026-08-31。这里记录“当前 RT 的成功标准已达成，但不应扩大进本次范围”
> 的后续事项；它不能用来拆走未完成的本 RT 目标。
>
> 条目格式由 `.aodw-next/02-workflow/rt-manager.md`「遗留事项」规定，门禁 G114/G115
> 按 `### DI-0NN` 标题行和下面的「发现于 / 状态」两行认账。改格式前先看那两条判据。

## 待认领

### DI-001 — RT-022 与 RT-026 的脚本演化预留槽位已被占用

| 字段 | 内容 |
|---|---|
| **发现于** | RT-029（AODW 产品基线收敛），2026-08-31 |
| **状态** | 已结清（RT-030，2026-08-31） |
| **建议认领时机** | ——（已由 RT-030 提供前向续槽机制） |

产品基线 `fe4add1` 里有两处 2026-08-30 的日常维护改动，动了受
`pr001-script-evolution-v1` 契约管辖的脚本，但当时没有留下演化回执：

- `c26c7ad`「按作者筛选汇报列表」改了 `scripts/cwk_wiki_query.py`（策略把该文件
  的唯一演化槽位 stage-06 预留给 RT-022）；
- `dc96c28`「云 Wiki 主力模型切到 glm-5.3-flash」改了
  `scripts/cwk_nightly_pipeline.py`（stage-08，预留给 RT-026）。

RT-029 的处理：策略已冻结，且回执 schema 把 `owner_rt` 限定为策略声明值，
没有任何方式把这两处改动登记到 RT-022/RT-026 之外的名下。为避免重签已签名证据，
RT-029 用预留槽位如实登记了真实的 `from`/`to` 哈希转移，并在两份 migration note
的「Provenance」小节里写明这不是该 RT 的工作。

遗留问题：`max_ordinal` 均为 1，槽位现已用尽。**当 RT-022 真正实现 Query Broker、
或 RT-026 真正实现试点影子切换时，若需再改这两个文件，必须先修订
`policy_v1.json`**，而修订会牵动已签发的 stage-09/stage-10 回执、两份独立验收报告、
安全登记表常量和 guard helper 里的人工审查基准值。

**RT-030 的处置（2026-08-31，已结清）**：不改 `policy_v1.json`——它的 sha256
被 `security_gate_registry_v1.json` 交叉固定，改它就是重写已签名证据。改为旁挂前向
版本 `.aodw-next/06-project/governance/script-evolution-v2.json`
（schema `cwk.governance.script_evolution_overlay.v2`）：

- v2 用 `inherits` 按 sha256 钉住 v1 策略与安全登记表两份上游权威，任一漂移即红；
- 为两个用尽的槽位各开一条 `continuation_slots`：`cwk_wiki_query.py`（owner RT-022）、
  `cwk_nightly_pipeline.py`（owner RT-026），`v2_ordinal_start: 2`、`v2_max_ordinal: 5`；
  `v1_chain_tip_sha256` 取自 v1 stage-06/stage-08 回执的 `to_sha256`，已验证与当前
  磁盘哈希逐字相等——v1→v2 的交接点是可证明的，不是声称的；
- v2 回执写在 `RT/<owner_rt>/receipts/script-evolution-v2/`，与 v1 的
  `receipts/script-evolution/` 目录分离，因此不落进 v1 闭合性测试的 glob，
  不会污染既有回执集合；
- `owner_rt` 仍锁定 RT-022 / RT-026，与 v1 的归属语义一致——续槽不改主。

由 `make governance-audit`（GA-V2-* 判据）与 `tests/test_governance_audit.py`
的 `TestContinuationSlots` 强制。若后续 RT 需要突破 `v2_max_ordinal: 5`，
按同样的前向叠加方式再开一版，仍不得回改 v1。

### DI-002 — 两个 legacy frozen 脚本的基线指纹已重新锚定

| 字段 | 内容 |
|---|---|
| **发现于** | RT-029（AODW 产品基线收敛），2026-08-31 |
| **状态** | 已结清（RT-030，2026-08-31） |
| **建议认领时机** | ——（已由 RT-030 建立有主的演化路径） |

同一次模型切换 `dc96c28` 还改了 `scripts/cwk_ai_common.py`（模型允许清单从
2 个扩到 4 个）和 `scripts/cwk_cloud_wiki_compile.py`（两个默认模型字面量）。这两个
文件属于安全登记表的 `legacy_frozen_files`，**没有**演化回执机制可用。

RT-029 的处理：只更新了安全登记表里这两条指纹，使其描述已获批准的基线。核对过：
这两个文件不在 RT-016 genesis 表、不在 `companion_immutable_paths`、不在 VG-A 已签
收执的 artifacts 列表里，登记表本身的哈希也没有被任何地方固定，因此该操作没有重写
任何已签名证据。门禁机制未改动，对后续漂移仍然 fail closed。

遗留问题：`legacy_frozen_files` 缺少「有主的演化路径」。今后再有人改这 53 个文件，
仍然只能靠改指纹来放行，缺少 migration note 与验收测试的约束。建议后续 RT 评估是否
把高风险项（尤其是承载模型允许清单的 `cwk_ai_common.py`）迁入有回执机制的管辖。

**RT-030 的处置（2026-08-31，已结清）**：同样不动安全登记表本体（它自己也被
`inherits` 按 sha256 钉住），在 v2 叠加层里给这 53 个成员建立
`legacy_evolution` 管辖：

- `default_steward: "CWK maintainer"`——这一族不再是无主状态，缺 steward 即报
  `GA-LEGACY-OWNER`；
- `authorized_change_procedure` S-1..S-5 定义了唯一合法路径：开 RT → 写 v2 回执
  （`from_sha256` 必须等于登记表当前指纹、`to_sha256` 等于改后哈希）→ 写 migration
  note → 更新登记表指纹 → 复跑门禁；
- `high_risk_members` 显式点名 `cwk_ai_common.py`（模型允许清单）与
  `cwk_cloud_wiki_compile.py`（默认模型字面量），即 DI-002 正文点到的两个文件。

关键在于**判据面是当前磁盘内容**：`make governance-audit` 直接把这 53 个文件的
实际哈希与登记表指纹比对，漂移且无合规 v2 回执即 `GA-LEGACY-DRIFT` 红。
`tests/test_governance_audit.py::TestLegacyFamilyEvolution` 双向验证——
无回执的漂移必红、有合规回执的漂移放行、`from_sha256` 对不上的回执**不能**洗白漂移、
拿掉 `default_steward` 会把 DI-002 重新打开。「只靠改指纹放行」这条老路已被堵死。

### DI-003 — `pre-commit` hook 在本仓库无法安全安装

| 字段 | 内容 |
|---|---|
| **发现于** | RT-029（AODW 产品基线收敛），2026-08-31 |
| **状态** | 已认领（RT-030，2026-08-31）——转为受控例外 EX-001，未结清 |
| **建议认领时机** | 例外复查日 2026-11-30 之前；或原 PR-001 工作树处置完毕时提前处理 |

AODW 的 G001 判据检查 `git rev-parse --git-common-dir` 下有没有 `pre-commit`
hook，未装则每个 RT 报一条告警。CWK 的实际情况是：`main` 检出、RT-028/RT-029 的
worktree 与原 PR-001 工作树共用同一个 git common dir
（`/Users/evan/.openclaw/.../CWK/.git`）。hook 是 common dir 级别的，装一次对**所有**
worktree 生效——包括用户明确要求保持原样、不得写入的 PR-001 工作树。

RT-029 的处理：不装。G001 是告警级，不阻断；`make aodw-check` 会照实报出来。
用「为了消一条告警而去动受保护工作树的提交路径」交换是明显的坏买卖。

遗留问题：等 `main` 的落点定了、原 PR-001 工作树处置完毕，再决定是否启用 hook。
在那之前防绕底线层没有生效，RT 门禁只在有人主动跑 `make aodw-check` 或 CI 时执行。

**RT-030 的处置（2026-08-31，已认领未结清）**：仍然不装 hook——理由未变，且用户
明确要求不得写共享 git common-dir hook。但也不接受「永久 warn」这个形态：warn 没有
主、没有到期日、没有退出标准，本质上就是无限期豁免。改为在
`.aodw-next/06-project/governance/code-ownership-manifest.json` 里立**受控例外
EX-001**，四要素齐全且全部机器可验：

| 要素 | 内容 |
|---|---|
| owner | CWK maintainer |
| trigger_condition | 多个 worktree 共用同一 git common dir，且其中含用户要求只读的 PR-001 工作树 |
| exit_criteria | PR-001 工作树处置完毕、common dir 不再被只读工作树共用之后，安装 pre-commit hook 并撤销本例外 |
| review_by | 2026-11-30（过期即 `GA-EXCEPTION-EXPIRED` 红，不自动续期） |
| scope_limit | 仅豁免「本地 pre-commit 时点拦截」；不豁免任何判据本身 |

补偿控制不是声明，是当场验证的（`GA-CONTROL`）：

- **CC-1**：`.github/workflows/ci.yml` 必须有 `run:` 步骤真的在跑 `make ci`
  ——只认执行位置，注释里写 `make ci` 不算数（这一点是被测试抓出来的实漏洞）；
- **CC-2**：`make ci` 的配方里必须包含 `governance-audit`；
- **CC-3**：`Makefile` 必须存在 `governance-audit` 目标。

三条任一被摘掉，例外当场失效、门变红。换言之：本地拦不住，就必须让权威门在 CI 上
真的存在——把门摘了想借例外蒙混过关是走不通的。
由 `tests/test_governance_audit.py::TestExceptionBoundaries` 与
`TestCompensatingControlsAreReal` 强制。

未结清部分：hook 本身仍未安装，防绕底线层在本地依然不生效。这条留在台账里，
到 2026-11-30 必须重新决策——届时门会因为例外过期自己变红来提醒。

## 已认领 / 已结清

条目正文仍留在上面「待认领」各自原位（保持发现时的事实原样，便于对照 RT-029 当时
的判断），此处只做索引：

| 编号 | 认领 RT | 结果 | 落点 |
|---|---|---|---|
| DI-001 | RT-030 | 已结清 | `script-evolution-v2.json` 的 `continuation_slots`；判据 `GA-V2-*` |
| DI-002 | RT-030 | 已结清 | `script-evolution-v2.json` 的 `legacy_evolution`；判据 `GA-LEGACY-*` |
| DI-003 | RT-030 | 已认领，未结清 → 受控例外 EX-001 | `code-ownership-manifest.json` 的 `exceptions`；判据 `GA-EXCEPTION*` / `GA-CONTROL`；复查日 2026-11-30 |
