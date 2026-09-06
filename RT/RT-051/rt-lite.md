# RT-Lite: RT-051 - 正文级词法召回与元数据召回融合 v1

> profile: Spec-Lite | execution_mode: collaborative
> 本文件是唯一方案权威。生命周期、分支和工作目录只见 [meta.yaml](meta.yaml)。
> **方案门待用户/开发 Agent 批准；本轮仅完成设计准备，不授权代码开发。**

## 方案（给人看）

- **做什么**：给现有知识库增加可显式选择的“正文词法 + 元数据”召回。能找到只出现在文档中后部的词，并打开真正命中的原文段，而非只给文档开头。
- **为什么**：当前网关查询只匹配编号、标题和路径；现有引文虽实时读原文，却固定截前 500 字。两者必须一起补，否则“搜到了”仍不能核验。现况依据为 E01–E04，见 [证据](evidence.md)。
- **代价**：网关所在机器需要受控的本地派生索引、独立构建任务和失效检查；中文切词、编号和版本变化需要专项评估。索引可重建不等于服务不会中断。
- **这次故意不做**：不做 embedding、稠密向量、LLM reranker、图检索、回答生成、格式转换升级、原始数据迁移、跨库联搜、PR-001 多租户空间/逐文档授权、生产部署与定时器修改。
- **用户怎样算成功**：标题和路径没有目标词时也能找到正文；点击引文能看到命中段并验证版本与 SHA；跨库、撤权、旧索引不能漏出正文；不传新参数时仍按原来的元数据规则查询。具体研发成功条件为 C01–C10。
- **推荐**：先做纯 Python BM25 与现有元数据的排名融合；SQLite FTS5 留作同一脱敏集上的对照备选。推荐基于可解释、标准库及中文 token 可控，不是性能优越的实测结论。如果批准规模下 Python 超出预算，再用同 token/同快照/同引文契约比较 FTS5。
- **范围必须再过门**：这是“正文词法与元数据融合”，**不是 Yuxi 的 dense + BM25 混合检索**。用户已授权准备这个候选方案，不等于已批准该范围、引擎或开发。

### 待批准决策（先看这里）

| ID | 推荐及代价 | 决策责任/不批准时 |
|---|---|---|
| D01 | 首期正文词法融合，不做 dense；Python BM25 优先。语义同义改写未必命中 | 用户批准范围，开发 Agent 复核可实施性；否则修订本文件，不开发 |
| D02 | 仅索引当前主 raw 文档的可追溯正文；XLSX sheet CSV、结构化意见链/JSON 元数据不并入 v1，覆盖受限 | 用户确认裁剪；若要 sheet 正文必须补 artifact 级版本/SHA/citation 设计后再过门 |
| D03 | 单一可信构建宿主、独立 factory 进程、本地派生磁盘；所有该库 ingest/run/refresh 写入共用写锁与可读屏障 | 用户/运维确认拓扑、磁盘及写入口可收敛；否则只可离线原型，不可生产启用词法 |
| D04 | 本版本沿用“库快照授权”，不是源系统实时逐件 ACL；已观测库级撤权/移除必须立即拒绝；未知上游删除不可推断 | 用户确认业务安全边界；若要求实时源撤权，先由权限/摄取 owner 提供权威事件，再扩大方案，不能假装已满足 |
| D05 | 接受 C08 的建议评估规模、时延、内存、召回与引文门槛 | 用户批准目标，开发 Agent 实测；当前无基线、无跑分，不可宣称容量足够 |
| D06 | 新模式默认严格失败，只有显式允许才降级为元数据；新增引文段接口不改变旧接口正常形状 | 用户批准可用性取舍；不能用静默 fallback 掩盖正文检索失效 |
| D07 | 开发阶段新脚本需要正常代码归属登记；既有 owner 不转移，RT-050 写面串行协调 | 开发 Agent 先核对活跃改动并取得相应规范/治理登记授权；本轮禁止修改规范，不能以设计文件代替该授权 |

## 假设与现状

### 取证基线与不重复立项

- CWK 产品代码基线：`80b8b850dd25c2527c18822923ccacae104205e8`；RT 占号基线为此提交的子提交。Yuxi 基线：`fd0d9c4f48ba0e4701457196a2f967232be6091c`。准确路径、行号与文件摘要在 [sources.json](sources.json)。只读源文件，不读真实 raw、凭据或生产回执内容，不访问线上服务。
- 当前宪章、根 AGENTS 和 manifest 均为 **AODW v0.6.1**；没有擅自把用户“LDW”口头称谓当成仓库已升级。rt-manager 末尾“动 RT 外才建 worktree”的旧句与宪章冲突，遵宪章的“占号后立即建 worktree”。
- RT-049 是已落代码的多库路由，RT-050 refresh 已在取证代码树；占号基线索引为 `created`，不能据此推断尚未实现，也不以其部署描述认定线上已部署。最终复查main已由另一会话推进到 `bb414a63c78371e8aca57d38ed96a18dc32582a3`，只改RT-050文档与RT/index.yaml（050改completed并补051）。产品代码无变化，本worktree未合并该文档提交。
- RT-021 的目标是 tenant/space Projector 与索引；RT-022 是可信身份、双 ACL、Broker。当前主线未找到其计划的 `cwk_space_projector.py`、`cwk_space_registry.py`、`cwk_query_broker.py`、`cwk_query_contracts.py`；不得把历史报告标题等同当前运行实现。它们与本 RT 不是同一验收目标。PR-001 ABI 与专属脚本不在本 RT 改动面。
- 旧 Wiki 路径已存在中文 n-gram/BM25 和 raw 引文选择，但召回对象是 summary/navigation，入口与 kb_gateway 不同。它不是“仓库完全没有 BM25”，也不是“网关已有正文 BM25”。只借鉴思想，首期不 import/修改该受 PR-001 管理的实现。
- 占号前已检查目录、完整 index、所有本地 refs（含 remote-tracking、tag、stash）、worktree 的 RT 目录/索引及 reflog，高位均未超过 050。相关 RT 正文和 DI-001–003 已读，无待认领同目标事项；不更新需求池、不假认领。

### 必须纠正的旧说法

1. 六道黄金题有明确关键词→lineage→现场 SHA 的重复验证语义；不能断言“必须人工执行、不能回归”。但它们不是覆盖正文-only、中文短词、无答案与安全竞态的完整新评估集。没有运行这些旧题，也没有将旧回执当今天结果。
2. “133 件”是 RT-045 的历史样本，不是当前容量、不等于生产需求上限。
3. `raw-index.prev` 是上一索引备份；当前 publish 顺序是 index→provenance→state，并非跨文件事务。已有原子性测试主要在本地 `os.replace` 处注入中断，不能证明 NAS 上传或跨进程事务原子性。
4. 索引可重建仍会造成不可用、结果缺失、错误旧版或权限泄露的服务事故，必须有失效/恢复/值班责任。
5. FTS5 要求本地 SQLite 数据库文件及文件系统语义；不能在 FileStation HTTP URL 上直接执行，也不能把读远端 SQLite 字节当远程数据库事务。
6. classify 会覆盖主 raw，同一旧版本记录可能指向已更新路径；“存在版本链”不保证旧版字节仍在。新接口遇到这种情况拒绝，不返回 v2 冒充 v1。

## 实现备注（用户不问可不展开）

以下是**待批准的实现规格**，不是当前已有 API。C 编码是本 RT 条款，不是另建判据台账；实际测试后续仍放 tests/。

### C01 范围、分层和选型

分类叠加，不推翻旧结构：`授权库身份 × lineage × source_version × artifact × chunk × generation × engine_version`。source/version 定事实身份，generation 定派生快照，二者不得互代。

| 方案 | 中文、短词、编号 | 依赖/运行约束 | 判断 |
|---|---|---|---|
| 纯 Python BM25（推荐） | 自有固定 tokenizer；中文 1/2/3-gram，ASCII 与编号完整 token；可保留原文位置 | 标准库；对象/倒排内存与启动成本可能较大；需有界读、加载及评分 | 最容易把召回、span、版本和回滚解释清楚；性能未测 |
| SQLite FTS5 | 默认 unicode61 不等于中文分词；预分词后存 token 串或 native tokenizer 各有代价。trigram 对 1/2 字短词有盲区；分隔符可能拆编号 | Python sqlite3 是否编入 FTS5、SQLite 版本、扩展装载及中文依赖均需探测；禁任意 load_extension；DB 必须 gateway-local | 若内存/规模压力实测明显，优先比较“相同预分词”的 FTS5；不能直接拿默认分词结果比较速度 |
| Yuxi/Milvus dense+BM25 | 中文 analyzer + embedding + sparse index，两路 AnnSearchRequest，经 WeightedRanker | Milvus、模型、向量维度/版本、网络与费用、重建与运维边界 | 真混合路线，仅作未来参考，不引入本 RT |

未来 dense provider 可消费相同 `AuthorizedLibrarySnapshot` 与 chunk 证据身份，输出自己的 `channel`、rank、score_kind、engine_version；embedding model/version/dimension 独立版本化。v1 不保留“假的向量字段”，不叫 hybrid，不复用 RRF 数值作余弦阈值。接口允许将来新增 provider，不替 PR-001 冻结新 space ABI。

### C02 原始映射、分块与身份契约

**权威输入**：NAS（local backend 时为指定库根）中 raw 主件 + `_system/raw-index.json` 映射；`origin_sha256` 是来源原件摘要，不能代替转换后 `sha256`。索引 row 的 `version` 是本地摄取修订号，不冒充 DocDB 上游 revision。构建器严格校验映射，不能使用网关旧 `parse_entry` 的宽松默认值掩盖缺字段。

资格：当前 row 有合法 lineage、正整数 version、完整 lowercase 64-hex SHA、安全 raw 路径，`status=ok` 且 `artifact_kind=document`，并与 ingest-state 的 converted/对应摘要一致。placeholder/skipped/failed/pending 不进正文候选；所有排除项以稳定 reason 与计数记录。可转换但失败项不是“无正文”正常项，`coverage_complete=false`。缺 SHA、重复 lineage、冲突版本、非法路径、输入损坏使构建失败。原本没有正文的 placeholder 可列 `excluded_expected`，也必须显式显示覆盖口径。

**文本范围**：仅当前主件。Markdown/text 保留字节，不重写原文。若 CWork envelope 有唯一 `<content>…</content>`，正文范围为标签内部；结构化意见链/JSON 元数据不纳入 v1 正文索引（若要求可检索，需要 D02 扩围并单列可追溯抽取契约）；若没有，取 frontmatter 后、`## List Row Metadata` 前的内容。重复/歧义标签 fail 或列为 `body_range_ambiguous`，不隐式索引 JSON 元数据。普通转换 Markdown 从 frontmatter 后起；主件中的标题可检索，但正文-only 测试必须排除该标题和 path 命中。v1 不渲染 HTML、不重跑转换器。该抽取器需以合成 envelope 明确测试，不照搬 legacy strip 后偏移。XLSX 当前主件常是 sheet 目录，sheet CSV 只作“本版不支持”计数，不宣称已覆盖表格内容。

**分块建议参数（随 D01 批准/实验冻结）**：目标 800 个 Unicode code point、硬上限 1200、相邻重叠最多 120；先段落/标题/行边界，超长段再硬切。块必须是原 raw 上单个连续区间；不拼接标题、跨正文 range 或去空白后伪称连续。相同段落重复出现按原始位置区分；空白块不生成。块边界切分要先维护字符→UTF-8 字节映射，CRLF、BOM、emoji、组合字符必须测。正文 UTF-8 非法则拒绝该件，不用 replacement 字符生成可信坐标。

| 字段 | 类型/确定规则 | 来源/用途 |
|---|---|---|
| `schema` | `cwk.kb.lexical.chunk.v1` | 本 RT 派生契约 |
| `library_instance_id` | 完整 SHA256(domain + canonical JSON of trusted backend binding + kb_code) | 从已授权挂载取得，kb_code 与库身份文件及raw-index交叉核对；不从请求自报路径，重建同名库不同kb_code不共用缓存 |
| `kb_id` | 已解析的挂载 ID | 库级路由，不等于 PR-001 tenant_id/space_id |
| `lineage_id` | 严格沿用 raw-index 字符串 | 源类型+稳定 ID；跨库相同值不得合并 |
| `source_version` | 正整数，等于 row.version | 与既有 citation 的 version 对接 |
| `raw_sha256` / `origin_sha256` | 主 raw / 原件的完整 SHA256；origin 可空但不可替代 raw | 原文核验 / provenance |
| `artifact_id` | v1 固定 `primary` | 禁默默读取 extras；未来多 artifact 需升 schema |
| `span_start_byte` / `span_end_byte` | 从完整原 raw 起算，0-based、半开区间；非空，UTF-8 边界 | 权威定位，不是归一化 token 坐标 |
| `line_start` / `line_end` | 1-based、含端点；按原文 LF 统计 | 人工导航，byte span 才是精确定位 |
| `chunk_sha256` | 原 raw `[start:end]` 字节 SHA256 | 防定位错/重复段落错配 |
| `chunker_version` / `tokenizer_version` | 不可变策略名称+参数 digest | 参数变化必换，禁止就地重解释 |
| `engine_version` | 引擎实现版本+BM25/融合参数 digest | 查询/持久化兼容检查，未知版本拒绝 |
| `chunk_id` | `ck_` + SHA256(`cwk.kb.chunk.v1` + canonical JSON数组：library_instance_id,lineage_id,source_version,artifact_id,raw_sha256,start,end,chunker_version) | 不截短；不用 path 或构建时序，raw 移动而内容身份不变时稳定；换词法 engine 可复用 chunk identity，但检索引用仍绑定新 engine/generation |
| `generation_id` | `gen_` + deterministic manifest logical SHA；由查询上下文/外部pointer提供 | 不写入参与文件hash的chunk/doc/posting记录，避免generation→文件hash→generation循环；不进入chunk_id，同输入可重现 |

canonical JSON 固定 UTF-8、ensure_ascii=false、sorted object keys、无空白分隔、禁止 NaN；数组顺序如上。原文 SHA 不对文本做 NFC/NFKC、大小写或换行归一化。目录路径只是 locator，存派生 manifest 内且不从 HTTP 参数接收。`source_snapshot_sha256` 对库绑定及按lineage排序的当前投影计算，投影字段固定为lineage_id/version/path/sha256/origin_sha256/title（缺省空字符串）/status/artifact_kind、ingest-state.status及其origin_sha256；不含updated_at/batches/历史attempts等易变运行字段；另在本地 build-state 构建回执中记实际 raw-index 文件 byte SHA、ingest-state SHA、root-manifest SHA 供本次读取握手的一致性校验；这些易随账本时间变化的物理摘要不参与派生 generation 身份。禁止以 mtime 判定 unchanged。

### C03 Token 与两路融合/score

**词法规则候选 v1**：连续汉字生成 unigram、bigram、trigram（含扩展汉字范围）；ASCII 字母数字串 lowercase，保留点/短横/下划线/斜杠连接的编号完整 token，并保留组成部分；不 stemming、不停用词、不把正文改成分词文本。所有 posting 保存原 raw token byte span。查询同规则：汉字段长度≥2 时用 2/3-gram，不以单字充斥长词得分；一字查询用 unigram，二字必须可召回。Latin 单字符也支持但受候选/时延上限限制。

编号约束：查询中含长度≥5 的纯数字，或同时含数字与 ASCII 字母/连接符的编号（如 `AB-017`、`v0.9.43`），标作 exact code；正文候选必须含每个 exact code 的完整 token，不允许只匹配 017 或 v0。普通年份/自然语言不被擅自转成 ID 查询。对大小写不同的完整编号按 ASCII lowercase 比较；不自动同义扩展或把任意 FTS MATCH 语法当用户指令。超长查询>256 code points、新模式重复/未知控制参数、控制字符返回 400。

- BM25 使用每库**当前合格块**的 N、df、avgdl。推荐 k1=1.5、b=0.75；query term 去重，`idf=ln(1+(N-df+0.5)/(df+0.5))`，`score=sum(idf(t)*tf(t,d)*(k1+1)/(tf(t,d)+k1*(1-b+b*dl/avgdl)))`；dl为块内已发射token数，N为合格块数，空/全零长度单独返回零；只对出现词得分，空 corpus 返回空结果且不得除零。不混合其他库的 df、词表、缓存或分数统计。
- 词法路先取有界 top chunks（建议 `candidate_k=100`，服务端上限 500，不暴露任意调大），按 BM25 降序、lineage_id、span_start_byte、chunk_id 确定性打平。以每 lineage 当前版的最高块分数生成 body 文档排名，不求和奖励长文；最多附带 3 个不同命中块，不足覆盖的相邻重复 span 去重。
- 元数据路保持现有 `q.strip().lower()` 在 lineage/title/path 拼接字段上的完整子串匹配，按 lineage 排序；在**新模式**中同样使用当前资格域，避免被删除/failed 条目绕到这一路。元数据排序本身是字典序不是相关性排序，接受这一限制并在消融评估中说明。该路独立取相同candidate_k上限（不是默认query limit20）；有额外匹配则candidate_truncated=true，最多两路各candidate_k个文档进入融合。
- 新模式以 reciprocal rank fusion：`fusion_score = 1/(60+body_rank) + 1/(60+metadata_rank)`，缺一路项为 0，两路等权作为待批准初值。不把 metadata 任意造为 BM25。最终按 fusion_score 降序，再按 lineage_id 确定性排序，limit 沿用默认20/最大200；若候选不足，返回真实不足数。
- `score_kind=rrf_rank_v1`、`score` 为上述值；`channel_scores.body_bm25` 仅作同库同代内部诊断，非概率、非答案置信度，不能跨库/版本/engine 比大小；rank 从1开始。元数据独中时 body_bm25=null，`match_kind=metadata`、`chunks=[]`；不得声称其正文命中。
- 新模式 `matched` 为融合去重后的有界候选数，伴 `matched_relation=lower_bound|exact`、`candidate_truncated`；达到上限但未证明枚举完就写 lower_bound。旧模式 matched 的全量子串计数语义不变。不用 Top-K 为无答案自动编造非零分。

### C04 Query、命中段及 citation 的新旧接口

旧 `/query?q=...&kb=...&limit=...` 或显式 `retrieval_mode=metadata`：保留 `cwk.kb.gateway.query.v1`、字段、正常排序、计数及宽解析行为；不依赖新派生索引、不自动切正文模式。`query_index` 的现有默认签名兼容；新读侧 provider 独立注入。`kb_wizard query` 仍调用同一读侧语义，新 `--retrieval-mode`、`--allow-degraded` 是**拟议** flag，当前不存在，不能现在执行。

新 `/query?...&retrieval_mode=lexical_fusion_v1`：显式 `cwk.kb.gateway.query.v2`，可选 `allow_degraded=metadata`，未给即严格。新 schema 只在显式模式下返回：旧文档标识字段 + `retrieval_mode_requested/effective`、`degraded`、`degradation_reason`、`coverage_complete`、`excluded_counts`、`index_state`、`source_snapshot_sha256`、`generation_id`、`engine_version`、`matched_relation`；每 hit 有 `match_kind`、rank、score/score_kind、channel ranks 及 `chunks`。本地索引绝对路径、词表、其他库统计不出 HTTP。

每 chunk hit 携 `chunk_id, source_version, raw_sha256, chunk_sha256, span_start_byte, span_end_byte, line_start, line_end`，以及 `citation_ref` 的结构化字段 `kb,lineage,version,generation,chunk`。客户端必须原样带 kb；不得接收用户自报的路径/任意 offset。query 输出的是**召回候选，不是 verified evidence**；本版不把缓存原文 snippet 放进 query，防索引成功却返回旧正文。可仅显示“第 N–M 行命中”，读 citation 后才展示原话/作答。

新 `/citation?kb=...&lineage=...&version=...&generation=...&chunk=...`（五项完整，未知/重复项拒绝）：

1. 先按库鉴权，再定位挂载；校验 generation 属本库，chunk tuple 与 lineage/version 完整对应。只在只读本地 manifest 及其SHA绑定的chunk映射查 locator；不遍历 raw、不根据 chunk ID 猜路径。
2. 读取当前可读屏障与映射，确认仍是当前 source_version/raw_sha/资格。v1 新段引文不服务历史检索。generation 可以是尚保留的旧派生代，但必须映射身份仍与当前完全一致，且对应 engine 可读；否则 409 `stale_reference`，提示重新查询。
3. 从本库 StorageBackend 现场读取主 raw 字节；验 full SHA=当前 row SHA=chunk.raw_sha；严格 UTF-8、验证 byte span、chunk SHA；按块区间返回，不能回落 `text[:500]`。上限见 C08。
4. 再读权限、屏障与当前映射；漂移则丢弃整个 payload。成功 `schema=cwk.kb.gateway.citation.v2`、`evidence_status=verified`、`matches_index=true`、`fetch_mode=live-backend-read`，excerpt 就是该 byte span 对应的原文，回同一 span/SHA/lineage/version/kb。verified只证明来源/字节/位置有效，不等于该段回答了用户题意；答案依据还须人工gold或获授权的下游核对，本RT不产生答案。
5. SHA不一致/缺原件/坏定位统一 503 `evidence_unavailable`，无 excerpt；未授权401/403沿用当前库级边界；坏请求400；未知 chunk 在已授权库返回404固定消息、不回路径。引用旧版不会偷偷读取 originals 并现场重转格式。

旧无 chunk 的 citation 正常响应仍为 v1/前500字（兼容测试保留），不能把它叫段级证据。其 `matches_index=false|null` 仍不具有可信证据资格。新增**返回前二次库鉴权**是安全收紧而非正常形状改变，须纳入 D06 与测试；不把 v1 的 mismatch 观测行为默默改成与 v2 一样的 strict 模式。新模式客户端必须走 v2 段引文，并检查schema和effective mode；旧服务器可能忽略未知参数而返回v1，客户端必须报“不支持段检索”，不能把HTTP200误判已启用。

**降级判定优先级**：先auth/mount，再源资格/屏障，再本地索引；只在第三层故障允许fallback。

| 情形 | strict默认 | 显式 allow_degraded=metadata |
|---|---|---|
| token失效/无scope/未知挂载 | 既有401/403/404，无候选 | 相同，绝不降级绕auth |
| 源backend不可达、映射/身份损坏、dirty或缺屏障、source snapshot不可确认 | 503 source_unavailable，无候选 | 相同；不能确认当前资格域则无安全metadata fallback |
| 本地generation缺失/损坏/过时/未知engine/容量超限；当前源可完整核实 | 503 lexical_unavailable，稳定reason | query.v2、effective=metadata、degraded=true、reason必填；只查C02当前资格域，chunks=[]，不报verified |
| 词法deadline耗尽 | 503 retrieval_timeout | 剩余总deadline可完成资格/metadata才降级，否则503；禁止再开一个完整timeout预算 |
| ready且确实零命中 | 200空结果，degraded=false | 相同；不能把无答案当索引故障 |
| v2 citation SHA/版本/span不符 | 409 stale_reference或503 evidence_unavailable，无excerpt | citation从不降级成原文开头/旧缓存 |

允许metadata降级时coverage_complete描述**当前检索资格覆盖**，不宣称正文召回完整；degraded本身明确词法路不可用。原始无mode的v1兼容入口仍保持旧宽容范围，两种metadata口径分别标明schema，不混写同一版本契约。

### C05 隔离、删除、撤权和旧缓存

- 先解析目标库→token scope 判断→挂载查找→获取该库 generation→召回。既有 RT-049 的“单库授权问未挂载邻库403；admin未挂载404；不回落主库”不变。所有评分与候选生成只在选定库内；禁止全库召回后过滤。
- `AuthorizedLibrarySnapshot` 由网关构造并冻结：library_instance_id/kb_id、可信挂载修订、授权 decision identity（token_id/membership_epoch/generation；仅内存）、索引及源快照、read fence epoch。客户端不能提供这些可信值。admin 走独立 mode，不虚构 binding epoch。
- 返回前重新执行相同 token 决策并比较身份/epoch，检查 library mount identity、read fence 与源快照。撤权、过期、reissue、挂载改变或登记表不可读拒绝整请求。新模式、段引文及允许降级路径都做；旧普通响应也加二次库鉴权。仍有“最后检查到网络发送”的极小线性化窗口，以最后成功校验为响应准许点，不声称可撤回已交付数据；若要求与远端撤权事务完全线性化须新增权限服务协议。
- 日志只记路由枚举、状态码、耗时、计数及 opaque request ID，不记完整 GET target、查询词、标题/正文/凭据。当前 `log_message` 沿用 HTTP 格式字符串，不能声称它已隐藏查询串；开发需在同一 gateway 写面补脱敏日志并验证错误/访问日志均无查询与正文。响应继续使用已有 `Cache-Control: no-store`，它不是服务端授权替代品。
- 本版无 query/answer 持久缓存，禁缓存原文引文。只允许只读 immutable generation 的解析对象，key 含 library_instance_id/generation/engine，失效不得 fallback 到其他库。旧 generation 还在磁盘不等于可返回。
- 当前版更新：源 snapshot 变化立即令旧词法代 `stale`；在新代发布前不混用元数据新版本和正文旧版本；strict503，显式降级则走已重新核实的当前元数据域。classify 覆盖窗口由 C06 的屏障兜底。
- 每次召回前必须核实源快照；发现 lineage 已从当前 raw-index 移除/资格失效，即禁止将其交给召回器；在途返回前发现即拒绝。历史版本只存身份链，不参加新召回。没有当前映射就不可从缓存/old generation/prev 索引复活。
- **删除事实来源边界**：RT-050 是快照不删。源列表缺项可能是分页/权限/网络故障，不能据此物理删除或证明已撤权。v1 不发明源删除API/逐件 tombstone 权威；必须由既有库管理 owner 以获授权的映射移除或库级撤权处置。要求源侧实时撤权时 D04 不通过，停启用词法，不能以 NAS 仍有 raw 为许可。
- 库级撤权只使该持有者失去读取能力，不删除仍由其他人合法使用的库索引。整库退役/敏感源删除时由授权 factory operator 处置所有代与备份，网关从不执行删除。本地倒排/块引用也可能泄露原文词汇，按敏感派生物管理，目录0700/文件0600且不提交Git、不进不受控备份、不输出正文日志。磁盘加密与物理残留政策由运维在 D03 确认；逻辑拒读不能被叫作安全擦除。

### C06 Gateway-local 索引、代际发布与恢复

**两进程约束不变**：factory/独立本地 builder 写派生索引；gateway 只读。不得在 query 首次命中时偷偷建库、拉全 raw、写SQLite/WAL/缓存。read-only provider 模块与 writer/CLI 分离，import 图与写陷阱测试扩展到整个新依赖链。网关不调用 ingest，不向 NAS 写 search index。FTS5 若获选，以只读 immutable DB 打开；构建在同机 staging，本机完成 checkpoint/close 后才发布，不把活动WAL搬给读者。

建议派生布局（受控本地目录，**不是 NAS 路径，也不是仓库目录**）：`<approved-index-root>/<library_instance_id>/generations/<generation_id>/{manifest.json,postings.json,documents.json,chunks.json}`，外层 `current.json`、`build-state.json`、`source-read-fence.json` 及锁。文件只允许 validated hash leaf，拒绝 symlink/path traversal，不接受HTTP根路径参数。JSON严格有界解析，禁止pickle或可执行反序列化。chunks 存身份/span/token定位，不存完整正文；倒排仍属敏感数据。

**SoR 分工**：NAS raw/index/state/root-manifest 是源事实与映射；本地 generation 是可重建派生；本地 build-state 是该构建操作的恢复账；本地 source-read-fence 是唯一获批准写宿主的查询一致性屏障，不取代NAS事实或上游ACL。所有写入口必须经同一 owner 协调；远端/第二宿主直接写无法被本地锁保证，D03 不成立就禁止生产新模式。

**状态机**：`missing → building → validating → ready → stale`；building/validating 出错→`failed`，当前旧代保持或变stale；损坏→`corrupt`；磁盘/预算耗尽→`capacity_exceeded`。stale/failed/corrupt/missing 不伪装 ready。所有状态以 reason enum 和计数说明，不吞错误变成空命中。

**源写屏障及锁顺序（需要开发新增，不是既有能力）**：

1. 所有 `run`/`refresh --yes` 最外层持有同一 `(library_instance_id)` 进程锁，从 Accounts 加载之前起覆盖 raw写、逐件publish、manifest重签及refresh-state保存；同机锁用OS释放型锁，不能只靠PID文本/mtime。lock busy 返回可重试，不自动抢占。不同库可独立；gateway 无写锁操作。
2. factory 首个源写前原子发布 dirty fence（新 epoch，fsync文件及父目录）；源写完成且一致性验证通过后标 clean/final source digest。崩溃留 dirty，即使锁随进程释放，gateway也拒绝词法/段引文，不能因“PID不在”宣称安全。
3. 构建器持有 source lock 获取已完成输入 snapshot；构建与源写不能重叠。新 raw-index/state/root-manifest与实际映射一致前不得清 dirty。多账本不一致由摄取 owner 恢复/对账，builder 无权限“修好源”。处理完成才允许重建。
4. 只读网关在查询开始与返回前读取 clean fence + 源映射摘要，两次必须相同，并确认 generation 的 source snapshot 一致。没有 fence/权限不可验证/被外部写破坏，strict拒绝；未批准接屏障的库只能走旧metadata，不开放新mode。两次物理摘要在同一次请求内要稳定，但generation匹配使用logical snapshot；一次已完成的纯refresh记账不应使相同内容代失效。

**构建与发布协议**：

1. 读取单库一致快照 S0（索引、资格状态、manifest），验证 schema/身份、来源账对得上；只遍历映射中的主raw，不扫共享 raw。read返回后先限制大小再严格解码；当前 backend.read 全量返回bytes，无法靠事后len避免分配峰值，C08上线前必须补有界读取能力或证明硬源大小约束，不能冒称已解决。
2. 逐件现场回读 SHA，确定正文range与chunk。主件缺失、SHA错、不可读或任一合格件无法建立完整chunk映射则失败，**不发布悄悄少件的新代**。正常unsupported计数必须与资格规则对齐。
3. 在同文件系统 staging 生成排序稳定的 postings/doc/chunk 和 manifest：schema/engine/tokenizer/chunker/source_snapshot_sha256、源件数、合格件数、块数、excluded reasons、每文件sha/byte length、logical hash。各immutable文件不得带时间/耗时/机器绝对路径/本次握手物理摘要，这些只进build-state回执。manifest内部不自哈希：logical hash对不含generation_id和自身digest字段的确定性core计算，再填generation_id；current pointer绑定完整manifest文件hash。相同logical输入重建得到同目录名与同文件字节，已有同代必须逐文件hash相同才幂等，否则报冲突不覆盖。
4. 对账：合格映射数=distinct document数；每 document所有span合法、source identity完全匹配；每posting引用存在且tf等于位置数；每块SHA匹配构建时原文；无孤儿/重复chunk；记录源失败/unsupported使服务端可呈现覆盖口径。容量不达标或解析失败不切pointer。
5. 再读 S1 与S0逐字段及digest一致；不一致整代作废并有界重试。验证所有文件→flush/fsync→close→原子重命名staging为immutable generation→fsync目录→写pointer临时文件/fsync→os.replace current→fsync父目录。仅最后一步是激活点，不把NAS上传当该原子操作。
6. 单builder锁防两个发布者覆盖；pointer包含expected_previous_generation并在锁内检查，检测外部发布/回退。读者一次读取并固定current+manifest，验证整代hash，整个请求不得切到新代部分对象；校验后才服务。重新打开时未知engine拒绝。

**重试和崩溃恢复**：存储暂时失败仅瞬时错误重试，复用错误分类但不能把每raw 6次重试再乘整构建无限重试。建议构建整体最多3次/30分钟、指数退避上限8秒；查询全流程受 C08 deadline 管，不用底层默认退避耗尽HTTP worker。auth/SHA/schema/路径错误不重试为成功。dirty fence恢复必须先经owner核对源账；staging无pointer绝不服务；generation已落但pointer未切为可验证孤儿，不自动认为当前；pointer切后崩溃重新校验manifest/文件。当前代坏时只有“前代 source snapshot仍是当前且资格仍有效”才允许显式恢复；否则拒绝/显式metadata降级，不回滚源映射/撤权事实。

**并发与保留**：初版网关保持现有单线程HTTP，不为性能目标擅自多线程共享FileStation session。builder与HTTP读者分进程；将来并发读者仍必须pin generation。自动GC不在v1实现范围；开发测试模拟旧代保留与发布，不运行真实清理。生产磁盘至少容纳两已发布代+一个staging；超过配额停止新构建并告警。旧代回收只能由单独获授权maintenance操作，在确认没有读者lease/进程引用且权限政策允许时执行；禁止为腾空间删除仍被请求使用的代。

### C07 RT-050 refresh 接线与所有权

现有真实接点：`refresh_library` 在完成各source的 `execute_plan` 后，最后 `save_refresh_state` 重签manifest；不是每件 `Accounts.publish` 后触发词法构建。当前没有词法hook、跨进程writer lock或发布通知；这些是待实现项。

- 一个 factory orchestration 入口持源锁，包围 RT-050 refresh apply 全过程；仅当 apply实际执行、无guard、source/reconcile处于可读状态时请求本地build。源完成与词法完成分别回报 `source_refresh_ok` / `lexical_index_state`，不能把RT-050的“已知失败不计红”当“词法覆盖完整”。对 `failed_items` 独立检查，新旧failed都不能被正文合格域吸收。
- dry-run不触发builder、不写fence/job/索引；unchanged可检查source logical snapshot，完全一致则build no-op，但refresh账自身变化不应导致每次无意义重建（manifest byte SHA用于握手，logical snapshot用于派生身份）。
- 消息不是事实源。任务只带 library_instance_id、source_snapshot_sha256、engine_version、request_id；不带凭据/正文/任意shell。重复任务同key幂等；旧任务被新源摘要取代时标superseded；失败恢复从当前源检查重来，不盲放旧指针。
- 另有独立重建命令供首次安装、engine升级、崩溃恢复；只在获批准factory宿主运行，不在查询入口触发，不加管理HTTP路由。具体CLI名称由开发Agent落地后写回本文件，设计期不伪造可运行命令。外层refresh先完成clean fence并释放source lock，再调用/通知独立builder；builder自行重取同一锁并复读当前快照，禁止嵌套重复获取造成死锁。等待期间有新refresh则旧request失效，以新源为准。
- ownership：kb_ingest为现有RT-043管辖/RT-050当前演化；kb_gateway/kb_wizard归RT-044；kb_storage为RT-042。有界read能力若需改storage，单独评审协议兼容。新读侧契约/索引读取器/构建器由RT-051登记。**不得修改** PR-001 schema、cwk_wiki_query、cwk_wiki_search_index、entity_catalog、nightly或release flag。
- 开发前重查 main与worktrees，对 kb_ingest 和 gateway 的并行更改先协调，不能依据本次18:45后的快照永久认定无冲突。新增脚本需标准治理清单与模块登记授权，不能通过把实现藏在RT目录绕过scripts exact-only规则。

### C08 评估和预算（建议门槛，全部待批准、未实测）

**最小脱敏集**：48题/2个合成库，每类6题：正文-only、中文1/2字短词、编号、无答案、旧版本/更新、跨库/撤权、损坏/缺失索引、元数据兼容。含相同lineage不同正文、重复段落、前500字外命中、CRLF/emoji、分块边界、classify覆盖及timeline保留。仅用人工编写或获批准脱敏文本，不抽取私有raw。不为“够48题”堆同义重复；每类必须含正/负或边界机制差异。

每题记录 `case_id/category/corpus_version/query/request_mode/authorized_library/expected_doc_versions/gold_spans/raw_sha/gold_quote_sha/no_answer_reason/expected_error/reviewer/reviewed_at`。gold以原文byte span和version为权威，chunk_id由冻结chunker派生且人工复核；避免换chunk策略后gold跟着实现自动自证。需要人工核实目标段确实回答题意、无答案在授权域确无依据；LLM生成/judge只能建议，不能独自签gold。当前只设计集规格，**未制作已核实gold或宣称人工验收完成**。数据集0题/未审gold必须使评估失败。

| 门槛 | 建议值与口径 | 基线/时点/验收人 |
|---|---|---|
| 召回 | answerable题 macro doc Recall@10≥0.90，正文-only与短词各≥5/6；body evidence span Recall@10≥0.85，同时报@1/@5/@10 | 当前metadata、body-only、fusion同集消融；基线未测；开发阶段末，用户/评估人 |
| 引文正确性 | v2 verified引文100% full SHA+span SHA+版本一致；被用作答案依据的段，人工确认可支持题意的比例100%；零错库/错版 | 逐返回段核验，空返回不得算100%通过；开发Agent机械+人工gold复核 |
| 无答案 | 6题不得产出verified答案依据；区分无词匹配与“有词但不回答题意”，后者允许候选但不得声称答案成立 | 无答案不计入Recall分母；记录候选误报和证据误认，人工判定 |
| 隔离 | 所有未授权/竞态用例泄露0条正文、标题、chunk、邻库统计；未授权请求触达邻库provider次数0 | 不拿总体平均掩盖单次泄露，安全硬门 |
| 兼容 | metadata既有成功结果字段/排序/limit/matched等价；新增二次auth的拒绝按批准安全收紧验收 | 回归现有相关测试；不要求时间戳字节相同 |
| 规模 | 合成 1k与10k主件两档，最大单件2MiB，总raw≤200MiB、总chunks≤100k、查询并发按当前单线程排队实况 | 不是133件推导；硬件/文件长度分布记录，超过任何值明确capacity_exceeded |
| 时延 | 本地暖index评分p95≤300ms；端到端query p95≤2s、v2 citation p95≤2s，整请求deadline≤5s；冷加载≤5s单独报告 | 每档≥200次测量，暖身20次单列；NAS耗时、排队、解析分别计，不拿MemoryBackend数字当NAS性能 |
| 内存/磁盘 | query进程峰值RSS≤512MiB，builder≤1GiB；单库派生配额建议2GiB，含两代+staging；超额拒绝不OOM | Python对象开销须实测，不能据raw字节推算；跨库总RSS需另测，不将每库预算无界相加 |
| 引文/响应 | 单段≤1200 code points且≤8KiB；每文档附≤3段；新query整体≤256KiB；只读raw最大2MiB | 超出给有界错误/截候选标记，不截断SHA所指段冒充完整引文 |
| 恢复 | 合成10k集全重建建议≤10分钟；故障后5分钟内给可识别诊断（不是自动修复承诺） | 负责人为开发Agent提交实测、运维确认上线SLO；未达标修订方案后再门审 |

no_answer含语义不足题，BM25不是答案生成器。Recall按 `(library_instance_id,lineage,version)` 去重；证据span覆盖须命中人工gold范围（正文回答关键span全部被所返块覆盖）；跨块答案允许多个chunk覆盖同一gold。错误题单独算预期拒绝率，不能从评估集合悄悄移除。报告所有样本数/失败/跳过，禁止只统计成功请求。时延目标可能被当前HTTP单线程及NAS读取约束推翻，D05批准的是待测目标而非保证。

### C09 分阶段开发、验收与回滚

**本轮到此为止**。以下阶段仅用于下一位开发Agent在方案门后执行：

| 阶段 | 预计写面/工作 | 出口证据与停点 |
|---|---|---|
| P0 方案门 | 复核D01–D07、当前基线、ownership与权限边界；用户批准且开发Agent确认；冻结评估gold | 无批准不写代码；独立人工gold不能由实现自签 |
| P1 契约/对照实验 | 拟新增 `scripts/kb_lexical_contracts.py`、读写分离模块及 `tests/test_rt051_*.py`（这些当前不存在）；先合成chunk/token/BM25，探测FTS5能力并可选对照 | C02/C03确定性向量、短词编号负例、相同数据两引擎实验；未达预算回方案门，不拿“可跑”代替选型 |
| P2 构建与快照 | 拟新增独立builder与reader，屏障/锁/manifest/发布/恢复；必要时storage有界read；由RT-050 owner协调run/refresh锁入口 | kill-point、lost publish、disk-full、两builder及读者pin实验；源NAS模拟HTTP故障；无业务数据与真API |
| P3 网关/向导接入 | 新显式mode、v2段引文、双鉴权、fallback可见；保持默认metadata与只读import图 | 前500字之外实证命中，v2 byte比对；多库同lineage/在途revoke；HTTP loopback+Memory/本地脱敏backend |
| P4 脱敏验收与接线 | refresh最后成功点hook、重建命令、状态报告；按C08评估与范围内回归 | [验收矩阵](acceptance-matrix.md)逐项交证据，独立复核失败样本；当前实际模块/flag回写此文件 |
| P5 用户收口门 | 展示用户能查到什么、哪些故障会拒绝、容量与风险；询问后续动作 | 不自动合并/推送/部署/启动定时；生产另需真实环境授权与安全边界确认 |

代码文件路径是拟议的分工，不是存在性证据。所有更改只在本RT分支，规范与治理登记需D07明确授权；不触及原PR工作树/旧RT账本。禁止为了消门禁告警重写历史或安装共享hook。

**开发后可用的现有局部测试命令**（当前仅记录，未执行产品测试；无真实NAS smoke开关）：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_kb_gateway.py'
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_kb_wizard.py'
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_kb_ingest.py'
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_kb_storage.py'
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_kb_token.py'
```

P1以后新增测试才可执行 `python3 -m unittest discover -s tests -p 'test_rt051_*.py'`，必须同时检查实际测试数>0和案例集合覆盖；不存在时0测试通过不能当成功。测试环境由可信测试runner使用脱敏fake，禁继承真实smoke启用变量，禁为跑测试读取.env。`make ci`是全量权威入口，但本次不跑；未来收口由开发Agent另排足够时间，不以局部绿灯宣称CI全绿。

**回滚**：用户批准部署之后才能操作。先关闭词法新模式/构建调度，保留metadata默认；停新发布、保留源与审计账，进行当前权限/映射检查。新mode返回明确disabled503或显式metadata降级，不默默旧词法。退旧engine仅能读其兼容且source仍匹配的代，否则重建或拒绝。不恢复过期token/已撤权条目，不把raw-index.prev覆盖回权威索引，不以删除索引作为无影响回滚。旧客户端v1正常路径无需迁移；数据安全检查不能随feature关闭被移除。真实清理、源修复、配置修改必须另有授权。

### C10 风险、自查与例外责任

| 风险 | 发现/控制 | 责任与停点 |
|---|---|---|
| 中文ngram噪音/编号误命中 | 消融、exact code门、短词/无答案负例；不把score当置信度 | 开发Agent；达不到C08回D01/D05，不上LLM掩盖 |
| 首部引文错位/归一化丢坐标 | 以原byte span切片+双SHA；重复段落/emoji测试 | 开发Agent；任一verified错位阻断验收 |
| classify原地覆写/源多账非事务 | source lock+dirty fence+前后snapshot；旧版不给替代正文 | 摄取owner+运维；所有写源不能收敛时禁止生产新mode |
| 无上游实时删除/ACL事件 | D04明示快照许可边界，库级撤权兜底；不宣称实时源撤权 | 用户/权限owner；需要强保证则先补事件方案 |
| Python内存/HTTP read无界/单线程排队 | C08容量实验、有界read、冷暖分开；不实际处理真实超大raw | 开发Agent；容量不达标重审FTS5或约束，不提高目标自证 |
| 索引损坏/磁盘满导致不可用 | 严格失败/显式降级、代际发布与恢复对账 | 运维对线上响应负责；源SHA错由摄取owner处理，builder不得改源 |
| 跨RT并行覆盖 | 开发前重查worktrees、ownership和主线差异；每次精确暂存 | 开发Agent；重叠时先协调，不抢共享文件 |
| 新文件成为治理孤儿 | D07授权后按标准登记；不修改PR-001冻结策略 | 维护者批准，开发Agent执行/自测，独立验收人复核 |
| 第三方许可/效果夸大 | Yuxi MIT实读，仅引用路径与设计观察；不复制实现、不跑其模型链 | 本RT作者；以后移植需独立许可核验含依赖，不凭顶层MIT一概放行 |

方法论四步自查：

- 分类：保留库/lineage，在其上叠加版本、chunk、generation、engine；身份/授权/内容/派生时间轴分开。
- 编码：C01–C10为实现条款，D01–D07为决策，P0–P5为后续阶段；对应关系在验收矩阵，不增设重复方案。
- 治理：源与派生SoR、状态机、量化候选门槛、异常拒绝、恢复责任均明确；审批=用户，实施=开发Agent，源写协调=RT-050/摄取owner，gold与上线复核=人工/独立验收者，运维=容量与事件响应。
- 证据：代码观察均有E引用及commit/hash；历史样本与部署旧回执不等于现况；所有预算/性能与范围裁剪仍是建议，缺实测与人工gold批准。不得由本文件作者自签开发完成。

## 验证

- 本轮仅文档：空白检查、Markdown相对链接/锚点及证据source行区间/hash核验、RT-051定向AODW门禁、提交范围核验，结果见 [validation.md](validation.md)。
- **判据**：上述检查抓文档坏链接、失效源码引用、夹带改动、RT格式错误；不证明BM25、隔离或恢复已实现。产品破坏实验本轮未做，因为禁止改实现/测试。
- **AI评审**：本会话按四份methodology reference直接自查，无委派、无额外模型/API；不是独立开发验收。独立方案/开发复核留给接手Agent与用户。
- **读产出**：逐条对照本文件的范围、引用、版本/SHA/span、屏障与只读界限、降级、预算和交接。未读取真实业务raw、未人工核验生产gold。
- 对照成功标准：**设计交付可核验，产品成功标准尚未实施/未验收**。AODW仓库级花名册要求index与目录一致，而宪章要求立项main只提交meta、index在收口更新；遵宪章，本轮不为消告警修改共享index。另一会话已在main补登记051，但本worktree未同步，G109仅描述本分支快照；不得据此宣称全仓AODW/CI绿。

## 变更记录

- 2026-09-06：用户授权RT立项与代码开发前准备；完成当前规范与两仓实现调查，提出正文词法+元数据融合、段引文与代际恢复规格。未修改任何产品行为、配置、规范或运行数据。
- 当前没有用户可使用的新检索功能；接手入口见 [developer-handover.md](developer-handover.md)。

## 遗留事项

无已转出/已认领的DI。本目标未进入实现，不关闭RT，也不以转出遗留事项假装目标完成。dense检索、sheet正文、实时逐文档源权限属于明确非目标/待决策边界，不擅自登记到共享需求池。
