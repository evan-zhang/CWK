# RT-Lite: RT-029 - CWK AODW 产品基线收敛

> profile: Spec-Lite | execution_mode: collaborative

## 方案（给人看）

- 做什么：把 `pr/PR-001-completion` 已提交的 `fe4add1` 固定为当前产品代码基线，
  在独立分支中将它与本地 AODW 主线收敛；修复基线本身的测试、AODW 规则与 CI，
  形成后续 RT 可以共同依赖的干净 `main` 候选。
- 为什么：当前完整产品代码、AODW 主线和 GitHub `main` 分处三条历史；继续开发会让
  RT 基于不同代码事实，测试结果也无法作为统一验收证据。
- 代价：本 RT 会处理历史测试债、平台兼容和方法层冲突，短期不新增产品功能；
  `RT-028` 必须等基线收敛后重新承接，不能直接把旧基线实现并入产品主线。
- 这次故意不做什么：不纳入 `pr/PR-001-completion` 工作树的未提交改动；不继续
  PR-001 在研功能；不启用 Work Agent；不接触真实工作汇报、生产配置、模型费用；
  不合并、不推送、不清理任何现有 worktree。
- 用户怎样算成功：本地出现一条可审阅的 RT-029 功能分支，它完整包含 `fe4add1`
  已提交产品代码；原 PR-001 脏工作树保持原样；支持的本地检查和 GitHub CI 使用
  同一套命令且全部通过；AODW 对新 RT 的创建、实施和门禁只有一套不冲突的规则。
- 建议：**推荐**先完成本 RT 再恢复 RT-028；否则 Work Agent 会继续建立在缺失当前
  产品代码的旧主线上，后续只能重复迁移。

## 假设与现状

- 关键假设：`fe4add1e752d1e1a2438ad0f421d635a96321d02` 是用户确认的产品基线；
  其后的未提交内容属于另一条在研工作，不代表本 RT 可采纳的产品事实。
- 现状依据：
  - GitHub 仓库为 `evan-zhang/CWK`，远端默认 `main` 停在 `445f661`。
  - 本地 `main` 在占号后为 `a0d24c1`，只承载旧产品代码、AODW 和 RT-028/029 元数据。
  - `pr/PR-001-completion` 已提交头为 `fe4add1`，相对共同祖先包含完整 PR-001
    产品实现；原工作树另有 24 个已跟踪文件改动和 3 个未跟踪目录。
  - 现有 GitHub CI 只执行 `make test`；AODW fixture 和 RT 门禁未进入 CI。
  - 当前 `spec-lite-profile.md` 与 AODW 宪章对 `rt-lite.md` 的落点表述冲突；
    宪章为权威，本 RT 只修正从属规则，不倒改历史 RT。

## 实现备注（用户不问可不展开）

- 计划改的文件：
  - 产品基线：把 `fe4add1` 的已提交树合入本 RT 分支，不复制原工作树状态；
  - 方法层：`.aodw-next/`、`AGENTS.md`、`Makefile`；
  - 自动验证：`.github/workflows/ci.yml`、必要的测试兼容修复与脚本演化回执；
  - 本 RT：`RT/RT-029/`。
- 不能破坏的约定：
  - `raw/` 仍是唯一事实源，CWK 对 CWork 保持只读；
  - 不降低现有脚本演化、权限、证据和安全门禁；
  - 只接受 `fe4add1` 中已提交的产品内容；原工作树不得被 stash、checkout、reset、
    暂存或提交；
  - 历史 RT 只作证据，不强行回填成 AODW 新格式；
  - RT-028 的设计和实验提交不在本 RT 合入范围，基线完成后另行迁移或重建。
- 内部阶段：
  1. **B0 基线冻结**：记录提交、树哈希、分支差异和原脏工作树指纹；在本 RT 分支
     合入 `fe4add1`，确认产品内容没有从工作树偷带。
  2. **B1 测试收敛**：在干净基线上运行 `make doctor`、`make test`，按失败族修复；
     已接受的脚本变化补可核验回执，平台差异通过兼容实现解决，禁止删除有效断言。
  3. **B2 AODW 收敛**：统一宪章与从属规则，校正工具状态，让默认门禁只约束
     AODW 管理的新 RT，并提供一个稳定的仓库级检查入口。
  4. **B3 CI 对齐**：GitHub CI 与本地复用同一检查入口，覆盖产品测试、AODW
     fixture 和 RT-029 门禁。
  5. **B4 候选验收**：重跑全部检查，核对原脏工作树指纹、分支历史和敏感文件；
     达标后停在收口门，由用户决定是否合并、推送和清理。

## 验证

- 要跑的检查 / 要点的界面路径：
  - `make ci`（= `make test` + `make aodw-check`；GitHub CI 跑的是同一条）
  - `make doctor`（含 Python 版本闸）
  - `make test`（doctor、py_compile、全量单测、三个脱敏 smoke）
  - `make aodw-check`（框架 fixture 79 条、受管 RT 门禁、RT 花名册一致性、宿主 skill 状态）
  - `.aodw-next/tools/rt-guard.sh --root . --rt RT-029`
  - `.github/workflows/ci.yml` 静态校验（YAML 可解析、job id 未改、入口即 `make ci`）
  - `git diff --check`、敏感文件/运行产物扫描
  - 原 PR-001 工作树 HEAD、状态清单、已跟踪 diff 和未跟踪路径指纹前后相同
- 对照成功标准的结果：见「变更记录」逐条对照。

## 变更记录

- 已建立 RT-029 并冻结产品基线与脏工作树保护边界。
- B1 测试收敛：定位到全部失败只有一个根因——产品基线里两处 2026-08-30 的日常维护
  改动（`c26c7ad`、`dc96c28`）动了受脚本演化契约管辖的文件，却没有留下登记记录。
  按用户确认的方案「登记到预留槽位」处理：
  - 为 stage-06（`cwk_wiki_query.py`，槽位属 RT-022）与 stage-08
    （`cwk_nightly_pipeline.py`，槽位属 RT-026）补写真实的 `from`/`to` 演化回执、
    migration note 与可运行验收测试；两份 note 均含「Provenance」小节，如实写明
    改动的真实来源提交与日期，不伪造演进历史。
  - 同一次模型切换还动了 `legacy_frozen_files` 里的 `cwk_ai_common.py` 与
    `cwk_cloud_wiki_compile.py`。该清单没有回执机制，只更新了安全登记表里这两条
    指纹；已核实它们不在 genesis 表、companion immutable 清单与 VG-A 已签收执的
    artifacts 中，登记表本身也未被任何地方按哈希固定，故未重写任何已签名证据。
  - 平台兼容：macOS 默认临时目录经 `/var → /private/var` 符号链接，会被 RT-012
    stage-09 的祖先符号链接加固正确拒绝。按 RT-012 migration note 已记载的既定做法，
    在测试夹具里传规范路径、并让 `make test` 显式使用规范 `TMPDIR`，不放松运行时检查。
  - 修正一处陈旧测试预期：`test_real_repo_receipts_are_all_policy_declared` 原先把
    「字典序」与「策略阶段序」两个顺序直接对比，这在只有 stage-09/10 两份回执时才
    偶然相等。该测试自身的 docstring 明确承诺 RT-022/RT-026 补登记后应继续通过，
    故改为同序比较；集合相等与「不得出现未声明回执」的断言均未削弱，阶段顺序仍由
    `replay_chain` 的闭前缀与哈希链强制。
  - 修正一处夹具漏洞：`_prepare_evolution_baseline()` 会把仓库里**已经落地**的演化
    阶段拷进夹具，并记进 `_materialized_stage_indices`；随后 setUp 只应补齐**尚未
    落地**的阶段。`test_pr001_security_gate_contracts.py` 一直带着
    `if stage_index not in self._materialized_stage_indices` 这道判断，
    `test_pr001_release_gate_validation.py` 的同一段循环漏了。基线里 stage-06/08
    是空的，漏判不显形；补登记之后，夹具会用合成回执覆盖同路径的真实回执，
    `from_sha256` 变成真实链尖而不是 genesis，于是「伪造出一处本不存在的断链」，
    连带让 10 份 SG 回执全部判为 invalid。补上同一道判断即可，两个夹具的语义就此对齐；
    没有放宽任何断言（该测试仍要求十个阶段齐全、链尖等于真实文件哈希）。
  - 上述四项修好后，全量首次跑完 2111 条：**14 失败 0 错误**，且 14 条同属一个根因，
    全部落在 `test_pr001_release_gate_validation`。根因是 G2 的 owner-code 触碰证明
    在干净 checkout 下不成立：夹具把 G2 绑到 `remediation_subject`，要求这条提交真的
    碰过 RT-012/RT-013 的两个运行时选择器（`scripts/cwk_instance.py`、
    `scripts/cwk_agent_binding.py`），但夹具克隆里本来就带着已验收的 Stage-09/10 字节，
    `_prepare_evolution_baseline()` 只是用同样内容重写一遍，于是这条提交对两个文件
    毫无改动 → `subject_commit_did_not_touch_owner_code`，再沿 G3/G4/G5/G6 与 archive
    语义级联成 14 条。改法：先把这两个文件的一个真实「前态」单独提交，再在 remediation
    提交里恢复回执绑定的字节——最终字节与已验收的 Stage-09/10 产物逐字节相同，触碰证明
    却由 Git 历史真实产生。没有放宽任何断言。
    需要用户知道的一点：原 PR-001 脏工作树（只读参考，未改动）里同一位置有一处等价的
    在研修复。本 RT 是在干净基线上独立复现这个修法，二者文本高度相近；等 PR-001 的
    在研工作真正落地时，这一段需要合并去重。
- 被中断的上一轮遗留 WIP 中，把 `cwk_nightly_pipeline.py` 等 5 个文件回退成 genesis
  状态的做法已被否决并还原：那会静默撤销一项有实测证据的产品决策，用产品倒退换门禁
  通过，与门禁目的相反。
- 三项后果已登记进 `RT/_deferred-items.md`（DI-001、DI-002、DI-003）。

### 提交清单

按主题切分，每个提交只有一个理由：

| 提交 | 内容 |
|---|---|
| `fb95d3c` | macOS 默认临时目录（`/var → /private/var`）兼容：夹具传规范路径，`make test` 用规范 `TMPDIR` |
| `de6743a` | 修正回执清单的陈旧顺序预期（字典序 vs 策略阶段序） |
| `6ba0976` | 为基线里两处未登记的脚本改动补登记：stage-06/08 回执、迁移说明、验收测试、两条 legacy 指纹重锚 |
| `4a14cd8` | 补齐 release 夹具漏掉的「已落地阶段」守卫 |
| `17e948e` | AODW 治理对齐：落点规则、门禁作用域、RT 花名册、`make aodw-check` |
| `8986e04` | CI 与本地共用 `make ci` 入口 |
| `d6b8f3d` | RT-029 过程记录、复检口径与遗留事项台账 |
| `a2da946` | 让 G2 的 owner-code 触碰证明在干净 checkout 下真实成立 |

`Makefile` 被两个主题各改一处（`TEST_TMPDIR` 属 B1，`aodw-check`/`ci` 目标属
B2/B3）。没有用交互式分块暂存，而是按主题分三次写入同一文件后分别提交，中间每个
提交的 `Makefile` 都是自洽可用的，最终内容与一次性写入逐字节相同。

### B2 AODW 收敛

- **落点冲突**：`spec-lite-profile.md` 原来第 1 步写「在 main 上写 `rt-lite.md` 并提交」
  且「不要先切 feature 分支」，与宪章「交付形态」（`main` 上只提交 `meta.yaml`，
  `rt-lite.md` 和方案讨论全部在 worktree 里）直接冲突，也与 `git-discipline.md`、
  `rt-manager.md` 的落点表冲突。宪章是权威，只改从属规则：现在第 1 步是占号 + 建
  worktree，第 2 步才在 worktree 里写 `rt-lite.md`，并写明了理由和权威来源。
- **门禁只约束新 RT，从散文变成机制**：接入报告原本只是写了一句「历史 RT 不作为
  门禁基线」，而 `rt-guard.sh --root .` 照扫 27 个 RT、报出 132 条告警，全部是
  「存量不是新格式」。现在作用域是配置（`.aodw-next/project.yaml` 的
  `rt_gate_scope.managed_from: RT-028`），存量条目在 `RT/index.yaml` 里带
  `backfill: aodw-adoption`——这是 AODW 自带的存量豁免键（G111 认它），不是新开的例外。
- **`RT/index.yaml` 名不副实**：扩展名 `.yaml`、内容却是 Markdown 表格，rt-guard 的
  G109 解析不了，于是仓库里每个 RT 都恒报一条告警，G111 的存量豁免也永远用不上；
  索引还漏了 RT-007/008/010，而编号规则要取「目录 ∪ 索引」的最大号，漏号就会撞号。
  已改成真 YAML 并补齐 27 条，字段与原表格一一对应；三个漏号的 RT 没有 `meta.yaml`，
  状态如实写 `unknown`，不臆造。没有改动任何 RT 自己的文档。
- **台账格式对上门禁**：G114/G115 只认 `### DI-0NN` 标题行和「发现于 / 状态」两行，
  且只从 `meta.yaml` 的结构化字段 `deferred_items_raised` 取编号。原来的
  `DI-RT029-01/02` 既不符合编号形态、条目也不是表格，写进 `meta.yaml` 会被判据静默
  跳过——正是 G114 要防的「宣称已转出、台账一个字没写」。已改为 DI-001/002/003 并
  改成判据认得的形态，`meta.yaml` 填上 `deferred_items_raised`；G114 现在实报
  「扫描 3 个 / 全部命中」，而不是「无字段可校验」。
- **统一检查入口**：新增 `make aodw-check`（框架 fixture + 受管 RT 门禁 + 花名册
  一致性 + 宿主 skill 状态）和 `make ci`（= `make test` + `make aodw-check`）。
  实现放在 `.aodw-next/06-project/aodw-check.sh`（项目专属区，框架升级时不冲突），
  不放 `scripts/`——那里每个文件都受 PR-001 安全登记表的封闭清单管辖。
- **状态记录与实测对齐**：接入报告原写「`handover-pack` 已通过符号链接安装」，
  实测 `.agent/` 在 `main` 检出和各 worktree 里都不存在。已把 skill 装好，
  并且更重要的是把这条断言改成每次可复检的一项——`make aodw-check` 第 4 项当场测。
  它是本机状态（已 `.gitignore`），在 CI 里必然缺失，所以只告警、不阻断。
- **故意不做**：不安装 `pre-commit` hook。本仓库的 `main` 检出、RT worktree 与原
  PR-001 工作树共用同一个 git common dir，hook 是 common dir 级的，装一次会写到
  用户要求保持原样的那个工作树的提交路径上。G001 是告警级，照实报出；记为 DI-003。

### B3 CI 对齐

- `.github/workflows/ci.yml` 的执行命令由 `make test` 改为 `make ci`，与本地同一条。
  覆盖面从「编译 + 单测 + smoke」扩到「+ AODW 框架 fixture + 受管 RT 门禁 + 花名册」。
- job id 保持 `smoke` 不动：分支保护的 required status check 认的是 id，改名会让
  保护规则指向一个不存在的检查。已在文件里写明这一点，改名需要用户同步改保护规则。
- 加了 `timeout-minutes: 150`。全量单测本身就要一个多小时（PR-001 证据链测试大量做
  KDF），默认 6 小时会让真正挂住的任务很晚才被发现。

## 遗留事项

- RT-028 Work Agent 需要在本 RT 形成新 `main` 后重新迁移或重建；这不属于本次
  基线收敛的成功标准，收口时再决定是否进入全仓遗留事项台账。
- 已写入 `RT/_deferred-items.md` 并登记进 `meta.yaml.deferred_items_raised`：
  - **DI-001** RT-022 / RT-026 的脚本演化预留槽位已被占用，`max_ordinal` 用尽；
    这两个 RT 真正开工时若还要改同一批文件，必须先修订已冻结的 `policy_v1.json`。
  - **DI-002** 两个 legacy frozen 脚本的指纹已重新锚定；`legacy_frozen_files`
    这一族缺少「有主的演化路径」，改动只能靠改指纹放行。
  - **DI-003** `pre-commit` hook 在本仓库无法安全安装（与受保护工作树共用
    git common dir）；等 `main` 落点定了、原工作树处置完毕再议。

## 收口决策（2026-08-31）

- 用户于 2026-08-31 22:51 在 Discord #工作协同 确认收口方案：追认方案 1
  「登记到预留槽位」（两处未登记脚本改动按 6ba0976 的回执与迁移说明记账）；
  feature/RT-029-aodw-baseline-convergence 快进合并进本地 main；暂不推送 GitHub。
- RT-028 在新 main 落定后重新承接，不在本 RT 内迁移或合并。
