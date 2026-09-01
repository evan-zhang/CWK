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
- 对照成功标准的结果：待方案门通过并实现后填写；当前只完成方案和证据核对。

## 变更记录

- 2026-09-01：根据真实多 Agent sandbox 审计，将 RT-031 从“文档型最小上手”续做为
  “核心安装 + 四种 OpenClaw 接入模式”的完整安装合同；尚未修改业务代码。

## 遗留事项

- 暂无。实现中若发现 OpenClaw 版本差异或 PR-001 冻结 owner surface 需要独立治理，
  先作为本 RT 阻塞报告，不用改写历史合同或扩大范围来绕过。
