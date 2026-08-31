# RT-030 接管审计 — 当前代码树的归属与演化管辖

> 口径：**当前 `main` 上所有受版本控制的文件**，不是「以后新增的才受管」。
> 判据面由 `git ls-files` 给出，任何文件没有归属判断即为孤儿，门直接红。
> 本文档只描述**接管范围与边界**；判据本身是机器可执行的，写在
> `.aodw-next/06-project/governance/code-ownership-manifest.json`，
> 由 `make governance-audit` 强制，`make ci` 包含它。

- 建立于：RT-030，2026-08-31
- 基线：`16b4ee0789be3c4e6580a909497c9e7a017d0d47`（RT-029 全量验收通过的 SHA，
  已由 orchestrator 快进推送到 `origin/main`）
- 检查入口：`make governance-audit` → `.aodw-next/06-project/governance-audit.py --root .`
- 回归测试：`tests/test_governance_audit.py`（44 个用例）

## 1. 接管前的实际状况

接管前，本仓库唯一存在的「代码归属」判据是 PR-001 的
`security_gate_registry_v1.json`：它按
`namespace_pattern = ^scripts/cwk_[a-z0-9_]+\.py$` 加
`explicit_managed_paths = ["install.sh"]`，把 **65** 个文件分进
owner / central / legacy 三族，并做闭合性校验。

基线 `16b4ee0` 上受跟踪文件共 **578** 个。也就是说：

| | 文件数 | 占比 |
|---|---|---|
| 有任何归属判据 | 65 | 11.2% |
| **无任何归属判据** | **513** | **88.8%** |

这 513 个里绝大多数是文档、RT/PR 记录、测试和上游引入的 AODW 框架文本——
风险不高，但同样属于「没人说过归谁管」。真正的问题是其中夹着**产品运行路径上的
文件**。逐个追证据后确认了 6 个 runtime / 门禁盲点：

| 文件 | 为什么它在运行路径上 | 此前为何漏掉 |
|---|---|---|
| `scripts/cwk_wiki_batch_driver.sh` | `100755` 可执行，内含 `MODEL` / `REPAIR_MODEL` 字面量，向 `runs/` 写入 | 正则要求 `.py` 结尾，`.sh` 不匹配 |
| `skill/templates/CONFIG.example.json` | `make doctor` 与三个 smoke 目标都读它；`install.sh:59` 也读 | 不在 `scripts/` 下 |
| `config/entity-family-registry.json` | `cwk_entity_catalog.py:473` 的权威实体族登记表 | 同上 |
| `references/relation-gold-v1.json` | `cwk_relation_eval.py:64` 的默认 gold set | 同上 |
| `Makefile` | 定义 `make ci` 本身——门的定义 | 同上 |
| `.github/workflows/ci.yml` | CI 权威门 | 同上 |

其中 `cwk_wiki_batch_driver.sh` 是本 RT 的起因：一个带模型字面量、会写运行目录的
可执行脚本，只因为扩展名不是 `.py` 就完全落在管辖之外。

## 2. 接管后的覆盖矩阵

当前工作树受跟踪文件 **585** 个（578 基线 + RT-030 新增 7 个：`RT/RT-030/` 三份
记录、治理清单与 v2 叠加层两份判据、检查脚本、测试文件）。
分域计数之和等于总数——差额意味着有文件没被计入任何域，测试
`test_every_tracked_file_is_accounted_for` 对此有断言。

| 域 | 文件数 | 要求 owner | 要求演化路径 | 要求变更入口 |
|---|---|---|---|---|
| runtime | 69 | ✔ | ✔ | ✔ |
| test | 102 | ✔ | — | — |
| build_ci | 9 | ✔ | ✔ | ✔ |
| docs_governance | 308 | ✔ | — | — |
| vendor_external | 97 | ✔ | — | — |
| **合计** | **585** | | | |

### 2.1 runtime（69）

| 规则 | 匹配方式 | 数量 | owner | 演化路径 |
|---|---|---|---|---|
| `R-runtime-pr001-managed-scripts` | delegated | 65 | PR-001（逐文件见登记表三族） | `pr001-script-evolution-v1` |
| `R-runtime-wiki-batch-driver` | exact | 1 | RT-030 | `cwk-governance-repin-v1` |
| `R-runtime-config-template` | exact | 1 | RT-030 | `cwk-governance-repin-v1` |
| `R-runtime-entity-family-registry` | exact | 1 | RT-030 | `cwk-governance-repin-v1` |
| `R-runtime-relation-gold` | exact | 1 | RT-030 | `cwk-governance-repin-v1` |

`delegated` 不是「引用一个大 glob」。它按上游权威**自己的**命名空间语义取集合：
`namespace_pattern` 全匹配，或命中 `explicit_managed_paths`。
这条过滤不可省略——登记表的 `entries[].owner_code_path_prefixes` 里同时装着
`RT/RT-0NN/specs/*.md` 这类文档选择器，不过滤就会把 31 个文档误吞成 runtime。
本 RT 实现过程中确实先犯了这个错（runtime 报 100 而非 69），已修正并留了注释。

### 2.2 build_ci（9）

| 规则 | 文件 | pin |
|---|---|---|
| `R-build-makefile` | `Makefile` | ✔ |
| `R-build-github-ci` | `.github/workflows/ci.yml` | ✔ |
| `R-buildci-governance-audit-script` | `.aodw-next/06-project/governance-audit.py` | ✔ |
| `R-buildci-ownership-manifest` | `.aodw-next/06-project/governance/code-ownership-manifest.json` | 不动点，见下 |
| `R-buildci-evolution-overlay-v2` | `.aodw-next/06-project/governance/script-evolution-v2.json` | ✔ |
| `R-buildci-aodw-check-script` | `.aodw-next/06-project/aodw-check.sh` | ✔ |
| `R-build-gitignore` | `.gitignore` | — |
| `R-build-env-example` | `.env.example` | — |
| `R-build-version` | `VERSION` | — |

**门自己也被门管住**（`GA-SELF`）。这一条是实做时发现自己犯了同一个错才补上的：
首版把 `.aodw-next/06-project/` 整个交给一条 `prefix` 规则、落在 `docs_governance`
域——那个域既不要求演化路径也不要求变更入口。于是检查器自己、判据清单自己、
v2 叠加层自己全躺在「宽前缀 + 无 pin」之下：把 `governance-audit.py` 掏成
`sys.exit(0)`，门照样绿。这正是本 RT 要消灭的形态，只不过发生在门自己身上。

修法：`.aodw-next/06-project/` 列入 `exact_only_zones`，8 个文件逐个显式登记，
4 个门禁机器进 `build_ci` 并带 pin。`GA-SELF` 强制这几点不能被撤销——
删掉检查器自己的规则、把它降域、或去掉 `sensitive` 标记，都当场判失败。

**清单本身不能 pin 自己**：写入自身哈希会构成不动点。这一点在规则里用
`self_pin_impossible` + `self_pin_reason` 如实声明，并且**只有清单本文件**
可以声明它——其他规则声明即报 `GA-SELF`，防止它退化成通用免检通道。
清单的完整性改由结构判据（`GA-ZONE` / `GA-STALE-RULE` / `GA-ORPHAN`）与
测试对具名规则的断言兜底。

另外，`.aodw-next/` 的上游前缀规则必须用 `exclude_prefixes` 显式把项目专属区
挖掉，否则它会够到 exact-only 区、把新文件静默吸收成「上游资产」。
撤掉这个排除项 → `GA-ZONE` 红；拿个无关子目录搪塞也不算数（排除项必须真的
覆盖该区）。两条都有测试。

### 2.3 test（102）/ docs_governance（311）/ vendor_external（97）

按前缀与精确集合分派，逐条列在清单的 `rules` 里。
`vendor_external` 是从 AODW 上游 `bd-eval-loop @ 08b0523c` 整体引入的
`.aodw-next/` 框架文本，本仓库不单独演进它。

## 3. 防「加宽 glob 蒙混过关」的结构约束

用户明确要求：不许靠放宽 glob 让未知文件混过去。清单里有三条结构性约束，
都是机器强制的：

1. **`exact_only_zones`**：在 `""`（仓库根）、`scripts/`、`config/`、
   `references/`、`skill/`、`.github/`、`.aodw-next/06-project/` 这七个区域内，
   只允许 `exact` / `exact_set` / `delegated` 规则。任何 `prefix` 规则**够得到**
   这些区域 → `GA-ZONE` 红（含从外层伸进来的宽前缀，除非用 `exclude_prefixes`
   显式挖掉整个区）。后果是：往这些目录里新增文件，必然是孤儿，必须显式登记才能过。
2. **零命中规则即失败**（`GA-STALE-RULE`）：写一条谁也不匹配的规则来「预留空间」
   是不行的，清单不会慢慢偏离现实。
3. **闭合性**（`GA-ORPHAN`）：`git ls-files` 全集逐个必须命中某条规则，
   首个命中生效，无命中即孤儿。

## 4. 敏感文件：pin 是漂移探测器，不是放行条件

6 个文件带 `pin_sha256`（含 mode）：4 个 runtime + `Makefile` + `ci.yml`。

用户明确要求「不允许只靠固定 hash 就永久放行」。这里的语义是：

- pin **只**用来发现「有人改了但没走流程」（`GA-PIN-DRIFT`）；
- 放行条件是 `change_entry` 描述的动作：开 RT → 在 `rt-lite.md` 写明理由 →
  更新 pin → 复跑 `make governance-audit`；
- 一个「有 pin 但没有 evolution_path」的 runtime 规则**本身就判失败**
  （`GA-EVOLUTION`）。测试
  `test_sensitive_file_with_pin_but_no_evolution_path_fails` 锁住这一点——
  「只靠固定 hash 永久放行」这个形态被显式禁止。

## 5. 冻结历史的处理：只做前向叠加，不回改

三条硬约束决定了扩展方式（都已实测确认）：

1. `policy_v1.json` 的 sha256 被 `security_gate_registry_v1.json`
   **交叉固定**（`script_evolution_policy_sha256`）；
2. 登记表 schema 是 `additionalProperties: false`，加不了新键；
3. v1 的回执闭合性测试（`tests/test_pr001_script_evolution_guard.py:3110`）
   glob 的是 `RT/*/receipts/script-evolution/*`。

结论：**不能改 v1**，只能旁挂前向版本。做法是
`.aodw-next/06-project/governance/script-evolution-v2.json`：

- `inherits` 按 sha256 钉住上面两份上游权威，任一漂移即 `GA-UPSTREAM` 红；
- 回执根目录 `RT/<owner_rt>/receipts/script-evolution-v2/` 与 v1 分离，
  不落进 v1 的 glob，不污染既有回执集合；
- 兼容规则 CR-1..CR-6 显式声明 v1 语义如何延续（尤其 `owner_rt` 锁定不变）。

**v1 → v2 的交接点是可证明的**：v2 每条续槽的 `v1_chain_tip_sha256` 取自
v1 stage-06 / stage-08 回执的 `to_sha256`，已验证与当前磁盘哈希逐字相等。
这不是声称的衔接，是算出来的。

本 RT **没有修改任何 PR-001 冻结契约**——
`tests/.../TestRealRepository::test_frozen_upstream_contracts_are_untouched`
当场验证这一句话。

## 6. 例外清单

全仓库当前只有 **1 条**例外。

| 编号 | 主题 | owner | 触发条件 | 退出标准 | 复查日 | 作用域限制 |
|---|---|---|---|---|---|---|
| EX-001 | DI-003：pre-commit hook 无法安全安装 | CWK maintainer（RT-030 认领） | main 检出、RT worktree 与用户要求只读的 PR-001 工作树共用同一 git common dir；hook 是 common dir 级别的，装一次会写到受保护工作树的提交路径上 | PR-001 工作树处置完毕、不再共用 common dir 后，安装 hook 并撤销本例外 | 2026-11-30 | 只豁免「本地 pre-commit 时点拦截」，不豁免任何判据，不降低 CI 强制性 |

例外的四要素（owner / 触发条件 / 退出标准 / 复查日）缺一即 `GA-EXCEPTION` 红。
复查日过期即 `GA-EXCEPTION-EXPIRED` 红，**不自动续期**——这是它区别于永久 warn
的地方。

补偿控制是当场验证的，不是声明：

| 控制 | 断言 | 验证方式 |
|---|---|---|
| CC-1 | CI 是权威门，且它跑的就是 `make ci` | 解析 `ci.yml`，要求存在一个 `run:` 步骤真的在执行 `make ci` |
| CC-2 | `make ci` 真的包含 `governance-audit` | 解析 `Makefile` 的 `ci` 目标配方 |
| CC-3 | 本地有等价入口，不需要写 hook | 断言 `Makefile` 存在 `governance-audit` 目标 |

三条任一被摘掉，例外当场失效、门变红——想靠「摘掉门 + 留着例外」蒙混是走不通的。

CC-1 的实现被自己的测试抓出过一个真漏洞：最初对 `ci.yml` 全文做 `make ci` 子串匹配，
而该文件的**注释**里就写着「make ci = make test + …」，于是把
`run: make ci` 改成 `run: make test` 之后控制依然假绿。现在只认执行位置
（`run:` 的行内标量或块标量），注释不算数。见
`test_comment_mentioning_make_ci_does_not_satisfy_the_control`。

## 7. 证据边界：什么**没有**被接管

明确记录，避免把「在研内容」误当成已接管资产。

### 7.1 原 PR-001 工作树 — 在研，不属于当前 main 基线，不计入接管

路径：`/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK`

用户要求该工作树**只读**，其 27 项未提交内容不属于当前 main 基线。
本 RT 对它只做了只读指纹复检，未执行任何写操作
（无 stash / reset / checkout / clean / add / commit / 修改）。

复检结果（RT-030，2026-08-31），与 RT-029 冻结值**逐字相同**：

| 项 | 值 |
|---|---|
| HEAD | `fe4add1e752d1e1a2438ad0f421d635a96321d02` |
| `git status --porcelain=v2` SHA-256 | `57c287cb154d956ea0667e12ed1433d5281e03d49f165dd9379db0501c151d8d` |
| 已跟踪二进制 diff SHA-256 | `6d42d47989dfa8b6c5ba734cf2b12af12f246f32871e97c9885c5426d5628b81` |
| 未跟踪路径清单 SHA-256 | `e0fb23503b348f0f8ea55120fd16b9a5bc40b504f181ced2aed4ea406f3f0ca8` |
| `status --porcelain` 条目数 | 27 |

复检命令见 `RT/RT-029/baseline-audit.md`「复检命令（只读）」，三条均只读。

**这些内容不计入接管覆盖率**：它们不在 `git ls-files` 的判据面上，
本清单不对它们做任何归属声明。将来若要合入 main，届时会被闭合性检查当作
新增文件对待——没有归属就是孤儿，必须显式登记。

### 7.2 RT-028

基于旧产品主线，含尚未合入的 Work Agent 设计与实验实现。本 RT 不迁移、不并入。

### 7.3 未跟踪与运行数据

`runs/`、`knowledge/`、`raw/`、`collected-raw/` 等默认不提交，不在 `git ls-files`
判据面上。判据面就是版本控制面——这一点是有意的：接管的对象是「受版本控制的代码」。

### 7.4 历史 RT 的记录格式

未按新格式回填历史 RT，也未回溯造账。`RT-017..RT-026` 等接管前存量在清单里
以 `owner: 历史存量（AODW 接入前）` 如实登记，事实保持原样。
`.aodw-next/project.yaml` 的 `rt_gate_scope.managed_from: RT-028` 界定了
方法层门禁面；本清单界定的是**代码层**归属面，两者不互相冒充。

## 8. 门会咬人的证据

`tests/test_governance_audit.py`，54 个用例，全部在合成仓库上做**改坏 → 必须红**
与**合规 → 必须绿**的双向验证。合成仓库先自证基线为绿
（`TestSyntheticBaselineIsClean`），否则后面所有「改坏就红」的断言都没有意义。

| 用户要求的断言 | 对应用例 |
|---|---|
| 新增孤儿 runtime 文件会失败 | `TestOrphanDetection`：新增 `.py` / `.sh` / config / 根文件各一例 |
| 显式分类之后能通过（门可用，不是只会拒绝） | `test_explicitly_classified_file_passes` |
| 敏感文件无 owner / 无演化路径会失败 | `TestRuntimeOwnershipRequirements` 四例 |
| 「只靠固定 hash 永久放行」这一形态判失败 | `test_sensitive_file_with_pin_but_no_evolution_path_fails` |
| 例外有边界 | `TestExceptionBoundaries`：缺 owner / 缺退出标准 / 缺补偿控制 / 已过期 各一例 |
| 加宽 glob 直接判失败 | `TestExactOnlyZones` 两例 |
| 冻结契约被动即红 | `TestUpstreamFrozenContracts` 两例 |
| DI-001 续槽可用且不可越界 | `TestContinuationSlots` |
| DI-002 双向闭合 | `TestLegacyFamilyEvolution` 五例（含「假回执不能洗白漂移」） |
| 补偿控制是真的 | `TestCompensatingControlsAreReal` 六例 |
| 门自己也被管住 | `TestGateGovernsItself` 十例 |
| 当前真实树确实全覆盖 | `TestRealRepository` 四例 |

反向作弊路径也被堵住：删断言会被 `TestRealRepository` 抓；放宽 glob 会被
`GA-ZONE` 抓；写空规则占位会被 `GA-STALE-RULE` 抓；改冻结契约会被 `GA-UPSTREAM`
抓；掏空检查器本身会被 `GA-PIN-DRIFT` 抓；撤销门对自己的管辖会被 `GA-SELF` 抓。

## 9. 仍需用户决策

1. **EX-001 到期处置**（2026-11-30）：取决于原 PR-001 工作树何时处置。
   到期不处理，门会自己红——这是有意设计的强制函数。
2. **`legacy_frozen_files` 的 steward**：v2 叠加层当前写的是
   `default_steward: "CWK maintainer"`。这 53 个文件里
   `cwk_ai_common.py`（模型允许清单）与 `cwk_cloud_wiki_compile.py`
   （默认模型字面量）已标为 `high_risk_members`，是否要指定具体的人或 RT 承接，
   由用户决定。
3. **RT-029 状态不一致**（本 RT 发现，未擅自改动）：
   `RT/index.yaml` 记 RT-029 为 `in-progress`，而 `RT/RT-029/meta.yaml` 记
   `completed`。这是历史记录的事实差异，属于 RT-029 的收口面，
   本 RT 不静默修改别人的账。请确认应以哪一边为准。
