# RT-Lite: RT-054 - 大库词法融合检索快照/预计算性能治理

> profile: Spec-Lite | execution_mode: collaborative
> 唯一方案权威。仅诊断和规划；本文件不是实现授权。

## 方案（给人看）

- **做什么**：第二次独立评审已允许进入 P0 零写诊断；先用 P0 单变量分段计时找出 cwork-3m 融合搜索慢在哪里。P1b 仍是 **NO-GO**：只有数据、writer fence 和存储前提全部证明后，才可另过方案门实施「单一当前指针 + 不可变查询快照 + postings 驱动评分」。
- **为什么**：用户提供的生产观察是 cwork-3m（482 docs、66,500 chunks）在 `q=会议`、`lexical_fusion_v1`、`page_size=1` 下功能正确（`total=121`、generation `eabcb725…`），冷请求 425.685s、后续一笔 300s 超时；spbp（128 docs）也有 60s+。这些数字证明有严重问题，**尚不能证明 NAS 是首因**。静态代码还发现 BM25 当前会在每个 chunk 评分时重算全库 `avgdl` 并重分词 query，最坏近似 O(chunks²)，故快照而不改算法不足以达标。
- **推荐**：P0 先隔离测量；P0 的候选 postings 算法仅是离线/测试等价与计时基准，绝不接生产请求路径。只有 P0 数据、正式 writer 清单、FileStation 可恢复协议、物理 NAS 数值预算和容量实测经方案门批准后，P1b 才可采用单库单写者、stale+epoch fence、双 collect 校验和一代一缓存的查询快照。
- **代价**：写面必须统一经过 fence，builder 多一次一致性 collect 和额外不可变发布物；网关限定每库一代内存。代价换来可证明的换代、回滚、崩溃和拒绝旧结果，而非用 timeout 或猜测缓存掩盖问题。
- **这次故意不做什么**：不改产品代码、测试、存储实现、timeout、NAS/生产或部署；不改 RT-053；不在 P0 数据完成前锁定最终实现；不把 P1a 正文分页/每次完整正文 SHA 验证混入本 RT；不做跨库、向量、FTS5、网关持久 cache 或隐式 metadata 降级。
- **用户怎样算成功**：批准实现且完成验收后，真实大库的冷/热融合搜索在固定时间、请求数、字节和内存上限内完成；源换代、撤权、错误/回放快照、双 builder、OOM 前置拒绝都不会返回旧候选。document_ref、span/read SHA、only-current、授权先于 I/O 和 GET 零持久写保持不变。
- **建议**：**推荐**先批准 P0 诊断授权而非直接锁定 P1b 实现。P0 会在不改业务行为的前提下给出可比较分段数据；若它推翻网络首因假设，则按数据调整实现，不能拿本稿预设覆盖证据。

## 假设与现状

- 当前合并现实：`main`/`origin/main` 是 `09c8aff`，已合并 PR #7/RT-053；本 branch 的 merge HEAD 是 `d40e36b`（以 `09c8aff` 为父）。RT-053 代码和登记已在本分支基线中；本 RT 不修改它，也不依赖其未合并工作。
- 独立 Codex 评审裁决为 **GO-WITH-CHANGES**；本稿吸收其八项阻断意见。RT-051 的 P1a 保持独立范围：`read`/`inspect` 仍会读目标全文并复核 SHA；本 RT 的 search 不读取候选正文，也不因此声称解决 P1a 大文成本。
- 当前 `_v2_search` 读一次 raw-index；`_v2_load_lexical` 下载完整 lexical index 又读一次 raw-index，并解析整个 JSON。融合还全量扫描 metadata、对所有 chunks 调 BM25、为返回 hit 再扫描 chunks 取 span。builder `_collect` 才逐合格正文 read+SHA；search 本身不做候选正文 read/SHA。
- `LexicalIndex.avgdl` 每次求值都遍历 `chunk_lengths`；`bm25_rank` 对每个 chunk 调 `chunk_score`，`chunk_score` 又对同一 query 调 `tokenize`。因此在 N chunks 时，单 query 的 `avgdl` 路径为 N 次 × O(N)，近似 O(N²)，另有 N 次相同 tokenization；`best_spans` 还会重复评分。该结论来自静态代码路径，P0 要测其真实占比。
- gateway 是刻意的单线程 `HTTPServer`：单个 FileStation session 一次只服务一个请求。不得用并发共享 session 伪造吞吐改善；single-flight 仍须定义，保证未来内部加载路径、测试直调和两个连续冷请求不会重复下载/接纳未验证对象。
- RT-053 的 shared token 走同一个 `TokenFile`：非 admin token 每次 `authorize()` 都重读 token registry，现有测试证明撤销后的**下一请求** 401。P1b 的返回前再鉴权必须扩展为确定性并发点：评分暂停后 revoke 同一 `shared_kb` token，再恢复响应，必须 401 且没有 hit/body/span；不能把「下一请求」测试冒充为返回前撤权测试。
- 本轮不连接生产/NAS、不运行真实 builder/gateway，不报告任何新生产性能数字。

## P0：先测量，再决定

P0 是唯一可以进入实现前的诊断阶段；只可加入临时、零写、默认关闭的计时/计数装置并在授权环境运行。**P0 数据、原始样本、分段总和及结论未完成前，不得锁定最终 P1b 文件格式、阈值归因或实现选型。**

### 单变量、可复现实验

固定：同一 KB/current generation、同一 query、`page_size=1`、同一 token、同一 gateway 进程、无候选正文 read、无 timeout 改动。每个实验仅改变一项，并记录 monotonic clock 的以下段：

| 段 | 直接回答的问题 | 单变量对照 |
|---|---|---|
| auth + pointer/index 获取 | 授权或小元数据是否在阻塞 | 同请求仅替换已验证内存 bytes，不改变认证 |
| raw-index / lexical download | NAS payload、登录、重试、TLS 各占多少 | 同一已下载 bytes 做离线解析；保留逻辑与物理请求数/字节 |
| JSON + structure parse | 大对象反序列化是否首因 | 同 bytes、同机器、跳过网络 |
| metadata filter | 字段扫描是否显著 | 同已解析 index，固定 query |
| tokenize / BM25 / span | O(N²)、全 chunk 扫描和重复评分各占多少 | 同 index，对旧算法和候选 postings 算法逐段计时、比较逐项结果；候选算法仅离线/测试基准 |
| RRF / JSON encode | 输出层是否显著 | 固定候选/分数，只编码 |

每条记录必须包含 KB 匿名标识、generation、docs/chunks、query 类别、冷/热定义、阶段 elapsed、logical/physical requests 与 payload/wire bytes、重试次数、RSS 峰值和错误；不得记录原文、token、路径或 NAS 凭据。先做至少 5 次同条件全过的诊断样本；若要报告 P50/P95，样本数必须 **≥20**。P0 输出应能证伪「NAS 首因」及「O(N²) 首因」任一假设。

### Physical NAS 预算：P0 的固化方式

现有 FileStation `read()` 一次把 download bytes 交给调用方，session login 有进程内 SID 缓存，默认 `RetryPolicy.attempts=6`。因此 P0 不能只报一个总数，必须逐请求标记 `pointer|snapshot|legacy-index`、`login`、每个 retry attempt、download，分别记录 HTTP wire bytes（可观察时含 envelope/header）和业务 payload bytes。

P1b 之前先采用不可放宽的结构上限：pointer payload `<=64KiB`；冷态至多 1 次 pointer logical read、1 次 snapshot logical read、1 次 login 和每个逻辑操作至多 6 个 transport attempts；热态保持同一 backend SID 时至多 1 次 pointer logical read、0 snapshot/login，若 SID 丢失则归类为 reconnect/cold 样本而非热态。两态均为 0 raw-index、0 候选正文、0 gateway write。超出这些初始数量上限即 P0 记录为失败，不以延长 timeout 消化。

字节上限不能在没有真实 snapshot 尺寸时虚构：P0 对每库、每态各取不少于 20 个有效样本，按每一类别分别固化 `B = ceil(max(observed) * 1.25)` 的 wire 与 payload 字节数，保留所有异常/超限样本。P1b 方案门必须把 cwork-3m、spbp 的具体 B 值、样本数、连接/SID 状态、retry policy 和允许的物理调用表回填本 RT，并由用户批准；在那之前 P1b 没有可接受的 physical NAS 预算，保持 NO-GO。

### P0 实施门

仅当下列全部成立才可固定 P1b 设计：

1. 计时覆盖上述所有阶段，阶段和与端到端误差在 5% 或 50ms（取较大者）内；异常/超时单列，不能删样本。
2. 旧逻辑与离线重放结果在同 generation 下逐 hit 的 lineage、version、raw SHA、body/metadata rank、RRF、candidate span、total、排序完全一致；任何差异先解释或阻断。
3. 数据显示下载/解析或当前 O(N²) 之一对超时有实质贡献；若两者都不成立，停在方案门，带数据重选方案。
4. 存储读侧能在解析前证明大小上限/流式上限，正式 writer 清单已穷尽并逐一接入 epoch fence，且上节 numeric physical NAS budget 已由 P0 固化并获方案门批准；任一做不到，不启动 P1b。

## 实现备注（P0 通过后才生效）

### 1. 单一权威、digest 与不可变快照

`_system/lexical-query-current.v1.json` 是 P1b search 的**唯一** readiness/pointer 权威；旧 `lexical-readiness.json` 仅可作 libraries 展示，不能授权 search、不能作为 fallback。它有 `kb_code`、schema、`state: stale|ready`、单调 `epoch`、`query_corpus_digest`、generation、engine、snapshot locator、snapshot SHA、size/limits 与创建者状态。未知字段/版本、缺字段、跨库、非 ready、digest/epoch 不符全部 503 fail-closed。

`query_corpus_digest` 不是只对 `(lineage,version,SHA)` 哈希。它对**所有影响 search 可见结果**的规范化投影做 domain-separated SHA-256：资格状态、lineage、current version、raw SHA、artifact/readability/reason、title/display label/source、metadata haystack、每个 document raw size、chunk id/owner、byte span、term/posting/tf、候选顺序规则、coverage/excluded，以及其 schema/normalizer 版本。编码为 UTF-8 canonical JSON：对象键 Unicode code-point 升序、数组明确排序、整数十进制、字符串 NFC；metadata/title 与 query 使用同一标明版本的 Unicode normalize + case-fold 函数，禁止语言/locale 隐式差异。

`generation = SHA256("cwk.lexical-query.generation.v1\\x1f" + query_corpus_digest + "\\x1f" + engine + "\\x1f" + snapshot_schema)`。快照 locator 固定为 `_system/lexical-query/v1/<generation>.json`，不含其自身 SHA，避免循环哈希；pointer 另存 snapshot SHA。builder 只允许首次创建：locator 已存在但 bytes/SHA 不同立即拒绝，完全相同才幂等成功，绝不覆盖。snapshot 自身不写 pointer SHA；读取后计算 SHA 与 pointer 比对，才允许结构解析。

结构验证必须在评分前完成：schema/kb/generation/digest/engine 精确一致；所有 map key 唯一；每 posting 指向存在 chunk、term/tf 为正、chunk owner 属同一文档；`n_chunks`/预存 `avgdl` 与 chunk lengths 可复算；每 span `0 <= start < end <= raw_size`、同文档 raw SHA/version 一致、chunk 不跨文档；候选排序所需字段完整。缺、重复、截断、错库、错 SHA、回放旧 epoch、指针指向另一代或结构不自洽一律拒绝，不以旧 cache、raw-index 或 metadata 路补答。

### 2. 正式 writer 枚举、单库写者、S0/S1 与 epoch fence

本节是 `d40e36b` 的静态清单，不把测试直写、读侧 gateway/wizard、token access-file import 或「可能存在的 repair」冒充生产 writer。`git grep` 的固定路径结果如下；P1b 实现前必须再用同样的全仓扫描和动态目标审计复核，发现任何新 writer 都是阻断项。

| 路径/入口 | 对 raw-index 的事实 | P1b fence/Q39 义务 |
|---|---|---|
| `kb_create.create_library()` | 初建树通过固定文件表写空 `_system/raw-index.json` | 新库只能是 pointer missing/stale；第一次 builder ready 前 search 503。Q39：创建后不得从残留/跨库 cache 返回 hit。 |
| `kb_ingest.execute_plan()` → `Accounts.publish()` | **实际运行时 writer**。当前没有 `Accounts.commit()` 方法；`publish()` 先备份 `raw-index.prev`，再写 raw-index、provenance、ingest-state；它逐 item、失败状态和批次收尾都会被调用 | 仅当 raw-index query projection 将改变时，在第一次 raw/index 变更前推进 stale epoch；新件、升版/重分类导致的 current-row 变化各一例 Q39：下一 search 无重启 503，重建后只见新 SHA/version。失败状态但 raw-index projection 不变须证明不误推进或误报。 |
| `refresh_library(..., apply=True)` | 不是独立 writer；它调用 `execute_plan()`，故继承上一行 | cwork/docdb refresh 各做一例 Q39，证明没有绕过 `Accounts.publish()` 的 fence。 |
| `kb_migrate` 的通用 tree copy | 不引用常量，但可把源树的动态 `target`（包括 raw-index）写入 destination；它是否能迁移到被 gateway 服务的 live KB 尚未被本轮证明 | **未穷尽阻断**：P1b 前要么在迁移目的地接入同一 fence/Q39，要么以代码级 guard 证明 live KB 不可作为 destination；不能仅靠操作约定。 |
| `reconcile_coverage`、`kb_wizard`、`kb_access_file.import_access`、`kb_lexical_builder`、`save_refresh_state` | 当前静态路径分别为只读、读侧、token 本地文件、词法发布物、refresh-state；不写 raw-index | 维持非 writer；新增 raw-index 写入即加入本表、fence 和 Q39 后才可合入 P1b。 |

现有 `StorageBackend`/FileStation 只有单对象 `write`（覆盖语义）和重试，没有多对象事务、create-if-absent 锁或 CAS。因而「ledger 保护的锁」不是已实现事实。P1b 前必须先证明一种可恢复的单库排他原语（持久 owner/epoch/lease、不可安全接管即拒绝）或获得受控外部单写者服务；否则双 builder/source writer 下的正确 stale fence 无法证明，P1b 保持 NO-GO。

可接受的无多对象事务恢复顺序是：先取得已证明的单库锁 → 读取 current pointer/epoch → 单独把 pointer 写为 `stale(epoch+1, writer-id)` 并确认读回 → 写 raw artifact/index（每对象原子语义）及现有 accounts/manifest → 释放给 builder。任一点崩溃、读回不一致或锁丢失都保持 stale；builder 不可把旧 ready 当恢复。builder 在同锁下 collect S0（逐件流式 SHA）→ 写且回读校验 immutable snapshot → collect S1；仅 S0=S1、pointer 仍是自己的 stale epoch、锁仍有效时才把 pointer 写 ready。第二次 collect 的 I/O/bytes/elapsed 必须单列。

gateway 在授权后读取 P0 ready pointer；在评分**前**再次读取/比较 P1，在返回前读取 P2 并重新调用目标库授权。P0=P1=P2、epoch/digest/generation/snapshot SHA 均相同，且 return-time authorization 成功才返回；任一变化、shared token revoke 或鉴权失败都丢弃计算结果。最后核验只准许尚未发送的响应，不能撤回已送字节。

### 3. 快照算法与等价性

快照预存 `n_chunks`、`sum_chunk_lengths`、`avgdl`、query-visible metadata projection、倒排 `term -> postings` 和每 posting 所需 document/chunk/span/tf。每请求：query 仅 tokenize 一次；对去重 query terms 查 postings；只为命中的 chunk 累计 BM25，按 document 取 best score；每 `(query,chunk)` 只算一次并复用给 rank 和 `best_spans`；metadata 仍按同一规范化 projection 计算；RRF/candidate_k/tie-break 与旧合同不变。空 term/posting 正确返回 0，不扫描全 chunks。

新旧等价测试用同一脱敏 corpus 与固定 query matrix，逐字段比较，包含 CJK 1/2/3-gram、ASCII、空查询拒绝、无答案、tie、重复词、正文-only、metadata-only、融合、最多三 non-overlap spans、candidate_k 截断和跨库隔离。性能优化若改变任一既有合法结果，先修等价性，不以更快为通过。

### 4. 缓存、下载与资源边界

服务器当前是单线程；加载器仍做每 `(kb,epoch,generation,snapshot_sha)` single-flight，第二个冷请求只等待同一个已开始的验证，不再启动下载。只有完整下载/流式 SHA、大小限制、解析、结构校验、P1 指针比较都成功后才可入缓存；失败对象绝不缓存。每 KB 同时仅保留 current 一代；pointer 前进时先退休旧代并释放，再可装入新代，跨库缓存键永远含 kb_code。

硬限制须在分配/解析**之前**执行：pointer max bytes、snapshot compressed/wire bytes、decoded bytes、docs/chunks/postings、单 query postings work、单库已验证缓存和进程总缓存均有常量上限。读取采用 StorageBackend 新增的只读受限/流式读合同：先取可信大小元数据或以 `max_bytes+1` 流式计数，超限中止；同时流式 SHA，禁止 `read()->bytes` 后才检查长度。builder 正文校验也走流式 SHA；P1a endpoint 语义仍独立，不能借本 RT 静默改变它。

256/512/64MiB 不是脱离数据的通过宣言。P0 必须实测 cwork-3m snapshot 的 wire、decoded、parse 工作区和 verified-cache 尺寸，并在 P1b 方案门把它们代入「wire + decoded + parser workspace + current cache」预算；只有证明每库 `<=256MiB`、进程 `<=512MiB`、单请求额外 `<=64MiB` 才可固定这些容量门。任一不满足即回方案门重定格式/预算，不得靠 OOM 后逐出或偷偷放宽；实现后仍必须在下载/解析前 503 `capacity_exceeded`。单线程并不豁免此硬门。

### 5. 回滚与恢复

**代码回滚**：先停止把新 gateway binary 投入流量；旧 binary 只走其已支持的 legacy 词法路径，不能解释 P1b pointer 就必须显式 lexical unavailable，不能把未知快照当旧索引。**指针回滚**：仅在单库写锁内，目标 immutable snapshot 的 SHA/schema/kb/engine 与当前 raw `query_corpus_digest` 完全一致时，才可把 ready pointer 切回；否则保持 stale 并重建。回滚也增加 epoch，禁止 replay 旧 ready pointer。崩溃恢复从 pointer+snapshot 验证开始；stale/half-written/staging 无一可响应。

## 可执行验收合同（P1b）

### 口径与性能门

冷态 = 新建 GatewayApp、该 KB 的 pointer/snapshot 缓存为零、无 in-flight、同一已 ready generation；报告 NAS/OS cache 与连接复用状态，不能把它叫“物理冷盘”。热态 = 同进程同 KB 已验证 current snapshot，仍每次读/复核 pointer。连续 **5 次独立冷态必须全部**低于阈值，故不报告 P95；热态与 P0 若报告 P95，样本 **≥20**（本方案用 30）。

| 面 | 通过条件 |
|---|---|
| cwork-3m | 冷态 5/5 `≤10s`；热态 30 次 P95 `≤2s`、max `≤3s`；`会议` 仍 total=121，generation 与 current pointer 一致。P0 若证明此阈值不可行，先回方案门，不加 timeout。 |
| spbp | 冷态 5/5 `≤5s`；热态 30 次 P95 `≤1s`；如实记载 generation/total。 |
| logical NAS | cold ≤2 logical StorageBackend reads（pointer+snapshot），hot ≤1（pointer）；两者均 0 raw-index、0 候选正文、0 gateway writes。 |
| physical NAS / bytes | P0 先按「pointer/snapshot/login/retry/download × wire/payload」分列 ≥20 次 cold/hot，计算每类别 `ceil(max*1.25)`；将 cwork/spbp 的**数字**和连接/SID 状态回填、经方案门批准后才成为 P1b budget。此前 P1b NO-GO；初始数量硬门见 P0 节。 |
| 内存 | 满足每库/进程/请求硬门；以 RSS 与受控对象计量双报。故意构造临界大 snapshot 必须在 OOM 前 503，缓存、指针、文件与 ledger 均不变。 |

### 行为、故障和突变门

- builder 两次 collect 的文档数、SHA、digest 一致才 ready；第二次额外 NAS I/O/bytes/elapsed 明细必报。测试双 builder、source 在两 collect 中换代、builder 崩溃、锁失效，均只见 stale 或完整 ready。
- 正式把 RT-051 Q39 类源升级场景升级为上表逐 writer 的 epoch-fence 判据：create、`Accounts.publish()` 新件/升版、refresh cwork/docdb、以及已解封的 migration destination 各自突变后，next search 无重启 stale；新 builder ready 后只给新 version/SHA。枚举不全、动态 migration 目的地未守卫或漏接 writer 必红、P1b NO-GO。
- 双冷请求验证 single-flight 只有一份 snapshot download；跨库相同 generation/SHA 不得命中彼此 cache；旧 pointer/snapshot/replay、截断、hash 篡改、错误 locator、错 schema、span 越界、重复 posting 全 fail-closed。
- 评分前/返回前分别突变 pointer、source、token/撤权；尤其 RT-053 `shared_kb` token 必须在评分暂停后 revoke，恢复时 401 且不得输出 hit。hidden backend、无 token、跨库 token 的 I/O trap 证明授权先于任何 pointer/snapshot/raw/body I/O。
- WriteTrap 证明全部 GET 与 cache hit/miss 零持久写；候选正文 read trap 证明 search 从不读正文；无 lexical 时只有显式既有 metadata 请求可走 metadata，`lexical_fusion_v1` 不隐式降级。
- 新旧算法等价矩阵、既有 RT-051/052/存储回归、`make ci` 全绿；每项关键判据做真实行为突变（断掉 pointer 复核、恢复全量扫描、缓存未验证对象、移除返回前鉴权、允许覆写 locator）并确认变红后还原。
- 读产出：人工核 cwork/spbp 的 total、generation、排序、version/SHA、span 和 P0 分段原始记录；测试绿灯不替代真实性能/安全读数。

## 风险与不选方案

| 方案 | 不选原因 |
|---|---|
| 增大 300s timeout | 不减少任何请求、字节、O(N²) 或队头阻塞，只把故障拖长。 |
| 仅 cache 当前 JSON | 不解决冷态或 O(N²)；不加 epoch/fence 会服务旧 generation。 |
| 仅做快照、不改评分 | 仍反复全量 `avgdl`/tokenize/chunk 扫描，P0 已要求量化并阻断。 |
| search 逐候选读正文/SHA | 违反本 RT 边界并增加 NAS 往返；正文证据仍只走 P1a read 合同。 |
| 多线程共享 FileStation 或网关写磁盘 cache | 破坏单 session/GET-only 边界且扩大状态面，不能当性能捷径。 |
| FTS5/向量 | 新引擎、格式、部署和回滚面超出最小 P1b；除非 P0+P1b 证据失败才另立决定。 |

## 验证

- **本轮已完成**：第二次独立评审 GO-WITH-CHANGES 后，仅修订 P0/P1b 方案；复核 `09c8aff` 已合并的 RT-053 token 路、当前 query/builder/storage、raw-index 静态写者和单线程 server。没有改产品代码/测试，未调用生产、NAS、真实 token、builder 或 gateway。
- **当前授权边界**：P0 零写诊断获允许；P1b 仍 NO-GO，必须先通过 P0 数据、numeric physical budget、容量实测、writer 穷尽与 FileStation 排他/恢复原语方案门。任何未完成数据只能标未测，不能称性能已治理。
- **AI 评审**：实现收口前独立复核所有 writer 是否接入 fence、回滚是否能重放旧代、指针是否真为单一权威、限额是否在下载/解析前、性能判据能否被 cache/timeout 假绿。

## 变更记录

- 2026-09-07：创建方案稿；无产品行为变化、无部署。
- 2026-09-07：吸收独立 Codex GO-WITH-CHANGES 八项阻断意见：P0 单变量测量、O(N²) 修复、单一 pointer/epoch、双 collect、不可变快照、single-flight/限额、受限流式读和可执行回滚验收。仍等待方案门。
- 2026-09-08：第二次独立评审后更新合并基线（main `09c8aff`、branch merge `d40e36b`）；P0 获准、P1b 保持 NO-GO。补入 RT-053 shared-token 返回前撤权、正式/动态 writer 审计、FileStation 无事务恢复前提和 physical NAS 数值预算固化门。

## 遗留事项

- P1a 正文分页每请求完整下载/SHA 的传输优化保持独立；本 RT 不让 search 读取候选正文，也不宣称消除此成本。
- P0 若证明瓶颈不是网络、解析或现有评分，或 fence/受限流式读不能落地，带原始数据回方案门；不得预先登记尚未创建的产品文件或 ownership。
