# RT-Lite: RT-031 - CWK 内部小团队最小上手

> profile: Spec-Lite | execution_mode: collaborative

## 方案（给人看）

- 做什么：把 CWK 的“程序安装”和“接入 OpenClaw”拆开。程序安装在所有环境使用同一条
  核心流程；OpenClaw 接入由使用者明确选择四种模式之一：宿主管理正式 Skill、
  Workspace 自助正式 Skill、`AGENTS.md` 路由、只安装程序不接入 OpenClaw。
- 为什么：现有安装器把本地模板、脱敏检查和 Skill 软链接绑成一个动作；云端 sandbox
  的 Skill 目录是只读挂载，按现有文档执行必然卡住。另一方面，非 sandbox 环境仍然需要
  正式 Skill 安装，不能为了迁就一种环境把所有人都降级成路由模式。
- 代价：安装器会多一个明确的接入模式合同，`doctor` 会识别多种 Skill 根和 `.env`
  存在性，文档与测试要同步维护。模式选择会比原来的单一 `--install-skill` 多一步，但可
  避免脚本暗中改错目录或要求不该下放给 sandbox 的 OpenClaw CLI 权限。
- 这次故意不做什么：不把 OpenClaw CLI 装进公共 sandbox 镜像；不把 Gateway token、
  宿主配置或 Docker 权限交给 sandbox；不修改远端 Agent、Gateway、镜像或挂载；不执行
  真实 CWork 读取、DocDB 发布、cron、外部发送；不复制任何人的私有配置或数据；不把
  四种模式扩展成新产品功能。
- 用户怎样算成功：同一份 CWK 在四类环境里都能得到明确、可验证的结果——sandbox
  自助时核心安装成功并可选择路由；运维可只给指定 Agent 注册正式 Skill；非 sandbox
  可在自己的可写 Workspace 正式安装；不需要 OpenClaw 集成时只安装程序。每种结果都
  输出机器可读状态，不泄露凭据，也不会因 Skill 目录只读而把整个安装判成失败。
- 建议：**推荐**采用“核心安装 + 显式接入适配器”。默认只做核心安装，不静默修改
  `AGENTS.md`、不静默写 Skill 目录；正式 Skill 优先由宿主或可写 Workspace 安装，路由
  只用于完全自助的 sandbox。

## 假设与现状

- 关键假设：
  - 每个员工只使用自己的 Agent 和 Workspace，机器由运维统一管理；共享 sandbox 镜像
    不应携带控制面凭据。
  - CWK 本身只需要现有 Python、Git、Make 等执行依赖，不依赖 sandbox 内的
    `openclaw` CLI。
  - `AGENTS.md` 路由属于 Workspace 内的显式接入方式，只能在用户选择 `router` 时改，
    且必须保留文件中原有内容。
- 现状依据（文件 / 历史 RT / 提交）：
  - `install.sh` 当前用 `--install-skill` 把 `skill/` 软链接到默认
    `$HOME/.openclaw/skills` 或 `--skills-dir`；程序安装与 Skill 安装耦合。
  - `scripts/cwk_doctor.py` 当前只从 `$HOME/.openclaw/skills` 与
    `$HOME/.agents/skills` 找公司 Skill，也只看进程环境，不读取项目 `.env`。
  - `docs/SANDBOX_ONBOARDING.md` 与 `prompts/OPENCLAW_SANDBOX_BOOTSTRAP.md` 当前要求
    写 `/workspace/skills`，与已核实的只读保护挂载冲突。
  - OpenClaw 的 per-agent 正式 Skill 根是 `<workspace>/skills`；sandbox 中
    workspace Skill 与物化公司 Skill 对 Agent 可读，但受保护挂载可禁止 Agent 自写。
  - 对目标多 Agent 环境的只读审计确认：Workspace 可写，三个 Skill 根只读，公共镜像
    被多个 Agent 共用；这证明“公共镜像放 CLI”与“只给某个 Agent 安装 CWK”是两件事。
  - RT-031 仍为 `in-progress`，验收目标就是内部员工安装、自检和试跑。AODW 要求同一
    验收目标续做原 RT，不新建重复 RT。
  - `install.sh` 与 `scripts/cwk_doctor.py` 位于 PR-001 为 RT-026 声明的 owner surface；
    实现必须保留其安全边界并运行对应合同测试。若冻结证据不允许本次变化，停止并单独
    提交治理取舍，不能改写历史合同来换绿灯。

## 安装与接入合同

核心安装始终先完成，且不依赖 OpenClaw CLI：

1. 只在当前 CWK 项目中创建缺失的私有模板，已有文件不覆盖。
2. 新建 `.env` 与 `cwk-mirror.local.json` 时权限收紧为 `0600`。
3. 编译 Python，运行脱敏 smoke；不读取真实 CWork，不发布 DocDB。
4. 输出 `CWK_CORE_READY` 或清楚的失败原因。

OpenClaw 接入只能显式选择下列一种：

- `host-skill`：不尝试写受保护 Skill 根；输出
  `SKILL_REGISTRATION_REQUIRES_HOST_ADMIN` 和非敏感的 Skill 源路径，交由运维使用与
  Gateway 同版本的宿主控制面，只给指定 Agent 做复制安装。
- `workspace-skill`：仅当目标 `<workspace>/skills` 明确可写时，把公开 `skill/` 复制到
  `cwk-mirror-workflow`；不复制 `.env`、本地配置、数据或运行产物。目标已存在时默认
  拒绝覆盖，避免覆盖其他来源或手工改动。
- `router`：在指定 Workspace 的 `AGENTS.md` 中维护一个带固定起止标记的 CWK 路由块，
  让 Agent 按需读取 `<workspace>/CWK/skill/SKILL.md`。首次追加、重复执行不重复、完整
  旧块可更新；缺半边标记或出现多个块时拒绝修改并报告冲突。
- `none`：只保留核心安装，不接入 OpenClaw。

检测只负责报告推荐，不静默选择模式。`--install-skill` 保留为兼容入口，但要给出迁移
提示并映射到受约束的正式 Skill 安装合同；不得继续把“能建一个软链接”等同于“OpenClaw
一定会发现并加载”。

统一输出状态至少包括：

```text
CWK_CORE_READY
OPENCLAW_INTEGRATION=FORMAL_SKILL
OPENCLAW_INTEGRATION=AGENTS_ROUTER
OPENCLAW_INTEGRATION=NONE
SKILL_REGISTRATION_REQUIRES_HOST_ADMIN
```

“一个 Agent 只启用一种模式”不只是文档约定，安装器必须强制：装 `workspace-skill` 前
检查目标 Workspace 的 `AGENTS.md` 是否已有受管路由块，装 `router` 前检查目标 Skill 根
是否已有正式 Skill；命中时输出 `OPENCLAW_INTEGRATION_CONFLICT` 与
`CWK_EXISTING_INTEGRATION=...`、退出码 3、不写任何文件。`--force` 的语义严格限定为
“替换同一种模式下已存在的目标”，不得放行混装，也不得自动拆除另一种模式。

`doctor` 只报告凭据“已配置 / 缺失”，永不打印值、前缀、哈希或可逆片段；读取 `.env`
时使用最小 dotenv 解析，不执行 shell 内容。

## 实现备注（用户不问可不展开）

- 计划改的文件：
  - `install.sh`：核心安装、显式接入模式、只读目录降级与稳定状态输出；
  - `scripts/cwk_doctor.py`：安全加载项目 `.env`、Skill 多根发现、集成状态检查；
  - `prompts/CWK_AGENTS_ROUTER.md`（拟新增）：受管路由块模板，不含凭据和机器专属值；
  - `tests/test_distribution.py` 与必要的安装隔离测试：四模式、只读目录、幂等与冲突；
  - `skill/SKILL.md`、`skill/references/migration.md`、`skill/references/operations.md`：
    项目根定位与四种接入方式；
  - `README.md`、`docs/INTERNAL_DISTRIBUTION.md`、`docs/SANDBOX_ONBOARDING.md`、
    `prompts/OPENCLAW_SANDBOX_BOOTSTRAP.md`：统一命令和责任边界；
  - 若新增文件或敏感文件 pin 发生变化，按 RT-030 的全树归属规则同步最小治理登记；
    不修改任何已冻结 PR-001 合同，除非用户另行批准治理变更。
- 不能破坏的约定：
  - CWork 默认只读；raw 仍是唯一事实源；安装和 doctor 不访问真实业务数据；
  - 不读取、回显或提交凭据；不自动发布、建 cron、改 Gateway 或放宽 sandbox；
  - Workspace 外不做自助安装；宿主模式只输出管理员交接，不从 sandbox 发起控制面调用；
  - 一个 Agent 最终只启用一种 CWK 接入模式，避免正式 Skill 与路由重复触发；
  - 现有非 sandbox 使用者要有明确兼容/迁移提示，不把旧链接静默删除或覆盖。
- 内部阶段：
  1. **I0 合同冻结**：把命令、状态、四模式、副作用和失败语义写成测试；核对 PR-001
     owner surface 是否允许变化。
  2. **I1 核心安装**：程序安装与 OpenClaw 接入解耦，落实 `0600` 和稳定状态。
  3. **I2 接入适配器**：实现 host handoff、Workspace 正式 Skill、路由和 none；处理
     幂等、只读、已有目标和异常标记。
  4. **I3 doctor 与 Skill 入口**：支持 `HOME=/`、物化 Skill 根、项目 `.env` 和
     `CWK_PROJECT_DIR` / `/workspace/CWK` 定位，不泄露值。
  5. **I4 文档统一**：四个入口只引用同一合同，删除当前不可执行的 sandbox 命令。
  6. **I5 验收**：定向测试、治理门、全量 `make ci`；全程只用脱敏 fixture。

## 验证

- 要跑的检查 / 要点的真实路径：
  - 临时 Workspace 中分别验证 `none`、`router`、`workspace-skill`、`host-skill`；
  - 模拟 `/workspace/skills` 与 `/workspace/.agents/skills` 只读，确认核心安装仍成功，
    host handoff 状态准确，router 只写指定 `AGENTS.md`；
  - router 连跑两次内容不重复；保留原有 `AGENTS.md`；半块/重复块拒绝写；
  - Workspace Skill 只复制公开 `skill/`，目标存在时不覆盖；
  - `HOME=/` 且公司 Skill 仅位于物化根时，`doctor --require-live` 能找到；
  - `.env` 含测试占位值时输出只能出现 configured/missing，不能出现测试值及其片段；
  - 新建私有配置 mode 为 `0600`，已有文件内容和权限不被擅自改写；
  - `tests/test_distribution.py`、新增安装合同测试、与 RT-026 owner surface 相关的
    PR-001 定向测试；
  - `make governance-audit`、`make aodw-check`、`make ci`、`git diff --check`；
  - 敏感文件、私有配置和运行产物扫描。
- 对照成功标准的结果（2026-09-02，定向验证与最终全量 CI）：
  - 四种模式在隔离的临时 Workspace 中逐一通过；只读 Skill 根下核心安装仍
    `CWK_CORE_READY`，host handoff 状态正确，router 只写指定 `AGENTS.md`。
  - router 连跑两次不重复、保留原有内容；半块 / 重复块 / 反序标记均拒绝写入。
  - Workspace Skill 只复制公开 `skill/`，目标已存在时拒绝覆盖。
  - `HOME=/` 且公司 Skill 只在物化根时，`doctor --require-live` 能找到。
  - `.env` 含占位值时输出只有 configured / missing，占位值及其前缀均不出现。
  - 新建私有配置 mode 为 `0600`；已有文件的内容与权限不被改写。
  - 已跑：`bash -n install.sh`、`python3.11 -m py_compile`、
    `tests.test_install_modes`（67 项）、`tests.test_distribution` +
    `tests.test_governance_audit` + `tests.test_pr001_script_evolution_guard`
    （314 项，1 skip）、`make governance-audit`（591 文件）、`make aodw-check`、
    `git diff --check`。
  - 最终全量 `make ci` 已在干净提交 `dd91579` 上通过：`make doctor` 通过；
    2243 项单元测试通过（8 skip，用时 3971.637 秒）；规则 smoke、AI dry-run smoke、
    AI degraded smoke 均通过；AODW 框架 fixture 79/79、RT-028～RT-031 门禁与 29 个 RT
    花名册一致性通过；`governance-audit` 覆盖 591 个受跟踪文件且无孤儿。唯一告警是本机
    `handover-pack` 未安装到 gitignored 的宿主目录，不阻断仓库交付。运行前显式清除了
    `CWK_PROJECT_DIR`、真实凭据和 DocDB 目标等环境变量，未访问真实 CWork/DocDB。

### 收口前评审的整改（2026-09-01）

独立只读评审提出 5 个阻塞项，均已在同一 worktree 内修复并补定向测试：

- **B1 安装期 doctor 校验错项目**：`install.sh` 调用 `cwk_doctor.py` 时未固定项目根，
  继承来的 `CWK_PROJECT_DIR` 若指向另一个合法检出，安装门就会给错误的对象放行。
  现改为显式传 `--project-dir "$PROJECT_DIR"`，并加了“继承变量指向别的检出时仍校验
  本检出”的回归测试。
- **B2 自检与运行时 dotenv 语义分歧**：`doctor` 曾接受 `export KEY=value`，而
  `cwk_nightly_pipeline.load_local_env` 不接受，会出现“自检说已配置、实跑读不到”。
  按最小改动原则移除 `doctor` 侧的 `export ` 兼容（不动 `cwk_nightly_pipeline.py`
  这个 PR-001 owner surface），并补三项测试：解析层拒绝、`export` 形式的 Key 报
  `missing` 且输出不含占位值、以及读源码文本（不导入、不执行）的解析器一致性守卫。
- **B3 私有文件创建不原子**：旧实现先 `: > "$target"` 再写模板，模板缺失或读失败会
  留下 0 字节 `.env`，而后续安装“已存在则保留”会把这个空壳一直保留下去。现改为
  `umask 077` 下同盘 `mktemp` 暂存、`chmod 600`、再用 `ln` 原子激活（EEXIST 时保留
  既有文件），失败与信号中断都清理暂存，不留残留。
- **B4 Workspace Skill 目录跨 uid 不可见**：`mktemp -d` 是 `0700`、`cp -R` 又受调用方
  umask 影响，原子改名后的 Skill 目录可能只有安装者能进，以别的账号运行的 Agent 发现
  不了。现在保留原子暂存的同时，把暂存树调成与仓库公开 `skill/` 一致的
  `a+rX,go-w` / 目录 `0755`，并对本次新建的 `skills/` 父目录同样处理；测试在
  `umask 077` 下断言最终 mode。
- **B5 RT 记录与实际状态不符**：即本节与上面的验证结果，只记录真实做过的验证，明确
  标注全量 CI 尚未运行。

同批修掉的合同 / 安全问题（评审列为非阻塞但影响正确性）：

- 双向强制“一个 Agent 一种模式”，`--force` 不再能静默造出混装状态，也不做破坏性
  自动切换；冲突时零写入、退出码 3。
- 路由块在写入前校验渲染结果恰好一对起止标记（`AGENTS_ROUTER_TEMPLATE_INVALID`）。
- 非 UTF-8 的 `AGENTS.md` 变成稳定的 `AGENTS_ROUTER_UNREADABLE` 失败，不再抛
  traceback，文件逐字节保留。
- 拒绝把文件系统根 `/` 和 CWK 项目根本身当作自助安装的 Workspace 边界。
- 暂存 / 临时产物在普通失败和 INT/TERM/HUP 下都清理。

### 治理决定

- `prompts/CWK_AGENTS_ROUTER.md`（新增）由现有归属规则 `R-docs-prompts` 覆盖
  （前缀 `prompts/`、owner RT-031、演化路径 `repo-standard-change`），`prompts/` 不是
  exact-only 区；`make governance-audit` 在含该文件时通过（591 个受跟踪文件）。因此
  **不新增也不修改任何共享治理清单**，只在本 RT 记录该判断。
- `install.sh`、`scripts/cwk_doctor.py`、`Makefile` 均未被 PR-001 的
  `legacy_frozen_files` 以 sha256 钉死，本次改动落在 RT-026 声明的 owner surface 内，
  相关定向测试（`test_pr001_script_evolution_guard`）全绿，未改写任何历史合同。
- 文档里的宿主 `openclaw skills install <path> --as <slug> --agent <id>` 与
  `openclaw skills info <name> --agent <id>` 已对照本机安装的 OpenClaw 2026.8.1
  `skills` CLI 文档核实存在，未使用未记载的参数，故保留原样、无需删改。

## 变更记录

- 2026-09-01：根据真实多 Agent sandbox 审计，将 RT-031 从“文档型最小上手”续做为
  “核心安装 + 四种 OpenClaw 接入模式”的完整安装合同；尚未修改业务代码。
- 2026-09-01：实现核心安装与四种接入模式、`doctor` 多根发现与安全 dotenv 读取、
  路由模板与四入口文档统一，并完成上述收口前评审整改。
- 2026-09-02：修复后独立复核确认 B1～B5 全部闭合；最终全量 `make ci` 在干净提交
  `dd91579` 上通过，RT-031 进入收口审阅。

## 遗留事项

- `make doctor` 目标仍未传 `--project-dir`；`Makefile` 在 CWK 项目根执行时行为正确，
  本次按“最小改动、不扩大 owner surface”未改。若之后要让 `make doctor` 也免疫继承来的
  `CWK_PROJECT_DIR`，单独提 RT。
- 评审提出的其余非阻塞项按约定未在本轮展开：自定义 `--agents-file` / `--skills-dir`
  只检查默认对侧路径，旧 `--install-skill` 兼容入口不参与新模式冲突检测，嵌套自定义
  Skill 目录的中间层权限，以及 Skill 复制包含未跟踪文件、历史规范文档和路径显示归一化。
  已评估为不影响四种新模式的默认主路径与本 RT 安全边界，留待需要时单独立项。
