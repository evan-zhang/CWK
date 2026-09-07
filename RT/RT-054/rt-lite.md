# RT-Lite: RT-054 - 大库词法融合检索快照/预计算性能治理

> profile: Spec-Lite | execution_mode: collaborative
> 唯一方案权威。仅诊断和规划；本文件不是实现授权。

## 方案（给人看）

- **做什么**：把大库的正文词法融合检索改为「发布时预计算、查询时只核对小型当前指针并使用已验证快照」。cwork-3m 的 482 件、66,500 chunks 在 `q=会议`、`lexical_fusion_v1`、`page_size=1` 下仍返回正确的 121 条和 generation `eabcb725…`，但冷请求已达 425.685 秒、后续一笔在 300 秒超时；spbp 的 128 件也已知超过 60 秒。本 RT 要消除这条查询路径的重复 NAS 大对象传输，不靠加长 timeout。
- **为什么**：现状每次融合搜索都会下载整个词法索引、再次读取完整 raw-index，并把大 JSON 还原为 Python 索引后扫描全部 chunks。大库慢的主因是这条同步全量 NAS 读取与反序列化/全量评分路径；不是搜索时逐候选读正文或逐候选 SHA 复核。后两者在 builder 和后续 `read`/`inspect` 路径发生，仍有各自的 P1b 问题但不能被误归为本次 425 秒。
- **推荐**：新增一个由现有可信写面发布的、不可变且带 SHA 的查询快照，以及一个很小的「当前词法指针」。搜索在授权之后只读指针；指针确认 source corpus digest 与 generation 同代才允许用内存中已验 SHA 的快照。冷态下载指针和一份快照，热态只读指针；绝不回读 raw-index、候选正文或旧 generation 补救。
- **代价**：发布/摄取写面需原子维护指针失效与重新就绪；每个已热库占受控内存，builder 的发布物多一份。换来的不是缓存猜测：源换代会立即令指针不匹配，下一次搜索 fail-closed，重建完成后无需重启网关即可见新代。
- **这次故意不做什么**：不改产品代码、测试、存储协议、timeout、NAS/生产或部署；不改 RT-053；不把 P1a 的正文分页/全件 SHA 验证一并重构；不做跨库搜索、向量索引、FTS5、压缩格式或后台网关写缓存。
- **用户怎样算成功**：批准后，cwork-3m 的融合搜索从数分钟级变为有固定上限的冷/热请求，且旧代、损坏快照、撤权、未授权库、源换代、错 SHA 都宁可失败也不返回错误候选；原有 span/read、仅当前版和 GET 零写语义不变。
- **建议**：**推荐**先实施这一最小 P1b。它直接去掉每次查询的重复大对象 NAS 往返，同时复用 RT-051 的 generation、corpus digest、文档身份和 builder 信任边界；若真实基准仍不能过阈值，再用数据决定是否做索引格式实验。

## 假设与现状

- 基线是 `origin/main` 的 `0033b26`（2026-09-07 本地 refs）；本分支从它创建。RT-053 是平行未合并工作，未作为本 RT 的事实或依赖。
- 已按要求阅读 RT-051 的方案、证据、验收和 A11 独立复核，以及 RT-052 方案与 Codex 的 GO-WITH-CHANGES 评审。RT-052 已确立：readiness 小投影不得以加载 15MB+ 词法索引冒充轻量读取，且授权域必须先过滤才可发生 NAS I/O。它是设计证据，不使 RT-052 成为本实现前置。
- RT-051 已诚实记录 P1a：`read` 逐请求读完整正文并复核 SHA，P1b 快照/传输有界顺延；它也把 A04 大档与 A09 累计带宽留为后续工作。当前 builder 在发布时逐合格件读取正文、核对 raw SHA、构建 generation/corpus digest，并已原子写 lexical index 与小型 readiness。
- 生产数值由用户提供，未在本轮连接生产/NAS复测：cwork-3m 482 docs、66,500 chunks；正确结果 `total=121`、generation `eabcb725…`；冷 425.685s，后续一笔 300s 超时；spbp 128 docs、60s+。因此路径归因有源码证据，425 秒中网络传输、JSON 解析与评分的精确秒数仍待实现阶段仪表验证。

### 静态成本模型（一次 `GET /v2/kb/search?...retrieval_mode=lexical_fusion_v1&page_size=1`）

| 路径 | NAS `read` / 传输 | CPU/内存 | 是否是候选正文 SHA |
|---|---:|---|---|
| `_v2_search` 预检 | 1×完整 raw-index | 解析 index、第一次 metadata 子串扫全部条目 | 否 |
| `_v2_load_lexical` | 1×完整 lexical-index，随后再 1×完整 raw-index | JSON 解析、反序列化 postings/chunk maps、重算资格域 digest | 否 |
| `_v2_fusion_payload` | 0×正文 | eligibility/metadata 再扫；BM25 对全部 chunks 评分；`page_size=1` 仍为命中文档扫描 chunks 取 spans；最多各 100 候选再去重/RRF/排序 | 否 |
| builder publish | 每一合格文档 1×正文 | 全件 SHA、UTF-8/body/chunk/BM25 建代 | **是，发布时** |
| `read` / `inspect` | raw-index + 每次目标正文完整下载 | 全件 SHA；`read` 再切 span/JSON | **是，读取时** |

结论：单次融合搜索至少有 3 次完整元数据/索引下载；FileStation 的登录、重试与 TLS 是这些 `read` 的附加请求，具体次数取决于连接状态。候选去重上限仅 200 个，不能解释 425 秒；全量 chunks 评分/反序列化会放大耗时，但在没有逐段时间计数前，不把它伪称为已量出的首因。首因是每次请求同步拉取大型词法发布物（并重复 raw-index），这也是唯一能同时解释冷慢、后续仍超时和 RT-052 15MB+ 警告的代码路径。

## 实现备注（用户不问可不展开）

### P1b 最小架构

1. **源当前态（写面）**：所有会改变 raw-index 资格域的既有受控 source publish，在同一账本/clean-fence 提交中先使 `lexical-query-current` 变为 `stale`，并写入新的 `source_corpus_digest`/source revision；不能原子证明此关系就不发布为 searchable。读面遇 missing、corrupt、stale、未知 schema 或不匹配一律 `503 lexical_unavailable`，不得回退读取 raw-index 或旧 snapshot。
2. **builder（唯一预计算者）**：沿用现有逐件正文读取与 raw SHA 复核；成功时一次生成不可变 `lexical-query-snapshot-<generation>.json`。它把当前融合搜索必需的资格域文档投影（lineage、version、raw SHA、status、metadata haystack）和 lexical postings/chunk byte spans 放在同一 generation 内，并记录 `snapshot_sha256`、`corpus_digest`、engine、coverage/excluded 及 schema。快照内容 SHA 不符、字段不齐或 generation/digest 不自洽即拒用。
3. **原子可见性**：先写并核对快照，再通过既有 ledger/manifest 让小型 `lexical-query-current.json` 从 stale 切到 ready；指针仅包含 schema、source digest/revision、generation、snapshot relative id、snapshot SHA、engine/coverage。崩溃只能看见 stale 或一整套 ready，不能看见半快照。旧 snapshot 仅按 maintenance 留存/回收，网关从不写它。
4. **gateway GET 路径**：先完成 token/库授权，再读一个小指针；用它同当前授权库绑定并检验 `ready`、source digest、generation。内存缓存键为 `(kb, generation, snapshot_sha256)`；未命中才读一次 snapshot、计算 SHA、严格解析并缓存。热命中不跳过指针检查。响应仍由快照中的同版 lineage/version/raw SHA 签发 document_ref，仍返回现有 raw absolute spans；正文引文继续走现有 read 合同。
5. **换代和 fail-closed**：source publish 后下一请求不重启也读到 stale 指针并拒绝；builder 成功发布新指针后下一请求装载新 generation。缓存绝不因 TTL、旧 generation 或“上次能读”通过。候选 SHA 的可证性来自 builder 已逐件复核的快照 provenance；实际正文 read 仍按其当前 P1a 合同再次验证，因此不会把搜索快照当正文读取授权。

### 不可破坏的合同

- only-current-version：snapshot 行和 document_ref 必须是 source pointer 所指的同一 current corpus；历史 version、过期/旧 generation 都不响应。
- stale：每次搜索先查 current 指针；source digest/generation/snapshot SHA/schema 任一不符即 fail-closed，响应不含旧 hits、标题、span 或正文。
- 授权先于 I/O：未授权 token、跨库 ref、隐藏库在读指针、snapshot、raw-index、正文前就拒绝；测试须以「一读即抛」backend 证明。
- GET-only 零写：gateway 不建目录、不写 cache、WAL、指针、快照、ledger 或 manifest；缓存只在进程内。builder/ingest 是显式写面。
- span/read：snapshot 保存 raw 绝对 byte span；span 必须落在同一 `(kb,lineage,version,raw_sha)`，随后仍由现有 UTF-8 边界、cursor、full-SHA read 路验证。搜索不返回缓存 snippet，不扩大正文。
- 资源：解析前限制指针和 snapshot 的 bytes/schema/entries/chunks/postings；超限/内存预算不足返回可诊断的 503，不降级到原始 NAS 扫描。

### 量化验收合同（实现后，以真实 cwork-3m、spbp 和脱敏 fakeNAS 分别证明）

| 面 | 条件与阈值 |
|---|---|
| cwork-3m 冷搜索 | 清空该库进程内缓存后，`q=会议, lexical_fusion_v1, page_size=1` 连续 5 次独立冷态：每次 ≤10s，P95 ≤10s；结果仍 `total=121`、generation 与当前指针一致。不得通过提高客户端/服务端 timeout 达标。 |
| cwork-3m 热搜索 | 同一已验证 generation 连续 30 次：P95 ≤2s、最大值 ≤3s；结果/排序、span、total 与冷态一致。 |
| spbp | 同口径冷态 P95 ≤5s、热态 P95 ≤1s；如实记录其当前 generation/total，不拿 cwork 结果代替。 |
| NAS 读取 | 每次冷态最多 2 个 FileStation download：1×小 current 指针 + 1×snapshot；热态最多 1×小指针。两种状态均为 0×raw-index、0×候选正文、0×写请求；登录/重试独立计数且无无限重试。 |
| 内存 | cwork 热缓存的该库增量 RSS ≤256MiB；单请求峰值相对热基线额外 ≤64MiB。越界必须拒绝/逐出已验证旧缓存，不能无界增长。 |
| 无重启换代 | source 资格域换代后，已运行网关的下一搜索 503 stale；新 snapshot 发布后无需重启即只返回新 generation，旧 hit/old raw SHA 为 0。 |
| 回归/突变 | 既有 RT-051/052/存储全回归与 `make ci` 绿；新增 fakeNAS 计数、WriteTrap、hidden-backend、损坏/截断/snapshot-SHA、pointer stale、source digest、generation、授权、span/UTF-8、内存上限测试。逐一破坏「不读 pointer」「接受 SHA 不符」「stale 时返回缓存」「授权后置」「GET 写入」应红，恢复后绿。 |

性能计时必须把 pointer 下载、snapshot 下载、JSON 校验/解析、BM25、metadata、span、认证、FileStation login/retry 分段上报；这样可证伪本次归因，而不是用一个总耗时掩盖未解决的部分。真实生产基准只在另获授权后运行，不在本 RT 文档阶段执行。

### 备选与不选原因

| 方案 | 不选原因 |
|---|---|
| 把 timeout 从 300s 加大 | 只掩盖同步全量读取；不会减少 NAS 请求、超时堆积或 stale 风险。 |
| 每次搜索继续读 raw-index + lexical index，只加 Python 缓存 | 冷态仍差，且若不每次验证 current digest 会服务旧代；若每次读两个大文件则没有达成请求数目标。 |
| 查询时逐候选读取/复核正文 | 与现有 search 路径不符且会把小 `page_size` 变成更多 NAS 往返；完整性应在 builder snapshot 和 read 合同分别保证。 |
| 只信 readiness，不让 source publish 使其失效 | source 更新后会将旧 generation 当 current，违反 stale/only-current。 |
| FTS5/向量/网关持久 cache | 引入新引擎、写权限、部署/回滚和更大安全面；没有先证明现有预计算快照不能达标，不在最小 P1b 范围。 |

## 验证

- **本轮已完成**：静态代码/RT/测试调查；基线与并行 worktree 检查；本 RT 的 YAML/治理门禁。未调用生产、NAS、真实 token、builder 或网关，也未跑产品测试。
- **方案批准后的判据**：上述三类真实/脱敏基准、FileStation 读取计量、突变测试与全 CI；每条判据要能抓住真实坏行为，不能只断言文案。
- **AI 评审**：实现完成后请独立评审聚焦「指针失效是否覆盖全部 source publish、快照是否会泄漏旧候选、授权是否真的先于任何 I/O、性能判据能否被 timeout/缓存假绿」。
- **读产出**：人工核对 cwork-3m 的 `会议` 命中 121、generation、候选 version/SHA/span 和换代前后拒绝/恢复记录；性能数字要保留分段原始测量。

## 变更记录

- 2026-09-07：创建方案稿。没有产品行为变化、没有部署；等待用户通过方案门后才可实现。

## 遗留事项

- P1a 正文分页每请求完整下载与 SHA 的传输优化仍是独立问题；本 RT 只确保融合搜索不额外读取候选正文，不能声称已解决 read/inspect 的大文成本。
- 若快照的真实 cwork-3m 冷/热阈值仍失败，保留分段证据后再决定格式/FTS 实验，不预先登记未创建的产品文件或 ownership。
