# CWK 功能说明

> 基线：`main` @ `b490f4c`（2026-09-02） · 版本 `v0.2.0-ai-pilot`
> 面向对象：准备使用 CWK 的同事、维护者。安装步骤看《员工上手一页纸》与
> `docs/INTERNAL_DISTRIBUTION.md`；本文回答“它到底有哪些功能、每个功能怎么运作”。

## 0. 一句话定位

CWK 是一条**对 CWork/工作协同只读**的知识镜像工作流：把你每天在 CWork 的汇报、
待办、已办与回复链，自动整理成本地权威镜像、结构化数据、每日摘要（Markdown/HTML）
与可信问答，并可选择把**派生**内容发布到公司知识库。全程默认只读、可审计、
需要“人工授权 → 确认 → 二次确认”才能进入自动运行。

按当前生产画像（Local-First）运行：完整本地镜像就是权威数据源；原始证据永不发布。

## 1. 功能总览

| 层 | 能力 | 入口 |
| --- | --- | --- |
| A | 安装与 OpenClaw 接入（四种模式） | `install.sh` |
| B | 激活向导：双重授权 + 状态机 | `scripts/activation_wizard.py` |
| C | 每日只读采集与本地权威镜像 | `scripts/cwk_nightly_pipeline.py` |
| D | 规则版每日摘要与事项中心 | 同上（产物） |
| E | 增量链接预览 | 同上（产物） |
| F | 可选 AI 增强通道（默认关） | nightly + `.env` 开关 |
| G | 可信 Wiki 问答（引文可溯源） | `scripts/cwk_wiki_query.py` |
| H | 可选 DocDB 派生内容发布 | `--sync-docdb/--sync-wiki` |
| I | 自检与运维 | `scripts/cwk_doctor.py`、`docs/OPERATIONS.md` |
| J | 调度交接与宿主任务登记 | `activation_wizard.py` 子命令 |
| K | 质量门与治理（CI） | `make ci` / `make ci-lite` |

## 2. 逐项详细说明

### A. 安装器 `install.sh`

- **功能**：把 CWK 装到本机。只需 Python 3.10+（推荐 3.11），仅用标准库，
  无第三方包依赖。
- **行为**：创建本机私有模板（新建时 `0600`、已存在不覆盖）、编译脚本、
  跑脱敏 smoke。成功输出 `CWK_CORE_READY`。
- **零副作用保证**：不读取 CWork、不写 DocDB、不创建定时任务、不修改 Agent /
  Gateway / 宿主配置，也不需要 `openclaw` CLI。输出
  `CWK_ACTIVATION=NOT_STARTED`——安装不产生私有激活状态，夜间自动化默认关闭。
- **OpenClaw 接入是独立显式的一步，四选一**：
  - `workspace-skill`：装进可写的 Workspace（自助）；
  - `host-skill`：受保护 Skill 根，只打印注册路径与映射，交运维注册，本机不写文件；
  - `router`：通过路由提示词接入；
  - `none`：只要程序，不要 Agent 集成。
- **互斥**：一个 Agent 只能一种模式；检测到已有其它模式时报
  `OPENCLAW_INTEGRATION_CONFLICT` 并停止，`--force` 也不放行混装。
- 输出 `OPENCLAW_DISCOVERY=UNVERIFIED`：文件到位 ≠ Skill 已被运行时加载，
  需在 Agent 里实际确认。

### B. 激活向导（装好 ≠ 授权）

判定完全由 `scripts/activation_wizard.py` 独占（AI 只负责对话，不充当授权记录）。

- **状态机**：`INSTALLED → READY_FOR_DISCOVERY → PROFILE_PROPOSED →
  PROFILE_CONFIRMED → PILOT_PASSED → ACTIVE`；另有 `PAUSED / DEGRADED /
  NEEDS_RECONFIRMATION`。所有迁移写历史回执（时间、事件、授权类型、输入回执摘要）。
- **两次相互独立的人工确认**：
  1. `confirm-discovery`：授权只读发现范围（scope 走闭合 schema，先校验归一再哈希）；
  2. 很久之后、在看过一次真实只读试跑结果后，`confirm-activation` 单独确认是否排期。
- **中间步骤**：`record-discovery`（用既有只读回执生成发现报告）、
  `propose-profile` / `confirm-profile`（业务画像草案→认领）、
  `render-contract`（只读复述每日执行合同）、`record-pilot`（判定一次只读试跑，
  必须带采集回执，失败关闭）、`schedule-handoff`（产出调度交接单）、
  `record-schedule`（登记宿主已建任务标识）。
- **任何中途变化自动作废确认**：配置、`.env`、画像、试跑证据变化都会使旧确认失效，
  要求重走；`check-drift` 显式比对合同与外部任务 ID。
- **状态存储**：`state/activation/`（0700、CAS 原子写、schema 校验、错误脱敏，
  不存凭据/raw/绝对路径）。
- **子命令**：`status / init / confirm-discovery / record-discovery /
  propose-profile / confirm-profile / render-contract / record-pilot /
  confirm-activation / schedule-handoff / record-schedule / pause / resume /
  check-drift`。

### C. 每日只读采集与本地镜像（nightly pipeline）

- **采集**：对授权业务日执行完整 `search-list` 只读分页，把暂存报告提升进本地
  `raw/YYYY-MM/YYYY-MM-DD/`——这是本机权威真源。
- **完整性门**：`source IDs = raw IDs = summary IDs`；当前业务日完整分页；
  迟到数据按可配置回看窗口补采。
- **不变量**：raw 只留本地，永不回写、永不外发（发布仅限派生内容）。

### D. 规则版每日摘要与事项中心（默认产物）

每次运行产出：

- `digest-human-v4.md` / `.html`：人读的每日摘要与独立阅读页；
- `action-center.md/.html`、`action-cards.json`：待办/已办/持续事项与行动卡；
- `nightly-pipeline-manifest.json`、`run.json`：本次运行清单与状态（含
  `overall_pass`）；
- 关系视图：在关系接口未部署前显示“关系待确认”，CWK **不臆测**“与我无关”。

### E. 增量链接预览

- `incremental-link-preview-v1.md`：对比历史基线，提示疑似关联/变化，供人工复核，
  不自动改任何记录。

### F. AI 增强通道（可选，默认关闭；旁路不替换规则版）

- 逐篇结构化理解（带证据引文）→ 跨篇事件聚类与优先级 → `digest-ai-enhanced.md/html`
  → `quality-review.json/md`。
- **模型白名单（硬约束）**：只允许 `newapi/BD-MiniMax`（MiniMax M3）与
  `newapi/BD-glm`（GLM 5.2），其它模型 ID 启动即拒绝。角色矩阵见 `MODEL_ROLES.md`。
- **离线验证**：`CWK_AI_ENABLED=true CWK_AI_DRY_RUN=true make smoke-ai`
  不调用模型即可验证整条编排。
- **降级**：模型失败保留原规则产物并计数，3 次失败置
  `fallback_terminal_error`；历史质量债可用批量驱动补齐。
- **运行时策略**：真实调用走 `openclaw agent --json`（不 `--deliver`），专用
  Agent 为无工具、无 Skill、无沙箱的转换器；临时 prompt 文件调用后即删。

### G. 可信 Wiki 问答

`scripts/cwk_wiki_query.py` 是只读检索入口：

- 生产用 `--mode local`：对完整本地镜像的摘要/主题/实体排序，再**逐条引文对照
  raw 验证**；
- 返回证据包而非自由发挥的答案：Agent 只能引用 `evidence_status=verified` 条目、
  标注 `report_id` 与 raw 路径、明示冲突；`confidence=none` 时必须弃权；
- 支持时间范围与作者过滤（`--from-date/--to-date/--writer`）、`--format json`；
- 使用前跑完整性门：`python3 scripts/cwk_wiki_query.py --lint`；
- 历史 `cloud`/`shadow` 提供方是暂停的实验路径，需二次显式解锁，不在生产画像内。

### H. 可选 DocDB 派生发布

- `--sync-docdb` / `--sync-wiki`：把派生 Wiki、每日 Markdown/HTML 按版本同步到
  DocDB；**raw 永不发布**；
- 默认写个人知识库（自动发现），写团队/共享目录需显式填
  `docdb_project_id` / `docdb_root_file_id`。

### I. 自检与运维

- `make doctor` / `scripts/cwk_doctor.py --check-only`：检查 Python、脚本完整性、
  `.env`（只报 `configured/missing`，永不打印值）、镜像根、OpenClaw 接入与激活状态；
- 运维手册：`docs/OPERATIONS.md`；迁移：`docs/MIGRATION.md`；
  激活对话参考：`skill/references/activation.md`。

### J. 调度交接与宿主任务登记

- 仓库**从不创建/修改/删除任何定时任务**，也不假设存在 OpenClaw 调度 API；
- 它只产出 `scheduler-handoff.json`：节奏、本地运行时刻、时区、完整 argv、
  所需环境变量**名**、前置条件——不含凭据值、不含宿主绝对路径；
- 使用者在宿主侧用自己的机制建好任务后，把宿主分配的任务标识回填给
  `record-schedule`，进入 `ACTIVE`；
- `pause/resume` 只改变本仓库姿态；真要停宿主任务仍需去宿主侧处理（文档已写明）。

### K. 质量门与治理（维护者视角）

- `make doctor / test / aodw-check / governance-audit / ci`：完整门禁；
  `ci` = test + 方法层自检（AODW/RT 门禁/花名册）+ 代码归属审计（613 个受跟踪文件）；
- `make ci-lite`：轻量车道，跳过约 46 分钟的 PR-001 重安全模块，供纯文档/回执合并
  与日常迭代；**发布或产品代码改动仍必须跑完整 `make ci`**；
- 配置来源分层：config > shell > 项目根 `.env`（按上游 `setdefault` 语义），
  合同与哈希覆盖全部行为设置，改动即漂移、即作废确认。

## 3. 常用命令速查

```bash
PYTHON=python3.11 ./install.sh [--integration workspace-skill|host-skill|router|none] [--workspace ...]
make doctor                       # 自检
python3 scripts/activation_wizard.py status    # 激活状态与下一步
make ci-lite                      # 轻量门禁（迭代）
make ci                           # 完整门禁（发布前）
```

## 4. 安全边界（红线，代码级强制）

- 对 CWork 默认只读；未经单独授权禁止：标已读、回复、审批/拒绝、完成待办、
  删除记录、上传真实 raw 到共享仓库；
- 凭据只放本机 `.env`（gitignored、只解析不执行），doctor 与向导永不回显其值；
- 状态/回执/交接单不落凭据、不落 raw 正文、不落绝对路径；
- 发布与自动运行都以“合同 + 双重确认 + 通过试跑”为前提，任何配置变化作废旧授权。

## 5. 文档地图

| 文档 | 内容 |
| --- | --- |
| `README.md` | 项目总览与生产画像 |
| `docs/DESIGN.md` | 设计规格 |
| `docs/USER_GUIDE.md` | 用户指南 |
| `docs/OPERATIONS.md` | 运维手册 |
| `docs/MIGRATION.md` | 迁移说明 |
| `docs/INTERNAL_DISTRIBUTION.md` | 内部小团队安装/试用流程 |
| `docs/SANDBOX_ONBOARDING.md` | 云端 sandbox 自助上手 |
| `docs/AI-PILOT.md` | AI 运行时策略 |
| `docs/RUNTIME_STATUS.md` | 运行画像与暂停路径 |
| `MODEL_ROLES.md` | 模型角色矩阵 |
| `skill/references/activation.md` | 激活对话分状态参考 |
| `skill/references/operations.md` | Skill 运维参考 |
