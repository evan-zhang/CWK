# RT-Lite: RT-055 - 双通道 OpenSearch 与原版 WeKnora 检索决策实验

> profile: Spec-Lite | execution_mode: collaborative
> 当前状态（2026-09-12）：Amendment 3 第四轮已按新增授权执行，在原生隐私门未建立时 INVALID 收口；三库均参与、无延期，未 freeze、未消费正式 holdout。清理及 after 完成，无遗留 OPS 后台任务；生产切流暂停，RT 选型目标仍未完成。以文末第四轮收口节为准，历史不改写。

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

## 2026-09-11 OPS 接管终态：INVALID

本节是最新状态，取代前文“尚未获得 OPS 授权”的历史停止点。Evan 已于 15:35 授权 §6，并于 18:46 要求接管执行。此次未重跑 builder，未将私有材料带离 OPS。

- 修正 verifier 错误的输入目录接线，原位读取 builder 文件；没有移动私有材料或修改校验算法。独立后台校验实跑退出 3。
- 六类覆盖通过，三库各 42 题；但 RT-054 **当前快照重建题池**交集为 4 / 16 / 15，排除及不相交证明均失败。此计数不冒充历史实际 holdout 的精确重叠数量。
- 按第一道失败门停止：未 freeze，未启动正式 A/B，holdout 未被候选消费。没有删题、重抽或反复运行取 PASS。唯一裁决为 **INVALID**，不是 A/B 质量 NO-GO；RT 不关闭。
- 接管时 A/B 冒烟已有运行成功回执，模型缓存已完整，无需重下载；B 无答案冒烟未答对。冒烟结果不作为正式质量/资源数据。
- 原聚合装配器因缺少 freeze 前提退出 1；OPS 与本地决策 harness 均返回 INVALID、退出 2。证据 JSON 使用 v2 闭集形状记录中止状态，未知值为 `null`，**不是 schema-valid 的完整 v2 结果**，不伪造 PASS。
- 清理脚本首次剩 1 个只读 Go 模块缓存，核实本轮归属后仅在该缓存内恢复所有者清理权限并删除；原失败及调和回执保留。最终未决清理失败 0，实验数据面和后台进程为 0；私有快照、题集、验证与审计材料仍留 OPS。freeze 从未生成，不存在遗失。
- 三 Gateway 的 8787/8788/8789 均 HTTP 200，进程/命令与基线一致；已覆盖的 NAS 元数据指纹一致。本次未修改生产、NAS、既有索引或配置。完整 NAS/现有索引/生产配置不变性与容器零残留缺独立证明，正式字段保留 `null`，不采信清理脚本推导的成功值。
- WeKnora 固定 HEAD 与 clean tree 已复核，core 未改。没有 push，没有将 `docs/handover/` 纳入提交。
- 工程验收：RT-055 合成回归 44 项通过；真实 verifier 和 harness 已证明失败关闭。主会话完整读脚本、白名单结果和清理回执，未安排新的独立 AI 复审，不冒充正式实验或独立评审通过。
- 未决：真实历史 pool 排除与三角色分离材料；freeze/模型/runbook 核验；Gateway 四能力实测；RSS/构建时间口径；正式日志/egress 证明；生产配置和现有索引完整基线。详见验收记录，未转出或关闭 RT。

证据：
- [中止轮 v2 字段记录](evidence/aggregate-report.v2.json)
- [本地唯一裁决](evidence/decision.json)
- [验收、清理和未决项](evidence/acceptance.md)

本轮已停止且无后台任务；后续需要新的明确指令，先解决上述缺口，再建立独立 holdout，不沿当前失败集合继续取 PASS。

## 2026-09-11 22:13 常设授权后的修复接管：权威历史池缺失

本节为最新状态。Evan 已授权围绕可信唯一裁决自主推进；不再停在逐步授权门，但按本次明确规则在**权威历史池数据缺失**时立即停止实验链。

- 接管基线 `1c4d609d88fea8fbbe9529693c2bdbe86002acf3`，工作树 clean。实查根因：builder 只随机排序全量来源，未在派题前排除历史来源项；verifier 用当前快照重建历史 query 池求交，不能替代历史实际记录。4 / 16 / 15 仍只表示该重建检查的交集。
- OPS 本地可读范围检索完成：88 个匹配位置、81 份结构化记录；5 个 RT-054 私有留存目录均只有字节码缓存。未找到权威历史题池、来源项清单或匹配归档。6 个权限拒绝位置未扩权检查；不宣称全机或离线备份绝无副本。历史最终报告与执行器均有删除私有题池和运行目录的证据。
- 旧候选题集及验证副本各 126 题，已整体归档作废，字节比对通过；原 builder/verifier 备份和查找审计留 OPS。未删题、重抽或改变候选算法。
- 在权威记录门停止：没有新 holdout、freeze、正式 A/B 或正式资源。OPS 实验/检索进程 0，三 Gateway 均 HTTP 200。第二阶段完整生产不变性基线未采集，不伪造其 PASS；未改生产/NAS/现有索引、alias 或配置。
- 唯一当前终态为 **INVALID**，无新逐库成绩，未选出 A/B，不能进入生产实现。旧中止轮 JSON 保持原样；未知值仍按原合同拒绝，不为满足 Schema 填充假数。
- 恢复只缺一个输入条件：在 OPS 0700 私有位置恢复可绑定到历史实际运行的 RT-054 题池/完整来源项清单和完整性证据。不需要再逐步授权，也不得用当前快照重建近似替代。
- RT-055 合成回归 44 项通过；OPS/本地 harness 均实际返回 INVALID、退出 2。Schema 自检通过，原中止报告按 false/null 拒绝；递归隐私、证据链接、AODW、791 文件治理审计及 diff 检查通过，仅既有宿主 skill 告警。实际根因、归档和检索证据由主会话核读，不冒充独立复审或正式实验通过。完整记录见 [验收补充](evidence/acceptance.md)。本 RT 不关闭，不 push，不包含 `docs/handover/`。

## 2026-09-11 22:53 父会话裁决：运行前修订排除权威 R

本节取代 22:13 的“等待恢复历史实际池”停止条件；前两轮 INVALID 原始记录保留，不删除或改写。起始 HEAD 为 `3b4c44419305aba1ca6478a32624b60cc129b1d3`，worktree clean。RT-054 私有池按合同销毁，是设计内不可恢复事实。

- 修订 [协议 §2.1](experiment-protocol.md#21-排除权威-r运行前修订evan-2026-09-11-2253-裁决)：用相同 RT-054 seed、sampling/split 版本确定性重建当前快照 R，明确其不是历史精确记录。已实读 acceptance 第 74 行的私有 workdir=0 与 quality JSON 第 112 行的 cleanup.workdirs_zero=true。
- 新增纯函数 R 与可版本化 builder/verifier、OPS 私有 I/O helper。三路径来源级排除先于锁序/派题；近邻同等排除、检索完整 corpus 保留；verifier 独立回读、重建 R、三类零交集和成员逐项核销。构建锁防止同轮覆盖重抽。
- 残余风险：内容跨 doc_id 迁移并同时改名仍可能漏检；约两天漂移窗口较小但不为零。旧集合 4/16/15 为当前重建命中，不冒充历史精确重叠。裁决接受该边界，以 R 为本轮权威。
- **工程判据**：14 项 synthetic-only 行为测试先红（10 failures）后绿；实际断开 builder 排除调用与绕过 verifier 拒绝分支，均使测试退出 1，恢复后绿；RT-055 回归 58 项通过。见 [红绿证据](evidence/r-revision-tests.json)。没有把私有题面放入本地测试。
- **AI 审查**：主会话逐项检查来源级排除、全库唯一性、近邻、账本核销及失败关闭；尚无新增独立 AI 复审，不把自检冒充独立审批。
- **读产出**：读取真实合成调用的排除账本/拒绝计数、两次破坏实验退出码，以及 RT-054 原始清理证据行；未读取 OPS 私有题面。OPS 接续实查实验进程 0、三个 Gateway health 均 200、无 freeze。
- 修订时新 holdout=0、正式 A/B=0；旧 126 题整体作废不动。先单独本地提交修订与测试，再同步 OPS 建一次新池；不 push，不含 docs/handover/ 或 runs/ 临时状态。授权内继续，仅协议硬冲突停止。

修订提交前检查：递归聚合隐私、18 条相对链接、diff、AODW 和 797 文件治理审计通过（仅既有宿主 handover-pack 告警）。隔离快车道先执行 2572 项测试；复制出的源码缺少 Git index，导致 3 failures/1 setUpClass error，均属于 Git 元数据检查。为该纯源码临时副本建立 Git index 后，只重跑受影响的 10 项，全部通过；随后三类 smoke 与 AODW/governance 均通过。没有把初次 `make ci` 的退出 2 写成退出 0，也未重复已通过的整套测试。


## 当前 R 修订轮：单次覆盖失败后的第二提交收口

- 首提交 `45a6080a01f4fb6f3ed1aeaa16e7f2d13dad59d4` 已在建池前固化 R 协议。当前随机 run UUID 为 `3bf93a6e-be8e-4c4d-835d-f17eb277f8c2`；本次接管未改 scripts/tests、抽样 seed 或候选实现。
- builder=1/PASS、verifier=1/FAIL exit 3；新池 **42 / 31 / 16**。六类顺序为标题/文件名、精确编号/日期、正文稀有短语、表格、无答案、近邻，目标 **10/8/10/4/5/5**，三库实际分别 **10/8/10/4/5/5；10/3/5/3/5/5；3/1/3/3/3/3**。后两库未达覆盖目标，唯一状态 **INVALID**，不是质量 NO-GO。
- R 三类交集各库全部 0；2158 个 R 成员逐库全核销、未核销 0；题目拒绝 0。R 重建、完整 source snapshot reread、资格过滤、材料完整性和隔离均通过，不抵销覆盖不足；理论历史迁移风险仍保留。只有 builder/verifier 独立过程材料，implementer 未执行，三角色完整证明 null。
- 不重建、不换 seed、不补/删题、不重跑 verifier。freeze 不存在，正式 A/B、holdout 消费及正式索引/服务/容器/数据面创建均 0，无第二次正式实验；三库正式成绩为 null。
- 同口径 after 和独立 NAS 全目录范围补证均已可靠结束。Gateway PID/命令、指定配置投影、4 个容器投影一致，8787/8788/8789 实时均 HTTP 200；docdb、spbp 的文件集合/元数据一致。**cwork 文件 1206→1248（+42），原有元数据变化 7，其中既有 index 变化 2；launchctl 标签 537→537 但集合增/减各 1，非已记录实验进程变化。真实漂移保留，不重置基线、不擅自归因或修复。**
- 完整 NAS 内容、index/alias、生产配置/进程全集与容器卷内容缺基线覆盖，均 null；不接受硬编码全测量或用 health 推导不变性。已测投影整体不变性 false，完整生产不变性未证明。
- 仅清理当前 UUID：6 个根执行器移入私有审计后删除入口，8 个字节码清除；37 份既有私有/审计材料复核不变。旧 126 题的 2 份整体归档仍与原件相同；新 89 题也保留并标 INVALID/禁止消费。精确 UUID 资源、未确认资源、非审计数据面、cleanup failures、相关进程最终均 0。
- 新增 [本轮中止证据](evidence/r-round-abort.json) 与 [闭集 Schema](evidence/r-round-abort.schema.json)，不冒充 A/B harness。旧 aggregate-report.v2.json / decision.json 与首提交字节不变，只代表第一历史中止轮。前两轮历史和首提交协议修订说明完整保留。
- **工程判据**：58 项 RT-055 合成回归全过；Schema/隐私/秘密/链接/AODW/governance/staged 范围及 diff 检查见 [QA](evidence/r-round-closeout-qa.json)。不把局部回归称为重新通过全量 CI。
- **AI 审查**：接管会话核对实际控制流及证据边界，待父会话独立复核第二提交；不自批。
- **读产出**：读取 OPS 白名单计数、集合/指纹比较布尔与清理回执，不读取或导出私有题面/标注/摘要。
- 第二提交只含 RT-055 最终证据、acceptance、rt-lite 和必要的中止格式终态；不含 scripts/tests、runs/、docs/handover/，不 push。提交 hash 随回执交父会话；worktree clean 才算本地交付。

详见 [本轮验收](evidence/acceptance.md#当前-r-修订轮失败收口2328-接管指令)。未决是已发现漂移的归属及未覆盖证明、被硬门挡住的正式阶段；它们不授予本轮重试资格。本轮收口完成、无后台任务；RT 选型目标未完成，不关闭。唯一下一步：父会话复核。

## 2026-09-12 Amendment 3：本地第一提交预承诺与收口

### 授权、前序与变更记录

- Evan 明确批准固定 T3→T2→T1 与逐库 DEFERRED；本次只接管现有未提交改动、修复验证并本地提交，不启动第四轮、不连接 OPS/NAS/生产、不 push、不改 WeKnora core。无 reset/checkout 丢改动；25 个继承文件先保留本地保护副本，再逐项审阅修复。
- 前序 `45a6080a01f4fb6f3ed1aeaa16e7f2d13dad59d4` → 基线 `c86519425e260a45a11a89917cb0c6600d46a11f` → 本 Amendment 3 第一提交。完整提交 hash 在提交后回执与 Git 中给出，不为自引用再造第二提交。
- 此前没有正式候选运行或可用于选型的 A/B 结果；历史冒烟与预冻结容量失败不是候选成绩。旧 INVALID、r-round-abort、旧 aggregate/decision 保持字节不变，不把历史 31/16 池重新评级或消费，不构成按成绩重跑取 PASS。
- 预承诺详见 [Amendment 3](experiment-protocol.md#amendment-3--固定容量-tier-与逐库延期2026-09-12运行前)：同一次构建、同一 seed，各 tier 独立完整派题，保留首个达 floor 的完整池；六类 floor 3/2/3/3/3/3、总数门 16，六类同时达标实际至少 17。T1 不足 DEFERRED，全延期 INVALID。至少一库参与，延期不进入 A/B/效用，所有生产切流暂停。
- 合同升级 v3，拒绝静默迁移 v2；新增 Schema 状态/分区约束、独立 verifier 选择重放、严格角色与指标闭集。修复继承测试的可变全局 floor 别名污染、baseline 导入即执行/失败退出码、空库 max([])、freeze 空清单绕过、角色权限仅信账面的问题。保留全部新 runner，并修复 A 数据面计量与 B 辅助函数未定义变量。

### 验证三格与证据边界

- **工程判据**：继承 72 项通过不作修订已完成证据；新增 24 项先红，纠正两个容量 fixture 后仍有 11 个真实行为失败，修复后完整 RT-055 103 项通过、0 skip。四项真实代码破坏实验分别破坏首个有效 tier 选择、verifier 选择核验、延期指标拒绝、角色枚举拒绝，均使对应测试失败；源树不动，在隔离副本中破坏，回到原树再跑 103 项全绿。[结构化证据](evidence/amendment3-local-tests.json) / [白名单 Schema](evidence/amendment3-local-tests.schema.json)。
- **AI 审查**：本接管会话完整审阅继承 diff、新 runner、协议及旧证据，检查坏实现能否穿过判据；没有另派独立 AI 评审，不冒充独立审批。新增回归确认三种库身份均能成为唯一参与者，参与库原有质量门和双通过效用不变。
- **读产出**：实际读取本地合成 builder/verifier 的 31@T3、16→T2/T1→DEFERRED、T2/T1 放宽后的完整题池/排除核销结果与子进程审计；公开仅保留计数、枚举和通过布尔。真实 OPS 原文、题面、标识、路径和私有 digest 没有读取或导出。
- 本地最终 Schema/编译/隐私/链接/AODW/governance/暂存范围门禁见 [本次验收](evidence/acceptance.md#amendment-3-本地第一提交2026-09-12)。完整 RT-055 回归不冒充全仓 `make ci-full`；当前不执行任何原生服务集成，不把未执行登记成 skip/PASS。

### 未决实施风险（不启动 OPS）

- OPS 三角色 0700/独立进程实测、原生正常及错误日志/egress 门、二进制与固定源码构建绑定、完整生产资源/索引/配置覆盖和精确清理仍需后续授权验证；本地编译不替代这些证明。
- 同 UID 审计是 Python 进程级拒读，不是 OS 用户隔离或任意 syscall 沙箱。现有 baseline 未测完整不变性则输出 null，不能据此进入生产。
- R 是当前快照重建而非历史原池；低 tier 放宽 query/token 后仍有历史语义迁移残余风险。旧 cwork/服务标签漂移未归因、未修复、未重置 baseline。
- 本次交付只保留当前分支的一个本地提交；不关闭 RT，不合并、不 push、不清理 worktree，无 OPS 后台任务由本次启动。

## 2026-09-12 Amendment 3 第四轮：隐私硬门失败收口

**INVALID；不是质量 NO-GO，不选择 A/B。** 00:50 新授权允许第四轮全闭环。预承诺 `2cc386394610bd22f8a833d80304de95651d552e` 后，先合成复现 before 执行器失败（1 test / 1 failure），只修 after 比较缩进，以 `db0d6b66efaeea6b1e67bd33cd26e7d9af58b09c` 提交再同步 OPS。未改采样、tier、质量、效用或 core。

### 实际完成与硬门

- 第三轮 8 份私有材料整体封存，最终原件/封存件同字节、正式消费 0。本轮 builder/verifier 各一次；恢复先查原进程和 claim，没有重启、reseed、重复构建或拼池。
- 参与：cwork-3m 42@T3、docdb-touqian 31@T3、spbp-2027 42@T2；延期=[]。SPBP 的 16@T3 因精确类为 1 正确失败，同 build/seed 独立完整派出 T2，不补题。verifier 全过、拒绝 0；各库 R 成员 2158 全核销、未核销 0。
- identifier/query 交集均 0，两个 T3 库 token 交集 0，SPBP T2 token 交集 114 如实保留。三角色 3 个 PID、1 个 UID、独立 0700 工作区，3 次拒读探针通过、禁止读取 0；仅证明 PROCESS_LEVEL_SEPARATION_SINGLE_UID。R 历史语义迁移风险仍在。
- 网络实测外连被拒、loopback 可连且 IPv4/IPv6 可 bind；但 A 原生启动出现 transport bind 权限错误，300 秒就绪等待后失败。B 正常 native canary、鉴权后错误 canary、Langfuse/OTEL 和模型调用的运行态证明未完成。11 份日志零检出不能证明未运行路径安全；不改 core、不放宽网络、不重跑取 PASS。
- 未 freeze，正式 A/B 各 0、holdout 消费 0。三库两候选质量与资源指标全为 NOT_RUN_PRIVACY_GATE_INVALID + null，不是 DEFERRED 或零分。既有本地 decision CLI 对 abort 实际退出 2、输出 INVALID；没有完成正式质量/效用计算。

### 清理、after 与保留限制

精确清理后本轮相关进程、数据/工具缓存、容器/卷/网络/镜像标签/服务均为 0，失败 0；96 份私有/审计文件字节未变，41 个公开实现文件再次核验一致。before/after 各一次，三库元数据文件数 1248/316/317 同值，受测索引和配置指纹同值，三个 Gateway 最终复查均 200、ok、read_only。

服务清单 539→540（新增 4、减少 3）未归因，services_unchanged=false，按合同 production_config_unchanged=false；没有重建 baseline 或修生产。完整文件字节、全部依赖/卷内容/端点登记未覆盖，NAS 和完整索引不变性仍为 null，all_items_measured=false。局部同值和 health 不证明生产全量未变。

### 验证与交付

- **判据**：已完成 105 tests、0 skip，四项行为破坏均被检测，恢复后全绿；本次实现未改，保留这些实测证据，不重复已完成实验。新证据另做 Schema、轨迹重算、本地 CLI、7 个拒绝反例、递归隐私/秘密、历史字节保持、链接、AODW/governance 与精确暂存检查，不冒称全仓 CI。
- **AI 自检**：主会话审阅真实运行及证明覆盖，识别硬编码成功值、未鉴权探针和未覆盖项并 fail closed；没有独立 AI 评审或批准。
- **读产出**：核对 OPS 公开 allowlist 的真实容量/角色、隐私失败、after/清理与本地 INVALID；私有输入、原始日志留 OPS。

详见 [第四轮验收](evidence/acceptance.md#amendment-3-第四轮-ops-收口)、[中止证据](evidence/amendment3-ops4-abort.json)、[闭集 Schema](evidence/amendment3-ops4-abort.schema.json)、[CLI 输出](evidence/amendment3-ops4-decision.json)、[QA](evidence/amendment3-ops4-qa.json)。8 份历史 JSON/Schema 不变；证据提交独立于必要修复，hash 和 clean 状态随 Git 回执交付。

本轮失败收口完成，无遗留 OPS 后台任务。不合并、不 push、不清理 worktree，RT 保持 in_progress、全部切流暂停。后续先复核原生隐私证明、服务漂移和完整不变性范围；任何再试须新授权及新协议轮，不能续消费本轮题池。


## 2026-09-12 同一第四轮 pre-freeze 恢复：READY_TO_FREEZE

后续父会话明确授权只修同一 run 的执行器隐私门，覆盖上节“另建协议轮”的停止条件；原中止证据保留。**当前止于 READY_TO_FREEZE，不是选出 A/B，也没有正式成绩。**

- 修复 macOS/JVM socket 接线：IPv4 loopback + A 仅入站，主动 JDK 外连拒绝；把 OpenSearch 临时目录限制在私有工作区。仅加 IPv4 会使旧策略放行 JVM 外连，已实测排除该不安全方案。
- 恢复已删除的公开 embedding 依赖和固定 gojieba v1.4.7 字典，五份字典经 upstream go.sum 校验，使用官方环境配置，未改 WeKnora core；改进失败进程清理和真实环境观察。新恢复回执绑定源与观测，旧失败/网络/baseline 证据不覆盖。
- 三个合成执行失败逐一保留；第四个合成目录真实 A/B 正常路径各成功一次，鉴权后两类错误均 400，模型调用 3/3，13 份日志 canary 0、trace header 0、112 个进程 socket 样本外连 0，三类 loopback listener/环境都得到实测。
- 96 份原私有/审计文件同字节；builder/verifier=1/1、禁止读取=0，freeze/正式 A/B/consumption 全 0。合成数据/服务清空、失败 0；公开依赖空闲保留待冻结，精确 UUID 其他资源 0，三 Gateway 新鲜 200。旧生产漂移和覆盖 UNKNOWN 不变，不重做 baseline，不关闭 RT，切流继续暂停。
- **工程判据**：初始 9 项红，最终 RT-055 123 项全绿/0 skip；8 个行为破坏均检出并恢复为绿，10 个公开合同反例拒绝。只称 RT-055 与定向隐私回归，不冒称全仓 CI。
- **AI 自检**：核查真实启动链、原生支持配置、仅改 flag 的外连风险及回执不可覆盖；没有独立 AI 审批。
- **读产出**：复核 OPS 白名单运行证明、依赖校验及字节保持；没有读取/导出私有题面或 digest。

证据与细节见 [本次验收](evidence/acceptance.md#第四轮同一-run-的-pre-freeze-执行器恢复2026-09-12)、[公开回执](evidence/amendment3-ops4-recovery.json)、[闭集 Schema](evidence/amendment3-ops4-recovery.schema.json)。只本地提交、保留分支；不 push、不合并、不自动执行下一阶段，无后台任务。

## 2026-09-12 正式接续：新基线绑定冲突

02:58 获准继续同一 run，未重建题池、重复 builder/verifier 或改变 tier/seed/规则。隐私恢复仍 PASS；但 09e 的 freeze 创建和复核只接受旧 before，而旧 before/after claim 不可覆盖。新增 formal-before 不能被原入口引用，改 freeze 源则使已绑定的恢复隐私门拒绝。

本次严格保持指定源码与既有回执，停在 freeze 前，按新硬门收口；不把这一执行器问题包装成质量 NO-GO。原 READY_TO_FREEZE 是已完成隐私恢复的历史状态，不等于新正式窗口已可冻结。后续可修复，但必须先解决窗口与来源绑定的迁移，不能覆盖旧基线或绕过源核验。细节见 [本次验收](evidence/acceptance.md#第四轮正式接续新基线绑定硬门2026-09-12)。

**最终 INVALID / FRESH_FORMAL_BASELINE_BINDING_CONFLICT，中止收口完成。** 新 formal-before/after 各一次并 PASS，未覆盖旧窗口；三库文件 1248/316/317、目录 616/166/171。新窗口服务集合新增 1/减少 1，services/config 强口径 false；NAS/index 强口径 UNKNOWN，完整性 false，不能切流。Gateway/容器/卷元数据投影相同，三个 Gateway 200。

freeze/正式 A/B/消费仍 0，三库两候选所有正式指标 null；builder/verifier 1/1；96 份保留材料和 22 份公开源字节一致。候选资源/未确认创建/清理失败 0，OPS 控制器与 watcher 已退出。隐私恢复仍 PASS，角色仍为单 UID 进程隔离，未改 core 或规则。

证据：[公开中止报告](evidence/amendment3-ops4-formal-abort.json)、[闭集 Schema](evidence/amendment3-ops4-formal-abort.schema.json)、[原 CLI 拒绝回执](evidence/amendment3-ops4-formal-decision.json)、[公开 QA](evidence/amendment3-ops4-formal-qa.json)。123 回归/0 skip、4 合成接线检查、14 Schema 反例通过；只本地提交，不 push/合并/清理 worktree。RT-055 选型仍未完成，无后台任务，不自动启动下一轮。


## 2026-09-12 唯一继任接管：消费 claim 后评分前中止

**当前 INVALID，任务 BLOCKED_PROTOCOL_SINGLE_USE_NO_REPLAY；没有选出 A/B。** 本节取代上文“未 freeze/未消费”的当前状态判断，历史原件保留。

- `1af1362b57e639de87a35a9e5c2ce1af7f296da1` 已完成 formal-window 迁移。OPS 的真实版本化隐私重绑与 freeze 重新核验通过；随机顺序 A→B，before/after、freeze/verification 各一次。builder/verifier 仍 1/1，角色仍 PROCESS_LEVEL_SEPARATION_SINGLE_UID。
- 上一会话已领取 A/cwork-3m 消费 claim（1），但逐库 runner 调用三库全覆盖 scorer，在第一次 candidate.search 前拒绝。正式查询 0、评分/完整库结果 0，B 未启动。三库 A/B 正式指标均 null，DEFERRED=[]；42@T3、31@T3、42@T2 只是保留题池数，不是成绩。
- 继任会话先只读对账，本地与 OPS 确认旧控制器/候选进程均 0；96 份原材料、499 份归档、19 份绑定源核验通过。再追加接管 checkpoint，未更改任何 claim 或冻结实现。
- 原接口实测拒绝 consumption_without_completion_no_replay。现行单次消费规则不允许用“查询尚未发出”注销 claim；freeze 仍有效，也不符合仅因快照绑定失效而全新重建的条件。继续必须有明确的消费/冻结恢复协议裁决，不能靠常规继续、换窗口或重建池绕过。本次没有修改评分源码或实验规则。
- 唯一 [裁决器结果](evidence/formal-window-scorer-decision.json) 为 INVALID / AGGREGATE_CONTRACT_INVALID / exit 2；这只是对不完整 [中止证据](evidence/formal-window-scorer-abort.json) 的正确拒绝，不是完整 aggregate v3 或质量 NO-GO。公开证据先经 OPS [闭集 Schema](evidence/formal-window-scorer-abort.schema.json) 验证，当前阻塞与历史 WAITING 状态的区别见 [本次验收](evidence/acceptance.md#2026-09-12-唯一继任接管冻结后评分接线冲突)。
- 资源已精确清理：实验进程/数据面/UUID 容器卷网络镜像服务/未完成下载/cleanup failure 均 0，两份新临时 TLS 文件已删除；旧公开依赖、私有池/claim/freeze/审计保留。三 Gateway 200。NAS/index 强口径仍 null，services/config=false；同窗服务 +1/-1，不归因、不修改生产追平。
- 最终树 RT-055 **142 tests、0 skip**，14 个 Schema 反例拒绝，三库真实 scorer 公开探针查询前拒绝。回归虽绿仍漏过组合缺陷，不能据此宣称正式实验成功；不冒称全仓 CI 或独立外部审批。见 [QA](evidence/formal-window-scorer-qa.json)。

中止收口完成，选型目标未完成，RT 继续 in_progress、所有切流暂停。仅本地提交证据，不 push/合并/清理 worktree；无后台实验，不重复 builder/verifier，不换 freeze、不重放 holdout。


### 接管后并发写入警报：本节收口材料尚未提交

在准备最终工程检查时，05:05–05:08 出现另一写入者对六份 scripts 和新 zero_exposure 测试的修改；来源尚未确认，本会话未写这些源码。前述“142通过/0skip”仅针对并发改动前的树，**不是当前最终树回归结果**；“源码未变”仅指本会话，不能用于声明当前工作树干净。本次证据、QA 与文档均为未提交快照，没有最终提交，不构成已完成交付。已停止代码写入、实验和提交，双方改动均保留；未 reset/stash/clean。OPS 最后只读核验仍为原有效 freeze/隐私绑定、A claim1/B0、评分0、实验进程0，尚无 zero-exposure 迁移部署。当前阻塞同时包含独占执行权失效及单次消费协议；需要先排除并发写入者，再作明确恢复协议裁决。本会话无后台任务。


## 2026-09-12 Amendment 4：严格零暴露执行器恢复

当前授权为修复同一第四轮到 **READY_TO_RUN**，不得执行正式 A/B。历史 BLOCKED/并发警报不改写，前一会话迟到材料已单独保留为 `cb9d2d8`；本次是明确的新消费/冻结恢复裁决，不是常规继续推翻单次消费。

范围与判据以 [Amendment 4](experiment-protocol.md#amendment-4--zero-exposure-prequery-void-与-replacement-freeze2026-09-12-0501-授权) 为准：保留旧 claim，严格零 query/score/result 与冻结源码/隐私重算一致才追加私有 void；单库 scorer 与全库旧语义并存；A/B 共用 arm→exposure→score→complete；有 exposure 即全 window 禁止重放。builder/verifier、96 材料和 42@T3/31@T3/42@T2 不变；不改候选算法/质量/效用，不重建池、不动生产/NAS。

本地红绿、行为破坏与治理见 [合成证据](evidence/zero-exposure-scorer-tests.json) 和 [验收](evidence/acceptance.md)。OPS 尚需独立新隐私实测、cleanup、新 before 与 replacement freeze；未取得最终恢复回执之前，不把本地通过说成 READY_TO_RUN。RT 继续 in_progress，选型未完成，所有切流暂停。


## 2026-09-12 Amendment 4 OPS 终态：READY_TO_RUN

本轮零暴露恢复完成，取代上节“尚需 OPS 实测”的当前状态；历史原件不覆盖。旧 A/cwork-3m claim=1 保留，只追加严格私有 void；旧窗口永久 INVALID 并追加 superseded。96 份材料与同一 42@T3/31@T3/42@T2 题池未变，builder/verifier=1/1。

新源码隐私实测与清理通过；新 executioner migration 为 `a82157ee-324b-4806-ae01-3a4675b1f8d4`。新 before、replacement freeze 与独立核验通过，新窗口 `0155202c-b6a0-40c6-a779-48aff0ab57fe`、随机顺序 B→A。全局 exposure=0，新 arm/attempt/正式 query/result/after claim 全为 0。三个 Gateway 200，合成进程/数据面/清理失败/活跃控制器均 0。

工程判据为最终 159 项 RT-055 回归（0 skip）、14 项既有行为破坏加 1 项独立缓存越界破坏，以及新增的合法双窗重放/原子竞争测试；AI 自检核对恢复授权和停止边界；读产出为 OPS 独立重算后的白名单终态。详细三格和限制见 [最终验收](evidence/acceptance.md#amendment-4-ops-收口ready_to_run2026-09-12)、[公开回执](evidence/zero-exposure-ready.json)、[Schema](evidence/zero-exposure-ready.schema.json)、[QA](evidence/zero-exposure-ready-qa.json)。

停止 READY_TO_RUN，不执行正式 A/B；评测和选型仍未完成，完整 NAS/index 不变性 UNKNOWN 与历史漂移不抵销，RT 保持 in_progress、切流暂停。只本地提交，不 push/合并/清理 worktree；没有后台任务。

## Replacement 正式执行终态：INVALID（2026-09-12）

新授权从 `c5901f4` 的 READY_TO_RUN 继续，旧记录不改写。只读硬门通过后，使用冻结源码 `a4b3c64` 和同一 replacement window `0155202c-b6a0-40c6-a779-48aff0ab57fe`，按 **B→A** 启动 B。B attempt=1，但在创建首个服务进程前被主运行网络策略匹配门拒绝：loopback/inbound-only 策略均未包含冻结执行器要求的新增账本保护。arm/exposure/query/score/complete/result 全 0，A attempt=0；不是模型或 SSH 传输重试场景。

本次没有修改冻结 scripts/config，没有重做 builder/verifier、privacy、before、freeze，也没有重试 B、启动 A 或创建新窗口。历史 synthetic privacy PASS 和 freeze/source 绑定仍有效，但不能证明主正式策略一致；正式策略门为 false。

同窗口 cleanup→formal-after 已完成，三库 before/after 绑定与比较重算通过。临时进程/数据面/文件、当前 UUID 资源、未知归属资源和 cleanup failures 均 0，三 Gateway 实时 200。Gateway 未变；NAS/既有索引完整不变性仍 null；服务列表变更使 production_config_unchanged=false，未修生产、未重建 baseline。96 份材料及旧 claim/void/window/freeze 保持不变，builder/verifier 仍 1/1。

唯一裁决 **INVALID**，OPS 与本地 decision CLI 一致；没有 schema-valid v3 正式成绩，用闭合公开 abort Schema 明确表达缺测。三库仍 42@T3、31@T3、42@T2，`deferred=[]`；逐库 A/B 全部正式指标为 null，候选四项 Gateway 能力未测。机械复杂度 A=1/6/4、B=2/7/3 来自冻结 runbook，不是性能分数。

本次交付是失败后的完整收口，不是评测成功。RT 保持 in_progress、禁止切流，本窗口 after 已关闭重放。159 项 RT-055 回归零失败/零跳过、36 文件编译、Schema/decision、隐私/secret、链接、历史保留和 AODW/governance 验证见 [QA](evidence/replacement-formal-qa.json)；完整事实与限制见 [验收](evidence/acceptance.md#replacement-正式执行收口启动前策略硬门-invalid2026-09-12)、[公开 abort](evidence/replacement-formal-abort.json)、[Schema](evidence/replacement-formal-abort.schema.json)、[唯一裁决](evidence/replacement-formal-decision.json)。只本地提交，不 push/合并/删除 worktree；后续修主运行策略一致性需要新的明确授权。

## 2026-09-12 Amendment 5：formal runtime policy readiness 修复

06:23 用户授权只恢复 READY_TO_RUN，禁止正式 A/B。旧 replacement window after 已闭合，永久 INVALID，B attempt1、零 Popen/arm/exposure/query/score/result。主策略缺四类账本保护，旧 freeze 只绑定 synthetic 隐私来源而未绑定主策略物化；本节不从 mtime 推断生成时间。

按 [Amendment 5](experiment-protocol.md#amendment-5--formal-runtime-policy-readiness2026-09-12-0623-授权) 增加独占版本化主 readiness，冻结其文件/来源/保护范围并在 spawn 重算。新 before 严格晚于 readiness，source migration 重跑公开真实隐私门，新窗口随机顺序；候选算法、题池、质量门、生产均不改。旧策略/回执只追加归档，历史原件不动。红绿、行为破坏与验收见 [本地验收](evidence/acceptance.md#amendment-5-本地修复主-run-runtime-policy-readiness)。第一提交不表示 OPS 已就绪；实际 READY 回执另行追加。


## 2026-09-12 Amendment 5 OPS 终态：READY_TO_RUN

[公开 READY 与独立核验](evidence/runtime-policy-ready.json) / [Schema](evidence/runtime-policy-ready.schema.json) / [QA](evidence/runtime-policy-ready-qa.json)。执行器源码 `58951f92656e7a29b334461c7aa6d1148fec4e8f`；新 migration `e5a11f4d-445c-4c05-90b1-2c7e1fbeb0b0`；新 window `835c5188-0f29-4f41-8fe6-119b61917e2d`，新 freeze 随机 **A→B**。

主 run runtime readiness 与新源码实际 synthetic 隐私门均 PASS；主策略准备严格早于 before，freeze 纳入文件/receipt/source/network/八类目录保护，独立 spawn precheck 两种策略 PASS 而未 Popen。旧策略/回执原件、旧 INVALID window/attempt/claim/void/freeze 及96材料不变；归档1222工件，builder/verifier1/1，三库42@T3/31@T3/42@T2。

全局 exposure/正式 query/result0，新 window attempt/arm0；旧 replacement B attempt1 但零进程与暴露。Gateway3×200，合成/候选/控制器进程0，临时数据面/新临时文件/新TLS/UUID资源0，合成依赖已清理。停止 READY_TO_RUN，不运行 A/B、不 push、不合并、不关闭 RT；完整 NAS/index 不变性仍 UNKNOWN，历史 abort 和服务漂移保留。具体根因、计数及仅观察未重启的 watcher 状态文件时序竞争见 [收口验收](evidence/acceptance.md#amendment-5-ops-收口ready_to_run2026-09-12)。


## Amendment 5 正式执行终态：INVALID（2026-09-12）

本节取代上一节 READY_TO_RUN 的**当前状态**，历史准备证据保持原样。使用冻结源 `58951f9`、migration `e5a11f4d-445c-4c05-90b1-2c7e1fbeb0b0` 和同一 window `835c5188-0f29-4f41-8fe6-119b61917e2d`，先只读硬门重算通过，再按冻结 **A→B** 启动正式 coordinator 一次。

A attempt1，已通过 spawn precheck并创建1个进程记录，但 OpenSearch 服务启动报 RuntimeError；B未启动。arm/exposure/正式query/score/完整库/result全0，没有重试或修改冻结源码/配置。只读日志分类发现受保护日志目的地直接错误36条，但唯一根因未证实；原始日志和私有材料不出 OPS，未重新打开 holdout 诊断。主 runtime/socket readiness PASS 并不证明完整候选服务可启动。

确认失败后，仅另调用冻结 INVALID-only 收尾分支一次，完成同窗口 **cleanup→after→abort→decision**。控制器审计共2次（正式1、只收尾1），不隐去第二次，也不把它当作候选重跑。after三库PASS、claim1，本窗口已关闭重放。候选/控制器进程、临时数据面/新tmp/TLS/UUID资源、cleanup failures终态全0，新增进程审计保留，Gateway3×200。

三库仍42@T3、31@T3、42@T2，builder/verifier1/1、deferred=[]。每库A/B的20项正式指标全null，Gateway四能力未测；机械复杂度A=1/6/4、B=2/7/3只是冻结runbook计数。唯一 **INVALID** 为对不完整abort的fail-closed裁决（OPS/本地逐字段相同），不是质量NO-GO或有效v3成绩。NAS/index完整不变性仍null；同窗services/config=false，历史漂移保留；96材料、1222归档及旧claim/void/window/freeze不变。

[完整验收与三格核验](evidence/acceptance.md#amendment-5-正式执行收口a-启动失败后-invalid2026-09-12)、[公开abort](evidence/amendment5-formal-abort.json)、[Schema](evidence/amendment5-formal-abort.schema.json)、[唯一裁决](evidence/amendment5-formal-decision.json)、[QA](evidence/amendment5-formal-qa.json)。174回归与128编译针对未变源码通过；真实启动缺口不能被回归全绿掩盖。

本轮中止收口完成，RT保持in_progress，选型未完成、切流禁止。仅本地提交公开证据和上述文档，不push/合并/清worktree。没有后台实验；任何修复或再跑均需新授权，不自动重建池、before、freeze或窗口。

## Amendment 6 — 恢复运行目录隔离，目标为新 READY_TO_RUN

2026-09-12 用户授权继续修复 A 启动失败，但旧窗口永久关闭，不重试、不正式查询。
冻结 runner 将日志放进 sandbox 禁写的正式账本；公开 syscall 已复现。改为 A/B 统一的
window/candidate/attempt 独占运行租约，账本仍不可读写；补最终日志扫描和精确清理。
主 root 必须真实完成公开 A/B 三库 startup 后才允许新 before/freeze。

见 [Amendment 6](experiment-protocol.md#amendment-6--candidate-runtime-workspace-recovery2026-09-12)
及 [本地验收](evidence/acceptance.md#amendment-6--runtime-workspace-修复验收本地阶段)。
当前为执行器开发验收，不代表新 OPS READY。旧执行曾打开私有 runner 输入，不能称为未发生；
global exposure/query/result0 是新窗口授权规则的依据，不得改题池或重复 builder/verifier。


## 最新断点 — Amendment 6 interrupted READY 已独立收口

本节取代前文“OPS尚未READY”的当前判断，历史原件不覆盖。部署/冻结源码 `ad7d6b6e282ed8a674f25924830c4c5df71137ed`；
window `c66757c6-93f6-481d-aa92-be7de83b9aa1`，migration `11b62e31-ddbe-469e-baca-8f4553be0299`，真实冻结顺序 **A→B**。
source-bound privacy、main policy、完整A3/B3/sidecar3公开startup、before/freeze和无Popen独立重算PASS。
唯一中断是验证器把全局旧claim1当成新window计数，已只修ignored verifier并重新只读验证；
原FAILED/finalizer.claim留存，没有重跑实验动作、控制器或正式A/B。

新attempt/arm/exposure/query/score/result/after0；global exposure/score/result0。旧window永久INVALID，
A1/B0/after及全局claim1/void1原样保留；builder/verifier1/1。96材料、1222前归档、2570本次归档
清单/字节一致；5个失败synthetic链完整且每个cleanup0。Gateway3×200，无活跃候选/controller/runtime。
[公开READY](evidence/candidate-workspace-ready.json) / [QA](evidence/candidate-workspace-ready-qa.json) /
[三格验收](evidence/acceptance.md#amendment-6-interrupted-ready-最终验收2026-09-12)。

停止 **READY_TO_RUN**；评测与选型尚未完成，RT仍in_progress、切流暂停、NAS/index强不变性UNKNOWN。
只本地提交公开证据/docs/必要tests，不push、不合并、不清worktree。下一步必须另获正式A/B授权。


## 最新终态 — Amendment 6 正式执行 INVALID（2026-09-12）

本节取代READY当前断点，历史保持。启动前exact READY只读复核PASS；冻结源 `ad7d6b6e282ed8a674f25924830c4c5df71137ed`，migration `11b62e31-ddbe-469e-baca-8f4553be0299`，同window `c66757c6-93f6-481d-aa92-be7de83b9aa1`，真实顺序 **A→B**。

正式coordinator1次，A attempt1/arm1，scorer输入校验报CandidateError；exposure/query/score/complete/result全0，B未启动。具体输入子类型未证明，不猜测、不重新读holdout诊断、不重试、不改冻结scripts/core/题池。只收尾coordinator另1次，共2份控制器审计，按精确自有TLS清理→冻结cleanup→after→abort→OPS decision闭合。

唯一 **INVALID**，OPS/本地逐字段一致；不是A/B质量NO-GO或有效v3 aggregate。三库仍42@T3、31@T3、42@T2且deferred=[]，每库两候选20项正式指标全部null。机械复杂度A1/6/4、B2/7/3；本次A的Gateway四能力实验室公开探针true，B未测。生产Gateway3×200不等于候选能力完整验收。

隐私/runtime/workspace/freeze源绑定重算PASS，builder/verifier1/1、单UID进程角色分离；A日志39文件0命中并保留OPS。96材料/1222前归档/2570本次归档、5失败synthetic和旧window/claim/void保持。终态候选/controller/runtime/临时资源/failures0，同窗after1且三库PASS，窗口禁止重放。不变性：`nas_unchanged=null`；`existing_indices_unchanged=null`；`production_config_unchanged=false`；`services_unchanged=false`；`gateway_unchanged=true`；`all_items_measured=false`；不修生产、不补baseline，历史漂移保持。

[完整验收与三格核验](evidence/acceptance.md#amendment-6-正式执行收口评分输入硬门-invalid2026-09-12)、[公开abort](evidence/amendment6-formal-abort.json)、[Schema](evidence/amendment6-formal-abort.schema.json)、[唯一裁决](evidence/amendment6-formal-decision.json)、[QA](evidence/amendment6-formal-qa.json)。201回归和133编译通过不掩盖真实scorer输入门失败；未冒称全仓CI。失败收口完成、无后台任务；RT选型目标未达成，保持in_progress、禁止切流。只保留本地公开证据提交，不push/合并/清worktree。

## Amendment 7 — 修复 scorer trial identity，止于新 READY_TO_RUN

用户授权恢复输入合同，不授权执行正式 A/B。旧 Amendment 6 window 已 INVALID/after，
A attempt1/arm1 而 exposure/query/score/complete/result0；旧证据留存。
根因是完整题池的合法跨类别同题被 `(kb,query)` 唯一键误拒；新增私有 `(kb,ordinal)`
identity，并要求 main scoring-input-readiness 严格早于 before/freeze，coordinator 在
attempt 前复核。无题池/候选算法/生产变更，三库保持 42/31/42。
详见 [协议](experiment-protocol.md#amendment-7--scoring-input-readiness2026-09-12) 与
[本地验收](evidence/acceptance.md#amendment-7-本地修复--pre-exposure-scorer-输入合同)。
本地实现完成不代表 OPS READY；正式运行仍需单独授权。

## 最新断点 — Amendment 7 READY_TO_RUN，正式 A/B 未执行

[公开 READY](evidence/scoring-input-ready.json) 与 [QA](evidence/scoring-input-ready-qa.json)
经主会话独立 OPS 只读重算。部署源码 `f94b6e1b0f60763443e56ed76c932f90d55f927b`；
migration `3e5c31a2-8741-420b-b83b-095c32446d95`；window
`d408cfab-40f7-41d3-8ccb-75fb0aaa2b65`；随机顺序 **B→A**。

主 scoring-input receipt 严格早于 before，runtime/privacy/workspace/startup/input/
freeze/verify 与无 Popen precheck 均 PASS。完整池42/31/42不变，合法同题4/4/5个二元组
均保留为独立trial；新 attempt/arm/exposure/query/score/result/after 全0，旧 INVALID/after
及其 A attempt/arm 1/1保留。builder/verifier1/1，96材料与历史归档字节不变；Gateway3×200；
无候选/控制器/runtime。219测试、11行为破坏、编译/Schema/隐私/链接/AODW/governance通过。
仅本地源码与公开证据提交，不push；无后台任务。后续正式 A/B 仍须单独授权，完整NAS/index
不变性未证明，不能把 READY 当作评分结果或生产切换批准。


## Amendment 7 正式窗口收口（2026-09-12）

- 同窗口 `d408cfab-40f7-41d3-8ccb-75fb0aaa2b65`，冻结 B→A 正式路径只启动一次；source `f94b6e1b0f60763443e56ed76c932f90d55f927b`。
- 唯一裁决 **INVALID**；formal complete=false，v3 aggregate available=false，deferred=[]。不切流、不清除旧漂移、不关闭 RT。
- [完整固定分母/质量/资源、Gateway、复杂度与收口事实](evidence/acceptance.md#amendment-7-正式窗口收口2026-09-12)。
- [OPS 公开收口](evidence/amendment7-formal-closeout.json)、[唯一裁决](evidence/amendment7-formal-decision.json)、[QA](evidence/amendment7-formal-qa.json)。

## Amendment 8 — 日志落盘前防火墙与公开同形 build readiness

继续同一 RT，授权执行器修复，不执行正式A/B。旧窗永久 INVALID/after，不再使用。
[协议](experiment-protocol.md#amendment-8--streaming-log-firewall-and-workload-readiness2026-09-12)
约定 controller 流式净化、完整EOF与post-scan、终止失败分类、同7200秒公开同形验证。
新源提交后部署独立migration，只有全门通过才新建window/freeze。保留所有历史、96材料和
builder/verifier1/1；不改生产/NAS/index/alias/config、WeKnora core或sandbox/egress。
当前本地源码修复在验证中，未建立新READY，未push。

### Amendment 8 公开夹具与容量修正

首个 migration 的公开 A 三库 build/search 与隐私门通过；B 在注入锁下首次请求立即失败，
正常导入0，不是 build timeout。保留该失败与cleanup0，不freeze。将注入改为独立公开
SQL失败探针（锁确保不能落文档，预期HTTP500），释放锁后才运行一次完整build。
私有全集needle的安全容量分类显示总字节大于64MiB、不超过128MiB，最长leaf不超过8MiB。
仅pattern bank改为128MiB、各drain共享同一父进程不可变bytes集合；每流日志输入64MiB、
read chunk64KiB、最长leaf8MiB、A/B每库build7200秒均不变。不丢pattern、不用hash替代。
正式input readiness新增读取现有private-corpus并构造完整Filter容量验证，source和corpus
均进入freeze绑定；无Popen、无query、不重做builder/verifier。新migration重跑公开验证。

### Amendment 8 注入时序修正

第二个migration仍为公开readiness失败：正常B首个导入0.084秒请求失败；仅检查这轮公开
净化日志，观测到一次SQLite busy与一次missing table。防火墙完整EOF/关闭和post-scan
均通过，不能把故障注入影响冒称B正常build结论，也不能外推旧私有failure子型。
将刻意SQL失败探针移至每库正常build及公开search完成之后，以免干扰native惰性初始化；
候选build、文档大小/42/31/42分母、7200秒timeout和core均不改。原失败证据和cleanup0保留。

### Amendment 8 同形尺寸与精确字节计量

第三轮在任何SQL故障探针之前即导入失败，未观测到SQL错误。公开upstream服务合同为
manualContentMaxLength=200000字符；首个512KiB ASCII夹具超过该上限。OPS只读布尔
核验确认全部冻结私有canonical documents在原生字符上限内（未导出具体长度或内容）。
改用源码独立编写的中文公开句子，保持42/31/42、512KiB/8KiB字节上界，且正文小于
199000字符，为canonical metadata留余量；A/B文本完全一致。不是截断或重建私有输入。
防火墙进一步同时封顶每流实际输出64MiB；input计量在os.read后、output计量按实际
write返回字节，溢出块也进入input计数（最多input cap+1个64KiB read），不进入日志。
HTTP失败新增封闭状态码枚举，不保留响应消息或私有值；7200秒timeout不变。

## Amendment 8 最终断点 — BLOCKED

部署源码 `a475b048c235b2f4a8b92d845760462b4879c83c`；migration `387d6d9e-5ae4-4ebb-96b7-d993275b6111`。
[公开readiness](evidence/amendment8-readiness.json)、[独立OPS复核](evidence/amendment8-ops-verification.json)
和[行为破坏QA](evidence/amendment8-mutations.json)为本轮入口。
窗口实际创建：false；顺序：None；未运行正式coordinator。
新attempt/arm/exposure/query/result/after全0，旧window保持INVALID/after与log FAIL。
builder/verifier1/1、96材料、5938原归档和全部迁移历史字节保持；Gateway3×200、cleanup0、无候选/runtime。
完整NAS/index不变性仍UNKNOWN，未清除历史漂移；1路Gateway缺read_only字段，未证明只读合同。前三次公开夹具/容量修正失败链保留，不是正式成绩。

公开B硬门尚未通过；不创建window/before/freeze、不消费holdout。源码修复不能替代真实workload PASS。
隐私合成证据已独立按源码重算PASS，但迁移privacy binding未创建；main runtime/workspace/scoring readiness均未运行。
B失败总回执未回填firewall_verified，单独最终drain回执6流全部PASS、20次替换、scan0，原件不改。
投前库native日志有UNIQUE_CONSTRAINT1；CWork的SQLITE_BUSY1来自公开刻意探针，不归因为投前失败，更不归因为旧私有B。

详见[Amendment8完整断点](evidence/amendment8-blocked.md)与[最终QA](evidence/amendment8-qa.json)。

## Amendment 9 — 修复公开 B SQLite seq_id 并发冲突

2026-09-12 用户明确批准从 Amendment 8 BLOCKED 接续，只建立新 readiness，不执行正式 A/B。建议和授权方案一致：候选 runner 加逐文档完成屏障，保留 native core、内容、三库隔离、模型与7200秒总预算。

- 判据：真实 SQLite 并发调度 RED；完成屏障、失败/重复/超时/firewall 破坏测试；完整 RT055 回归与静态/隐私/流程门。真实 OPS 42/31/42 同形是独立必需验收，不用 mock 代替。
- AI 评审：本轮由父执行者直接审查双重完成屏障、pending/terminal 单次 POST、计时与 source-bound 无 Popen 预检；不调用已退役固定 reviewer，不以自述代替证据。
- 读产出：父执行者读取计数与封闭状态的公开 OPS workload、最终 drain、Gateway 两次观测、freeze/order/零暴露和归档字节复核。旧私有值及 hash 不出 OPS，旧失败日志不复制。
- 收口与剩余边界：[公开 Amendment 9 证据](evidence/amendment9-summary.md)。未明确授权前禁止正式 coordinator；禁止 push/merge、生产/core/config/sandbox/egress 改动、重建题池和旧窗重开。

## Amendment 9 续接终态 — BLOCKED，无正式 A/B

部署源 `6b32584023cd27096e7c2167c0c28f47b380f922` 未改；migration `89edbd10-2905-41f1-9037-62b502890856` 的公开privacy与42/31/42同形仍PASS，未重跑。预留window `7652dbee-3679-4886-a391-ffcf871e712c` 的main runtime/workspace/scoring、严格before健康、before/freeze/verify已完成，冻结顺序B → A未重抽。

最终no-Popen coordinator precheck因旧claim/void验证的`_proof()`调用子进程而被guard拒绝。独立只读复核确认，未放宽guard或删历史。已freeze的原件不能删除或写成未freeze；仅一次cleanup/after尝试，cleanup PASS，after因NAS `TransientStorageError` 为FAIL（已测1库），after快照与comparison未生成。保留失败after claim/status，不重试。window INVALID/after-failed、READY receipt未创建。新attempt/arm/exposure/query/score/result及global formal exposure/query/result全0；after claim=1、after FAIL是失败收口记录，不冒充完整after验证或READY要求的after=0。

96材料、5938/97376/194848归档及历史集合/字节保持；builder/verifier1/1，cleanup0、无运行残留。Gateway before 与独立最终均 3×HTTP200、ok=true、read_only 字段存在且 true；完整NAS/index仍UNKNOWN。只本地公开证据提交，不push、不改source scripts、不执行正式coordinator。根因、全部门、QA和后续授权边界见[Amendment9最终证据](evidence/amendment9-summary.md)。


## Amendment 10 — 进程内 legacy 证明与授权正式接续（2026-09-12）

授权从 aa440f6 接续同一 OPS run，不重启任务。Am9 window `7652dbee-3679-4886-a391-ffcf871e712c` 永久 INVALID/after-failed，旧 after 不重试，所有旧 claim/void/freeze/window 只读保留。

最小源码修复只替换 legacy `_proof()`：固定公开全文 SHA 认证后，AST 选取原评分定义，在当前进程受限命名空间执行公开两题；literal 依赖核对，不导入归档模块，不读取私有题目，不提供 open/import/subprocess/socket，三库必须仍于首次 search 前以原 missing-category 拒绝。原历史字节校验、零暴露与 no-Popen/no-query 硬门不削弱。

变更产生独立新 source commit、新 append-only migration、新 archive 与全新 window；新窗 runtime/workspace/scoring-input/privacy、before、freeze、零暴露逐项重验。源码绑定合同 `rt055_workload_readiness.verify` 要求 workload 的 source_commit/migration_id 与当前 deployment 完全一致，不能把 Am9 回执复制改绑；本轮不扩改合同，预先登记 **Am10 公开 workload 仅运行1次**（Am9 原证据保持，额外运行原因仅 SOURCE_BINDING_REQUIRES_NEW_WORKLOAD）。不再诊断旧 PageError，不重跑 builder/verifier，不改题池/seed/42-31-42/7200/A-B 对称门。

新窗 before/after 各一次；NAS 错误保留 claim/status，若失败不覆盖不重试该窗，cleanup 后 INVALID 追加收口。READY 后按本轮用户授权直接继续唯一正式 coordinator：随机顺序只冻结一次；每 arm/library 暴露只消费一次，任何 exposure 后不得重放。成功则聚合、唯一裁决；失败则同窗 cleanup→after→abort/decision。私有值与 digest 不出 OPS，正式命中日志禁止读出；完整 NAS/index 仍按测量边界 UNKNOWN。只本地公开证据提交，不 push/merge/清 worktree，不改生产或 NAS。


## Amendment 10 唯一正式执行收口 — INVALID

Source `43207eefbe30557b53d556055fa5438c8afe47ee`；本窗 `be3ab168-0568-4d39-a06e-824b4cd26a58`；顺序 B → A，原 coordinator 1次，A/B attempts=0/1。B 首库IMPORT阶段 REQUEST_FAILED；arm/exposure/query/score/complete/result逐库全0，不重放、不启动A。原formal `WAITING_RECONCILIATION / EXECUTION_ATTEMPT_FAILED / RUN_B` 保留，独立失败helper1次完成 cleanup→after→abort/唯一INVALID decision；before PASS，after PASS（claim1不重试），cleanup0，无任务/临时数据面残留。候选6流firewall/scan通过；coordinator firewall false/CHILD_NONZERO、后置scan0，失败值如实保留。正式20指标及Gateway四能力缺测全部null；机械向量A=1/6/4，B=2/7/3来自冻结runbook。96材料、42/31/42、builder/verifier1/1及历史字节保持；旧Am9 after FAIL不重试。完整NAS/index UNKNOWN及历史drift不清除，未改生产，不切流，不以INVALID关闭选型RT。详见[完整证据、逐库指标及QA](evidence/amendment10-summary.md)。仅本地提交，不push/merge/清worktree。


## Amendment 11 终态 — INVALID / INVALID_CLOSED（2026-09-12）

源码 `fdfcfb6dfef57862cf5efd478e88820419e7c322` 修复公开可复现的native finalizing等待合同并增加封闭请求错误码；旧Am10 REQUEST_FAILED因果根因仍UNKNOWN。新migration `aee2e2eb-4cd7-4f97-a864-5ce6c3782529` 首次public privacy实测108 socket samples/1 external observation/0 observer errors，loopback_models_only硬门失败；不将其等同外部数据传输，不豁免、不重跑。public workload0，main readiness/before/freeze/随机顺序/formal coordinator均未运行；A/B attempt0/0，逐库arm/exposure/query/score/complete/result全0。

预留新window `3330a46e-bfdd-4639-bb0c-ea5c1b3df622` 仅作终态收口：cleanup PASS→原after实现唯一1次FAIL（fresh before未创建、RuntimeError、0库、无快照/比较，不借旧窗基线不重试）→abort/唯一INVALID decision；formal INVALID为追加abort状态，非正式coordinator执行。120正式指标和24 Gateway能力均null，机械向量A1/6/4、B2/7/3只为未变runbook计数。新public3流firewall/scan通过，旧B6流PASS、旧coordinator false/CHILD_NONZERO保留。独立核验390079历史文件/96材料字节不变，builder/verifier1/1，42/31/42不变、残留0、Gateway3×200/ok/read_only=true。本窗comparison不可测；完整NAS/index UNKNOWN、历史services/config drift不清除。

RT055回归265项与源码红绿/行为破坏通过；最终Schema negatives、隐私/凭据、QA/治理详见[Amendment11完整证据](evidence/amendment11-summary.md)。本修订已收口，无后台任务；RT-055选型未完成，不切流、不自动重试，仅本地提交，不push/merge/清worktree。


## Amendment 12 — historical socket attribution UNKNOWN / INVALID_CLOSED

源修复`6c524bc10369464f0a8cf50d1380504deb64f9aa`已本地272项回归及3项行为破坏验证；新migration`7f3a28e9-195f-4d5e-a4f0-5451b10fdb43`仅迁移隔离源码。历史单次socket缺PID/phase/state/target关联，归因与传输仍UNKNOWN，按明确fail-closed门停止：新privacy=0（上限1）、workload/readiness/before/freeze/formal=0；A/B0/0，逐库全部消费账本0。新terminal-only窗`999c334f-5018-4d83-b571-b406f49fe605`唯一cleanup PASS→after FAIL（无fresh before，0库，不重试）→INVALID裁决，120指标/24能力null；旧窗不重开，旧after/失败布尔不改。历史390197文件覆盖390079旧文件与96材料字节不变，builder/verifier1/1；最终三Gateway健康，资源0，完整NAS/index仍UNKNOWN，历史services/config false保留。

判据：红绿/真实sandbox probe/3项行为破坏；AI检查：主执行者针对未知归因、PID例外边界、空日志与null语义交叉复核（未设独立Agent）；读产出：OPS只读闭集、独立最终审计、唯一decision和公开摘要。首次终审额外全集字符串扫描1文件/1串命中（同为公开源码常量），因果UNKNOWN，扩展扫描FAIL原样保留；补充终审仅完成采集，未重跑实验。不是正式隐私PASS或质量NO-GO，RT-055选型目标仍未完成。

详见[Amendment12完整收口](evidence/amendment12-summary.md)、[终审](evidence/amendment12-final.json)、[QA](evidence/amendment12-qa.json)。


## Amendment 13 — 历史保留与当前 source 独立放行（2026-09-12）

本节只追加，不撤销 Amendment 11/12 的 INVALID、UNKNOWN、FAIL 或首次审计失败。用户已授权新隔离 migration 单次当前隐私门，以及通过之后的新 window 正式 A/B；不是授权重开旧窗或把旧失败变 PASS。

- **historical_invalid_preserved**：Am11 的 108 samples / 1 external observation 缺事件级 PID/phase/state/target，传输 UNKNOWN；Am12 的扩展旧 synthetic 日志 1 个私有字符串命中且同为公开源码精确常量，因果 UNKNOWN、扩展 scan FAIL、first audit failure 原样保留。旧日志只属于旧 source/旧 run；保存并校验其原字节，不要求它事后通过新的测试。Am12 current privacy=0、旧 after 无 fresh before 的唯一 FAIL 均不重试。
- **current_source_privacy_gate**：仅绑定新 source、新 migration、synthetic attempt=1。所有当前执行硬门仍必须通过；旧 UNKNOWN/FAIL 既不替代当前证据，也不构成必须改写旧原件的前置条件。当前 source 的一次调用失败即关闭，不跑 workload/readiness/freeze/A/B。
- controller 在安装禁止读取私有题池的 audit hook **之前**，只在 OPS 内存读取 builder/verifier 全部 JSON 的所有非空 string leaf，并生成原始 UTF-8 与两种 JSON-escaped 变体；含公开 canary 与 warmups。候选不读取私有输入，不接收 bank；bank 值不进入 argv/env/file/log/receipt，不输出 needle hash。输入文件库存/字节绑定单独留 OPS；bank 回执只有 count/bool/bytes。容量超限硬失败，无截断和子集降级。
- search/native/sidecar 的新 synthetic 日志通过 controller-only byte firewall，在任何写盘前使用完整 bank；公开 canary 扫描与独立全私有 byte scan 均须 0，闭合 EOF/FD、无溢出、无错误，归档库存严格核对。源码绑定验证重新检查 bank 输入、三流原件与独立扫描，不能仅改 receipt hash 通过。随后 public workload 日志也使用同一完整私有 bank 加其公开合成 needles；不改 workload 文档、题数或算法。
- socket 合同沿用 6c524bc 的独立 probe PID。候选三类 external 必须为 0，SYN_SENT/UNBOUND/未知归因不豁免；observer 不停。仅专用 PID/公开固定目标/拒绝结果/无 peer/闭合专属 TCP socket/零应用 payload 的主动 canary 可通过；不反推历史传输为 0。
- 先公开红绿/行为破坏/隐私凭据检查并提交 source，再 detached 部署。当前门通过后才进入必要 public workload → main readiness → fresh before → 新随机 freeze/verify → no-Popen/no-query precheck → 新 window 正式 A/B（各最多一次）。任何 exposure>0 不重放；7200、42/31/42、seed/tier/floor、A/B 对称门、builder/verifier 1/1 均不变。
- 成功发布 120 正式指标、24 Gateway 能力、源码机械复杂度、聚合和唯一裁决；未测指标为 null。失败按同窗唯一 cleanup → after → abort/decision 收口。完整 NAS/index 不变性仍 UNKNOWN，历史 services/config false 不清除；无 production/core/NAS/index/alias/config 变更，不 push/merge/清 worktree。
- 最后另起只读终态审计：历史原字节与 96 材料、当前与历史日志分层、ledger/唯一性、无运行残留、Gateway 3×health、before/after、builder/verifier。测试绿不冒称完整 OPS gate 或全仓 CI；实际结果另追加。
