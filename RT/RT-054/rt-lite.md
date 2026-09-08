# RT-Lite: RT-054 - 大库词法融合检索快照/预计算性能治理

> profile: Spec-Lite | execution_mode: collaborative
> 唯一方案权威。受限读取已实现并经本地 fake 验证；独立复审随后发现六项实现阻断，本轮已返修。P0 尚未完成，P1b 仍不是实现授权。

## 方案（给人看）

- **做什么**：采纳独立复审的六项返修阻断：完成并以本地 fake 验证 `StorageBackend`/FileStation 的受限读取合同，包含真实 raw streaming、严格字节预算、统一 deadline/attempt、取消脱敏与全路径关闭。三层 P0 方向成立、但 P0 未完成，P1b 仍是 **NO-GO**。
- **为什么**：用户提供的生产观察是 cwork-3m（482 docs、66,500 chunks）在 `q=会议`、`lexical_fusion_v1`、`page_size=1` 下功能正确（`total=121`、generation `eabcb725…`），冷请求 425.685s、后续一笔 300s 超时；spbp（128 docs）也有 60s+。这些数字证明有严重问题，**尚不能证明 NAS 是首因**。静态代码还发现 BM25 当前会在每个 chunk 评分时重算全库 `avgdl` 并重分词 query，最坏近似 O(chunks²)，故快照而不改算法不足以达标。
- **推荐**：先完成受限读取合同；P0 的候选 postings 算法仅是离线/测试等价与计时基准，绝不接生产请求路径。只有该合同、本地验证和独立评审完成后，才可另行批准一库一次有界 P0 pilot；只有 P0 数据、正式 writer 清单、FileStation 可恢复协议、物理 NAS 数值预算和容量实测经方案门批准后，P1b 才可采用单库单写者、stale+epoch fence、双 collect 校验和一代一缓存的查询快照。
- **代价**：写面必须统一经过 fence，builder 多一次一致性 collect 和额外不可变发布物；网关限定每库一代内存。代价换来可证明的换代、回滚、崩溃和拒绝旧结果，而非用 timeout 或猜测缓存掩盖问题。
- **这次故意不做什么**：不改业务检索行为、NAS/生产或部署；不改 RT-053；不在 P0 数据完成前锁定最终实现；不把 P1a 正文分页/每次完整正文 SHA 验证混入本 RT；不做跨库、向量、FTS5、网关持久 cache 或隐式 metadata 降级；不接 gateway/builder/pointer/cache/writer/search，也不解决 writer fence/CAS/migration、pointer/epoch/cache 或算法替换。P1b 继续 **NO-GO**。
- **用户怎样算成功**：批准实现且完成验收后，真实大库的冷/热融合搜索在固定时间、请求数、字节和内存上限内完成；源换代、撤权、错误/回放快照、双 builder、OOM 前置拒绝都不会返回旧候选。document_ref、span/read SHA、only-current、授权先于 I/O 和 GET 零持久写保持不变。
- **建议**：**推荐**先只实施、独立评审并本地验证受限读取合同，而非进行再次下载或锁定 P1b。合同通过后才能安全取得可比较分段数据；若数据推翻网络首因假设，则按数据调整实现，不能拿本稿预设覆盖证据。

## 假设与现状

- 当前合并现实：`main`/`origin/main` 是 `09c8aff`，已合并 PR #7/RT-053；本 branch 的 merge HEAD 是 `d40e36b`（以 `09c8aff` 为父）。RT-053 代码和登记已在本分支基线中；本 RT 不修改它，也不依赖其未合并工作。
- 独立 Codex 评审裁决为 **GO-WITH-CHANGES**；本稿吸收其八项阻断意见。RT-051 的 P1a 保持独立范围：`read`/`inspect` 仍会读目标全文并复核 SHA；本 RT 的 search 不读取候选正文，也不因此声称解决 P1a 大文成本。
- 当前 `_v2_search` 读一次 raw-index；`_v2_load_lexical` 下载完整 lexical index 又读一次 raw-index，并解析整个 JSON。融合还全量扫描 metadata、对所有 chunks 调 BM25、为返回 hit 再扫描 chunks 取 span。builder `_collect` 才逐合格正文 read+SHA；search 本身不做候选正文 read/SHA。
- `LexicalIndex.avgdl` 每次求值都遍历 `chunk_lengths`；`bm25_rank` 对每个 chunk 调 `chunk_score`，`chunk_score` 又对同一 query 调 `tokenize`。因此在 N chunks 时，单 query 的 `avgdl` 路径为 N 次 × O(N)，近似 O(N²)，另有 N 次相同 tokenization；`best_spans` 还会重复评分。该结论来自静态代码路径，P0 要测其真实占比。
- gateway 是刻意的单线程 `HTTPServer`：单个 FileStation session 一次只服务一个请求。不得用并发共享 session 伪造吞吐改善；single-flight 仍须定义，保证未来内部加载路径、测试直调和两个连续冷请求不会重复下载/接纳未验证对象。
- RT-053 的 shared token 走同一个 `TokenFile`：非 admin token 每次 `authorize()` 都重读 token registry，现有测试证明撤销后的**下一请求** 401。P1b 的返回前再鉴权必须扩展为确定性并发点：评分暂停后 revoke 同一 `shared_kb` token，再恢复响应，必须 401 且没有 hit/body/span；不能把「下一请求」测试冒充为返回前撤权测试。
- 本轮不连接生产/NAS、不运行真实 builder/gateway，不报告任何新生产性能数字。

## P0：三层安全采样，先测量再决定

P0 仍是唯一可做的实现前诊断。2026-09-08 的唯一 cold 样本已证明 legacy payload 和 RSS 远超原先的 256/512/64 MiB 假设，并在 BM25/span 前以 503 结束；它不能从一个失败样本证明成功路径的根因。任何再次取得约 1.5GB 对象前，必须先完成下列受限读取合同的独立评审和本地验证；P1b 继续 **NO-GO**。

记录只保留匿名 KB、generation 后缀、大小桶/结构摘要、阶段 self/total、RSS、白名单错误和 transport attempt 汇总；不记录正文、query、token、URL、路径、身份、凭据或异常 message。`urllib` 不能可靠提供 HTTP framing/header wire bytes，字段必须为 unknown，绝不拿 payload 估算。

### A. 网络层：有界真实 pilot

- 每次只允许一个预先批准的 logical read；每个 logical read 最多 6 个 transport attempts，并设 attempt、payload、总耗时和 RSS 的安全停止门。任一门触发立即停止该 request，不追热态、第二库或 20 次重下载。
- FileStation observer 逐类（login/API/download）记录 `attempt/success/error` count、成功 payload 的 min/max/total，以及 retry ordinal 和固定原因类别；它不记录 request identity。旧 evidence 只能称为 **6 个无法解释的 download events**，不能称 six attempts，也不能判断为六次重试、分块或重复对象下载。
- 网络层只报告单次/少量原始样本、冷/连接状态、错误率和上限命中；样本不足 20 时禁止 P50/P95。成功 response 才可以把 bytes 交给 B，且只在一次受控取得中发生。

### B. 解析/结构层：隔离重复测量

- 对 A 的单次受控 bytes，在无网络、无 gateway、无持久化的隔离进程中重复/分层测量 JSON decode、结构恢复、metadata 与 legacy parse 工作区。bytes 只保留在受控进程内存，子进程退出即释放；日志仅落结构摘要和统计量。
- 启动前设置 wall-time、RSS 和解析输入大小门。若单次 bytes 已超过门，安全中止，只输出大小/结构摘要与中止原因，不强行 decode，也不把数据落盘。B 可做至少 20 次独立进程试验后才报告分位数。

### C. 算法层：脱敏 shape-equivalent corpus

- 用脱敏的 docs/chunks/lengths/postings/term-frequency/tie 结构生成 corpus，对 legacy 与 postings scorer 做至少 20 次对照，逐项比较 rank/span，并报告分位数。它绝不读取 NAS、真实 KB、token 或 gateway。
- 这只能证明同 shape、同算法原语的行为/成本，不替代真实内容、Unicode/token 分布、端到端 fusion 或真实 payload 的等价；完整成功路径仍要单列判据。

### P0 安全前置：受限读取合同（已实施；仅本地 fake 验证）

这是 P0 安全采样的前置，不是 P1b 的解锁，也不改变既有 `read(path) -> bytes` 的默认行为。三种后端的旧 `read` 仍返回完整 `bytes`；bounded-read 独立使用 raw-response seam：未 pin 为真实 `urlopen` response，pin 为同一已验证 `HTTPSConnection` 的 response，均逐块读取并关闭，绝不以旧 `read/_download` 回退或读完后截断冒充上限。

- **唯一 API（不承诺 iterator）**：新增且显式 opt-in 的唯一入口固定为 `read_bounded(path, *, max_bytes, chunk_size, deadline, cancel, expected_sha256, on_chunk) -> BoundedReadReceipt`。它不返回 iterator，也没有第二个可替换流接口。实现先验证安全相对路径与参数，逐 chunk 增量 SHA-256；`expected_sha256` 是对象期望摘要。若没有同对象/同版本绑定的可信 length/version，调用者**必须**提供它，否则以 `bounded_read_unavailable` 拒绝。`on_chunk(chunk)` 的 bytes 只在该 callback 调用期间有效；consumer 不得保留引用，若要在 callback 后使用必须自行明确复制，且该复制及其内存不归 backend receipt 管控。callback 正常返回才表示接受该 chunk；抛异常则立即关闭、不给 receipt、以固定 `consumer_failed` 失败（不回显异常）。成功返回完整 receipt；所有失败/取消均抛 `BoundedReadError(code)`，不返回 partial receipt。
- **固定的脱敏错误和 receipt**：唯一对外 code enum 是 `not_found | bounded_read_unavailable | capacity_exceeded | integrity_mismatch | incomplete_stream | deadline_exceeded | cancelled | transient_exhausted | transport_failed | tls_verification_failed | filestation_error | consumer_failed`。接口、receipt、日志都不得附带路径、URL、query、token、TLS fingerprint、response body 或底层异常原文。成功 receipt 的全字段是 `logical_read_id`（随机且不含对象身份）、`object_version`（仅可信且匿名化版本标识，否则 `unknown`）、`payload_bytes`、`sha256`、`expected_sha256_present`、`integrity_basis`（`version_bound_length | expected_sha256`）、`transport_attempts`（login/API/download 的 count 与固定 outcome code）、`wire_bytes`（可靠计数或 `unknown`）、`peak_in_memory_bytes`、`started_monotonic`、`finished_monotonic`、`deadline_met`、`cancelled=false`。它不含 consumer 已累计的 bytes，也不含 payload 或可定位身份。
- **完整性与每次 read 的硬上限**：`max_bytes+1` 只证明没有超限，绝不证明对象未截断。成功必须同时有完整流的证明：同对象/同版本绑定且可信的 length/version（实际 bytes 精确相等），或必需的 `expected_sha256` 精确匹配；两者都没有或不能验证即 `bounded_read_unavailable`，不得成功。无论是否做过 preflight，**每一次**底层 read 都为 `min(chunk_size, remaining + 1)`；因此 preflight 后对象增长也只读到一个探测额外字节，立即关闭并以 `capacity_exceeded` 失败，不交付越界字节、不解析、不重试、更不回退 `read()`/`_download()`。
- **重试、deadline 与 cancel**：transient transport retry 仅可发生在尚未向 `on_chunk` 交付任何 chunk 前，且总数最多 6 attempts、仍在总 monotonic deadline 内；每个 retry 都重新打开并重验对象 pin/version 及 FileStation TLS pin。首 chunk 已交付后任何 transient/不完整/完整性失败立即 fail-closed，零重试，绝不以第二个 response 拼接。同步阻塞 read 不承诺立即 cancel：socket/read timeout 必须限制为剩余 deadline；若底层无法设置，则 read 返回后立即检查 deadline/cancel、关闭而不成功。所有打开前、每次 read 前后及 retry sleep 前均检查 cancel；取消或 deadline 均关闭 response/connection。
- **FileStation 边界与 JSON error envelope**：最小改造是新的 raw-response streaming transport，不改变既有 bytes transport：未 pin 分支按限额读 `urlopen` response，pin 分支在同一已验证 pin 的 `HTTPSConnection` 按限额读。它只暴露 fakeable `open/read/close`、可信 version/length metadata 和 timeout；既有 `Callable[[Request], bytes]` fake 仍只服务旧 `read`。无可信 object/version-bound size 且没有 required expected SHA 时，真实 FileStation 必须 `bounded_read_unavailable`，不得猜测 metadata。错误 envelope 只使用有限前缀缓冲（`MAX_ERROR_ENVELOPE_BYTES` 为实现冻结上限）；仅当该前缀在上限内构成**精确且完整**的失败 envelope 才解释为 `filestation_error`。超过上限、截断、歧义、或以 `{` 开头但不是精确失败 envelope 的普通 JSON 都 fail-closed 为 `transport_failed`，不解析为错误详情、更不把它作为 payload 成功。
- **内存、接线与可观测性边界**：`peak_in_memory_bytes` 只定义为 backend/transport 自有 buffer 的峰值（当前 chunk、协议/前缀 buffer 与 transport buffer）；不声称限制泛型 consumer。P0 A 只能接显式工作集上限的 sink；超过 B 的安全门时，A 不得把完整 bytes 交给 B。`payload_bytes` 仅是 backend 交付/消费量，`wire_bytes` 仅可靠时记录，否则 `unknown`，两者不得互推；RSS 另报。静态和行为护栏必须拒绝把该 API 接到 builder、pointer、cache、writer fence 或 search route；仅显式 P0 safe-sampling 和未来另行批准的 snapshot 路径可调用。合同不写磁盘、cache、日志或真实 payload；日志仅可有 receipt 允许字段和固定 code。

**本地 fake 验收矩阵（已实施，不能替代真实 pilot）**：LocalFS、Memory 与 fake FileStation 覆盖：(1) login+download 总计至多 6 attempts、首 chunk 前 transient 重开成功；(2) 首 chunk 后 transient 为零 retry、失败且无成功 receipt；(3) 每次 raw read（含 `{` envelope 前缀/续读）不超过 `min(chunk_size, remaining+1)`，增长 fail closed；(4) 无可信完整性依据、SHA mismatch/截断均无成功；(5) deadline、cancel callback 异常脱敏、剩余 timeout 与返回后关闭；(6) retry version/TLS 重验、真实 pinned/unpinned adapter 的同 socket、timeout、close；(7) 有界/歧义 envelope 与 `{` 开头普通 JSON；(8) consumer copy 不误报为受控 consumer 内存；(9) 成功/失败/取消零写、零旧 fallback、零敏感日志，且静态 guard 保持 P1b 路径无接线。每个负例无 partial receipt 或 payload 持久化；三后端 WriteTrap 覆盖仍为范围内要求。

**四道门（当前状态）**：合同的独立评审、仅实施 bounded-read 与 LocalFS/Memory/fake FileStation 验证均已完成；实施后复审发现的六项阻断亦已在本轮返修并重验。下一道且唯一未通过的门仍是 Evan 明确授权的一库一次真实 P0 pilot。P1b 始终 **NO-GO**，不因任一门通过而自动解锁。

### 新的 P1b 决策门

可决定根因与 P1b 方案的数据是：受限读取合同先经独立评审和本地验证；之后 A 的少量有界原始样本能区分 logical read、attempt success/error、payload 与安全停止；B 在相同受控 bytes 的 n>=20 分布能量化解析/内存；C 在 n>=20 脱敏 corpus 的 legacy/postings 对照能量化算法且无原语差异；并且成功路径至少有一个在门内的完整、分段可判样本。网络层不要求 20 个巨大 cold/hot 请求，也不得虚报 P95。任一层显示容量门不能在读取/解析前安全执行、writer/fence 恢复原语未证明，或成功路径仍不可判，保持 NO-GO，不偷做流式存储或 P1b。

### 下一轮（仅供获授权操作者，不在本轮执行）

1. 先实现并独立评审上述受限读取合同，在 fake transport/LocalFS/Memory 上完成本地验收；不接 NAS、不取真实 payload。
2. 通过后才可由受控启动面、另行授权一库一次有界 pilot；命令行、日志、RT 和 evidence 不出现 query/token/path/URL/凭据。安全门首先触发即停止，不追热态、第二库或 20 次下载。
3. 只导出脱敏 diagnostic record 与结构摘要；若 A 成功且未越门，把该进程内 bytes 交给一次性隔离 B；随后销毁进程。C 只用新生成的脱敏 shape corpus。每层独立签出 sample count 与未知字段。

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

硬限制须在分配/解析**之前**执行：pointer max bytes、snapshot compressed/wire bytes、decoded bytes、docs/chunks/postings、单 query postings work、单库已验证缓存和进程总缓存均有常量上限。读取必须采用上文 P0 安全前置的显式 `read_bounded` 合同：先取可信且对象绑定的 size preflight，或以 `max_bytes+1` 流式计数，超限中止；同时流式 SHA，禁止 `read()->bytes` 后才检查长度。builder 正文校验也走流式 SHA；P1a endpoint 语义仍独立，不能借本 RT 静默改变它。

256/512/64MiB 已被本次 legacy 样本的 1.495GB logical lexical payload 与 5.462GB RSS 反证，不能再作为 P1b 通过阈值或被静默放宽。B 必须先以受控单次 bytes 得到 wire（若可观测）、decoded、parse 工作区和 verified-cache 的 n>=20 分布；若输入先越安全门，安全中止并只保留结构摘要。之后才可回方案门重定格式/预算；不得靠 OOM 后逐出、重复下载或偷偷增加上限。P1b 若获准，仍须在读取/解析前 fail-closed `capacity_exceeded`。

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
| physical NAS / bytes | 网络层只取有界的单次/少量原始样本，分列 logical read 与 login/API/download attempts、success/error、payload 与 unknown wire；不得为分位数重放巨大对象。B/C 的 n>=20 分布与真实成功路径的单样本安全门共同决定后续预算，回填并再过方案门前 P1b NO-GO。 |
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

- **本轮已完成**：受限读取已经实现；独立复审发现默认 streaming 仅 fake、envelope 绕预算、控制面脱离 deadline/attempt、cancel 泄漏及 coverage 缺口六项阻断，本轮逐项返修。默认 streaming 现为真实 raw response，pin 在同一验证 socket；所有 bounded response/connection 路径关闭。未调用生产、NAS、真实 token、真实 builder 或真实 gateway。
- **历史证据更正（2026-09-08）**：早先记录的 **87 tests / OK** 组合在当时父环境中可选中 NAS smoke，故不得再作为纯本地受限读取证据；它仅保留为安全类别记录，不记载主机、路径或凭据，也不声称已对外确认清理。
- **受限读取纯本地验收（2026-09-08，本次）**：唯一入口 `make rt054-pure-local` 不接受测试选择或可覆盖解释器；Make override shell/flags 并由自身 realpath 推导项目根，固定 `/bin/sh` 调用 shell-quoted checked-in launcher。launcher 仅解析固定绝对候选中的 regular executable，首个 Python 已是 `-I -S`；runner 在任意 repo import 前校验 isolation/no-site。子进程只保留 C locale 和 pure-local flag，不继承 PATH、HOME、TMP、Python import/user-site 或任何 NAS/凭据变量；启动期及测试期 socket、`FileStationBackend.from_env`、默认网络 FileStation 写和 `.env` audit/四条 Python 标准 open route 均 fail-closed trap。**95 tests / OK, skipped=1**（21 bounded-read + 50 storage + 16 P0 + 8 pure-local guard）；verbose 摘要明确为 `NasSmokeTests` 的 `SKIP-reason: RT-054 pure-local gate`，completion marker 为四类 trap 均 0。普通父环境与 SHELL/CURDIR/RT054_PYTHON/PATH 直接赋值、MAKEFLAGS、PYTHONPATH sitecustomize/.pth marker canary 均经同一入口通过且无 canary 泄漏/执行；不读取真实 `.env`。`.env` 四路精确负例均失败且计数准确，覆盖仅声明为 Python 标准 open routes。仅 LocalFS、Memory 和 fake FileStation/raw socket 被调用；旧 `read` 与 injected bytes transport 保持不变。静态 guard 证明 gateway/builder/ingest 无 `read_bounded` 接线；未接 P1b、pointer/cache/writer fence/search route，未调用 NAS、生产、真实凭据或真实 payload。P0 safe sink 仍未实现；任何将来的接线须另有独立工作集上限。
- **当前授权边界**：P0 零写诊断获允许；P1b 仍 NO-GO，必须先通过 P0 数据、numeric physical budget、容量实测、writer 穷尽与 FileStation 排他/恢复原语方案门。任何未完成数据只能标未测，不能称性能已治理。
- **AI 评审**：实现收口前独立复核所有 writer 是否接入 fence、回滚是否能重放旧代、指针是否真为单一权威、限额是否在下载/解析前、性能判据能否被 cache/timeout 假绿。

## 变更记录

- 2026-09-07：创建方案稿；无产品行为变化、无部署。
- 2026-09-07：吸收独立 Codex GO-WITH-CHANGES 八项阻断意见：P0 单变量测量、O(N²) 修复、单一 pointer/epoch、双 collect、不可变快照、single-flight/限额、受限流式读和可执行回滚验收。仍等待方案门。
- 2026-09-08：第二次独立评审后更新合并基线（main `09c8aff`、branch merge `d40e36b`）；P0 获准、P1b 保持 NO-GO。补入 RT-053 shared-token 返回前撤权、正式/动态 writer 审计、FileStation 无事务恢复前提和 physical NAS 数值预算固化门。
- 2026-09-08：实现默认关闭、响应内存零写 P0 分段诊断与合成离线 postings 对照；以 self-time/parent-total 消除 nested-stage 双计数，并输出 unattributed/overlap。补默认关闭响应等价、WriteTrap、失败白名单、RSS 平台单位和 scorer mutation/矩阵测试；FileStation 只在本地 `--p0-diagnostics` wrapper 期间观测真实 attempt/login/API/download/retry 与 payload，wire/header 不可可靠观察时保持 unknown。本轮只跑脱敏本地测试，未连接生产/NAS/真实凭据。
- 2026-09-08：解析 controlled pilot 后改为 A 网络有界单样本、B 隔离内存解析、C 脱敏 shape 算法三层采样。旧 evidence 仅有 **6 个无法解释的 download events**，没有 per-attempt outcome，不能称 six attempts、不能推断重试/分块/重复对象下载；新 observer 只增加脱敏 attempt 账本。1.495GB legacy payload 与 5.462GB RSS 使原 256/512/64MiB 假设失效；BM25/span 未进入，但单个 503 不足以归因成功路径。P1b 保持 NO-GO。
- 2026-09-08：完整独立评审裁决 P0 未完成、P1b NO-GO。把受限读取合同列为任何再次约 1.5GB 下载前的唯一最小前置：现有 `read/_download/transport` 都会完整物化 response，故要求新的 urllib/HTTPS response streaming 边界，否则 fail-closed 拒绝；本轮只改方案，不改产品代码/测试，不访问 NAS/生产。
- 2026-09-08：按新增独立评审阻断意见重写 bounded-read 合同：唯一 callback API 与 `expected_sha256`、callback 所有权、固定脱敏 code/完整 receipt、首 chunk 后零重试、可信完整性、每 read 上限、阻塞 deadline/cancel、FileStation 有界 envelope、consumer 内存边界和拒绝接线护栏全部可由 fake matrix 证伪。证据仍仅称 **6 个无法解释的 download events**；现有验证仍仅为 **16 个 P0 tests**，不冒充新合同验收。四道门保持 P1b NO-GO。
- 2026-09-08：受限读取实施后独立复审发现六项阻断；本轮返修真实 unpinned/pinned raw streaming、envelope 逐 read 预算、统一 control/body monotonic deadline 与总 attempts、cancel 脱敏、close 和本地 fake coverage。仅本地验证；P0 pilot 与 P1b 继续 NO-GO。
- 2026-09-08：第三次复审唯一阻断 B-01：FileStation brace-envelope lookahead 曾在首 raw chunk 后绕过外层 cancel/deadline 检查。现将同一脱敏安全检查传入 wrapper，并置于每一次底层 raw read 前后；新增 cancel 与 deadline 的完整错误 envelope、`chunk_size=1` 回归，证明首 read 后触发时无 lookahead、stream/connection 关闭、无 callback delivery/receipt，结果仅为 `cancelled` 或 `deadline_exceeded`。仅本地 fake 验证；真实 pilot 与 P1b 继续 NO-GO。
- 2026-09-08：安全事件后将 RT-054 验收收束为唯一 pure-local runner：最小环境白名单重建 + 强制 NAS smoke skip，并以伪父环境 canary、skip 摘要和 backend-instantiation trap 证明不实例化 NAS backend、零网络/零写。早先 87 组合不再作为纯本地证据；未访问 NAS 或请求外部清理确认。P1b 继续 NO-GO。
- 2026-09-08：加固 pure-local 门为 Make 自身 realpath/override shell 与固定 `/bin/sh` launcher；launcher 封闭解释器选择并保证第一个 Python 即 `-I -S`，runner 在 repo import 前检查 isolation/no-site。SHELL/CURDIR/RT054_PYTHON/PATH 的 direct/MAKEFLAGS canary 不能改变 dry-run recipe 或无害实际入口；PYTHONPATH 的 local sitecustomize/.pth marker 不执行。`.env` audit hook 与 builtins/io/Path/os 四条 Python 标准 open route 负例均红、计数准确、错误脱敏。两次唯一入口（普通与全 canary 父环境）均为 95 tests / OK, skipped=1，四类 trap 均 0；正式 NAS smoke 保持 non-pure-local，旧 90-test evidence 同样撤销，不声称外部清理确认。P1b 继续 NO-GO。

## 遗留事项

- P1a 正文分页每请求完整下载/SHA 的传输优化保持独立；本 RT 不让 search 读取候选正文，也不宣称消除此成本。
- P0 若证明瓶颈不是网络、解析或现有评分，或 fence/受限流式读不能落地，带原始数据回方案门；不得预先登记尚未创建的产品文件或 ownership。
