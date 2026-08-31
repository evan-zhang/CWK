# RT-Lite: RT-030 - 现有代码全量规范接管

> profile: Spec-Lite | execution_mode: collaborative

## 方案（给人看）

- 做什么：把当前 `main` 上**全部 578 个受版本控制文件**纳入一份可机器验证的归属清单，
  并加一道每次 `make ci` 都跑的门。门的判据是「这棵树上没有无人认领的文件」，
  而不是「新加的文件记得登记」。
- 为什么：现在的管辖只覆盖 `scripts/cwk_*.py` 这一个命名空间（64 个文件）加
  `install.sh`。**其余 513 个文件没有任何归属判据**，其中至少 6 个是真正的运行时
  产品文件——包括一个可执行的 `scripts/cwk_wiki_batch_driver.sh`，它带着模型名字面量、
  会写 `runs/`、会碰知识镜像，却因为后缀是 `.sh` 而被 `^scripts/cwk_[a-z0-9_]+\.py$`
  静默漏掉。这不是假想的漏洞：DI-002 记录的 `cwk_ai_common.py` 模型清单漂移，
  和这个文件属于同一风险类别。
- 代价：今后新增 `scripts/`、`config/`、`references/`、`skill/`、`.github/` 下的文件
  或改动 6 个敏感文件，必须同时更新清单，否则 `make ci` 会红。这是有意的摩擦——
  它正是「接管」的含义。清单本身很小，改一行即可。
- 这次故意不做什么：不改任何 PR-001 冻结契约（`policy_v1.json`、安全登记表一个字节
  都不动）；不倒改历史 RT；不改产品运行行为；不合并、不推送、不清理 worktree；
  不把原 PR-001 脏工作树的在研内容算作已接管代码。
- 用户怎样算成功：`make ci` 里多一道 `make governance-audit`，它能当场回答「当前这棵
  树上每个文件归谁管、怎么改」；往 `scripts/` 里丢一个新文件、或悄悄改掉敏感文件而
  不登记，CI 会红；DI-001/DI-002 从「无路可走」变成「有主的前向演化路径」，
  DI-003 从「永久告警」变成「有主、有触发条件、有退出标准、且补偿控制被机器验证」的例外。
- 建议：**推荐**按本方案做。它不改产品行为，风险集中在「以后改文件要多改一行清单」，
  而收益是把 513 个文件从零判据变成有判据。

## 假设与现状

- 关键假设：`16b4ee0`（= 当前 `origin/main`）是本 RT 的基线；原 PR-001 脏工作树的
  27 项未提交内容属于在研工作，**不是**本 RT 要接管的代码。
- 现状依据（已实测，非凭记忆）：
  - `git ls-files` = 578 个受跟踪文件。
  - 现有唯一的代码归属机制是 `security_gate_registry_v1.json` 的
    `managed_script_inventory`：`namespace_pattern = ^scripts/cwk_[a-z0-9_]+\.py$`
    加 `explicit_managed_paths = ["install.sh"]`，三族划分
    owner(48) / central(1) / legacy(53) = 102 条声明，其中 37 条指向尚未存在的未来文件。
  - 该命名空间在文件系统侧确实是闭的：`tests/pr001_evidence_binding.py:1842`
    要求磁盘上匹配命名空间的文件 ⊆ 已声明集合。**但闭合只在这一个命名空间内成立。**
  - 未被任何判据覆盖的 513 个文件里，经追踪确认为运行时的有：
    | 文件 | 为什么是运行时 |
    |---|---|
    | `scripts/cwk_wiki_batch_driver.sh` | 100755 可执行；带 `MODEL`/`REPAIR_MODEL` 默认模型字面量；写 `runs/`；驱动 wiki 精编批次 |
    | `skill/templates/CONFIG.example.json` | `make doctor`、三个 smoke 目标、`install.sh` 都读它；安装时被复制成 `cwk-mirror.local.json` |
    | `config/entity-family-registry.json` | `cwk_entity_catalog.py:473` 作为 canonical 实体族注册表加载 |
    | `references/relation-gold-v1.json` | `cwk_relation_eval.py:64` 的默认 gold 集 |
    | `Makefile` | 定义 `make ci` 本身——门的定义 |
    | `.github/workflows/ci.yml` | CI 权威入口 |
  - 三条已知债务的真实约束：`policy_v1.json` 的 sha256 被
    `security_gate_registry_v1.json.script_evolution_policy_sha256` 固定；
    登记表 schema 是 `additionalProperties: false`；v1 回执闭合按
    `RT/*/receipts/script-evolution/*` 取全集（`test_pr001_script_evolution_guard.py:3125`）。
    这三条共同决定：**扩展只能走旁挂的前向版本，不能改 v1。**

## 实现备注（用户不问可不展开）

- 计划改的文件：
  - 新增 `.aodw-next/06-project/governance/`：`code-ownership-manifest.json`（全树清单）、
    `script-evolution-v2.json`（前向演化叠加层）、`README.md`（分层说明）；
  - 新增 `.aodw-next/06-project/governance-audit.py`（检查器，纯标准库）；
  - 新增 `tests/test_governance_audit.py`（正反向测试）；
  - 改 `Makefile`（加 `governance-audit` 目标并进 `ci`）、`AGENTS.md`（常用检查一节）；
  - 本 RT：`RT/RT-030/`、收口时 `RT/_deferred-items.md`。
- 不能破坏的约定：
  - `PR/` 下任何冻结契约与已签名证据**零字节改动**；v2 回执落在
    `RT/*/receipts/script-evolution-v2/`，不进 v1 的 glob；
  - 不新增 `scripts/cwk_*.py`（那个命名空间是闭的，加文件会撞 PR-001 闭合判据）；
  - 不放宽任何既有断言，不删测试，不靠加宽 glob 变绿；
  - 产品运行行为不变。
- 内部阶段：
  1. **G0 清单**：按「exact-only 区」建全树分类，runtime 只允许精确路径。
  2. **G1 检查器**：闭合、无孤儿、无失效规则、runtime 必须有主、敏感文件必须有演化路径、
     例外必须有边界且未过期、上游冻结契约按哈希校验完整。
  3. **G2 前向演化层**：v2 叠加层为 DI-001 的耗尽槽位与 DI-002 的 legacy 族提供有主路径。
  4. **G3 DI-003**：转为受控例外，补偿控制（CI 权威门）由机器验证。
  5. **G4 门禁接线 + 测试**：`make governance-audit` 进 `make ci`；正反向测试。
  6. **G5 全量验收**：`make ci` 跑满，B4 复核受保护工作树指纹。

## 验证

- 要跑的检查：
  - `make governance-audit`（新入口）
  - `make aodw-check`（不得回归）
  - `make ci`（= test + aodw-check + governance-audit，全量约两小时）
  - `python3.11 -m unittest tests.test_governance_audit -v`（定向）
  - B4：原 PR-001 工作树 HEAD + 三个 SHA-256 + 27 项计数逐字复核
  - `git diff --check`、敏感文件与运行产物扫描
- 对照成功标准的结果：见「变更记录」。

## 变更记录

接管范围、覆盖矩阵、例外清单与证据边界的完整版见
`RT/RT-030/takeover-audit.md`。这里只记「做了什么、结果如何」。

| 阶段 | 内容 | 结果 |
|---|---|---|
| G0 | 全树盘点：578 个受跟踪文件，仅 65 个有归属判据（11.2%），513 个无判据 | 定位到 6 个 runtime/门禁盲点，逐个追到调用证据 |
| G1 | 建 `code-ownership-manifest.json`（5 域 / 20 条规则 / 6 个 exact_only_zone） | 585 个文件全部命中，孤儿 0 |
| G2 | 建 `script-evolution-v2.json` 前向叠加层，结清 DI-001 / DI-002 | 未改任何 PR-001 冻结契约；v1→v2 交接点用链尾哈希证明 |
| G3 | DI-003 转受控例外 EX-001（四要素齐全，三条补偿控制机器验证） | 认领未结清，复查日 2026-11-30，过期即红 |
| G4 | `make governance-audit` 接入 `make ci`；`ci.yml` 同步；补 `GA-SELF`（门管住自己） | 54 个用例全绿 |
| G5 | 全量 `make ci` + B4 复核 | 见下 |

对照成功标准：

- **判据面是当前代码树全集**：`git ls-files` 585 个文件逐个命中规则，
  分域计数之和 = 总数（有断言）。不是「新增才受管」。
- **runtime 逐个有主、有变更入口、有演化路径**：69 个 runtime 文件全部满足；
  缺任一项的规则形态本身判失败（`GA-OWNER` / `GA-CHANGE-ENTRY` / `GA-EVOLUTION`）。
- **不靠固定 hash 永久放行**：pin 只作漂移探测器；「有 pin 无演化路径」被显式判失败。
- **不伪造历史**：未改 `policy_v1.json`、未改安全登记表、未回填历史 RT 格式；
  `test_frozen_upstream_contracts_are_untouched` 当场验证。
- **例外有边界**：全仓库仅 1 条例外，四要素齐全 + 到期强制复查 + 补偿控制可验。
- **不改产品运行行为**：本 RT 未触碰任何 `scripts/` 下的产品代码；改动集中在
  `.aodw-next/06-project/governance*`、`Makefile`、`.github/workflows/ci.yml`、
  `AGENTS.md`、`RT/` 记录与新增测试。`Makefile` 只新增目标，未改既有配方。

实现过程中自己抓到并修掉的三个真问题（都留了注释，防再犯）：

1. **delegated 解析过度吸收**：首版把上游 `owner_code_path_prefixes` 里的
   条目全部当作受管代码，误吞 31 个文档（runtime 报 100 而非 69）——
   这正是用户要求消除的「宽选择器吞掉未知文件」。改为只认上游自己的命名空间语义
   （`namespace_pattern` 全匹配或 `explicit_managed_paths`）。
2. **CC-1 假绿**：首版对 `ci.yml` 全文做 `make ci` 子串匹配，而该文件注释里就写着
   「make ci = …」，于是把 `run: make ci` 换成 `run: make test` 后补偿控制依然通过。
   改为只认 `run:` 的执行位置（行内标量或块标量）。由
   `test_comment_mentioning_make_ci_does_not_satisfy_the_control` 锁死。
3. **门没管住自己**。首版把 `.aodw-next/06-project/` 整个交给一条 `prefix` 规则、
   落在 `docs_governance` 域——该域不要求演化路径也不要求变更入口。于是检查器、
   判据清单、v2 叠加层自己全躺在「宽前缀 + 无 pin」下面：把 `governance-audit.py`
   掏成 `sys.exit(0)`，门照样绿。这是本 RT 要消灭的形态发生在门自己身上，
   属于最该堵的一类。修法见 `takeover-audit.md` §2.2：该目录列入 exact-only 区，
   4 个门禁机器进 `build_ci` 并带 pin，新增 `GA-SELF` 判据防止这套管辖被撤销；
   清单自身的不动点问题用**仅限本文件**的 `self_pin_impossible` 如实声明，
   其他规则声明即报错，避免它退化成通用免检通道。
   由 `TestGateGovernsItself` 十个用例锁死。

值得记下的一点：GA-ZONE 在我把 `.aodw-next/06-project/` 设为 exact-only 区之后，
立刻报出上游 `.aodw-next/` 前缀规则会伸进该区——那是真漏洞（新文件会被静默
吸收成「上游资产」），不是误报。修法是给它加显式 `exclude_prefixes`，
而不是放宽判据。门抓到了自己作者的错，这条判据是可信的。

## 遗留事项

本 RT 认领 DI-001 / DI-002 / DI-003（见 `meta.yaml.deferred_items_claimed`）：
前两条已结清，DI-003 只能部分处置，条目留在台账等复查日。
未转出新的 DI 条目（`deferred_items_raised: []`）。

需要用户决策的三件事（详见 `takeover-audit.md` §9）：

1. **EX-001 到期处置**（2026-11-30）——取决于原 PR-001 工作树何时处置。
2. **`legacy_frozen_files` 的 steward 归属**——当前是
   `default_steward: "CWK maintainer"`，其中 `cwk_ai_common.py` 与
   `cwk_cloud_wiki_compile.py` 已标 `high_risk_members`，是否指定具体承接人/RT 待定。
3. **RT-029 状态不一致**——`RT/index.yaml` 记 `in-progress`，
   `RT/RT-029/meta.yaml` 记 `completed`。属于 RT-029 的收口面，本 RT 不静默改别人的账。
