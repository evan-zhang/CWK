# CWK 设计说明书

> 文档状态：As-built（按当前实现编写）
> 基线日期：2026-08-04
> 适用范围：CWK 工作协同个人知识镜像、证据型 Wiki 与可信问答链路

> 当前生产基线为 Local-First，完整本地镜像是唯一权威持久主库。Cloud-First 与 cloud/shadow 查询自 2026-08-18 起暂停；DocDB 仅承担派生 Wiki 备份和日报/HTML 发布。未来目标与重启门禁见 [Cloud-First v2 重新审核与目标设计](CLOUD_FIRST_V2_DESIGN.md)。

## 1. 文档目的

本文说明 CWK 为什么这样设计、各模块如何协作、数据如何流转、系统如何保证完整性与证据可信度，以及当前能力边界。它是开发、评审、运维和后续演进的共同技术基线。

配套文档：

- [使用说明书](USER_GUIDE.md)
- [运维手册](OPERATIONS.md)
- [AI 运行策略](AI-PILOT.md)
- [模型角色矩阵](../MODEL_ROLES.md)
- [安全约束](../SECURITY.md)

## 2. 系统定位

CWK 是一个面向工作协同数据的**只读、Local-First 个人知识镜像**。它将授权用户可见的工作汇报转换为：

1. 本地磁盘中按业务日归档、带 SHA-256 的权威原文证据；
2. 一篇原文对应一篇可导航摘要；
3. 跨汇报的主题页和实体页；
4. 可回读原文、可验证引文的问答证据包；
5. 每日摘要、事件、实体和运行审计产物；
6. 可选同步到 DocDB 的派生 Wiki、日报和 HTML 预览副本。

CWK 不是工作协同的替代系统，也不是允许修改工作协同状态的自动化机器人。默认能力严格限制为读取、整理、检索和同步派生知识。

## 3. 设计目标与非目标

### 3.1 设计目标

- **完整**：指定业务日期内，源端 ID、raw ID、summary ID 必须一致。
- **可信**：事实答案最终来自 raw 原文，不以主题页或模型摘要作为最终证据。
- **可追溯**：每个摘要、主题引用和回答都能定位 `report_id`、source SHA-256 与本地 raw 路径；派生副本发布到 DocDB 时另记录 file ID。
- **增量**：每日只处理新增、更新、续报和有限历史回填，同时支持完整日期补采。
- **可恢复**：中断后可依据 manifest、磁盘文件和 retry queue 恢复。
- **隐私优先**：raw 只保存在授权用户的本地私有镜像；模型调用前隔离运行凭据；不把凭据写入运行产物。
- **低运维**：一条 nightly 命令完成采集、晋级、编译、审计和可选同步。

### 3.2 非目标

- 不回复、审批、驳回、删除、标已读或完成工作协同任务。
- 当前生产不把 raw 原文上传到任何 DocDB；已暂停的 Cloud-First 实验即使重启也只能使用个人私有 DocDB。
- 不把模型摘要当作不可质疑的事实源。
- 不提供 Neo4j 一类图数据库或任意多跳图推理。
- 不保证纯关键词检索对所有语义改写都能零噪声召回。
- 不替代权限系统；源端可见范围仍由 appKey 所代表的授权决定。

## 4. 总体架构

<div style="width: 1200px; box-sizing: border-box; position: relative; background: #fafbfc; padding: 20px; border-radius: 6px; border: 1px solid #e5e7eb;"><style scoped>.arch-title{text-align:center;font-size:22px;font-weight:bold;color:#1f2937;margin-bottom:16px}.arch-pipeline{display:flex;gap:0;align-items:stretch}.arch-stage{flex:1;padding:12px;border:2px solid #d1d5db;border-radius:6px;background:#fff;display:flex;flex-direction:column;box-shadow:0 1px 3px rgba(0,0,0,.04)}.arch-stage.blue{border-color:#3b82f6;background:#eff6ff}.arch-stage.amber{border-color:#d97706;background:#fffbeb}.arch-stage.green{border-color:#16a34a;background:#f0fdf4}.arch-stage.pink{border-color:#db2777;background:#fdf2f8}.arch-stage.gray{border-color:#6b7280;background:#f3f4f6}.arch-stage-title{font-size:12px;font-weight:700;color:#374151;text-align:center;margin-bottom:9px}.arch-arrow{display:flex;align-items:center;justify-content:center;width:30px;flex-shrink:0;font-size:20px;color:#9ca3af}.arch-box{border-radius:4px;padding:7px;text-align:center;font-size:10px;font-weight:600;line-height:1.35;color:#1f2937;background:#fff;border:1px solid #e5e7eb;margin:3px 0}.arch-box.highlight{border:2px solid #6b7280}.arch-note{margin-top:14px;display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.arch-note div{font-size:10px;color:#374151;background:#fff;border:1px solid #e5e7eb;border-radius:4px;padding:8px;text-align:center}</style><div class="arch-title">CWK 证据型知识镜像架构</div><div class="arch-pipeline"><div class="arch-stage blue"><div class="arch-stage-title">1. 源端读取</div><div class="arch-box">工作协同只读 API</div><div class="arch-box">日常有界采集</div><div class="arch-box highlight">日期范围完整分页</div></div><div class="arch-arrow">→</div><div class="arch-stage amber"><div class="arch-stage-title">2. 本地真相源</div><div class="arch-box">collected-raw 暂存</div><div class="arch-box highlight">raw/业务日/report.md</div><div class="arch-box">raw-manifest.json</div></div><div class="arch-arrow">→</div><div class="arch-stage green"><div class="arch-stage-title">3. Wiki 编译</div><div class="arch-box">summaries/一文一页</div><div class="arch-box">topics/主题聚合</div><div class="arch-box">entities/实体导航</div></div><div class="arch-arrow">→</div><div class="arch-stage pink"><div class="arch-stage-title">4. 可信检索</div><div class="arch-box">主题/实体召回</div><div class="arch-box">摘要排序与筛选</div><div class="arch-box highlight">raw 引文验证</div></div><div class="arch-arrow">→</div><div class="arch-stage gray"><div class="arch-stage-title">5. 服务与运维</div><div class="arch-box">OpenClaw 组织答案</div><div class="arch-box">DocDB 版本同步</div><div class="arch-box">审计/门禁/重试</div></div></div><div class="arch-note"><div>确定性主链路：采集、日期、去重、晋级、完整性审计</div><div>AI 增强链路：摘要精编；失败时保留可检索 fallback</div><div>证据边界：回答只能引用 verified raw evidence</div></div></div>

## 5. 架构分层

### 5.1 源端访问层

负责通过工作协同只读工具读取授权用户可见记录。

- `cwk_collect_live.py`：面向每日摘要的有界增量采集；优先新增、更新和续报。
- `cwk_backfill_range.py`：面向完整性的日期范围分页；源端默认 3.1 收件箱接口（秒级时间窗，`--source search-list` 保留毫秒 searchPage 回退），验证 API 返回总数与去重后的 ID 数一致。
- `cwk_source_coverage_audit.py`：重新读取指定日期范围的源端 ID，与本地 raw、summary 做集合对账。

日常采集和完整采集职责不同：前者控制成本与摘要工作量，后者提供数据完整性证明。不能再用“固定几页请求成功”替代“源端全部记录已采完”。

### 5.2 raw 真相源层

`cwk_raw_store.py` 将暂存 Markdown 晋级到本地 `raw/`。它承担：

- 按 `report_id` 去重；
- 依据业务日期归档；
- 原子写入文件；
- 对新增、更新、未变化、无效记录分别计数；
- 重建 `raw/_system/raw-manifest.json`；
- 明确标记 `raw_local_only=true`。

业务日期解析优先级：

1. 上游明确提供的接收时间；
2. 汇报发送/汇报时间；
3. 原文 `<meta>` 中的时间；
4. 创建时间；
5. 均缺失时进入 `raw/unknown/`。

目录规则：

```text
raw/YYYY-MM/YYYY-MM-DD/{report_id}-{title}.md
raw/unknown/{report_id}-{title}.md
raw/_system/raw-manifest.json
```

这里的日期是业务日期，不是 nightly 采集日期。

### 5.3 Wiki 编译层

#### 单篇摘要

`cwk_cloud_wiki_compile.py` 生成 `wiki/summaries/{report_id}.md`。每页包含：

- 原文链接；
- 发送人、时间和来源 lane；
- 摘要、关键事实、决策、行动项、风险；
- 候选主题与候选实体；
- 每项对应的原文引文。

模型返回必须满足 JSON 合约；引文必须能在原文中连续匹配。无法编译时生成不主张事实的 fallback 页，保证覆盖可导航，但质量状态独立记录。

摘要质量状态：

- `ai_refined`：模型输出已通过结构和引文校验；
- `fallback_pending`：已有导航页，等待精编；
- `fallback_terminal_error`：达到最大失败次数，保留 fallback；
- `unknown`：历史或异常状态，需检查 manifest。

#### 主题与实体

`cwk_cloud_wiki_topics_entities.py` 从 summaries 聚合：

- `wiki/topics/{topic}.md`；
- `wiki/entities/{type}/{entity}.md`；
- `wiki/index.md`、实体索引和系统状态页。

它们是召回和浏览导航，不是最终证据。

### 5.4 可信检索层

`cwk_wiki_query.py` 是无模型的只读检索器：

1. 对 query 分词；
2. 用 topics/entities 扩展导航命中；
3. 对 summaries 做词项相关性与 query coverage 排序；
4. 应用日期、发送人和页面类型筛选；
5. 回读 raw；
6. 输出经验证的原文片段；
7. 根据得分、覆盖率和证据状态计算置信度。

输出是**证据包**而不是最终自然语言答案。上层 Agent 必须：

- 仅使用 `evidence_status=verified` 的证据；
- 引用 `report_id` 和 raw 路径；
- 区分事实、估算、预算与目标；
- 显示冲突和数据边界；
- 当 `confidence=none` 时拒答或扩大检索范围。

### 5.5 派生知识与日摘要层

规则流水线还会生成：

- `daily/`：每日 Markdown/HTML；
- `history/`：按业务日组织的脱敏历史页；
- `events/`：事件候选；
- `entities/`：规则抽取的结构化实体；
- `_index/`：派生索引；
- `runs/`：验收结果和增量链接预演。

这些内容均可由 raw 或运行产物重建，不替代 raw。

### 5.6 同步层

`cwk_sync_mirror_to_docdb.py` 将允许的派生目录同步到 DocDB：

- 已存在页面使用新版本更新，不覆盖历史版本；
- 支持 changed-path manifest，只同步本轮变化；
- 失败路径进入持久 retry queue；
- 支持 dry-run 和文件大小限制；
- raw 不在 nightly 同步白名单内。

## 6. 核心数据模型与身份键

### 6.1 主键

`report_id` 是跨层唯一身份键：

```text
source row ID
  = raw frontmatter report_id
  = wiki/summaries/{report_id}.md
  = topic/entity 页中的引用 ID
  = 问答结果 report_id
```

标题和文件路径均可能变化，不能作为身份键。

### 6.2 关键 manifest

- `raw/_system/raw-manifest.json`：raw 路径与 SHA-256。
- `wiki/_system/manifest.json`：来源数、已编译 ID、精编/fallback 分区、失败队列。
- `runs/*/backfill-manifest.json`：日期回填结果。
- `runs/*/source-coverage-manifest.json`：源端/raw/summary 完整性。
- `runs/*/nightly-pipeline-manifest.json`：整条夜间流水线状态。
- `runs/docdb-sync-retry-queue.json`：云端同步补偿队列。

## 7. 数据流与处理时序

### 7.1 正常 nightly

1. 读取私有配置和环境变量；
2. 执行有界增量采集，生成每日摘要所需记录；
3. 晋级本轮 `collected-raw` 到 raw；
4. 对当天执行完整分页补采；
5. 生成规则摘要、HTML、行动中心和安全派生页；
6. 编译缺失 Wiki summary；
7. 在预算允许时限量精编历史 fallback；
8. 重建 topics/entities；
9. 对当天执行 `source IDs = raw IDs = summary IDs` 严格门禁；
10. 可选同步 daily/runs/派生目录和 wiki；
11. 写入总 manifest，并只在全部硬门禁通过时报告 `overall_pass=true`。

### 7.2 历史回填

1. 按日期范围完整分页并校验总数；
2. 仅抓取 raw 中缺失的 `report_id`；
3. 并发拉取全文；
4. 暂存到独立 run；
5. 晋级 raw；
6. 生成或补齐 summaries；
7. 重建导航；
8. 再次执行严格覆盖审计。

## 8. 完整性设计

### 8.1 完整性等式

对指定日期范围，CWK 的硬性验收条件是：

```text
source_report_ids = raw_report_ids = summary_report_ids
```

仅比较数量不够；必须比较 ID 集合。重复 ID 在源端分页后先去重，再与源端 `total` 校验。

### 8.2 覆盖与质量分离

```text
summary coverage = ai_refined + fallback
```

“有 summary”表示可导航，不表示已高质量精编。系统在 manifest 和查询结果中分别暴露覆盖状态与质量状态，避免把 100% 覆盖误报成 100% AI 精编。

### 8.3 幂等与恢复

- raw 按 ID 去重，内容哈希相同则不重写；
- 文件通过临时文件 + `os.replace` 原子提交；
- Wiki 每成功一篇即落盘 manifest；
- SIGTERM/SIGINT 时尝试刷新 manifest；
- `--reconcile` 可从磁盘 summaries 修复 manifest；
- 同步失败写入 retry queue，下轮补偿。

## 9. AI 设计

### 9.1 双模型角色

- `newapi/BD-MiniMax`：批量单篇摘要和记录理解；
- `newapi/BD-glm`：JSON 修复、跨文档综合、聚类和质量审核。

代码通过 allowlist 拒绝其他模型，防止定时任务静默漂移。

### 9.2 零工具 Reviewer

真实 AI 调用通过专用 `cwk-ai-reviewer`：

- `sandbox.mode=off`，不依赖 Docker；
- `tools.profile=minimal`；
- `allow=[]`、`alsoAllow=[]`、`deny=["*"]`；
- `skills=[]`；
- 不使用消息投递；
- 临时 prompt 调用后删除。

安全性来自“无工具、无技能、无交付”的最小能力模型，而不是容器隔离。

### 9.3 工作协同内容信任边界

凡通过工作协同只读接口获得的来源内容，均视为已授权、非涉密的知识源。
编译、AI 理解、Wiki、查询和发布链路不得因为文本形态类似 AppKey、Token、
API Key 或其他技术标识而脱敏、隔离、跳过或中断。系统只防止来源内容被
当作执行指令，不改变来源事实。

## 10. 安全与隐私边界

- 运行鉴权参数只从环境变量或 gitignored 私有配置读取，且不写入命令日志；
- 工作协同正文不适用运行参数脱敏规则，必须忠实保留；
- `runs/`、`knowledge/`、`state/`、`.env` 和 `.cwk-ai-runtime/` 不入 Git；
- raw 默认被通用同步器拒绝；已暂停的实验链路必须同时使用 `--allow-raw --experimental-cloud-raw`，且只能写入个人 DocDB；
- 生产只允许向 DocDB 发布派生 Wiki、日报和 HTML；Cloud-First 的 raw、压缩索引和对象清单发布均暂停；
- 工作协同 mutating commands 的预期集合始终为空；
- 个人镜像与团队镜像不应混用，除非权限边界经过明确批准。

## 11. 部署与目录结构

代码仓与私有数据镜像分离：

```text
CWK/
├── scripts/                  # 确定性执行层
├── tests/                    # 单元与 smoke 测试
├── docs/                     # 设计、使用、运维文档
├── skill/                    # Agent Skill 与配置模板
├── runs/                     # 本地运行产物，gitignored
├── state/                    # 采集状态，gitignored
└── knowledge/工作协同镜像 -> 外部私有镜像目录

工作协同镜像/
├── raw/                      # 本地权威原文证据；生产运行不得清理
├── wiki/
│   ├── summaries/            # 一篇原文一篇摘要
│   ├── topics/               # 主题导航
│   ├── entities/             # 实体导航
│   └── _system/              # Wiki manifest/查询契约
├── daily/                    # 每日可读报告
├── history/ events/ entities/ _index/  # 规则派生物
└── runs/                     # 镜像内验收页
```

软链使代码仓保持可发布，而真实知识镜像留在本机私有数据目录。部署时必须验证软链目标存在且权限正确。

## 12. 可观测性与验收

### 12.1 必查指标

- `overall_pass`；
- 源端、raw、summary 数量和 ID 集合；
- `missing_raw_count`、`missing_summary_count`；
- AI `degraded`；
- `ai_refined` 与 fallback 数量；
- Wiki failure queue；
- DocDB `failed` 与 retry queue；
- `mutating_cwork_commands_called` 是否为空。

### 12.2 验收命令

```bash
make test
make wiki-lint
make wiki-smoke
```

截至 2026-08-04 的验收快照：

- raw：1,450；
- summaries：1,450；
- topics：92（不含 index）；
- entities：439（不含 index）；
- Wiki lint：PASS；
- Wiki smoke：18/18 PASS；
- 单元测试：74/74 PASS。

快照是运行状态，不是代码常量；以后应以 manifest 和 lint 实际输出为准。

## 13. 当前限制与风险

1. **检索是词法相关性，不是向量语义检索**：可能出现同词不同义噪声；上层 Agent 需逐条审核证据。
2. **主题/实体是导航层**：还不是关系型图数据库，不能假定页面共现等于事实关系。
3. **fallback 质量不均**：覆盖完整不等于综合摘要完整，复杂问题应增加 `top-k` 并回读 raw。
4. **源端完整性受授权范围限制**：审计只能证明“当前 appKey 可见范围内完整”。
5. **unknown 日期**：上游缺时间的记录不能参与严格按日统计，需单独检查。
6. **云端依赖**：Cloud-First 下网络或 DocDB 故障会阻止新版本提交；查询不得静默退回陈旧本地数据。
7. **配置文档易漂移**：安全策略和模型角色必须以代码预检与 allowlist 为最终约束。

## 14. 关键设计决策

- **ADR-01（已替代）：个人 DocDB 为唯一持久主库**——raw 使用物理文件版本和 SHA-256；本地仅为可清理缓存。
- **ADR-02：业务日归档**——分析关注汇报/接收日期，不关注采集落盘日期。
- **ADR-03：覆盖与质量分离**——fallback 保证可检索，精编状态单独治理。
- **ADR-04：问答先检索后回读 raw**——模型或主题页不能单独成为事实证据。
- **ADR-05：日常有界采集 + 当天完整分页**——兼顾日报效率和全量完整性。
- **ADR-06：零工具 Reviewer**——取消无必要的 Docker 依赖，同时不授予宿主机工具能力。
- **ADR-07：DocDB 版本更新**——保留云端历史版本，失败进入补偿队列。
- **ADR-08：单调索引提交**——`index_version` 只在语料变化时递增；读端可指定最小版本，防止写后读到旧索引。
- **ADR-09：双重云端门禁**——同步回执生成对象目录，完整性审计要求 file ID 与 SHA-256 覆盖全部持久对象。

## 15. Cloud-First v2 提交协议

本节是已暂停的实验协议，不属于当前生产基线；当前生产行为以本文顶部的 Local-First 状态声明为准。

1. 工作协同源端按业务日完整分页，先写入运行期本地缓存。
2. raw 以物理文件新版本写入个人 DocDB；通用同步仍默认拒绝 raw。
3. `cloud-objects.json` 记录 `relative_path → file_id/parts → content_sha256`；超过 2MB 的 raw 使用内容寻址 gzip 分片，分片 SHA、压缩产物 SHA 和逻辑原文 SHA 三层校验。
4. summary frontmatter 固化 `source_report_id`、`source_sha256`、`source_cloud_file_id`。
5. 本地 `search-index.json.gz` 预计算 summary 与 topic/entity postings；云端发布为带 `index_version` 的多个不超过 1.5MB 的 `.bin` 分片，逐片 SHA 校验并重组；新分片和 metadata 全部写入并通过覆盖审计后，最后更新 `cloud-objects.json` 提交指针，避免读端混用新旧分片。
6. Wiki 与索引写入后先在本地合并候选对象目录，再执行全量路径/SHA pre-commit 门禁；只有门禁通过才把 `cloud-objects.json` 更新为云端唯一提交指针，失败时 nightly 必须为红。
7. 对象目录合并对空本地镜像和大比例裁剪 fail closed；本地挂载丢失、路径配错或误清空不得覆盖云端最后一个已提交目录。
8. 覆盖审计默认逐对象回读，持续断言 `project_id` 为当前鉴权用户的个人私有项目，将 `project_id/root_file_id` 写入审计产物，并要求 retry queue 为空。
9. 实验问答可使用 `--mode cloud --experimental-cloud` 从云端索引召回并下载 Top-K raw 验证；缓存命中时必须重新校验 SHA。
10. 实验比较可使用 `--mode shadow --experimental-cloud` 同时执行本地与云端；不得作为生产入口或静默降级。
11. 云端不可用或版本低于 `--min-index-version` 时明确失败，不使用未知新鲜度的本地副本。
12. 删除本地持久 raw/Wiki 前，必须完成两次独立空目录恢复演练并通过哈希校验。

## 16. 后续演进建议

优先级从高到低：

1. 持续清理 fallback 质量债务；
2. 为 unknown 日期增加人工修复清单；
3. 加入同义词词典或轻量语义 reranker，降低关键词噪声；
4. 为主题—实体—汇报关系增加显式关系类型和证据边；
5. 增加周期性全范围覆盖审计，而不仅审计当天；
6. 增加检索评测集，持续跟踪召回率、精确率、拒答准确率与证据正确率。
