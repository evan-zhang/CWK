# RT-Lite: RT-055 - 双通道 OpenSearch 与原版 WeKnora 检索决策实验

> profile: Spec-Lite | execution_mode: collaborative

## 方案（给人看）

- **做什么**：用一个全新、与 RT-054 隔离的三库 confidential holdout，同场裁决 A「OpenSearch 双通道」和 B「固定 commit 原版 WeKnora」。先在仓库完成候选身份、聚合结果 Schema、决策 harness、验收门、风险与清理合同；到建立临时索引/部署 WeKnora 前停在 OPS 授权门。
- **为什么**：RT-054 的 NO-GO 只说明当时 ICU v2 analyzer/mapping 没同时通过 cwork Recall 和 docdb exact 门，不代表 OpenSearch 架构失败。它同时证明当时存储投影使三库主索引缩小约 95%，但这不是完整 A 的已测成绩。下一步应把 exact 从中文分词排名中拿出来确定性处理，再以原版 WeKnora 作真正的替代路线对照，而不是继续调同一 holdout。
- **代价**：A 需要实现 exact resolver、SearchBackend、文档折叠和 Parent 展开；B 可能需要迁移控制面、摄取、授权、来源和 Gateway，但成本必须由固定原生栈实测，不能预设为事实。公平实验需要 OPS 临时算力和一次新的私有标注。
- **这次故意不做什么**：不复用或查看 RT-054 final holdout；不部署/修改 OPS、NAS、Gateway、生产配置、旧索引；不 push；不 fork/修改 WeKnora core；不把 rerank 混入 A 主候选；不实现生产 SearchBackend。
- **用户怎样算成功**：两个固定候选能接收同一份只留 OPS 的新三库 holdout，只返回逐库质量与资源聚合；任一库 Recall@10<0.90、exact<1、no-answer<1、leak>0 或公网 Gateway 硬门缺失都会可靠 NO-GO；通过后给出单一候选，不保留模糊双轨。
- **建议（推荐）**：选 A 进入实现，B 作为能推翻 A 的淘汰赛对照。A 复用 RT-054 已验证的 Parent/Child 存储机制，完整压缩/质量/资源必须在新实验复测，exact miss 用确定性 resolver 机制消除；只有 B 通过全部硬门且满足事前机械定义的替换效用才迁移。

## 假设与现状

- 基线：`f34918bb2158099f03eba99ffcc95e59714a15f2`；启动时工作树干净。
- 编号：核验 `RT/` 目录与 `RT/index.yaml` 并集最大号均为 RT-054，故下一个可用号是 RT-055；没有同目标 deferred item。
- SearchBackend 当前只是 RT-054 设计名词，尚无生产实现；Parent/Child 仅存在于 `kb_stage_b_poc.py` 和 benchmark runner。
- RT-054 最终机器结果作用域为 `lexical_analyzer_and_mapping_selection_only`；ICU Recall 为 0.88/0.92/0.96，exact 为 1.0/0.8/1.0，存储缩小 94.602%/95.997%/97.067%。
- WeKnora 固定 commit：`8d7298fb5d759973cb1e481cadc5ecdf16dca599`。仓库保存审计结论，未 vendoring 上游源码；真正实验须在 OPS checkout 后重新验证 HEAD。
- 详细代码事实与取舍见 `architecture-audit.md`；公平性、holdout、授权和清理见 `experiment-protocol.md`。

## 实现备注（用户不问可不展开）

- 本阶段新增 `scripts/kb_retrieval_decision.py`：只消费白名单聚合结果，验证两个候选身份、holdout 隔离、公平环境、逐库指标、公网能力、生产不变性和清理，再按冻结规则裁决。
- Schema 为 `contracts/aggregate-report.schema.json`；它不允许 query/expected/source/case/hit/locator 字段。
- 新脚本由 governance manifest exact 登记为 RT-055 所有；测试在 `tests/test_rt055_retrieval_decision.py`。
- 不能破坏：CWork/NAS 只读、raw 唯一事实源、服务端 KB 授权、查询与证据不出 OPS、WeKnora core 不改、RT-054 evidence 不改写。

## 验证

- **工程判据**：测试会构造坏行为而非断言文案——复用 RT-054、候选身份漂移、泄漏 query、逐库质量失败、清理失败都会拒绝；B 只有三项资源显著胜出且复杂度不高才推翻通过的 A。
- **破坏实验**：在测试对象中把 `rt054_final_holdout_reused` 改为 true、插入 query、把一库 exact 改为 0.99、令临时索引未清零，均预期红；还原后绿。
- **独立 AI 评审**：由父会话另行安排，题面应核验实验公平性、泄漏面、判据是否可被坏实现绕过、A/B 身份是否不对称、推荐是否超出证据。
- **读产出**：本阶段人工读取 RT-054 最终 JSON/结论、当前 gateway/lexical/Parent-Child 代码、本 RT Schema/protocol 和测试输出。真实三库搜索结果尚未生成，必须等 OPS 授权后由 OPS owner 私下读，仓库只收聚合。
- **已运行**：RT-054/055 相关回归 60 tests 通过、1 个真实 OpenSearch integration 因未提供 loopback URL 按合同结构化 SKIP；RT-055 自身 12 tests 全过；Draft 2020-12 Schema 自检通过；RT-055 guard 通过（仅既存 pre-commit hook 未安装告警）；AODW fixture/受管 RT/roster 全过（仅宿主 handover-pack 未安装告警）；governance 786 个受跟踪文件全有主；`git diff --check` 与提交范围检查通过，未包含 `docs/handover`。

## 变更记录

- 2026-09-10：建立 RT-055 决策实验；冻结两个候选、新 holdout 隔离、聚合输出、硬门和单一决策规则。完成仓库侧无需 OPS 写入的资产，停在 OPS 授权门。

## 遗留事项

- 真实候选运行与生产实现不是遗留转出：它们是本 RT 下一阶段，当前因明确 OPS/部署授权门尚未执行。

## 2026-09-10 本地开发续段

用户在当前频道确认恢复此前本地开发；继续不 push、不修改生产/现有索引、不写 NAS、不执行 OPS 部署。沿用已存在 worktree，基线 `4733632`，开始时干净。

- 已实现候选 A 的实验索引构建、通用 exact/ICU 双通道、doc collapse 和有界 Parent 展开。
- 已实现候选 B 的原生 manual ingestion/ready 检查/hybrid-search 适配；固定 upstream API 已只读核对。
- 已实现私有逐库评分；不把本地模拟变成真实质量、资源或 freeze 证据。
- 工程判据：新增合成 transport 行为测试，覆盖 exact 摄取/查询一致性、租户/库过滤、父段越界、部分响应/导入、错误不算 no-answer、超时、原生 Top-10、临时资源清理。最终 RT-055 局部回归 44 tests 全过（23 项候选 + 21 项裁决）；治理回归 62 tests 全过。两项真实行为破坏实验：断开 exact 摄取接线、把无答案异常计作答对，均导致对应测试失败；还原后通过。
- AI 评审：按 AGENTS 引用的判据纪律，安排只读独立评审，核验合同、公平性、泄漏面和坏行为绕过；独立评审发现原生零结果 null 被算错误（P1）、文件名吞入括号（P2）；父会话已修复，并分别通过 B 适配→评分、摄取→搜索行为测试核验。评审二次读取因 Native hook relay timeout 未完成，不冒充独立复审通过。
- 读产出：父会话读取真实的合成索引请求、搜索请求、Parent 展开与聚合计数，实际 loopback HTTP JSON/拒绝重定向路径，以及固定 upstream handler/types；真实服务结果尚未生成。
- 未完成：OPS 的新空库/认证与原生配置、服务部署、私有 holdout/冻结/独立 verifier、全栈资源与 Gateway 能力测量、日志保密验证、清理与生产不变性复核。这些仍属于本 RT，未转出也未关单。

### 当前检查状态

- `env LANG=C LC_ALL=C make aodw-check governance-audit` 通过：79 framework fixtures、全部受管 RT、53 项 roster 一致，788 个受跟踪文件全部有主。宿主 handover-pack 未安装仅为既有告警。
- 首次沿用宿主 `C.UTF-8` 时，macOS 自带 Bash 在既存方法脚本紧邻中文标点的变量处报 unbound variable；使用与隔离 CI 一致的 `LC_ALL=C` 重跑通过，未修改方法脚本/系统 locale。
- `git diff HEAD --check` 通过。
- 门禁脚本加固：`aodw-check.sh`/`rt-guard.sh` 紧邻中文标点的变量展开改为 `${var}` 括界，并按门禁要求刷新 manifest 中 `rt-guard.sh` 的 sha256 pin；此前一次全量尝试在受管 RT 门禁处失败，按失败提示的授权改法修复。
- 全量 `make ci-full` 最终验收通过（2026-09-11 回填；隔离环境、无项目 .env、独立合成 smoke run 名称；08:16:53–09:27:56，总用时约 71 分钟，退出码 0）：doctor PASS；py_compile 通过；unittest 3671 tests、skipped=12、OK（4245.087s）；smoke / smoke-ai / smoke-ai-degraded 产物门禁全过（模板 dry-run manifest 的 overall_pass=false 为内容层指标，CI 门禁校验产物存在）；aodw-check 通过（仅宿主 handover-pack 未安装既有告警）；governance-audit 通过（788 个受跟踪文件全有主）。日志：`/private/tmp/rt055-ci-final1/ci-full.log`。
