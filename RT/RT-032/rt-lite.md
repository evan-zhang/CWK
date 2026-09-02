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
| `python3.11 -m py_compile scripts/activation_{state,contract,wizard}.py` | 通过 |
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
| `python3.11 -m py_compile scripts/activation_{state,contract,wizard}.py` | 通过 |
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
   `activation_wizard … | head -1` 的真实退出码根本不在本模块承诺的 `EXIT_*` 里，
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
| `python3.11 -m py_compile scripts/activation_{state,contract,wizard}.py` | 通过 |
| `python3.11 -m unittest tests.test_rt032_activation_state` | 40 passed |
| `python3.11 -m unittest tests.test_rt032_activation_contract` | 52 passed |
| `python3.11 -m unittest tests.test_rt032_activation_wizard` | 83 passed（+20） |
| 三个模块合并运行 | 175 passed |
| `python3.11 -m unittest tests.test_collection_incremental tests.test_rt032_activation_contract` | 80 passed（同进程、采集器先导入） |
| 抽掉两处修复重跑新增用例 | 6 项如期变红，证明是真回归而非同义反复 |
| `git diff --check` | 通过（exit 0） |
| `bash .aodw-next/06-project/aodw-check.sh --root .` | 通过 |
| `python3.11 .aodw-next/06-project/governance-audit.py --root .` | 仍为既有 GA-ORPHAN ×3，无新增发现 |

### 2026-09-02（第四轮）：接入 RT-031 基线后的集成

RT-031 冻结实现已作为 `731c695` / `a52e113` / `313de70` 引入本 worktree，作为四模式
安装合同的既定基线。本轮把激活从「三个脚本」变成「产品里的一步」：治理补登、前三轮
记录的三个待议项全部落地、安装/自检/Skill/文档接入，并补齐对应测试。

**1. 治理归属补登（GA-ORPHAN 清零）。** 在 `code-ownership-manifest.json` 增一条
`R-runtime-rt032-activation`（`kind: exact_set`，owner=RT-032，domain=runtime），逐条
列出三个脚本——`scripts/` 是 exact-only 区，前缀规则吸收不了，而
`R-runtime-pr001-managed-scripts` 是委派给上游封闭集合的规则，新文件不在其中。
`evolution_path` 取 `repo-standard-change`：`cwk-governance-repin-v1` 只接受单
`path`（敏感 pin），`cwk-script-evolution-v2` 槽位则会硬撞 `GA-V2-SLOT`——它要求叠加
目标先存在于 PR-001 v1 的 `evolvable_paths`，新脚本不满足，凭空开槽就是伪演化路径。
故 `script-evolution-v2.json` 一字未动。新增的 `skill/references/activation.md` 落进
`R-docs-skill-facing`（`skill/` 同为 exact-only 区）；`tests/` 是前缀区，新测试文件
无需登记。审计现为 610 个受跟踪文件全绿。

**2. `propose-profile` 的失效回报补齐（第三轮待议项 3）。** 与 `record-pilot` 同一
缺陷、同一修法：接住写入新画像**之后**那次 `invalidate_stale_confirmations` 的返回值，
用 `_merge_gates` 并进负载。可达路径已加回归：`ACTIVE → 合同漂移 →
NEEDS_RECONFIRMATION → propose-profile`，此时 activation 门本来仍然有效，正是被这条
命令作废的，原实现却回报 `[]`。`confirm-discovery` / `record-discovery` 按迁移表推不出
「进门时该门已有有效确认」的状态（不可达），仍统一成同一写法并加结构性守卫测试
——四条写事实的命令必须都回报两批，新增命令漏写会红。**迁移与 `next_step` 未改动。**

**3. 交接单不再回显宿主绝对路径（第三轮待议项 1）。** 新增
`build_config_locator`：配置改用**项目内相对定位符** + 一份显式的项目根定位契约
（`CWK_PROJECT_DIR`，用 `scripts/cwk_doctor.py` 作存在性判据），`absolute_path_omitted`
写在负载里。配置落在项目外、含 `..`、含控制字符、或首段会被解析成命令行选项时，
抛 `ConfigLocatorError` 并以 `EXIT_REFUSED` **拒绝出单**，不退化成塞一个宿主路径。
副作用是好的：`handoff_sha256` 现在跨宿主目录布局稳定（已钉测试），宿主换机器不会
无缘无故作废第二道确认。

**4. 脱敏闸门加宽到裸相对路径（第三轮待议项 2）。** `state/activation/activation.json`、
`client/secret.json` 这类不带前导 `/`、`./` 的多段路径原本原样穿过。加宽用两条判据而
不是「凡带斜杠即抹」：末段有字母扩展名，或段数 ≥3 且至少一段含非单词字符。正反用例
各一组钉住——绝对/`~`/`./`/`../` 与裸相对路径都抹，`read/write mismatch`、`and/or`、
`input/output error`、`pass/fail`、`yes/no/maybe`、`24/7` 一律保留。

**5. 安装保持零副作用。** `install.sh` 在 `CWK_CORE_READY` 之后只增加一行
`CWK_ACTIVATION=<状态>`：它调用 `activation_state.readiness()` 这个**只读探针**
（不 mkdir、不加锁、不写），装完仍然没有私有激活状态、没有任何定时任务。四种接入模式
逐一验证过这一点；已有状态的字节不被改动；状态损坏时报 `UNREADABLE` 而**不是**
`NOT_STARTED`——后者是唯一会让已排期的运行显得无辜的答案——但安装本身照常成功，
否则用户没法靠重装脱困。

**6. `doctor` 转述而不重算。** 新增 `activation` 检查项，直接复用
`activation_state.readiness()`，不在 `cwk_doctor.py` 里重建状态表（已加静态断言
钉住：doctor 源码里不得出现 `PILOT_PASSED` 之类的状态词）。模块从 doctor 自己的包目录
导入，**绝不**从 `--project-dir` 导入——被检查的项目是数据，从里面 import Python 就把
「检查」变成了「执行」（已加炸弹用例）。输出只有枚举、状态名和下一步，无路径/哈希/
业务内容。私有状态不可校验时是**告警**不是错误，理由同上：`install.sh` 会跑 doctor。

**7. Skill 与文档一条路径。** 新增 `skill/references/activation.md`（分状态话术、
`next_step` 对照表、两道门、交接与回填、失败处理）；`skill/SKILL.md` 只做地图，指过去
不复述。README、内部/sandbox 上手、引导提示词、运维与迁移文档统一成一条路径：
安装（四模式 + `CWK_CORE_READY` + `CWK_ACTIVATION=NOT_STARTED`）→ 自检（`activation`
检查项）→ smoke → 只读试跑 → 激活对话（两道确认）→ 宿主建任务 → `record-schedule`
回填。RT-031 的 `AGENTS_ROUTER_ACTIVATION=NEXT_SESSION`、`OPENCLAW_DISCOVERY=UNVERIFIED`
与四模式语义原样保留并加测试钉住。迁移文档新增一条：`state/activation/` 不得复制——
它记的是谁在什么范围上确认过什么，搬走等于伪造别人的同意。

| 命令 | 结果 |
| --- | --- |
| `python3.11 -m py_compile`（三个激活脚本 + `cwk_doctor.py` + 新测试） | 通过 |
| `bash -n install.sh` | 通过 |
| `python3.11 -m unittest tests.test_rt032_activation_state` | 40 passed |
| `python3.11 -m unittest tests.test_rt032_activation_contract` | 63 passed（+11） |
| `python3.11 -m unittest tests.test_rt032_activation_wizard` | 94 passed（+11） |
| `python3.11 -m unittest tests.test_rt032_activation_integration` | 31 passed（新增） |
| 四个 RT-032 模块合并运行 | 228 passed |
| `python3.11 -m unittest tests.test_install_modes` | 67 passed（RT-031 基线不回归） |
| `python3.11 -m unittest tests.test_distribution` | 5 passed |
| `python3.11 -m unittest tests.test_governance_audit` | 62 passed |
| `python3.11 .aodw-next/06-project/governance-audit.py --root .` | **通过**：610 个受跟踪文件，GA-ORPHAN 清零 |
| `bash .aodw-next/06-project/aodw-check.sh` | 通过（RT-028…RT-032 门禁全过；仅剩本机 skill 未安装的告警） |
| `git diff --check` | 通过（exit 0） |

真实 CWork/DocDB、定时任务、Gateway、远端与模型调用一律未触发。安装器测试跑在
`InstallerFixture` 的隔离副本里（`install.sh` 自己 `cd` 进临时目录），用的都是脱敏
占位值；激活证据全部来自 `tests/fixtures/activation/`。**未跑完整 `make ci`**，按协调
留给独立审阅后的最终一次冻结 CI。

### 2026-09-02（第五轮）：终审两阻断 + 四残留整改

针对 `040c29f` 的独立终审提出两个阻断项与四个残留项，本轮逐条闭环。

**阻断 1：执行合同必须精确描述 nightly 的运行时行为。** 逐行读了
`cwk_nightly_pipeline.main()` 的取值组合，发现优先级**不是统一的**，原合同按一套
规则描述全部设置，因此会在真实配置下说错话：

| 设置 | 上游实际优先级 |
| --- | --- |
| 四个 cap / page size / 回溯天数 | 配置 > 环境变量 > 字面默认 |
| `backfill_enabled`、来源完整性 | 环境变量 > 配置 > True |
| `sync_docdb` | 配置 > 环境变量 > False |

布尔真值集合是 `{"1","true","yes","y","on"}`（含 `"y"`），其余一律 False 而**不是**
「没设置」；配置里的布尔走 Python 真值性，故 `"sync_docdb": "false"` 在上游是**真**。
这些语义现在由 `resolve_nightly_runtime` 忠实重实现，合同里每一项都带 `sources`
标注取值来源。

未把上游函数直接 import 复用，有硬理由：`cwk_nightly_pipeline.py` 属 PR-001 受管、
RT-026 所有，v1 演化槽位已用尽，从 RT-032 改它就是伪演化路径；且该模块**导入即执行**
`load_local_env(PROJECT/'.env')`，会把 `.env` 灌进本进程环境变量——向导绝不能碰。
因此改用双向钉死：新增 `tests/test_rt032_contract_fidelity.py`，一面用上游自己的
`env_bool` / `config_value` 重组出期望值做**行为等价**对拍（22 组具名用例，含
`CWK_SYNC_DOCDB=y`、配置 false + 环境 1、整数冲突、18 种布尔拼写 × 3 个变量），一面用
AST/源码断言**钉住上游的组合方式**，上游一改就红在这里而不是悄悄让合同说谎。

顺带查出一个终审措辞之外的更深问题：**即使合同正确读了当前环境，它描述的仍不是那个
定时任务将要跑的东西**——交接单的 argv 只有 `--config/--run-name/--date`，
`env_allowlist` 只有 `CWORK_APP_KEY`，任务根本看不见任何 `CWK_*`。故合同改为**解析
两次**（真实环境 vs 空环境）并输出 `runtime_resolution`：
`scheduled_environment_equivalent` 与 `settings_requiring_shell_environment`；Markdown
渲染带警告块；`build_scheduler_handoff` 在该列表非空时直接抛
`ScheduledEnvironmentMismatch` 以 `EXIT_REFUSED` **拒绝出单**——否则用户会对着一份
「今晚会发布镜像」的合同点头，而实际那台机器上跑的是另一件事。

**阻断 2：只读探针遇到符号链接必须失败关闭，且不吐 traceback / 绝对路径。**
`activation_state.readiness()` 改为 lstat/不跟随判定，并同时接住 `OSError` 与
`AtomicFileError`/containment 失败（`cwk_atomic_file` 的 `open_dir_nofollow` 抛的是
后者，不是 `OSError`——原来的 `except OSError` 接不住，异常会一路穿出去）。
`cwk_doctor.py` 两处守卫同步加宽为 `except Exception` 并附理由；兜底负载改为调用新
公开的 `unreadable_readiness()`，词表只有一个主人，不再抄第二遍。测试覆盖目录符号
链接、状态文件符号链接、悬空链接、二次硬链接、目录位置是普通文件、状态文件是目录、
chmod 000：每条都断言受害文件字节未变、链接仍是链接、没有创建任何状态、reason 落在
封闭词表内、负载里没有路径；并在 doctor 与**真实 `install.sh`** 两侧断言这仍是
**告警而非错误**、退出码 0、`CWK_ACTIVATION=UNREADABLE` 而非 `NOT_STARTED`。

**残留 1：check-drift 的负载语义与文档对齐。** 漂移是拿新配置重算后与状态里记的
`contract_sha256` 比对，**不会改写**那个哈希，所以绑定哈希照样对得上、自动失效那条路
不会触发。原实现因此产出自相矛盾的负载：`next_step: reconfirm_contract` 与
`activation.valid: true` 同时为真。现在真漂移时**显式吊销第二道确认**（新增
`_revoke_gate`），并落盘；`schedule-handoff` / `record-schedule` / `resume` 在此之后
一律 `EXIT_REFUSED`。`skill/references/activation.md` §8 改写成与之逐字对应。

**残留 2：`flag-drift` 非法时不得偷改 `degraded_reason_code`。** 改为**先问再改**：
`can_transition()` 前置判定（新增于状态模块），迁移非法就不改状态、不改降级原因。
否则一次「试跑失败后跑了 check-drift」会把 `pilot_failed` 覆盖成 `contract_drift`，
既无迁移记录也无授权，而使用者该做的 `rerun_pilot` 就再也推不出来了。凡是落盘的改动
都带 `revision` +1 与刷新后的 `updated_at`，保证「持久化的语义变更必有审计痕迹」没有
例外分支；`cmd_status` / `cmd_schedule_handoff` 的无迁移提交一并纳入。回归已加：
`pilot_failed -> drift check` 断言状态文件**字节不变**——成立的前提是那条路上已经没有
第二道门可吊销了（`record-pilot-fail` 那条迁移先把它收走了），不是「非法迁移一律不
写盘」。这条前提在第六轮被单独钉死，见下方残留 6。

**残留 3：产物回读改为与写入对称的 dir-fd / 不跟随读。** `_read_artifact` 走
`read_file(dir_fd, name)`，符号链接、目录、多一条硬链接一律拒绝，且「被换成链接」与
「本来就不存在」给不同答案——后者才该提示「先跑上一步」。测试用**与真品字节一致**的
诱饵：哈希校验本来会通过，只有不跟随读能拦住。另加结构性守卫，禁止任何函数再用
`(state_dir / X_FILE).read_text()` 绕过这唯一入口。

**残留 4：discovery `scope` 先按闭合 schema 校验归一，再哈希/上报。** 只认四个键、
四种类型，`subject_ref` 必须是标识符（`[A-Za-z0-9][A-Za-z0-9._:@-]{0,63}`，因此空格、
换行、控制字符、BiDi 覆盖字符、超长一律出局），`read_only` 必须**字面 true**，lane 顺序
归一后再哈希（同一份授权换个写法不会白白作废第一道门）。理由是这个对象会被原样写进
发现报告、再由 AI 念给用户听——任何自由文本叶子都是直通 Agent 上下文的注入通道，而它
偏偏是「用户到底授权了什么」的权威表述。写这组测试时自己抓到一处漏网：键名不匹配的
错误消息会**回显调用方写的键名**，等于把任意文本转发给读这条消息的 AI；已改成只报
个数（缺的键名可以说，那是 schema 自己的词表）。

副作用及其补偿：`tests/fixtures/activation/scope.json` 原有的 `_comment` 标注键被闭合
schema 挡掉，只好删除——而同目录其余 8 个 fixture 都靠这个键声明自己是脱敏合成数据，
少这一条会让「这份『用户授权了什么』的样例不是真人」这个事实无处可查。故新增
`tests/fixtures/activation/README.md` 承接该目录的来源声明，并写明 `scope.json` 为何
不能内联标注；同时加测试钉住：目录里每个 fixture 要么自带 `_comment`，要么被 README
逐名交代，否则新加的 fixture 可能悄悄没有任何来源证据。

| 命令 | 结果 |
| --- | --- |
| `python3.11 -m py_compile`（4 个脚本 + 5 个 RT-032 测试文件） | 通过 |
| `bash -n install.sh` | 通过 |
| `python3.11 -m unittest tests.test_rt032_activation_state` | 53 passed（+13） |
| `python3.11 -m unittest tests.test_rt032_activation_contract` | 80 passed（+17） |
| `python3.11 -m unittest tests.test_rt032_activation_wizard` | 110 passed（+16） |
| `python3.11 -m unittest tests.test_rt032_activation_integration` | 40 passed（+9） |
| `python3.11 -m unittest tests.test_rt032_contract_fidelity` | 22 passed（新增文件） |
| 五个 RT-032 模块合并运行 | 305 passed（上轮 228） |
| `python3.11 -m unittest tests.test_install_modes` | 67 passed（RT-031 基线不回归） |
| `python3.11 -m unittest tests.test_distribution` | 5 passed |
| `python3.11 -m unittest tests.test_governance_audit` | 62 passed |
| `python3.11 .aodw-next/06-project/governance-audit.py --root .` | 通过（612 个受跟踪文件，GA-ORPHAN 清零） |
| `bash .aodw-next/06-project/aodw-check.sh` | 通过（RT-028…RT-032 门禁全过；仅剩本机 skill 未安装的告警） |
| `git diff --check` | 通过（exit 0） |

真实 CWork/DocDB、定时任务、Gateway、远端与模型调用一律未触发。对拍测试全程用
`mock.patch.dict(os.environ, ..., clear=True)` 清空环境，本机 shell 里存在的
`CWORK_APP_KEY` 既未读取也未打印。**仍未跑完整 `make ci`**，按协调留给独立复审通过后
的最终一次冻结 CI。

## 第六轮整改（独立复审 NO-GO 的两个阻断项 + 四个残留项）

**BL-1：执行合同的取值模型必须是完备的，不能是一张手工列表。** 复现是
`{sync_docdb: false, wiki_sync: true}`：合同报「不发布」，交接单照出，哈希与
`wiki_sync: false` 时相同——而 nightly 的顶层 `if args.wiki_sync` 会去调
`cwk_sync_mirror_to_docdb.py --only-prefix wiki/`，不带 dry-run，也**不看**
`sync_docdb`。用户确认的那句「没有内容离开这台机器」是假的。

根因不是漏了一个键，而是「有没有漏」这件事没有独立判据。因此完备性改为**从上游
反向推**：`tests/test_rt032_contract_fidelity.py` 用 AST 解析
`cwk_nightly_pipeline.main()`，自己数出 41 个配置键、41 个环境变量键、全部
argparse 选项和每个键的取值优先级，再与 `NIGHTLY_SETTINGS` 对拍。实现漏一个，
测试**不会跟着漏**，因为两边的清单来源不同。取值优先级归为五类（`config_env_default`
/ `env_config_default` / `sync_docdb` 的反转 / `env_first_scalar` / 派生），分类器
按上游语句形状判定，不抄注册表。

不可排期的组合改为**确认之前就失败关闭**：`cloud_first`、
`publish_cloud_query_catalog` 会让 `enforce_cloud_pause` 在启动时 `SystemExit`，
而固定 argv（`--config/--run-name/--date`）永远递不出对应的 `--experimental-*`，
所以每一次被排期的运行都会在第一秒退出。给这种配置渲染一份和气的合同，等于请人
授权一个跑不起来的任务，然后让他以为镜像是新的。现在 `render-contract` /
`record-pilot` / `schedule-handoff` / `check-drift` 四道门一律 `EXIT_REFUSED`。

**顺手挖出并修掉一个更严重的问题：交接单没有校验命令行上的 `--config`。**
交接单里写的是 `--config <这次命令行给的路径>`，也就是说**今晚真正被读的是现在
传进来的这份配置**；而原实现只比对「盘上的合同 == 确认过的哈希」，那两者可以都对，
而 `--config` 指向另一份完全不同的配置。不需要有人使坏，树里放两份配置、tab 补全
补错一次就够了。现在 `cmd_schedule_handoff` 就地用命令行上的配置重算一次合同并
要求等于 `contract_sha256`：被确认的那句话，必须就是今晚会跑的那句话。绑定的是
合同内容而不是文件名，所以同内容换个位置仍然放行。

**BL-2：FIFO / 非常规文件会让激活路径永久挂住。** 上一轮已经给状态目录、状态文件、
锁和产物换上「`O_NONBLOCK` 打开 + `fstat` 判定」的激活本地原语（`cwk_atomic_file.py`
是 PR-001 冻结文件，RT-032 不能改）。本轮补上最后一条读路径：`_read_text`——它读的是
命令行给的 `--config` / `--scope-file` / `--pilot-report`，原实现是
`is_file()` 之后 `read_text()`，正是复审点名禁止的「先 lstat 再 open，窗口里仍会挂」。
新增 `read_regular_path()`：**只 open 一次**，在同一个描述符上判定，判定和读取因此
作用在同一个对象上，中间没有可以被替换的缝。与 `read_regular_at` 分工不同的一点是
**保留跟随符号链接**：那守的是我们自己的 0700 私有目录，这守的是用户自己的文件，
用户把配置放成软链或与 dotfiles 同 inode 是他自己的事，一刀切拒绝是我们越界。

`tests/test_rt032_nonregular_inputs.py`（新增，26 tests）把这条性质当作**活性**
来测，而不是当作「拒绝了错的文件」：进程内测挂住的代码，看起来只是套件变慢，断言
永远轮不到执行。所以每一条都是真子进程 + 真超时，超时本身就是失败。覆盖 readiness
探针、`cwk_doctor.py`、**真实 install.sh**、向导的读命令 / 写命令 / 抢锁、产物回读、
以及命令行输入文件；植入 FIFO、目录、套接字、软链到 FIFO、断链、软链到 `/dev/zero`、
硬链接七种形态。另外验证了拒绝仍然脱敏（无绝对路径、无 errno、无 traceback）、
探针不写盘、失败的产物回读不推进状态、以及拒绝路径不漏描述符。清理一律按名字
`unlink`，绝不 open——否则 teardown 自己就会挂，还会伪装成无关的 flake。

**残留 1：吊销第二道门必须留下历史证据。** 原先只有 `revision`/`updated_at` 变化。
新增 `_commit_after_gate_loss()`，覆盖**每一条**「门没了但没有迁移可写」的路径
（`cmd_status` 与 `cmd_check_drift` 两个分支），写一条 schema 合法的
`revoke-activation` 自环回执，记下吊销了哪道门、以及失效相对的是哪份合同摘要。
`flag-drift` / `record-pilot-fail` 那两条路**不重复记账**——迁移本身就是回执。
`PILOT_PASSED → confirm-activation → 试跑失败 → DEGRADED → check-drift` 全程已加
回归，并顺带发现 `cmd_status` 原本存在一条**无审计的吊销落盘**，一并堵上。

**残留 2：文档不再声称「一个字都不写」。** `skill/references/activation.md` §8 与
本文件残留 2 都改成实话：非法迁移时**状态和降级原因**不变，但若当时还有一道有效的
调度确认，它会被吊销、并留下回执；只有「已经没有东西可吊销」时文件才逐字节不变。

**残留 3：测试期不再读取 `.env`。** `tests/test_rt032_contract_fidelity.py` 不再
import pipeline，而是 AST 解析源码、**摘掉那一次 `load_local_env` 调用**、在
`clear=True` 的空环境里 exec，并在加载后断言本进程环境未变；`load_local_env` 随后
被替换成会直接抛断言的桩。用一份读别人 `.env` 的测试去证明产品不读 `.env`，不是测试，
是同一个缺陷换了身衣服。已实测：注入哨兵 `CWORK_APP_KEY` 后跑完 62 条，环境不变、
输出无哨兵、全程只 open 了 1 个文件且不是 `.env`。

**残留 4：交接单 argv 逐值钉死。** 不再只比对旗标名集合，改为断言 argv **逐项逐序**
等于 `["python3", "scripts/cwk_nightly_pipeline.py", "--config", <定位符>, "--run-name",
"nightly-{{YYYYMMDD-HHMM}}", "--date", "{{YYYY-MM-DD}}"]`，并遍历**上游 argparse 里
的每一个**选项，断言它及其 `--no-` 形式都不出现在 argv 任何位置。清单同样来自上游
AST，不来自实现自己的常量。

| 命令 | 结果 |
| --- | --- |
| `python3.11 -m py_compile`（4 个脚本 + 6 个 RT-032 测试文件） | 通过 |
| `bash -n install.sh` | 通过 |
| `python3.11 -m unittest tests.test_rt032_activation_state` | 53 passed |
| `python3.11 -m unittest tests.test_rt032_activation_contract` | 80 passed |
| `python3.11 -m unittest tests.test_rt032_activation_wizard` | 130 passed（+20） |
| `python3.11 -m unittest tests.test_rt032_activation_integration` | 40 passed |
| `python3.11 -m unittest tests.test_rt032_contract_fidelity` | 62 passed（+40，整文件重写） |
| `python3.11 -m unittest tests.test_rt032_nonregular_inputs` | 26 passed（新增文件） |
| 六个 RT-032 模块合并运行 | 391 passed（上轮 305） |
| `python3.11 -m unittest tests.test_install_modes` | 67 passed（RT-031 四模式不回归） |
| `python3.11 -m unittest tests.test_distribution` | 5 passed |
| `python3.11 -m unittest tests.test_governance_audit` | 62 passed |
| `python3.11 .aodw-next/06-project/governance-audit.py --root .` | 通过（613 个受跟踪文件） |
| `bash .aodw-next/06-project/aodw-check.sh` | 通过（RT-028…RT-032 门禁全过） |
| `git diff --check` | 通过（exit 0） |

六条既有闭环单独复跑（94 passed）确认不回归：布尔/整数取值优先级含 `y`、符号链接与
路径泄漏、门一致性、无审计的 `degraded_reason` 篡改、dir-fd 不跟随产物回读、
discovery scope 闭合归一。

治理边界未越：`scripts/cwk_atomic_file.py`（PR-001 冻结）与
`scripts/cwk_nightly_pipeline.py`（RT-026 所有，ordinal 已用尽）**只读不改**；本轮
改动全部落在 RT-032 自己拥有的三个脚本、RT-032 的测试、以及 RT-032 自己的文档里。
真实 CWork/DocDB、定时任务、Gateway、远端与模型调用一律未触发。**仍未跑完整
`make ci`**。

## 第七轮整改（独立复审 NO-GO 的一个根因阻断项）

**根因：合同的「环境」这一层只画了一半。** 上游 `cwk_nightly_pipeline.py` 在**模块
体**里就执行 `load_local_env(PROJECT / ".env")`——不是在 `main()` 里，是 import 阶段。
它按 `os.environ.setdefault` 填空：当前 shell 有的名字不动，没有的名字由项目根 `.env`
补上。也就是说今晚真正生效的环境是 **shell ∪ `.env`**，而合同只解析了 shell。后果是
一份放在项目根、谁都没再打开过的 `.env` 里写一行 `CWK_WIKI_SYNC=1`，就能让 nightly
把 wiki 推去 DocDB，而合同、`contract_sha256`、`check-drift` 和排期等价性判定**全部
看不见**——用户确认的仍然是「不发布」，哈希一个比特都不动。

第六轮把「有没有漏一个键」变成了可判定的问题，这一轮补的是更前面的一层：**取值从哪
些地方来**。41 个键一个不少，但入口少了一个，完备性就还是假的。

**先把上游的解析逐字复刻，不「改良」。** 静态读源码 + 合成临时夹具实测了 18 种形态，
`parse_project_env()` 与之逐条对齐：`B = 2` 键值都 strip；`export C=3` **被拒**（键名
`"export C"` 过不了 `[A-Za-z_][A-Za-z0-9_]*`）；`#` 注释与无 `=` 行跳过；`E=` 是空串
不是缺省；引号只脱一层且**先双后单**，所以 `H='"mixed"'` 得到 `"mixed"`、
`I="'mixed'"` 得到 `mixed`；重复键**第一次出现的赢**（`setdefault`，不是后写覆盖）；
`\x0b` / ` ` 也算换行（`.splitlines()`）；`S=a=b=c` 只切第一个 `=`。任何一条猜错，
合同就会在某个边角上如实地说谎，而那正是最难被人发现的一类假话。

**四道门共用同一个有效运行时模型。** `build_execution_contract` 先读 `.env`，
`merge_runtime_env()` 按 shell > `.env` 合成一份有效环境，再交给
`resolve_nightly_runtime`——**合并发生在环境层内部**，所以每个键原有的优先级类别
（配置优先 / 环境优先 / `sync_docdb` 的反转 / 派生）一条都没被改动，`sync_docdb` 仍然
配置优先地压过 `.env`。取值来源词表从 `env` 细分成 `shell` / `project_env`，进入
`contract_sha256`，`render-contract`、`record-pilot`、`schedule-handoff`、`check-drift`
因此自动同源：`.env` 改一个字，哈希就动，确认就作废。

**排期等价性因此变得更准，而不是更严。** 定时任务只拿得到 `CWORK_APP_KEY`，但它**会
重新加载同一个 `.env`**。所以第二次解析喂的是「允许清单里的 shell 值 + 同一份 `.env`」：
shell 遮住 `.env` 的值仍然判不等价并拒绝出交接单（届时遮罩消失、翻上来的是文件里的
值），而只由 `.env` 决定的取值**不再被误拒**——它今晚确实还在那儿。`CWORK_APP_KEY`
只传名字、不传值这条老保证原样保留。

**只说结构，不说内容。** 合同里关于 `.env` 的披露是闭合词表：文件名、在不在、由谁加载、
shell 是否优先、定时是否重载、**被建模的变量个数**、以及由它决定了注册表里的哪几个键。
不出现任何外来变量名、任何值、任何路径、任何原始行。`.env` 正是凭据所在——把没见过的
名字回显进产物或日志，等于亲手做了一次泄漏。

**失败关闭，但不把「缺席」当错误。** 上游对文件不存在是直接 `return`，那是正常状态，
所以 `read_project_env` 也照样返回空层。反过来，凡是上游会用它而我们建不准的形态一律
`ProjectEnvironmentError` → `EXIT_REFUSED`：目录（上游 `IsADirectoryError`）、非 UTF-8
（上游 import 期 `UnicodeDecodeError`，nightly 根本起不来）、读不动。FIFO 是最要命的
一种——上游 `path.exists()` 为真，`read_text()` 会**永远停在那里**；复用第六轮的
`read_regular_path`（只 open 一次、`O_NONBLOCK`、同描述符 `fstat`）后实测 11 种形态：
缺席/断链按正常缺席处理，fifo/目录/套接字/软链到 fifo/软链到 `/dev/zero`/非 UTF-8/
不可读全部在 0.000s 拒绝，空文件与软链到普通文件正常读。错误消息里只有「.env」三个字，
无路径、无 errno、无正文、无 traceback。

**「.env 在哪」本身不接受外部输入。** 没有 `--project-dir`、没有环境变量覆写：一个能
被外部指向别处的「.env 位置」本身就是漏洞——指错地方就等于给人看一份描述别处配置的
合同。只有一个私有模块常量 `_PROJECT_ENV_ROOT = _PROJECT`（与上游 `PROJECT` 同一个
算式、同一个目录），加上 `build_execution_contract` 的显式 kwarg 供测试注入；
`--project-dir` 语义的 `project_dir` 刻意**不**参与。

**测试不碰真实 `.env`。** 对拍预言机不再手抄一遍解析，而是**驱动上游那个函数本身**：
加载 pipeline 时把真函数存到 `__real_load_local_env__` 再打桩，跑对拍时用一个只有
`environ` 属性的 `_EnvironShim` 把它的 `os` 换掉，让它写进沙箱字典，跑完断言本进程
`os.environ` 逐键未变。合同类套件全部改为显式指向一个空的临时目录（`NO_DOT_ENV` /
`use_project_env_root`）——否则这些期望哈希会随开发机上碰巧有没有 `.env` 而变，且在真
树里种一个 `.env` 有覆盖掉开发者自己那份的风险。已确认工作树与主树都没有真实 `.env`
（只有 `.env.example`）。

**三处被测试当场揪出的实现认知偏差**（都不是测试写错，是我一开始想错了）：
`sync_docdb` 是唯一的配置优先键，用它做「`.env` 单独生效」的样本会被配置正当地压掉；
`cmd_check_drift` 的载荷键是 `contract_drift` 不是 `drift`；`CWK_DETAIL_CAP` 敌不过夹具
里的 `detail_cap: 60`。前者顺手变成了一条正向性质——`.env` **不得**改变配置优先键的
次序，现在有专门的回归钉着。

| 命令 | 结果 |
| --- | --- |
| `python3.11 -m py_compile`（3 个脚本 + 4 个改动测试文件） | 通过 |
| `bash -n install.sh` | 通过 |
| `python3.11 -m unittest tests.test_rt032_activation_state` | 53 passed |
| `python3.11 -m unittest tests.test_rt032_activation_contract` | 80 passed（改为对空 `.env` 根解析） |
| `python3.11 -m unittest tests.test_rt032_activation_wizard` | 143 passed（+13） |
| `python3.11 -m unittest tests.test_rt032_activation_integration` | 40 passed |
| `python3.11 -m unittest tests.test_rt032_contract_fidelity` | 109 passed（+47） |
| `python3.11 -m unittest tests.test_rt032_nonregular_inputs` | 31 passed（+5，真子进程真超时） |
| 六个 RT-032 模块合并运行 | 456 passed（上轮 391） |
| `python3.11 -m unittest tests.test_install_modes` | 67 passed（RT-031 四模式不回归） |
| `python3.11 -m unittest tests.test_distribution tests.test_governance_audit` | 67 passed（5 + 62） |
| `python3.11 .aodw-next/06-project/governance-audit.py --root .` | 通过（613 个受跟踪文件） |
| `bash .aodw-next/06-project/aodw-check.sh` | 通过（RT-028…RT-032 门禁全过） |
| `git diff --check` | 通过（exit 0） |

新增的 `.env` 层性质（合成夹具，共 65 条）：文件单独把 `wiki_sync` / 发布翻开并推动
哈希；shell 压过文件；配置优先类不被重排；整数与别名（`CWK_WIKI_LIMIT` 等）；激活后
改文件触发漂移并吊销第二道门；同一份文件让排期运行判为等价；仅 shell 差异判为不等价
并拒绝出单；凭据形态的无关条目从不进入产物/日志/状态；畸形与非常规输入确定性且不阻塞。

第六轮的既有闭环全部复跑未回归：41 个行为设置与发布/不可排期路径、argv 与配置内容
逐值绑定、FIFO/非常规状态与产物与用户文件读取、符号链接边界、吊销历史证据、闭合
scope schema、无错误回显与路径泄漏、RT-031 四模式、安装零副作用。

治理边界未越：`scripts/cwk_atomic_file.py`（PR-001 冻结）与
`scripts/cwk_nightly_pipeline.py`（RT-026 所有）**只读不改**——本轮全程静态读源码，
一次都没有 import 到会触发真实 `load_local_env` 的路径。改动落在 RT-032 自己的
2 个脚本、4 个测试文件与 2 份文档里。真实 CWork/DocDB、`.env`、凭据、定时任务、
Gateway、远端一律未触发。**仍未跑完整 `make ci`**，按协调留给下一次独立复审之后。

## 第八轮整改（完整 CI 的真实回归：71 个 PR-001 门禁失败）

**这不是 runner 的问题。** 首次完整 `make ci` 被执行工具超时截断，但截断前的进度里已有
71 个真实失败；在最小化净化环境与普通环境下都能独立复现。全部 71 条**共用同一个根因**：
消息逐字相同（`missing v2 security owner scope for RT-017`），调用链逐帧相同
（`setUp` → `materialize_valid_g6_world` → `materialize_security_receipts`
→ `test_pr001_release_gate_validation.py:1453`）。没有第二个根因。分布在 7 个测试类里，
按进度点排成 20 + 32 + 14 + 5 四簇，全部落在 `tests.test_pr001_release_gate_validation`
这一个模块内（该模块 182 tests / 71 failures）。

**根因：`scripts/cwk_*.py` 不是命名习惯，而是一张冻结安全清单的成员判据。**
`tests/pr001_evidence_binding.py:1564` 处：

```python
actual_managed = {p for p in all_records if namespace.fullmatch(p) or p in explicit}
if not actual_managed <= declared_managed:
    return None  # a new managed script has no frozen owner/category
```

`namespace` 就是 `^scripts/cwk_[a-z0-9_]+\.py$`。而门禁夹具是
`git clone --shared <本仓库>`——它把**真实仓库的 HEAD** 克隆进去，于是 RT-032 新增的三个
`scripts/cwk_activation_*.py` 出现在 `all_records` 里，落在
owner/central/legacy 三族之外，闭包判定失败关闭，`security_owner_scope_tree_sha256`
返回 `None`，RT-017…026 的历史 owner-scope 快照全部拿不到摘要。实测确认：受跟踪文件中
落在该命名空间内、却未被声明的路径**恰好只有那三个**（declared 102，actual 67，
undeclared 3）。

**GA-ORPHAN 清零并不等于进了 PR-001 的登记表。** 这是两道问不同问题的门：AODW 治理问
「这个文件有没有主」，PR-001 问「这个文件在不在我冻结的集合里」。第六轮给
`R-runtime-rt032-activation` 建的 exact_set 规则只回答了前一个问题——它的 rationale 甚至
已经写明「这三个新文件不在该集合内」，但没人把这句话接到 PR-001 那道门上。往
PR-001 登记表里加三行、或凭空开 v2 演化槽位，都是伪造别人冻结证据里的成员资格，不做。

**修法：把三个模块移出那个保留命名空间。** 备选方案按「直接 CLI 调用 / 同级 import /
`cwk_doctor.py` / 安装与分发复制 / py_compile 与构建覆盖 / Skill 与文档命令 / 治理
exact-set 归属 / 路径隐私 / 测试」逐条评估，选 `scripts/activation_*.py`，因为它是唯一
**零附带改动**的一条路：

| 判据 | `scripts/activation_*.py`（选中） | `scripts/activation/` 子包 |
| --- | --- | --- |
| `.env` 根算式 `parents[1]` | **逐字不变**，与上游 `PROJECT` 仍同一算式 | 要改成 `parents[2]`，动到第七轮刚钉死的最敏感不变量 |
| `py_compile scripts/*.py` | 自动覆盖 | 漏掉，Makefile 要改 |
| Makefile（sha 已 pin） | 不用动，不用重新 pin | 要动要重新 pin |
| `sys.path.insert(0, scripts)` 同级 import | 不变 | 不变 |
| 分发复制 | `scripts/` 整目录，不变 | 不变 |
| 直接 CLI | `python3 scripts/activation_wizard.py` | 包内直跑要额外处理 |

先例是现成的：`scripts/cwk_wiki_batch_driver.sh` 本来就在 PR-001 命名空间之外、由 AODW
清单直管。代价是打破了「`scripts/` 下全是 `cwk_` 前缀」这条不成文习惯——但把归属写进
路径本身，比藏在一张别人的登记表里更容易看见。

**迁移映射（三条，逐一改名，不留任何 `scripts/cwk_*.py` 兼容壳）：**

| 旧路径 | 新路径 | 模块名 |
| --- | --- | --- |
| `scripts/cwk_activation_state.py` | `scripts/activation_state.py` | `activation_state` |
| `scripts/cwk_activation_contract.py` | `scripts/activation_contract.py` | `activation_contract` |
| `scripts/cwk_activation_wizard.py` | `scripts/activation_wizard.py` | `activation_wizard` |

改名用 `git mv` 保留历史；引用是**统一前缀替换** `cwk_activation_` → `activation_`，
因为全部 73 处引用无一例外都是这个前缀。覆盖 22 个文件：3 个脚本本体、`cwk_doctor.py`、
`install.sh`、7 个测试、8 份文档/提示词、治理清单、以及本文件。改完
`git grep cwk_activation` 为空。

**新增回归（`ManagedScriptNamespaceTests`，5 条，落在 RT-032 自己的套件里）：** 直接调
PR-001 **真实的** `_security_managed_inventory_parts` 解析登记表，再拿 `git ls-files`
（与夹具看到的已提交内容同源，未跟踪文件本来就进不了克隆）对拍——不抄它的规则。四条分别
断言：没有任何受跟踪文件进了该命名空间却未被声明；RT-032 的三个模块都在命名空间之外且
确实存在；治理清单的 exact_set 与实际文件集合**逐条相等**（再加第四个模块必须显式登记，
不会被吸收，因为 `scripts/` 是 exact-only 区、不许用前缀规则）；没有任何受跟踪文件还写着
迁移前的模块名（needle 运行时拼出来，否则这份要描述迁移的文件会把自己搜出来）。
**已做反向验证**：临时 `git add` 一个 `scripts/cwk_rt032_probe_delete_me.py`，第一条当场
变红并点名该文件，移除后恢复绿；证明它咬得住，不是恒真断言。

未削弱任何 PR-001 断言，未改动 PR-001/RT-026 的策略、登记表或测试，未调整任何预期清单
计数。

| 命令 | 结果 |
| --- | --- |
| 决定性首个失败（`DelegatedFamilyCanonicalReuseTests.test_capability_sequence_two_needs_archive_link_and_fresh_probe`） | 迁移前 FAIL → 迁移后 **1 passed** |
| `python3.11 -m unittest tests.test_pr001_release_gate_validation` | 迁移前 182 tests / **71 failures** → 迁移后 **182 passed / 0 failures** |
| `python3.11 -m py_compile scripts/*.py` | 通过（glob 自动覆盖三个改名模块） |
| `bash -n install.sh` | 通过 |
| `python3.11 -m unittest tests.test_rt032_activation_state` | 53 passed |
| `python3.11 -m unittest tests.test_rt032_activation_contract` | 80 passed |
| `python3.11 -m unittest tests.test_rt032_activation_wizard` | 143 passed |
| `python3.11 -m unittest tests.test_rt032_activation_integration` | 45 passed（+5 新回归） |
| `python3.11 -m unittest tests.test_rt032_contract_fidelity` | 109 passed |
| `python3.11 -m unittest tests.test_rt032_nonregular_inputs` | 31 passed |
| 六个 RT-032 模块合并运行 | 461 passed（上轮 456，+5） |
| `python3.11 -m unittest tests.test_install_modes` | 67 passed（RT-031 四模式不回归） |
| `python3.11 -m unittest tests.test_distribution tests.test_governance_audit` | 67 passed |
| `python3.11 .aodw-next/06-project/governance-audit.py --root .` | 通过（613 个受跟踪文件） |
| `bash .aodw-next/06-project/aodw-check.sh` | 通过 |
| `git diff --check` | 通过（exit 0） |

第七轮及更早的全部性质原样保留：`.env` 有效运行时模型（`parents[1]` 未动）、41 个行为
设置、`contract_sha256` 与漂移、非阻塞读、闭合 scope schema、激活状态机、RT-031 四模式、
安装零副作用。**本阶段仍未跑完整 `make ci`**，按要求留给下一次独立复审之后。NB-1…NB-5
不在本阶段实现，仍记为非阻断待办。

## 变更记录

- 2026-09-02：根据用户批准的安装后使用路径，确定独立 RT；采用“AI 沟通 + 确定性状态/回执”的激活架构，并与 RT-031 安装接入职责分离。
- 2026-09-02：实现零重叠确定性核心并通过定向测试；治理归属声明按并行协调要求转为待集成项。
- 2026-09-02：迁移表补 `(PILOT_PASSED, record-pilot-pass) -> PILOT_PASSED` 自环。重跑一次通过的试跑本身安全：回执是内容寻址的，**证据变了**才会产出新回执并作废旧的第二道确认，证据一模一样则回执哈希不变、确认继续有效。（初版记录写成"重跑必然作废"，与实现不符，已按实际行为更正；两种情形都已加回归测试。）
- 2026-09-02：整改独立审阅的两个阻断项——试跑门禁对采集回执改为失败关闭并把回执事实绑进哈希；CLI 边界收口 OSError/ContractError 为脱敏 JSON 与既有退出码。测试加固保留并提交。
- 2026-09-02：接入前定向加固。`record-pilot` 的 `invalidated_gates` 改为回报本条命令作废的全部门（原先漏掉写入新回执后才失效的那批，导致「成功但什么都没失效」与 `next_step: confirm_activation` 自相矛盾）；stdout 断管道时把描述符改指 devnull，进程退出码从解释器的 120 收回到约定的 2 且 stderr 不再有噪声；并把「输入/持久化失败不进 DEGRADED、不写迁移回执」这条既有行为固定成测试与模块文档。交接单绝对路径、裸相对路径脱敏、`propose-profile` 的同类失效回报缺陷记入「接入前待议项」，本轮不动。
- 2026-09-02：在 RT-031 冻结基线上完成集成。治理补登（`exact_set` 规则，`evolution_path=repo-standard-change`；不开伪 v2 槽位），GA-ORPHAN 清零；三个待议项全部落地——`propose-profile` 失效回报补齐并加可达回归、交接单改项目相对定位符并对无法安全表述的配置拒绝出单、脱敏闸门加宽到裸相对路径且不误伤 `read/write` 类词组；安装保持零副作用并输出 `CWK_ACTIVATION`，`doctor` 复用同一探针报 `activation`，Skill 新增激活参考、README/上手/引导/运维/迁移文档统一成一条路径。RT-031 四模式合同与 `NEXT_SESSION` 语义不回归。

- 2026-09-02：整改终审的两个阻断项与四个残留项。执行合同按上游 nightly 的**真实**取值优先级重实现（三种优先级不统一：cap/回溯是配置优先，`backfill_enabled` 是环境优先，`sync_docdb` 是配置优先且默认 False），并新增行为等价 + 源码钉死的双向对拍；进一步发现定时任务只拿得到 `CWORK_APP_KEY`，故合同双次解析并标注来源，凡依赖当前 shell 的设置一律拒绝出交接单。只读探针与 doctor 对符号链接改为 lstat 不跟随、失败关闭、不吐 traceback/路径，安装仍非致命。`check-drift` 真漂移时显式吊销第二道确认（消除「要求重新确认」与「确认仍有效」并存的自相矛盾），迁移非法时一个字都不写（`pilot_failed` 不再被覆盖），凡落盘必带 revision/updated_at 痕迹。产物回读改为 dir-fd 不跟随，与写入对称。discovery `scope` 改闭合 schema，先校验归一再哈希，错误消息不回显调用方内容。

- 2026-09-02：第六轮整改。执行合同的完备性改为从上游 AST 反推（41 配置键 / 41 环境键 / 全部 argparse 选项 / 五类取值优先级），实现漏键测试不会跟着漏；`{sync_docdb:false, wiki_sync:true}` 不再谎称「不发布」；`cloud_first` / `publish_cloud_query_catalog` 在四道门一律确认前失败关闭。顺带修掉交接单不校验命令行 `--config` 的问题——原先可以「确认合同 A、却把 B 排上去」。`_read_text` 换成只 open 一次的 `read_regular_path`，最后一条 lstat-then-open 的 TOCTOU 挂起窗口关闭，并以真子进程 + 真超时覆盖 readiness / doctor / 真实 install.sh / 向导读写与抢锁 / 产物回读 / 命令行输入的七种非常规形态。吊销第二道门补 `revoke-activation` 回执并消除 `cmd_status` 的无审计落盘；文档不再声称「一个字都不写」；对拍测试不再读 `.env`；交接单 argv 逐值逐序钉死。

- 2026-09-02：第七轮整改。补上执行合同缺的那一层输入：上游在 import 阶段就
  `load_local_env(PROJECT / ".env")`，按 `setdefault` 用项目根 `.env` 填补 shell 没有
  的名字，所以今晚真正生效的环境是 shell ∪ `.env`；原实现只解析 shell，导致一份没人
  再打开过的 `.env` 里一行 `CWK_WIKI_SYNC=1` 就能打开发布，而合同、哈希、漂移与排期
  等价性全部无感。现在解析逐字复刻上游（含 `export` 被拒、引号先双后单、重复键取先者、
  `\x0b` 算换行等 18 种形态实测对齐），合并在环境层内部完成因而不改动任何键原有的优先级
  类别，来源词表细分为 `shell` / `project_env` 并进入 `contract_sha256`，四道门同源。
  排期等价性改为「允许清单里的 shell 值 + 同一份 `.env`」：shell 遮罩仍判不等价并拒绝
  出单，仅由 `.env` 决定的取值不再误拒。披露只给闭合结构（在不在、由谁加载、决定了哪
  几个注册表键），绝不回显外来变量名、值、路径或原始行。缺席按上游语义视为正常；目录 /
  非 UTF-8 / 读不动一律拒绝，FIFO 复用只 open 一次的 `read_regular_path` 在 0.000s 拒绝
  而不是永久挂住。`.env` 位置不接受任何外部输入，并由 AST 对拍钉死。对拍预言机改为驱动
  上游函数本身（`os` 打影子、跑完断言本进程环境未变），合同类套件一律指向空临时目录，
  测试全程不碰真实 `.env`。

- 2026-09-02：第八轮整改（完整 CI 的真实回归）。首次完整 `make ci` 暴露 71 个真实失败，
  全部同一根因：`scripts/cwk_*.py` 是 PR-001 冻结安全清单的成员判据，而不是命名习惯；
  RT-032 新增的三个模块落在 owner/central/legacy 三族之外，使
  `_security_scope_snapshot` 的闭包判定失败关闭，RT-017…026 的历史 owner-scope 摘要
  全部取不到。GA-ORPHAN 清零回答的是「有没有主」，与 PR-001 的「在不在冻结集合里」
  是两道门。修法是把三个模块移出保留命名空间——`cwk_activation_*` → `activation_*`，
  仍留在 `scripts/` 下，因此 `.env` 根算式 `parents[1]`、`py_compile scripts/*.py`
  覆盖、Makefile 的 sha pin、同级 import 与分发复制全部不用动；不留任何
  `scripts/cwk_*.py` 兼容壳。73 处引用统一前缀替换，覆盖 22 个文件。新增
  `ManagedScriptNamespaceTests` 直接调 PR-001 真实解析器对拍并做过反向验证。
  未削弱 PR-001 断言，未改其策略/登记表/测试。
- 2026-09-02：最终收口。冻结实现 `dd92550` 通过唯一一次完整 `make ci`
  （2704 tests，skipped=7，退出码 0）与 PR-001 release-gate 独立长通道复跑
  （182/182，退出码 0）；RT-032 转入 `reviewing`。NB-1…NB-5 保持非阻断，
  转入试用反馈；本回执之后不再追加复核轮次。按用户明确授权，后续合并
  `main` 并推送 `origin/main`。

## 最终全量 CI 回执（冻结实现 `dd92550`）

- 命令：`make ci`，在 `env -i` 白名单净化环境运行（HOME/PATH/USER/TMPDIR/
  LANG/LC_ALL/PYTHONNOUSERSITE/PYTHONHASHSEED=0）；无任何 CWK/CWORK/XG/
  凭据类变量；项目根无 `.env`（doctor 实测 `env_file: absent`）。
- 时间：2026-09-02T02:45:47Z–03:54:22Z（约 68.6 分钟），退出码 0。
- `make doctor`：PASS。
- `python3 -m py_compile scripts/*.py`：通过（含 `activation_{state,contract,wizard}.py`）。
- 单测：`Ran 2704 tests in 4098.987s` → **OK（skipped=7）**。
- `make smoke`：通过（ci-smoke 处理 1 条样例）。
- `aodw-check.sh`：通过（RT-028…RT-032 门禁全过；30 个 RT 花名册一致；
  唯一 WARN 为本机 handover-pack skill 未安装，属宿主状态，不阻断）。
- `governance-audit.py`：通过（613 个受跟踪文件全部有主）。
- 独立复核补充：PR-001 release-gate 长通道完整复跑 **182/182 通过**
  （2760s，退出码 0）；独立审阅结论无阻断。

## 遗留事项

- 完整 `make ci` 已在冻结实现 `dd92550` 上通过（见上文回执），验收标准第 7 条
  闭合；非阻断项转入试用反馈，不再追加复核轮次。
- 合同建模的是**渲染那一刻**的项目根 `.env`。文件在渲染之后、02:30 之前被改，与配置
  文件被改是同一类风险，同样只能靠下一次 `check-drift` 暴露——本仓库不监听文件系统。
  差别在于 `.env` 通常没人当成「配置」来对待，所以文档专门写了这一条。
- `.env` 的位置固定为项目根（与上游 `PROJECT` 同一算式），**不接受任何外部输入**。
  上游若改文件名、改锚点或改成 `cwd`，`ProjectEnvLocationTests` 会红——它从上游 AST
  里把 `PROJECT = Path(__file__).resolve().parents[1]` 和 `PROJECT / ".env"` 逐项钉死。
  仍需人工跟进的是：那时红的是测试，正确的新位置得由人来定，不会自动跟随。
- `resolve_nightly_runtime` 是对 `cwk_nightly_pipeline.main()` 取值逻辑的**忠实重
  实现**，不是共享的同一份代码。上游属 PR-001 受管、RT-026 所有且 v1 演化槽位已用尽，
  从本 RT 改它或凭空开 v2 槽位都是伪演化路径；`import` 复用又会触发它模块级的
  `load_local_env`。当前靠 `tests/test_rt032_contract_fidelity.py` 的行为对拍与源码
  断言防漂移——上游一改就红。真正的单一真相源要等 RT-026 侧有正当演化入口时再合并。
- `record-schedule` 只登记使用者说自己建过的宿主任务，**不验证**该任务真的存在——
  这是刻意的边界（仓库不碰宿主调度面），但意味着「`ACTIVE` 且宿主任务已被删」这种
  状态只能靠 `check-drift` 在下一次人工检查时暴露，不会自己报警。
- `pause` 只改本仓库的姿态，不会停掉宿主侧任务；文档已写明，但真要停跑仍依赖使用者
  去宿主禁用任务。
- `doctor` 与安装器的 `activation` 只**转述**已记录的状态，不读配置与合同文件，因此
  配置改了但还没人跑 `check-drift` 时它仍会报 `active`。这是「探针不写、命令才写」
  这条边界的代价：漂移判定必须把重算结果落盘并作废确认，那是命令的职责，不是只读
  探针的。文档已把 `check-drift` 写进运维路径，但它不会自己触发。
- 宿主侧「怎么建这个任务」没有可执行文档可依：本仓库不发明 OpenClaw 调度 API，只给
  交接单，具体机制由使用者的宿主决定。
