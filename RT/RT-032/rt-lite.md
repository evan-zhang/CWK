# RT-Lite: RT-032 - CWK AI 激活向导与自动运行编排

> profile: Spec-Lite | execution_mode: collaborative

## 方案（给人看）

- 做什么：在 CWK 安装接入之后增加一段由 AI 主导、由确定性状态与回执约束的激活流程。AI 先解释边界并取得只读发现授权，再基于一次发现结果提出业务画像，让使用者确认关键业务、实体歧义和关注规则；随后生成可审计的“每日执行合同”，完成一次只读试跑，只有试跑通过且使用者再次确认后，才允许创建定时任务。
- 为什么：当前项目有安装、自检、采集、日报和验收能力，但这些能力仍是运维步骤。使用者不知道安装后还要确认什么，也无法在启用定时前清楚回答“每天抓什么、怎么抓、产出什么、不会做什么”。
- 代价：新增激活状态、发现画像与调度交接合同，并补充 Agent 对话规则和测试；会让首次启用比直接写 cron 多两个确认点，但换来可解释、可暂停、可审计和默认不越权。
- 这次故意不做什么：不把向导塞进 `install.sh`；不在聊天中收集凭据；不修改 CWork；不读取真实业务数据；不创建或修改生产 cron、OpenClaw Gateway、远端机器或全局配置；不把个人 raw 同步到 DocDB；不替代 PR-001 的多租户调度与授权体系。
- 用户怎样算成功：安装完成后，AI 能主动引导用户走完“只读发现授权 → 画像确认 → 执行合同确认 → 试跑 → 定时启用”路径，并用实际配置准确说明每日来源、处理上限、完整性回看、输出、发布边界和禁止动作。任何未确认、试跑失败或状态异常的情况都不能进入自动运行。
- 建议：定论。AI 负责沟通，脚本负责状态、计算和门禁；不能让自然语言会话自己充当授权记录或直接生成无审计的定时任务。

## 用户路径与产品决定

```text
INSTALLED
  → READY_FOR_DISCOVERY
  → PROFILE_PROPOSED
  → PROFILE_CONFIRMED
  → PILOT_PASSED
  → ACTIVE

旁路：PAUSED / DEGRADED / NEEDS_RECONFIRMATION
```

1. **安装完成不等于激活完成。** 安装器只交付程序和 OpenClaw 接入，不读取 CWork、不做发现、不建定时任务。
2. **发现先于提问。** AI 先问个人/团队、关注目标、发现日期范围、发布边界和期望运行时间；凭据只通过受保护入口配置。得到明确授权后，才执行一次只读发现。
3. **画像是提案，不是自动定论。** 报告必须区分原始名称/别名数、候选实体族数和用户确认实体数，并列出关键业务候选、依据、歧义和覆盖边界。
4. **执行合同必须可复述。** 合同说明每天读取哪些来源、每轮处理上限、完整性回看范围、会读取哪些详情、生成哪些产物、是否发布派生内容，以及绝不执行的动作。
5. **两个独立确认点。** 第一次确认允许只读发现/试跑；第二次确认接受试跑结果并允许创建定时任务。前一确认不能代替后一确认。
6. **调度器不是安装器的一部分。** 本仓库生成机器可读调度交接和回执门禁；OpenClaw/宿主在用户确认后创建实际任务，并把任务标识回填。默认状态不得包含已启用的外部任务。
7. **持续治理。** 画像、执行合同、数据范围或调度改变时进入 `NEEDS_RECONFIRMATION`；失败进入 `DEGRADED`；用户可暂停并看到下一次预计运行时间和恢复条件。

## 当前依据

- `install.sh` 与 RT-031 的四种接入模式只负责核心安装和 OpenClaw 接入，明确不创建 cron。
- `scripts/cwk_doctor.py` 已能检查安装、凭据是否配置、公司 Skills 和接入状态，但不会选择模式或完成激活。
- `scripts/cwk_collect_live.py` 当前读取 inbox、pending、outbox、unread 和 pending todo；默认日常处理上限 60、持续事项 15、历史回填 20，并轮转历史 inbox/outbox/pending report/pending/completed todo。
- `scripts/cwk_nightly_pipeline.py` 可完整分页当前业务日并默认回看前 2 个业务日，读取正文、基本信息和节点/意见链，生成日报、行动中心、关系、主题/实体、覆盖与验收回执。
- `scripts/cwk_sample_pilot.py` 与 nightly manifest 已提供 `overall_pass`、处理数量、实体/关系统计和失败信息，可作为试跑门的输入。
- `skill/SKILL.md` 已规定真实只读试跑通过后才允许启用 cron，但目前没有对话式状态、画像确认、执行合同或调度回填实现。

## 实现范围

### 1. 激活状态与私有持久化

- 定义版本化、可校验、原子写入的激活状态；默认位置在 gitignored 的私有运行目录。
- 记录每次状态迁移的时间、前置状态、授权类型、输入回执摘要和下一步；不得记录凭据或 raw 正文。
- 非法跳转、缺失确认、陈旧画像、试跑失败和外部调度不一致均 fail closed。

### 2. 只读发现与业务画像

- 从既有只读运行回执和派生索引生成发现报告，不把模型猜测当事实。
- 报告至少包含授权可见范围、业务日期范围、来源 lane 数量、记录数、主题数、实体名称/别名数、候选实体族数、关系未知数、关键业务候选及证据摘要。
- 将高频但低价值的重复报表、关键业务候选、关注人物/项目/组织、实体合并歧义分开呈现；使用者只需确认高价值和有歧义的项目。

### 3. 每日执行合同

- 从实际配置生成，而不是写死文案。
- 必须说明：来源 lane、详情读取动作、增量/延续/历史回填上限、当前业务日完整分页、迟到数据回看天数、日报/行动中心/Wiki/实体产物、DocDB 派生发布开关、raw 边界、只读禁止动作、运行时间与时区。
- 合同生成稳定摘要；配置、画像或合同摘要变化后，已通过的确认自动失效。

### 4. 试跑与调度门禁

- 试跑门验证 nightly manifest、acceptance、source completeness 和同步失败；失败只进入 `DEGRADED`，不能进入 `PILOT_PASSED`。
- 通过后生成调度交接，不直接在安装脚本或普通 Python 运行中控制 Gateway。
- 只有在用户明确确认执行合同和试跑结果后，Agent/宿主才可创建计划任务；创建后以外部任务 ID 和合同摘要回填为 `ACTIVE`。
- 支持暂停、恢复、重新确认和调度漂移检查；不自动删除或覆盖未知外部任务。

### 5. Agent 对话与文档

- Skill 在安装后发现未激活状态时主动提出下一步，并按状态只问当前必要问题。
- 对话不收集或回显密钥，不展示真实 raw，不把“工具可调用”解释为“用户已授权”。
- 更新上手和运维文档，明确 sandbox 自助、宿主管理和非 sandbox 都共用同一激活合同。

## 验收标准

1. 脱敏 fixture 可完整走通 `INSTALLED → … → ACTIVE`，每个状态都有机器可读回执和人读摘要。
2. 未授权发现、未确认画像、失败试跑、合同漂移、重复激活、未知调度 ID 均被拒绝或进入安全旁路状态。
3. 发现报告准确区分实体别名、候选实体族和用户确认实体，并清楚标注“仅代表当前授权可见范围与发现日期范围”。
4. 执行合同从配置计算并准确列出默认采集来源、60/15/20 上限、当天完整分页、2 日回看、详情读取、输出、发布开关和禁止动作；配置变化会改变摘要并要求重新确认。
5. 定时任务只能在试跑通过和第二次明确确认后创建；代码与测试不触发真实 cron、Gateway、CWork、DocDB 或模型调用。
6. 安装/接入模式与激活模式解耦；RT-031 的 `host-skill`、`workspace-skill`、`router`、`none` 合同不回归。
7. 使用 Python 3.11 通过定向测试、语法/编译、`git diff --check`、AODW、治理审计和完整 `make ci`；独立审阅没有未处理的阻断项。

## 实施顺序

1. 等 RT-031 冻结版通过完整验证并提交，再把其提交作为本 RT 的前置依赖引入，避免两个活跃 worktree 同时修改安装/Skill 入口。
2. 先实现状态 schema、迁移门禁和脱敏 fixture，再实现发现报告与执行合同。
3. 接入试跑回执、调度交接、暂停/重确认和漂移处理。
4. 更新 Skill/文档，补攻击与回归测试。
5. 运行完整验证、独立审阅并提交本地功能分支；不合并、不推送、不改远端。

## 验证记录

### 2026-09-02：零重叠确定性核心（与 RT-031 并行，未接入安装/Skill 层）

本轮只实现与 RT-031 无路径重叠的部分：激活状态机与私有持久化、只读发现与业务画像
计算、执行合同与哈希漂移、试跑门禁、调度交接与回执校验，以及对应的脱敏测试。
`install.sh`、`scripts/cwk_doctor.py`、`skill/SKILL.md`、上手文档与 RT-031 的测试
一律未改动，也未引入 RT-031 的提交。

| 命令 | 结果 |
| --- | --- |
| `python3.11 -m py_compile scripts/cwk_activation_{state,contract,wizard}.py` | 通过 |
| `python3.11 -m unittest tests.test_rt032_activation_state` | 40 passed |
| `python3.11 -m unittest tests.test_rt032_activation_contract` | 44 passed |
| `python3.11 -m unittest tests.test_rt032_activation_wizard` | 45 passed |
| 三个模块合并运行 | 129 passed |
| `git diff --check` | 通过（exit 0） |
| `bash .aodw-next/06-project/aodw-check.sh --root .` | 通过（补登 `RT/index.yaml` 后；仅剩本机 skill 未安装的告警） |
| `python3.11 .aodw-next/06-project/governance-audit.py --root .` | **未通过：GA-ORPHAN ×3**，见下方待集成项 |

真实 CWork/DocDB、cron、Gateway、远端与模型调用均未触发；全部证据来自
`tests/fixtures/activation/` 的脱敏样例。

### 2026-09-02（第二轮）：独立审阅阻断项整改

独立审阅在零重叠核心上判出两个阻断项，本轮修完并加回归；范围仍不含安装/Skill/文档/治理。

1. **试跑门禁对采集回执改为失败关闭。** 原实现只在"给了采集回执"时才检查日采完整性，
   因此**不给**采集回执反而能通过。现把回执存在性、形状合法性、运行成功三条拆成独立
   显式判据，四种情况一律进不了 `PILOT_PASSED`：完全不传、文件缺失/不可读、形状非法、
   采集本身没成功。同时把校验出的回执事实与三份证据的哈希一起绑进试跑回执的
   `receipt_sha256`，证据一变哈希就变，旧的第二道确认自动作废。
2. **CLI 边界补错误处理。** `main` 原先会让 OSError 家族的输入/持久化失败以 traceback
   逃逸。现在按既有退出码契约收口为 JSON 输出，消息统一脱敏（抹掉路径样式片段、控制
   字符，截断到 240 字符），绝不回显绝对路径或文件内容；`ContractError`（不可规范化的
   JSON 数值）同样映射为用法错误。**没有**兜底 `except Exception`，程序 bug 仍然暴露。
3. **保留并提交测试加固。** `tests/test_rt032_activation_contract.py` 原先用
   `sys.modules` 断言"没导入采集器"，在全仓库单进程发现下会被先导入采集器的
   `tests.test_collection_incremental` 污染而误报。改为静态源码断言 + "解析不执行被解析
   文件"的炸弹用例，语义更强且不依赖进程状态。

顺带的最小相邻修正：状态目录缺失的报错不再回显路径；采集回执 fixture 补上真实采集器
会输出的 `errors` 字段（现属必需形状）。

| 命令 | 结果 |
| --- | --- |
| `python3.11 -m py_compile scripts/cwk_activation_{state,contract,wizard}.py` | 通过 |
| `python3.11 -m unittest tests.test_rt032_activation_state` | 40 passed |
| `python3.11 -m unittest tests.test_rt032_activation_contract` | 52 passed |
| `python3.11 -m unittest tests.test_rt032_activation_wizard` | 63 passed |
| 三个模块合并运行 | 155 passed |
| `python3.11 -m unittest tests.test_collection_incremental tests.test_rt032_activation_contract` | 80 passed（同进程、采集器先导入的顺序，验证第 3 项） |
| `git diff --check` | 通过（exit 0） |
| `bash .aodw-next/06-project/aodw-check.sh --root .` | 通过（仅剩本机 skill 未安装的告警） |
| `python3.11 .aodw-next/06-project/governance-audit.py --root .` | 仍为既有 GA-ORPHAN ×3，无新增发现 |

### 2026-09-02（第三轮）：接入前定向加固

范围仍是 RT-032 零重叠三脚本 + 对应测试，未触碰 RT-031、治理/PR-001、`install.sh`、
`cwk_doctor.py`、`skill/`、上手文档、cron/Gateway/远端。

1. **`record-pilot` 的失效回报补齐。** 一条命令可能作废两批确认：进门时清掉本来就
   过期的，写入新的合同/试跑回执之后再清掉刚被这条命令弄过期的。原实现只回报第一批，
   于是「换了采集证据重跑试跑」这条最危险的路径会返回
   `invalidated_gates: []`，同一份负载里 `next_step` 却是 `confirm_activation`——
   Agent 会照字面告诉用户「什么都没失效」，然后解释不了那个多出来的确认。
   现在两批并集回报（去重、按 `GATES` 固定顺序）。**状态迁移与 `next_step` 本来就是
   对的，没有改动**；这是纯可观测性修复。两侧都加了回归：作废时必须报出
   `["activation"]`，未作废时必须是 `[]`（不能喊狼来了），FAIL 重跑时既报门也照常
   落 `DEGRADED`。
2. **stdout 自身写不出去的路径收口。** 原实现在 `json.dump`/`flush` 抛 `OSError`
   时返回 `EXIT_USAGE`，但 CPython 在解释器退出时**还会再 flush 一次** stdout：管道
   已断时那次 flush 会失败，把退出码改写成 **120** 并在 stderr 打印
   `Exception ignored in: <_io.TextIOWrapper …>`。也就是说
   `cwk_activation_wizard … | head -1` 的真实退出码根本不在本模块承诺的 `EXIT_*` 里，
   Skill 无法解析。现按 CPython 对 SIGPIPE 的建议，把 stdout 的描述符改指
   `os.devnull` 后再返回，退出时那次 flush 落进空洞。实测：修复前进程退出码 120 +
   stderr 有噪声，修复后退出码 2、stderr 干净。测试用受控假 sink（首字节即断、
   写到一半断、只在 flush 断、`ENOSPC`）与一条真实断管道验证，全部在进程内完成，
   不起子进程。**没有加兜底 `except Exception`**：假 sink 抛 `RuntimeError` 时
   仍必须原样抛出，程序缺陷不被吞掉。
3. **把 I/O 契约写成断言。** 读不到输入文件、写不进状态目录属于**用法失败**，不是
   业务判定：命令中止，状态文件逐字节不变，不进 `DEGRADED`，也不追加任何迁移回执——
   因为压根没产生过试跑结论。当前实现本来就是这样（`_write_artifact`/`cas_write` 都在
   `apply_transition` 之前，失败即整条命令中止），本轮**没有改这个行为**，只是把它
   固定成测试和模块文档：缺文件、传目录、非 JSON、非对象、以及注入的 `ENOSPC`/`EROFS`
   持久化失败，五种情况都断言「状态原样 + 无迁移记录 + `degraded_reason_code` 仍为
   null」。

| 命令 | 结果 |
| --- | --- |
| `python3.11 -m py_compile scripts/cwk_activation_{state,contract,wizard}.py` | 通过 |
| `python3.11 -m unittest tests.test_rt032_activation_state` | 40 passed |
| `python3.11 -m unittest tests.test_rt032_activation_contract` | 52 passed |
| `python3.11 -m unittest tests.test_rt032_activation_wizard` | 83 passed（+20） |
| 三个模块合并运行 | 175 passed |
| `python3.11 -m unittest tests.test_collection_incremental tests.test_rt032_activation_contract` | 80 passed（同进程、采集器先导入） |
| 抽掉两处修复重跑新增用例 | 6 项如期变红，证明是真回归而非同义反复 |
| `git diff --check` | 通过（exit 0） |
| `bash .aodw-next/06-project/aodw-check.sh --root .` | 通过 |
| `python3.11 .aodw-next/06-project/governance-audit.py --root .` | 仍为既有 GA-ORPHAN ×3，无新增发现 |

## 待集成项（治理声明，本轮按协调要求不落盘）

并行协调要求本轮不修改 `code-ownership-manifest.json`、`script-evolution-v2.json`
及任何 PR-001 登记/receipt，因此下列声明只登记不执行。由于 `R-runtime-pr001-managed-scripts`
是**委派的封闭集合**（rationale 明确：新增 `scripts/cwk_*.py` 不在声明集合内即判孤儿），
在补登前 `make governance-audit` 必然红：

```
GA-ORPHAN: scripts/cwk_activation_contract.py
GA-ORPHAN: scripts/cwk_activation_state.py
GA-ORPHAN: scripts/cwk_activation_wizard.py
```

补登内容（待 RT-031 冻结、治理文件解冻后执行）：

1. 在 `code-ownership-manifest.json` 的 `rules` 中新增一条 `kind: exact_set` 规则，
   owner=RT-032，domain=runtime，覆盖上述三个文件；`scripts/` 是 exact-only 区，
   不能靠前缀规则吸收。
2. 确认是否需要在 `script-evolution-v2.json` 登记演化路径；三个文件均为新增运行时
   脚本，不改动既有受管脚本的行为面。
3. `tests/` 不是 exact-only 区，`tests/test_rt032_activation_*.py` 与
   `tests/fixtures/activation/` **无需**新增规则——本轮审计对这 15 个文件零发现，已验证。

## 接入前待议项（本轮按协调要求只记录，不改代码）

三条都已实测复现，都不阻塞本轮验证，但接入 Skill/文档层之前要有结论：

1. **交接单里的配置路径是调用方原样给的绝对路径。** `build_scheduler_handoff` 用
   `config_path=str(args.config)` 直接进 `command_spec.argv`，因此
   `schedule-handoff` 的**成功**负载会把绝对路径回显给 Agent（实测：fixture 的
   绝对路径出现在返回的 JSON 里）。这和错误路径的脱敏姿态不一致。它同时进
   `handoff_sha256`，所以改成项目根相对路径会改变交接单哈希、作废已有的第二道确认——
   这正是要跟接入层一起决定的：宿主要在什么工作目录下执行这条 argv。
   现有的 `assert_no_absolute_path` 只覆盖失败负载，成功负载没有这条断言。
2. **`redact_message` 只认「以 `/`、`~/`、`./`、`../` 开头」的片段。** 裸相对路径
   （实测 `state/activation/activation.json` 原样穿过）不会被抹。当前所有错误消息都是
   固定文案 + errno，不含裸相对路径，所以现在不漏；但这是靠调用点自觉，不是靠闸门。
   要不要把闸门加宽，取决于接入层会往错误消息里塞什么。加宽有误伤代价——
   `read/write mismatch` 这类词组不能被当成路径（已有回归钉住）。
3. **同一个失效回报缺陷在 `propose-profile` 里也可达，本轮按范围**未**修。**
   路径：`ACTIVE → check-drift 判出合同漂移 → NEEDS_RECONFIRMATION`（此时
   activation 门仍有效）`→ propose-profile` 换新画像。实测该命令作废了 `profile` 与
   `activation` 两道门，却回报 `invalidated_gates: []`。修法与 `record-pilot` 完全相同：
   接住第二次 `invalidate_stale_confirmations` 的返回值，用 `_merge_gates` 并进负载。
   `confirm-discovery` / `record-discovery` 里有同样的写法，但按迁移表推不出「进门时
   该门已有有效确认」的状态，属不可达，可一并改成统一写法或留白。

## 变更记录

- 2026-09-02：根据用户批准的安装后使用路径，确定独立 RT；采用“AI 沟通 + 确定性状态/回执”的激活架构，并与 RT-031 安装接入职责分离。
- 2026-09-02：实现零重叠确定性核心并通过定向测试；治理归属声明按并行协调要求转为待集成项。
- 2026-09-02：迁移表补 `(PILOT_PASSED, record-pilot-pass) -> PILOT_PASSED` 自环。重跑一次通过的试跑本身安全：回执是内容寻址的，**证据变了**才会产出新回执并作废旧的第二道确认，证据一模一样则回执哈希不变、确认继续有效。（初版记录写成"重跑必然作废"，与实现不符，已按实际行为更正；两种情形都已加回归测试。）
- 2026-09-02：整改独立审阅的两个阻断项——试跑门禁对采集回执改为失败关闭并把回执事实绑进哈希；CLI 边界收口 OSError/ContractError 为脱敏 JSON 与既有退出码。测试加固保留并提交。
- 2026-09-02：接入前定向加固。`record-pilot` 的 `invalidated_gates` 改为回报本条命令作废的全部门（原先漏掉写入新回执后才失效的那批，导致「成功但什么都没失效」与 `next_step: confirm_activation` 自相矛盾）；stdout 断管道时把描述符改指 devnull，进程退出码从解释器的 120 收回到约定的 2 且 stderr 不再有噪声；并把「输入/持久化失败不进 DEGRADED、不写迁移回执」这条既有行为固定成测试与模块文档。交接单绝对路径、裸相对路径脱敏、`propose-profile` 的同类失效回报缺陷记入「接入前待议项」，本轮不动。

## 遗留事项

- 治理归属补登见上方「待集成项」，需在治理文件解冻后完成，否则 `make ci` 持续红。
- Skill 入口、上手文档与 `install.sh` 接入层尚未实现，等 RT-031 冻结提交后再做；
  届时须保证 `host-skill`、`workspace-skill`、`router`、`none` 四种接入模式不回归。
