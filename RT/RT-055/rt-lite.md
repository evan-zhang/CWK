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
