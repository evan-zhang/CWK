# RT-051 代码证据与边界

本文件只记取证，不另立方案。原有各节为初稿历史证据；本轮增量见文末，不将历史44文件核验计成本轮。唯一设计为 [rt-lite.md](rt-lite.md)。

## 证据口径

- 取证日期：2026-09-06，系统UTC时钟已读取。CWK产品基线 `80b8b850dd25c2527c18822923ccacae104205e8`；占号提交 `d63097d7095ac02c13207d3fac24667593d0d523` 只增加RT登记，产品字节不变。
- Yuxi本地工作副本 `/tmp/yuxi-study`：`fd0d9c4f48ba0e4701457196a2f967232be6091c`，取证时工作树干净；不声称与远端最新提交同步。
- [sources.json](sources.json) 为按commit/文件/行区间和全文件SHA固定的证据清单，只有源码/文档摘要，不含业务原文。行区间的存在与hash检查只能证明引用有效，语义结论由本次实读给出。
- 不读 `.env`、真实库raw、运行数据、生产验收JSON；不真实执行网关/摄取/模型/DocDB/NAS。源码中环境变量读取语句可以审阅，未执行或读取变量值。历史RT讨论只用来识别意图和重叠，现况以当前代码为准。

## CWK直接代码证据

| ID | 文件与已读行号（CWK基线） | 实际观察 | 对设计的约束 |
|---|---|---|---|
| E01 | [kb_gateway.py](../../scripts/kb_gateway.py) L205–323 | IndexEntry.haystack拼lineage/title/path，query_index是strip/lower后的完整子串、lineage排序；不读取raw | 不能称现网关已做正文/语义召回；metadata默认须兼容 |
| E02 | 同文件 L326–398、L655–690 | resolve_version按版本找path；build_citation实时读字节、计算SHA，excerpt固定前500字符，mismatch仍ok=true；没有chunk/span参数 | 新模式必须有段定位接口与严格verified语义，旧接口不能冒充新接口 |
| E03 | 同文件 L435–463、L475–509、L530–579、L616–653 | kb参数在auth之前解析；auth后才选择挂载；未挂载不回落；dispatch只一次authorize | 先库授权后召回；返回前二次检查是新增设计而非既有保障 |
| E04 | [kb_wizard.py](../../scripts/kb_wizard.py) L522–531、L583–586 | wizard query直接调用gateway.query_index，仅q/limit参数 | 两个查询入口应复用同一实现，新增flag当前不存在 |
| E05 | [kb_ingest.py](../../scripts/kb_ingest.py) L2009–2066、L2356–2415 | upsert记录lineage/source/stable_id/path/artifacts、raw SHA/origin SHA、version/versions等；未写title字段；timeline写v2旁件，classify原地覆盖 | 不假定title总有；版本身份不能等同路径/上游revision；旧版不保证有旧raw |
| E06 | 同文件 L1945–2006、L2112–2175 | already_done查状态/原件hash/artifact存在；publish顺序prev→index→provenance→state，多次独立write | prev是备份，非整个事务；源与派生发布须分开 |
| E07 | [kb_storage.py](../../scripts/kb_storage.py) L214–249、L255–297、L890–912、L957–967 | 通用protocol无CAS/事务；local临时文件fsync+replace（此函数未fsync父目录）；NAS为HTTP upload/download | 不把local单文件语义套NAS，更不能远程跑FTS5；本地代际发布需独立实现 |
| E08 | 同文件 L146–204、L495–525、L801–845 | 路径/符号链接限制、仅瞬时异常重试、整字节下载返回 | 继承路径防护与错误分类；有界读、查询deadline需要补，不宣称已有 |
| E09 | [kb_ingest.py](../../scripts/kb_ingest.py) L2790–2812、L2822–2861、L2864–3037 | refresh读取source.json；循环execute_plan；最后保存refresh-state/重签；dry拒写，已知失败不算new_failed；无词法hook、全流程writer锁 | 挂接必须在最终收尾后；源ok不代表正文索引完整；并发写需协调 |
| E10 | 同文件 L1689–1788、L2549–2633 | passthrough保原字节；XLSX主件是sheet目录，CSV在extras；reconcile检查raw/index/originals、缺失与SHA | 主件正文范围要明确，不假称sheet已能召回；新索引需独立完整性对账 |
| E11 | [kb_token.py](../../scripts/kb_token.py) L809–881 | 每次decide重载登记表、检查revoked/expired/scope，decision携membership_epoch/generation；读坏拒绝 | 可复用两次判定；没有PR-001逐文档grant/membership能力 |
| E12 | [kb_gateway.py](../../scripts/kb_gateway.py) L716–741、L778–788 | HTTP响应no-store；日志按HTTP格式写入；server刻意单线程 | 不能默认有吞吐余量/日志已去query；新模式脱敏日志需测试 |
| E13 | [cwk_wiki_query.py](../../scripts/cwk_wiki_query.py) L126–146、L351–411、L420–565、L2175–2209；[cwk_wiki_search_index.py](../../scripts/cwk_wiki_search_index.py) L115–173 | legacy tokenizer/BM25存在；summary/navigation召回，raw回读/段选择；不同数据模型与入口 | 不说整个仓库无BM25，也不直接import受管legacy模块到网关 |

## 实读测试能证明的范围（本轮未运行产品测试）

| ID | 文件/范围 | 测试实际行为与边界 |
|---|---|---|
| T01 | [test_kb_gateway.py](../../tests/test_kb_gateway.py) L226–258、L474–550 | 写陷阱backend与import方向检查。新增词法读面仍必须不触达任何write |
| T02 | 同文件 L588–681、L687–775 | 引文改字节后SHA变；前500字是现有断言；版本fixture有独立存档path，不覆盖真实classify原地覆写风险；metadata计数与排序已钉住 |
| T03 | 同文件 L1293–1403 | 选库、同lineage按库citation、unknown/403/404、不泄挂载面；新增词法不能绕过这些分支 |
| T04 | [test_kb_ingest.py](../../tests/test_kb_ingest.py) L1365–1465 | timeline保旧件/classify覆盖实测断言；J3IndexAtomicityTests在本地os.replace上抛异常，检查旧index/prev，未证明NAS或跨账事务 |
| T05 | 同文件 L2268–2353 | refresh无变化仍写refresh账/manifest，但业务件不写；已知failed第二次ok仍为failed；dry-run零写 |
| T06 | [test_kb_wizard.py](../../tests/test_kb_wizard.py) L510–557 | query的lineage/title/path/编号合同，需要保持默认结果 |
| T07 | [test_kb_storage.py](../../tests/test_kb_storage.py) L262–327、L585–623 | 路径拒绝及FakeFileStation的roundtrip/retry/permanent错误；不代表本轮访问真实NAS |

## 相关RT/PR原文与去重结论

- [RT-049方案](../RT-049/rt-lite.md)：已读全篇。当前代码确有多库路由，故E03引用实现而不是部署旧回执。本RT消费路由，不重建挂载与token管理。
- [RT-050方案](../RT-050/rt-lite.md)：已读全篇。取证基线refresh代码存在（E09），当时索引为created且其目录无meta；只记录格式不一致，不修改。最终main的索引已被另一会话改为completed，见文末漂移记录。两者目标不同：050管源摄取，051管派生检索及接线。
- [RT-021 intake](../RT-021/rt-intake.md) 和 [技术方案](../RT-021/specs/技术方案.md)：已读全篇，明确space registry/projector/current-index/cleanup与rebuild、独占legacy entity/search适配；生产space snapshot adapter由021交付。
- [RT-022 intake](../RT-022/rt-intake.md) 和 [技术方案](../RT-022/specs/技术方案.md)：已读全篇，可信身份、grant∩membership、双ACL和安全cache；022拥有ProfileSpaceSnapshotProviderV1 ABI。本RT不伪造或绕过它。
- [PR-001 DESIGN](../../PR/PR-001-multitenant-knowledge-spaces/DESIGN.md) L514–555、L799–806：已实读查询链及owner条款。文件中计划的Broker/projector模块当前主线不存在；结论仅限此代码基线，不否认其他分支未来工作。
- [RT-022迁移说明](../RT-022/reports/migrations/stage-06-cwk-wiki-query-ord1.md) L18–38：记录早期改动被政策槽位约束而登记到RT-022，并明确非该RT实现。结合 [DI台账](../_deferred-items.md) 已结清的前向演化机制，不倒改旧policy/receipt。
- [RT-044](../RT-044/rt-lite.md) 的读写进程与接口合同已读；[RT-045黄金题](../RT-045/golden-questions.md) L1–31有固定query/lineage/SHA链，并非“没有可回归标准”。133只是该历史案例规模，不能继承作容量目标。
- RT-004/006/010的关键词匹配后，再实读E13的legacy实现来限定重叠；不以那些旧报告的“BM25”标题推断当前kb网关实现。

## Yuxi实现观察（不移植）

以下路径以Yuxi根为准，行号固定于上述commit；全文件hash在sources.json。此工作副本未修改，未安装依赖，未运行模型或评估任务。

| ID | 路径/行范围 | 观察与不移植原因 |
|---|---|---|
| Y01 | `backend/package/yuxi/knowledge/implementations/milvus.py` L36–41、L396–445、L853–994 | content字段启中文analyzer，FLOAT_VECTOR embedding + SPARSE_FLOAT_VECTOR/BM25函数；vector与keyword各自检索，hybrid创建两路AnnSearchRequest并经WeightedRanker。**这是dense+BM25，非metadata+词法**。不要将其权重、阈值或性能直接带入CWK |
| Y02 | `backend/package/yuxi/knowledge/chunking/ragflow_like/dispatcher.py` L9–85 | chunk_id为file_id+chunk序号，位置从source_text.find(text,search_from)得到，可为空；本RT不能沿用为版本绑定的严格byte-span身份 |
| Y03 | `backend/package/yuxi/knowledge/chunking/ragflow_like/parsers/general.py` L14–68 | 分段、naive_merge、默认512近似token、重叠参数、硬切；借鉴“可配置边界+上限”，不照搬参数或strip/rejoin的定位假设 |
| Y04 | `backend/package/yuxi/knowledge/eval/metrics.py` L18–42、L100–141 | Recall/F1可用chunk IDs离线计算；无gold/无retrieval返回空metrics可能让聚合缺样本；本RT必须将应答题空召回算0、空集单列，不能仅算成功题 |
| Y05 | `backend/package/yuxi/knowledge/eval/evaluator.py` L59–137 | 检索评估和答案judge分开，可配置答案模型；本轮不运行它；CWK正文召回无需先引入LLM judge |
| Y06 | `backend/package/yuxi/knowledge/eval/benchmark_generation.py` L24–39、L73–100、L190–242 | 从KB收chunks、vector取邻居、LLM造题/答案/gold IDs，并将IDs过滤到允许集；过滤合法不等于gold人工核实正确，不能独自签收测试基准 |
| Y07 | `backend/test/unit/knowledge/eval/test_metrics.py` L10–50 | metadata.chunk_id及aggregate已有可回归单测，进一步支持“黄金集可以机器回归”而非必须手工运行 |
| Y08 | `LICENSE` L1–21 | 顶层MIT，Copyright 2025 Yuxi Project Contributors；复制/实质部分须保留许可，AS IS。仅理解设计、引用代码位置，没有复制实现；未来移植还须逐依赖核实，不能由顶层MIT推及所有组件 |

## Git占号及并发调查回执

- 占号前main=`80b8b85`，branch=main，worktree与index干净，未安装实际pre-commit hook（仅sample，无core.hooksPath覆盖）；无index.lock。占号命令再次核对HEAD/状态/占号并使用精确路径提交，提交后核对唯一文件为本RT meta。
- refs、目录与各index交叉核对上限050；历史tag `v0.1.0-internal` 没有index属可解释缺失，仍检查其RT目录/其他来源；未因该tag缺文件终止占号检查。
- 独立工作目录紧接占号提交建立。未修改其它会话暂存区、未stash、未reset/rebase/clean、未拉取或推送。
- 旧worktree提示（取证时相对占号前main）：`/private/tmp/cwk-ci-fix` 落后63、`/private/tmp/cwk-rt039` 落后59、`/private/tmp/cwk-rt041` 落后57；entity-retrieval工作树落后176且有未跟踪RT-010目录。RT-042/043/044工作树分别落后45/37/38。所有当前HEAD均无超出main的非RT差异，不构成本RT独立文档写面冲突；未进入这些未跟踪文档或私有运行目录。旧树不清理，不为了数量暂停安全的设计工作。
- **本会话**在主线仅登记meta；未写RT/index.yaml。按宪章不为绿色回执扩大main修改范围。
- 最终复查发现另一会话的 `bb414a63c78371e8aca57d38ed96a18dc32582a3`（提交时间2026-09-06T18:54:02+08:00）已在main更新RT-050文档和RT/index.yaml：050改completed、补051索引created。已核对d63097d..main变更路径仅这两份文档，四个kb模块字节无变化。本设计工作目录仍基于d63097d，不合并/rebase该提交；sources.json固定的是明确的取证基线而非声称main从未前进。G109/花名册差异仅适用于本worktree的index快照；当前main已有051登记。


## 本轮增量证据与审核处理（2026-09-06，effb1de修订）

来源边界：用户/父会话交接独立Codex `o40MC61R` 在 `effb1de` 的静态 **GO WITH CHANGES** 及Issue #2 OPEN/无评论；本轮未重新调用Codex或GitHub。会话memory检索不可用，没有因此编造审核原文。下面是作者定向源码复核及处理，不是再次独立审查。历史sources.json字节保持不变，未重核其全44项。

| 本轮定向取证 | 观察与边界 |
|---|---|
| scripts/kb_gateway.py 205–398、610–700、530–580 | metadata匹配lineage/title/path，query只首page；citation现场全read/hash后text[:500]，handler不消费offset/limit；GET-only、scope先于mount。未执行服务。 |
| scripts/kb_storage.py 214–244、665–708、795–852、949–973 | Protocol read->bytes，TLS pinned和普通HTTPS均response.read()，FileStation下载全对象；Range/stream未实现，不把事后len当内存边界。 |
| scripts/kb_wizard.py 510–600 | query经build_backend直读，parser仅create/ingest/status/query，无read；客户端包装是待开发而非存在事实。 |
| scripts/kb_ingest.py 2015–2067；main定向diff | upsert不写title，raw/origin SHA和版本独立；main fe9f858的vanished只报告不删除，不能作为ACL事件。相关测试diff只读，未运行。 |
| skills/cwk-kb-query/SKILL.md 1–82 | from_env/read、walk_files、本机NAS gateway与管理凭据兜底、两三词零断言无资料须在开发阶段获准移除；本轮未改Skill，不读取其示例引用的任何凭据。 |
| RT/RT-044/rt-lite.md；其references/CLI-SPEC.md | RT044链接CLI合同，wizard裁剪未实现read；定位实际CLI-SPEC文件复核read约定，不能按根references不存在路径假称已读。 |
| AODW宪章/交互/overview/Git/判据/Spec-Lite、根AGENTS；rigorous-plan-methodology与4份references | 已读，按分类/编码/SoR状态机/量化候选门槛/证据边界自查；无新增方法论文件或hash清单。 |

### 审核意见处理（意见来源为上述交接，不是逐字审核原文）

| 意见/裁决 | 处理 | 合同/验收 |
|---|---|---|
| 先补完整访问，后词法 | 采纳；已授权list/metadata分页/open/read/continue与工具接线为基础，不再挂范围待确认 | C01–C06、P1/P2→P3；A01–A05 |
| ref/cursor/完整性缺失 | 采纳安全目标，改为document_ref绑定源身份/SHA/expiry且不绑lexical generation；服务端防篡改游标、UTF8/span/覆盖回执 | C01–C03；A02–A07 |
| 所有read依赖chunk | 不采纳该手段：chunk只作召回建议，已知lineage和安全范围独立打开，统一reader | C02/C07；A01/A03/A11 |
| 全read/hash只返回500，建议分页 | 采纳分页目标、不照搬每页全对象下载；推荐OPS一次流式全SHA不可变快照，有界seek和每页引文 | C02/C06/C08；A03/A04/A09 |
| 长文预算与只读冲突 | 纠正旧2MiB硬拒与缓存禁令：gateway仍零持久写，独立broker/builder写受控快照，prepare/status/续期/GC明确；大文按页完整可达 | C05/C06/C08；A04/A09 |
| 版本覆写/撤权风险 | 采纳；新版只当前，拿不到确切旧bytes拒绝；每页前后校验源与权限，cache不绕撤权 | C03/C04/C06；A06–A08 |
| 缺title/占位部分转换 | 采纳；null+来源标签，传输完整与源解析完整分开，未知不猜 | C01/C02；A02/A05 |
| 工具真实接入/Skill凭据边界 | 采纳；优先扩现有wizard为token-only客户端，固定可调用包装、能力发现，未来移除NAS兜底；reviewer零工具不扩权 | C05；A01/A10/A12 |
| 旧服务/注入/结论诚实 | 采纳；v1兼容冻结但新包装拒假200；任务覆盖不等于理解，2–3词零不得断言库无资料 | C03–C05；A10/A12 |
| 缺实际验收 | 保留为开发硬门；12项真实ingest+脱敏backend+实际Agent工具链，未跑产品测试、不称产品已修 | C08/C09、A01–A12 |

本轮Git并发检查：9个worktree，RT051初始干净/无暂存，所有分支相对main的branch-only非RT差异为空；main有RT050非重叠改动，entity旧树仅未跟踪RT010目录，未读其内容。此结论仅是当时快照，不是未来开发无冲突保证。最终漂移/检查结果见validation。
