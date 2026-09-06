# RT-Lite: RT-051 - Agent 受控完整访问与正文词法召回

> profile: Spec-Lite | execution_mode: collaborative
> 唯一方案权威；生命周期和工作目录见 [meta.yaml](meta.yaml)。**停在方案门、代码授权之前。**
> 本轮修订设计，不表示功能已实现。下述 HTTP、CLI、tool schema 和预算均为拟议合同，不能当作现有命令执行。

## 方案（给人看）

- **做什么**：让 Agent 在已授权的单库内，像用本地目录一样自主 browse/search/inspect/open/read/continue。先能分页列举、查元数据、按已知文档直接打开、按需读任意安全范围或续读到末尾，再增强正文词法与元数据融合召回。
- **为什么**：当前 query 只查 lineage/title/path，最多返回200项且没有分页；citation 每次读全件、算全 SHA，却只回前500字符；wizard 没有 read。不能把“找不到关键词”变成“已授权文档不可访问”，也不能靠 Agent 直连 NAS 补洞。
- **成功是什么**：已知 lineage 不搜索就可打开；中部、末尾和全文都可达，但不强制把全文一次塞进上下文；每次 read 自带同版 SHA/span 引文。超过200件可完整遍历；任何缓存、续读、词法索引都不能绕过撤权或把新版当旧版。
- **代价与推荐**：基础访问独立于词法索引。推荐 OPS 独立 builder 一次流式读取并验证、生成受控不可变阅读快照，gateway 只读分页；不是每页全量下载。完整访问及实际工具接线是已确认范围，不再列“要不要做”的 D 项。Python BM25 为第二阶段推荐，FTS5 只在同条件性能实验有必要时对照。
- **同源问题**：[Issue #2](https://github.com/evan-zhang/CWK/issues/2) 纳入本 RT，不新建 RT。它提出的 citation 分页建议只保留用户目标，不照搬每页整件 read/hash。Issue 状态是父会话所报 OPEN/无评论，本轮不调用 GitHub、不修改或关闭它。
- **非目标**：跨库联搜、任意 NAS/服务器目录浏览、向量/embedding、新增 OCR/转换器、跨文件拼答案、回答生成、PR-001 多租户 ABI、源系统实时逐件 ACL 新体系、生产部署/定时器/真实数据验证。阅读现有主 raw 不等于已完整转换源附件；已有 extras 不冒充主文档正文覆盖。

### 真正仍需人的决定

| ID | 事项、推荐、责任和停点 |
|---|---|
| D01 | **代码开发授权**：用户审本修订后单独允许开发；本轮授权只覆盖 RT 文档和本地提交，不得自行开工。工程师负责技术选型，不逐个参数再问用户。 |
| D02 | **启用前宿主/成本**：推荐单可信 OPS 写宿主、独立受控快照磁盘和所有源写入口共用屏障。用户/运维确认磁盘、权限、留存和容量成本；不能收敛多写宿主时禁止启用新版 reader，不让 gateway 临时落地替代。脱敏开发实验不需要生产授权。 |
| D03 | **启用前权限语义**：当前是库快照授权，不是源系统实时逐件 ACL。已观测库撤权、映射移除立即拒读；无法确认源权限/版本也拒读。若业务要求未同步上游撤权立即生效，权限 owner 先补权威事件/协议；不能用 NAS 仍有文件冒充许可。用户需接受此业务安全边界后才能生产启用。 |
| D04 | **未来越界改动**：新增代码归属/模块登记、Skill 发布需维护者授权并按各自规则执行；本轮不改规范/Skills。若容量实验要求更大磁盘或新增基础设施，带实测代价再报用户，不将技术参数都伪装成人决策。 |

## 假设与现状

- 历史证据基线、44文件清单仍保留在 [sources.json](sources.json)，不重写历史，也不宣称本轮重核44文件。历史代码观察见 [evidence.md](evidence.md)。
- 本轮开始 HEAD=`effb1decec71d30a73b3af4abbce78825b212347`，工作树/暂存干净。main=`fe9f85870c6c7030d72e55a3d2d23c8697ea609e` 已增加 RT-050 vanished 报告（不删除），修改 ingest/其测试/create Skill；main 另有 RT-050 文档未提交；随后该文档单独提交为main的a9209d3。本轮已读这些定向差异，不合入、不覆盖，不再称 main 产品代码未变。9个 worktree 检查未发现 RT-051 重叠写面；开发前必须再查。
- Codex 静态审核 `o40MC61R` / `effb1de` 的 **GO WITH CHANGES** 来自父会话/用户交接。本轮定向读源码复核关键事实并处理意见，不是再次独立 Codex 审查，没有跑产品测试。采纳/调整理由见证据处理表。
- `raw-index` 是权威当前映射，不是 BM25 索引；ingest 当前未写 title。`version` 是摄取版本，不是上游 revision；classify 可原地覆盖，版本链存在不保证旧字节仍在。`origin_sha256` 与转换后 raw 的 `sha256` 不可互换。
- `StorageBackend.read -> bytes` 与 FileStation 的 transport `response.read()` 当前都是全对象读取；不能靠拿到 bytes 后检查 len 声称有界。NAS Range/流式错误封装尚需实验，不假定已支持。
- query Skill 当前包含直连 FileStation/from_env/walk 和本机 NAS gateway 兜底；这些是待移除的越界接线，不是新 reader 的退路。现有 reviewer 仍为零工具 JSON transformer。

## 实现备注（用户不问可不展开）

### C01 分层、身份与权威映射

依次分三层：**权威访问面**（授权/映射/metadata/reader）→**派生召回面**（chunks/BM25）→**Agent 包装**（实际调用前两者、维护任务覆盖）。基础访问不等待词法 generation，不要求命中 chunk。

拟议 `DocumentIdentity` 字段：`library_instance_id:string`（可信挂载绑定+kb_code 的域分离 SHA，完整64 hex），`kb:string`，`lineage_id:string`，`source_version:positive int`，`artifact_id:"primary"`，`raw_sha256:64-lowercase-hex`。路径只在服务端当前映射中解析，客户端不传服务器路径、NAS prefix 或存储凭据。

`document_ref` 为不透明、认证保护的句柄，绑定以上身份、`subject_binding`（服务端可信 token identity/授权 epoch，不含 token）、`issued_at/expires_at` 与 schema/key_id。推荐服务端 AEAD 封装，密钥由宿主受控配置提供，不复用 NAS 密钥；不明文暴露 locator。句柄不授予权限，每次仍独立鉴权。**不绑定 lexical generation**。修改任一字段、换库/换持有者必须失败；key 轮换或到期须重新 resolve。

基础 list/search 对当前 raw-index 严格解析：lineage 唯一，source_version 正整数，SHA/安全 locator/挂载 identity 合法才签发可读 ref；合法但 placeholder/failed/partial 条目仍可列举和 inspect，`readable` 与 reason 明示。坏 row 不用 parse_entry 默认值冒充有效；可安全显示的诊断项无 ref，冲突 lineage 或根映射损坏则整个映射不可用。title 缺失返回 `title:null,title_source:"missing",display_label:lineage_id`，不是伪造文件名；若摄取补 title，须明确来源、不用模型猜。可选 category 仅为映射中的逻辑分类，不暴露物理目录；metadata match 允许服务端使用旧 path 作为内部匹配字段，但不回传它。

resolve 已知 lineage 只查当前映射，无关键词、无 chunk、无 BM25。无映射、坏 root identity、dirty 屏障、backend/权威权限不可核验是 `source_unavailable`；仅词法代缺失/损坏是 `lexical_unavailable`，二者不可混淆。

### C02 拟议 HTTP 合同与内容完整性

以下 `/v2/kb/*` 是**建议新增**的 GET-only 路由（表中操作名即后缀；metadata search 后缀为 `search`，resolve/open统一后缀 `resolve`，continue统一后缀 `read`），所有调用显式 `kb`；先 token scope、再 mount（保留绑定 token 跨库403/admin未知库404）。无凭据示例只表示载荷。未知/重复参数、未知 schema、超长字符串、非法整数拒400，不静默忽略。

| 操作 | 请求参数（省略者可选） | 成功 schema / 必要结果 |
|---|---|---|
| capabilities | `kb` | `cwk.kb.capabilities.v2`；access_contract、supported_operations、limits、offset_unit、prepare_transport、lexical_modes；只报已授权库能力 |
| list | `kb,page_size=50,cursor?,category?` | `cwk.kb.documents.v2`；items、metadata_snapshot、total、returned、next_cursor、eof |
| metadata search | `kb,q,page_size=50,cursor?,category?` | 同 documents.v2，加 `query_kind=metadata`；同 list 可完整分页，不用 Top-K 冒充全量枚举 |
| resolve / open | `kb,lineage,version?` | `cwk.kb.document.v2`；identity、document_ref、expires_at、readable、reason、inspect 摘要。open 是 CLI/tool 对 resolve 的别名，不再设第二事实路径 |
| inspect | `kb,document_ref` | 同 document.v2；当前身份、size_bytes、encoding、parse_status、view、source_content_complete、read_state、prepare_required |
| read | `kb,document_ref,max_bytes=16384,start_byte?,end_byte?,line_start?,line_end?,cursor?` | `cwk.kb.read.v2`，内容和证据字段见下；范围/行/cursor 三选一，默认从0开始 |
| continue | `kb,document_ref,cursor` | read 的包装别名；同 schema，同预算，不能让 Agent 自增偏移代替服务端 next |
| renew | `kb,document_ref` | 同 document.v2；到期不超过建议10分钟宽限、身份/源版本/SHA/权限仍一致才换 ref；内容变了409，不静默换新正文 |
| read-status | `kb,document_ref,job_id?` | `cwk.kb.read-status.v2`；仅查看该身份的构建状态、reason、retry_after、已验证字节计数；不触发构建 |

分页 list 示例（synthetic label，不是生产结果）：

```json
{"schema":"cwk.kb.documents.v2","ok":true,"kb":"demo-a","metadata_snapshot":"ms_demo","total":231,"returned":1,"items":[{"lineage_id":"docdb:demo-001","title":null,"title_source":"missing","display_label":"docdb:demo-001","source_version":1,"readable":true,"document_ref":"opaque-ref"}],"next_cursor":"opaque-list-cursor","eof":false}
```

read 请求示例：`GET /v2/kb/read?kb=demo-a&document_ref=opaque-ref&start_byte=0&max_bytes=16384`。实际 ref/cursor 只能使用返回值，日志不记录原始 URL。

read schema 必需字段：`ok,kb,identity,document_ref,view,encoding,total_bytes,span_start_byte,span_end_byte,returned_bytes,text,page_sha256,raw_sha256,full_sha_verified,evidence_status,verification_id,eof,next_cursor,expires_at,coverage_scope,source_content_complete`。`identity` 含 C01 全部内容身份，引文直接取该身份+span+page_sha256；不为引用再调用 citation 重读整件。建议可附 `line_start/line_end` 作为人读导航，不取代 byte。

可复算的空文示例（身份中的 raw_sha256 同下）：

```json
{"schema":"cwk.kb.read.v2","ok":true,"kb":"demo-a","identity":{"library_instance_id":"<64hex>","kb":"demo-a","lineage_id":"docdb:empty","source_version":1,"artifact_id":"primary","raw_sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},"document_ref":"opaque-ref","view":"raw_utf8","encoding":"utf-8","total_bytes":0,"span_start_byte":0,"span_end_byte":0,"returned_bytes":0,"text":"","page_sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","raw_sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","full_sha_verified":true,"evidence_status":"verified","verification_id":"opaque-verification","eof":true,"next_cursor":null,"expires_at":"<RFC3339>","coverage_scope":"returned_span","source_content_complete":true}
```

完整性分开表达：

- `view=raw_utf8` 逐字节对应现有完整主 raw（含 envelope/frontmatter），严格 UTF-8，保留 BOM/CRLF/组合字符，不 strip、不 normalize、不 replacement decode。hash 对原 bytes，不对 JSON 转义文本/HTTP压缩体。
- `parse_status=complete|partial|placeholder|failed|unknown` 描述摄取转换；`source_content_complete` 只有可信转换回执明确完整才 true，未知为 null，部分/占位为 false。`read_state=ready` 只表示该 raw 可完整读取。读取全部 XLSX 主目录 Markdown 不等于读完 sheets；origin_sha256 不是 raw SHA。
- 有可验证 partial raw 时允许 read 并标 partial；placeholder 原样可读只说明“已读占位件”，不能算源正文完整。failed 无 artifact 返回 `content_unavailable`。inspect 的 title/状态不通过扫描正文或搜索结果猜。
- 无效 UTF-8 可 inspect、返回 `unsupported_encoding`，本 RT 不新增转换/OCR；大且有效的现有 UTF-8 主 raw 不能仅因超过旧2MiB检索预算一律拒绝。

### C03 游标、范围、续期和完整读取回执

1. 所有 byte offset 是完整 raw 的 **0-based、半开区间** `[start,end)`；行是1-based、两端包含，LF 分行（CR 保留），空文0行。行索引由 builder 流式建立，只用于加速定位。合法 start=end=total 返回空 EOF；负数、越界、反向416 `invalid_range`；截在 UTF-8 中间416 `invalid_utf8_boundary`。不把所有自报 offset 当越权：先 ref/权限/当前映射，再验证安全范围，禁止自报路径。
2. `max_bytes` 是**正文 UTF-8 bytes** 上限，不是字符数或 JSON 包长度；默认16KiB、建议最大64KiB，至少4以保证一个 code point可进。服务端向前取合法边界，非空未到末尾每页必须推进；JSON response 总预算另限。指定 end/行范围大于一页可继续，cursor 同时固定请求终点。`range_complete` 可附加，**eof 仅指到整件 total**；范围读完但未到文件尾时 eof=false、next_cursor=null，需显式请求新范围，不能叫全文完成。
3. read cursor 为 AEAD 不透明值，绑定 schema、subject、库/lineage/version/SHA、view、next_start、range_end、page_budget、expiry。每页按签名与服务器边界重验；改参数或交换 list/read cursor 400 `invalid_cursor`。ref 续期后同 identity 的旧 cursor 可在它仍有效时使用；过期 cursor 需持新 ref 从最后已确认 span_end 显式恢复并重新校验。epoch 已变时不能靠 renew 跨授权重签。
4. 同 ref/cursor/参数在有效且源未变的条件下返回同 span/text/SHA/next（时间性诊断字段不保证字节相同），不消耗一次性游标，重复请求幂等。客户端丢失响应可重试一次，不重复计覆盖。空文第一次即 EOF、next=null，SHA 为空 bytes 的标准 SHA，不能无限续读。
5. list 与 metadata search 推荐**源变更明确409**而非持久快照：cursor 绑定元数据规范投影 digest、排序键（lineage_id Unicode code point 升序、唯一）、筛选/q/页大小/subject/expiry。每页前后重新核实同一映射，metadata_snapshot 不同409 `metadata_changed`，丢弃该页并提示重开；已收页面不能混新快照凑全量。total 为该固定投影条件下精确件数，页大小1–200（超过200报400，不截成200），>200 通过 keyset 到 EOF，无遗漏重复。变化频繁时明确遍历未完成，不无限自动重开。映射解析须 C08 有界，必要时 OPS 构建独立 metadata projection，与词法无依赖。
6. 每页调用开始和返回前都复核 token、mount、映射与 clean fence。版本/SHA变化409 `stale_reference`，撤权401/403，映射移除404 `document_unavailable`；均无 text/标题/旧缓存。最后成功核验为响应准许点，不声称能撤回此前已送字节。服务端无法核实当前源时503，不使用“TTL内应该没变”替代验证。
7. **range SHA 不证明 full SHA**。只有 C06 builder 真正流式走到 EOF、对完整 bytes 计算 SHA 等于权威映射，发布不可变快照后，read 才能标 `full_sha_verified=true`。单页 hash 可由调用者复算，全 SHA 依赖该已验证对象及可信服务端 provenance；没有全件验证，不能发 verified 原文证据。引用不需要每页重算全 SHA。
8. Agent 包装在任务内按 `(library, lineage, version, raw_sha, view)` 累积区间并集；对同版去重、检测重叠冲突、排序覆盖 `[0,total)` 且 EOF 才产生 `transport_read_complete=true`。若实际拼接全部页，`text.encode('utf-8')` 的合并 bytes hash 必须等于 raw_sha；仅范围任务则返回 `partial` 和已覆盖区间。receipt 区分 `transport_read_complete`、`source_content_complete`、`task_review_complete`（由任务真实审阅状态确认，不由 HTTP 自动置 true）。工具收全 ≠ 模型理解全文；有缺页/未消费/摘要压缩丢弃的部分不得称完整审阅。通常只读相关范围，不强迫每题通读。

### C04 错误、新旧兼容与可发现性

所有新版错误：`schema=cwk.kb.error.v2,ok=false,error:{code,retryable,retry_after_ms?},request_id`，不含正文/标题/路径/token/原请求。401 `unauthorized`、403 `forbidden`、404 `unknown_kb|document_unavailable`；400 `bad_request|invalid_cursor|unsupported_contract`；409 `stale_reference|metadata_changed|prepare_conflict`；410 `reference_expired|cursor_expired|snapshot_expired`；416 范围错误；422 `unsupported_encoding|content_unavailable`；429 `budget_exceeded|queue_full`；503 `source_unavailable|lexical_unavailable|evidence_unavailable|read_not_ready`；504 `read_timeout`。503 read_not_ready 可给 prepare_required/status 操作提示，绝不以空 text+ok=true 隐藏失败。字节/hash损坏为 evidence_unavailable，不重试成成功。

保留旧 `/query` v1 默认 metadata、原字段/排序/matched/limit200；旧 `/citation` v1 前500字符与 mismatch 观测行为冻结作兼容基线，不把其 `ok=true` 当 verified，也不让新版 Agent 用它通读。旧服务会忽略 offset/limit 等未知参数，故新版客户端先鉴权请求 capabilities，确认版本及 read/list/metadata/prepare 能力；404、v1 schema、缺必要字段，即 `unsupported_contract` 非零退出，**HTTP200 不是能力证据**。服务谎报能力仍须每响应验证 schema、identity、实际 byte span/returned_bytes/next/EOF；不得继续接受500字符旧包。

新版默认仅当前版；显式请求旧 version409 `stale_reference`，不承诺历史全文服务。将来若加历史 read 必须拿到被授权的确切旧版 bytes并核SHA，否则拒绝，绝不用新正文满足旧版引用。旧 v1 仍可能暴露 mismatch，但不能进入新版证据链。兼容旧行为不是复制旧安全缺陷：新增日志脱敏和返回前二次库鉴权列安全回归，正常响应形状不变。

### C05 实际 CLI / Agent tool 接线与权限边界

推荐**扩展现有 `scripts/kb_wizard.py` CLI**，抽出薄 `scripts/kb_gateway_client.py`（拟新增）供 read-side verbs 使用 HTTPS/受控 loopback HTTP 客户端，复用同一协议校验器。不要另造无实现的 MCP 名字。旧本地 query 管理路径继续兼容，但 Agent 新包装只允许固定 read-side 子命令，不能传 `--backend/--prefix/--kb-root`，不能落入 build_backend/from_env。

拟议可调用形式（不是现有可运行命令）：`python3 scripts/kb_wizard.py <capabilities|list|search|open|inspect|read|continue|renew|read-status|read-prepare> --kb <id> ...`；端点与绑定 token 由宿主注册的受控连接配置注入，**不从模型提供 URL/token 参数，不放 argv、shell 展开或日志**。search 默认 metadata，词法需显式 `--retrieval-mode lexical_fusion_v1`。read 参数与 C02 同名 kebab-case；stdout 单个结构化 JSON，stderr 仅脱敏诊断，退出0成功、1远端/权限/预算错误、2请求/不兼容协议错误；202 prepare 的退出0仅“已受理”，不是读取完成。

拟议一个实际固定可执行包装 `kb_access`，以下为完整公共字段及操作分支 JSON Schema（范围互斥/身份与边界另由服务端执行 C03）：

```json
{
  "type":"object",
  "properties":{"operation":{"type":"string"},"kb":{"type":"string","minLength":1,"maxLength":128},"lineage":{"type":"string","minLength":1,"maxLength":512},"version":{"type":"integer","minimum":1},"document_ref":{"type":"string","minLength":1,"maxLength":4096},"cursor":{"type":"string","minLength":1,"maxLength":4096},"q":{"type":"string","minLength":1,"maxLength":256},"category":{"type":"string","minLength":1,"maxLength":256},"page_size":{"type":"integer","minimum":1,"maximum":200},"max_bytes":{"type":"integer","minimum":4,"maximum":65536},"start_byte":{"type":"integer","minimum":0},"end_byte":{"type":"integer","minimum":0},"line_start":{"type":"integer","minimum":1},"line_end":{"type":"integer","minimum":1},"job_id":{"type":"string","minLength":1,"maxLength":128},"request_id":{"type":"string","minLength":1,"maxLength":128},"retrieval_mode":{"enum":["metadata","lexical_fusion_v1"]},"allow_degraded":{"enum":["metadata"]}},
  "additionalProperties":false,
  "oneOf":[
    {"properties":{"operation":{"const":"capabilities"}},"required":["operation","kb"],"propertyNames":{"enum":["operation","kb"]}},
    {"properties":{"operation":{"const":"list"}},"required":["operation","kb"],"propertyNames":{"enum":["operation","kb","category","cursor","page_size"]}},
    {"properties":{"operation":{"const":"search"}},"required":["operation","kb","q"],"propertyNames":{"enum":["operation","kb","q","category","cursor","page_size","retrieval_mode","allow_degraded"]}},
    {"properties":{"operation":{"const":"open"}},"required":["operation","kb","lineage"],"propertyNames":{"enum":["operation","kb","lineage","version"]}},
    {"properties":{"operation":{"const":"inspect"}},"required":["operation","kb","document_ref"],"propertyNames":{"enum":["operation","kb","document_ref"]}},
    {"properties":{"operation":{"const":"read"}},"required":["operation","kb","document_ref"],"propertyNames":{"enum":["operation","kb","document_ref","max_bytes","start_byte","end_byte","line_start","line_end","cursor"]}},
    {"properties":{"operation":{"const":"continue"}},"required":["operation","kb","document_ref","cursor"],"propertyNames":{"enum":["operation","kb","document_ref","cursor"]}},
    {"properties":{"operation":{"const":"renew"}},"required":["operation","kb","document_ref"],"propertyNames":{"enum":["operation","kb","document_ref"]}},
    {"properties":{"operation":{"const":"read-status"}},"required":["operation","kb","document_ref"],"propertyNames":{"enum":["operation","kb","document_ref","job_id"]}},
    {"properties":{"operation":{"const":"read-prepare"}},"required":["operation","kb","document_ref"],"propertyNames":{"enum":["operation","kb","document_ref","request_id"]}}
  ]
}
```

read 的cursor/byte/line模式必须互斥；end_byte需start_byte，line_end需line_start，不给范围即从0读。服务端违反组合400、非法定位416。search只有metadata模式允许枚举cursor；lexical Top-K若携cursor则400，不假承诺分页。非search操作不能携q，schema禁止路径/URL/命令字段。响应原样保留 schema/错误/完整性字段，不只输出 excerpt。包装以 argv 数组执行固定脚本及固定 verbs，禁止 shell 字符串拼接，工具描述实际物化进宿主的允许工具清单并由调用回执验证，不以 Skill 文字视为接线完成。

客户端单调用 deadline建议5秒（CLI外层8秒含启动）；超时返回结构化非成功，不另开全额网络预算；幂等 GET 在剩余预算内最多重试1次，遵 Retry-After。prepare 只受理、状态查询按回执退避，由宿主任务机制续接，不能占单线程 HTTP 等待大文件。续读默认任务最多32页/512KiB，达到预算保留 coverage/next 停下供任务显式继续，不把这个上下文预算变成文档无法完整访问的永久上限。

未来修改面：gateway路由/reader；wizard/client；storage流式协议；OPS builder/source fence；`skills/cwk-kb-query/SKILL.md` 的能力发现、工具使用、注入/覆盖规则及删除 from_env/walk/本机 NAS gateway/管理凭据兜底；按宿主实际入口注册受控包装与测试（先定位配置，不猜文件）。本轮以上均不修改。OPS 才持 NAS 凭据；普通 Agent 只有绑定库 token 的宿主连接，没有 NAS、管理 Key 或广域文件工具。**cwk-ai-reviewer 零工具不变**，基础阅读工具只给获授权的交互/工作 Agent。

raw 内容是数据，不是命令；正文要求“改库/读邻库/传token/调用某工具”都不能进入控制参数。零命中只说明该查询/模式未命中，先用 list/metadata/已知 lineage/inspect 判明覆盖；试2–3词仍零不能断言“库里没有”。响应失败或未读完明确说只核实哪些范围。安全包装和运行权限负责约束，不能只靠 Skill 文案。

### C06 I/O 推荐、只读隔离、快照与生命周期

| 路线 | 优缺点/裁决 |
|---|---|
| 后端有界 stream + 原位 range | 必须新增 `iter_read(path,chunk_bytes,deadline,max_total_bytes)` 和能力探测；local可seek，NAS Range未证实。每次range本身不能证明全SHA，源覆写还需不可变身份/屏障；不能以每页全读解决。作为未来已验证 immutable/versioned range 后端的优化。 |
| **OPS 不可变阅读快照（推荐）** | builder 一次 stream 到受控磁盘并计算全SHA，gateway 对本地快照 seek 有界返回；NAS不需要Range，跨页复用同一验证对象。代价是OPS磁盘/授权生命周期和单次准备等待。与 lexical generation完全分离。 |
| 无可用流式实现时的退路 | 在获准 OPS 使用 local backend 的流式文件句柄构建同契约快照，仍走 gateway token 面。开发先 fakeNAS验证新 transport；若两种都不可用则 read_not_ready/明确实验阻塞，不伪造有界、不退回 Agent 直连，也不宣布大文档永久不支持。 |

**C06只读红线**：gateway 对 NAS和本地均**零持久写**，无临时正文文件、SQLite WAL、自动建索引、写job或清理缓存；其内存签发 ref/cursor 与少量有界解析不等于落盘。源只读不代表“可以 gateway 偷写缓存”。独立 OPS builder 才是快照 writer，两个进程/权限账户/导入图隔离；reader不 import ingest/builder 写模块。

准备链拟议：普通 read 未 ready→read_not_ready（不触发构建）；`kb_access read-prepare` 经同一受控客户端调用 **OPS 独立 preparation broker**，不是新增 gateway 写路由。推荐本机受限 Unix socket或受控HTTPS端点，地址由宿主绑定、不让模型指定；跨机需要HTTPS，网络许可属D02。broker 请求 `{kb,document_ref,request_id}` 使用同一绑定 token 校验，只能为当前授权文档申请派生副本，不接受任意路径或命令、不写源。broker入口拟议 `POST /v2/read-preparations`，成功202 `cwk.kb.read-prepare.v2` + job_id/state/retry_after_ms；状态通过 C02 gateway只读回执。此处允许独立broker写job/快照，不改变gateway GET-only合同。

prepare key=`(library_instance_id,lineage,version,raw_sha,view)`，request_id 仅幂等关联。重复已在构建返回同任务，ready返回现成身份，失败重试只对同当前身份且在总预算内；撤权不泄露他人job，旧源任务标 superseded不自动换内容。状态：`missing→queued→building→validating→ready`；错误→`failed|capacity_exceeded`，源漂移→`superseded`，TTL→`expired`。read-status 每次同样鉴权/核源，进度是已读字节不是“完成百分比”，未知total返回null。排队满429，不占HTTP等待；一次job建议最多3次/30分钟，瞬时错误退避上限8秒，权限/SHA/schema错误不重试。无broker能力在capabilities明确false，普通Agent获得阻塞，不隐式触发factory。

builder 在 **获准单一OPS宿主** 持单库OS释放型源锁；所有 run/refresh/classify写入口必须先dirty fence并持同一锁，完成raw/index/provenance/state/root-manifest一致性核对后 clean fence+epoch。崩溃留dirty不因PID消失自动放行。builder只读取账本与源，不“修复”NAS；持锁冻结源输入S0，流式按建议64KiB块读到EOF、hash并写staging，同时验证UTF-8与稀疏行索引。stream接口必须覆盖TLS pinned与普通transport、错误封装、deadline/关闭和重试；已读流失败不能拼接重试残片为一个对象，应丢staging重新计入job累计网络预算。size声明不可信，要按实际读字节实时计量，不能全读再len检查；禁止gzip无限展开/超配额后继续下载。

完成再读 S1，身份/源物理握手摘要/fence与S0一致、全SHA等于raw-index SHA才发布。快照 manifest 包含完整identity、length、encoding、full SHA、分段校验/稀疏行定位、source witness、builder/schema与 verified_at；局部读校验覆盖该页的固定存储分段摘要（与manifest绑定）再计算page SHA，不能只相信本地文件名。manifest自身不循环自hash；原子 staging→immutable dir→pointer，fsync文件/父目录，目录0700/文件0600，由reader专用只读身份访问。每页只读必要分段；坏分段/manifest拒503。同身份的重复构建验证一致后复用，不覆盖已有不同字节。原文版本权威仍为源映射，不是快照名。

返回前复核当前源权限/映射/fence。source可核实且仅词法索引坏，已知文档继续读；source不可达/映射丢失/dirty即使有快照也拒读。旧 raw-index.prev 或旧快照不得复活被移除件。外部绕锁写源无法被本地屏障保证，D02未收敛就不生产启用；metadata-only老入口不是新版证据通道。

**TTL/磁盘**：建议ref/cursor15分钟；快照lease建议60分钟（以最近一次broker prepare为续租点，不把reader访问当已续租，不要求gateway写访问记录）、绝对24小时。为维持gateway零写，reader一次请求持OS共享读锁，builder/GC持独占锁；过期时只读状态返回snapshot_expired，prepare broker收到显式请求才能更新留存lease。长任务在到期前主动prepare/renew，同SHA可续，不把到期换新版；到硬TTL可重建同一当前身份后从验证过的位置继续。GC仅OPS maintenance做，不删在用共享锁对象；崩溃锁由OS释放，staging从不服务，重启对账后才清理。逻辑拒读不等于物理安全擦除；整库退役/备份/加密由运维授权处置。禁止写Git或不受控备份、禁止正文/URL/token日志，HTTP no-store。

本设计允许大文完整按页到达，仍有有限容量而非无限资源承诺：超quota时broker报 capacity_exceeded、建议提高获准磁盘或使用验证过的流式/range后端；预算调整须实测代价，不以固定2MiB裁剪最终访问能力。基础快照配额与词法派生配额分别记账，互不阻断已ready reader。

### C07 第二阶段：正文词法+metadata融合复用 reader

仅在 C01–C06 与真实工具链验收后实现。纯Python BM25推荐，FTS5若对照必须相同文本/token/span/资格，探测本机编译能力；不使用默认FTS中文trigram取代短词能力、不装任意extension。Yuxi dense+BM25只作历史参考，不移植模型链/参数，不把本RT叫dense hybrid。

索引当前完整、可追溯主 raw 的正文；partial/placeholder/failed/unknown覆盖明确excluded，不说raw reader完整就语义索引完整。CWork唯一content envelope内正文、普通Markdown frontmatter后正文；边界歧义不猜，结构化意见JSON/XLSX extras不纳入首版正文召回，但可inspect其覆盖限制。基础reader仍可读完整主raw，词法抽取范围不能裁掉reader可访问范围。

chunk：建议目标800 Unicode code points、硬1200、重叠≤120，优先段/行切分；保原文byte映射，单个连续span，不strip/rejoin后伪造坐标。字段为完整DocumentIdentity + start/end/line/span SHA/chunker_version/tokenizer_version；chunk_id域分离hash绑定这些事实，不含generation。派生generation由确定性manifest core hash得出，不自引用、不含时间戳/易变运行账，参数改变换engine版本。

token：中文1/2/3-gram，长查询优先2/3，单/双字不能漏；ASCII lowercase保完整编号及组成部分，查询含≥5纯数字或字母数字连接编号要求完整token命中（AB-017不等于017）。原文不归一化；控制字符、未知参数、q>256 code points拒400。

BM25建议k1=1.5,b=0.75；idf=`ln(1+(N-df+0.5)/(df+0.5))`，标准tf长度归一化，只在授权单库当前合格chunks统计。空集显式0，不除零。每文档取最高块分数（不相加奖励长文），同分按lineage/span/chunk_id稳定排序。每路candidate_k建议100、上限500、每文最多3个去重span；metadata路保持旧strip/lower完整子串、lineage排序，在新模式使用同资格域。两路文档rank从1开始，RRF=`1/(60+body_rank)+1/(60+metadata_rank)`，缺项0；score_kind=rrf_rank_v1，非概率/跨库可比值。

拟议显式 search `retrieval_mode=lexical_fusion_v1` 响应 `cwk.kb.search.v2`：requested/effective/degraded/reason、generation/engine、coverage_complete/excluded_counts、matched_relation=exact|lower_bound、candidate_truncated、hits。Top-K不承诺完整枚举；要盘点走list/metadata。每hit含document_ref及候选span/page或chunk hash，不含缓存snippet，不标verified；**同一 C02 reader**读取span后生成引文，不另立generation绑定citation路径。chunk属于召回建议，客户端可继续安全范围读；词法代更新不使仍当前的ref失效。

源不可核实一律source_unavailable；仅词法故障严格lexical_unavailable，显式allow_degraded=metadata且源仍可核实时回degraded=true/effective=metadata，不暗降级。真正零命中200不降级，不说库无资料。generation过时/损坏不得返回旧候选；有已知ref可独立read。

独立lexical builder消费已验证阅读快照并核对源S0/S1，无法覆盖任一合格件则不发布悄悄少件新代。同机staging/manifest/files hash校验→原子pointer，pin代，崩溃只见旧/新完整代；双builder同库锁、旧代恢复仅在当前源仍一致时允许。状态 missing/building/validating/ready/stale/failed/corrupt/capacity_exceeded。如需FTS以只读immutable DB，不让gateway写WAL。索引GC与阅读快照不同，授权maintenance保留两代+staging，正在读不可删。

refresh接线在所有source execute_plan和save_refresh_state最终收尾后；先clean fence/释放源锁，再通知独立builder重新取锁核当前源，禁止嵌套死锁。dry-run零fence/job/index写；unchanged逻辑投影相同无需重建，不因refresh账时间变化重建。任务只传库身份/源digest/engine/request_id，幂等、过时代superseded；源成功、阅读准备、词法构建分别报状态。main新增vanished只是差异报告，不是权威撤权/tombstone，不自动删源或清缓存来“修好”。

### C08 建议预算、实验证据与验收

**以下全部为建议、未实测，非容量保证。** 验收映射在 [acceptance-matrix.md](acceptance-matrix.md)，共12项可证伪行为；本轮不跑产品测试。

| 资源/效果 | 建议目标及测量口径 |
|---|---|
| 基础完整访问 | 231/1001条可枚举；单件2MiB、32MiB、128MiB合成UTF-8均完整可达，不将2MiB检索样本上限套reader。大文按页续读，不一次返回全文。 |
| 页/请求 | 正文默认16KiB/max64KiB；JSON响应≤512KiB（含escaping）；metadata页≤200/响应≤512KiB。实际不足页大小如实returned并next，不能截JSON。q≤256 code points；完整性/错误元数据同样受限。 |
| 资源 | gateway RSS≤512MiB、builder≤1GiB、stream块≤64KiB；阅读快照单库建议4GiB（含staging），词法另2GiB（两代+staging），跨库总量需全局配额不能逐库无界相加。单job最多占阅读配额一半，超限明确容量协商，不OOM。 |
| 映射与网络 | 映射解析建议≤64MiB输入且有条目/字段上限，超出须OPS独立metadata projection或报capacity，不读全巨大JSON冒充有界。源核验可重读映射但不得每页重下正文；完整扫描N字节、P页，首次source正文下载≤N+有界失败重试量，后续同快照正文source下载0；单次无故障≤1.1N，总正文传输量O(N)而非O(PN)。元数据I/O、TLS、重试另报实际字节，不混入掩盖正文重复。 |
| 时延 | ready本地reader p95≤300ms、端到端≤2s、调用deadline5s；CLI外层8s。≥200次/暖身20次单列；冷prepare异步另报构建时长/排队/吞吐，不把fakeNAS视真NAS性能。单线程gateway不等待builder，不擅自多线程共享FileStation session。 |
| Agent任务 | 默认32页/512KiB每轮预算，受控继续可完成更长文；重复页不计两次覆盖，耗尽报partial+next。每页任务证据有出处，声称全审阅须覆盖和实际任务处理状态。 |
| 词法 | 48题/2合成库，每类6题：正文-only、中文短词、编号、无答案、版本、隔离撤权、索引故障、metadata兼容。macro doc Recall@10≥0.90，正文-only/短词各≥5/6，gold关键span Recall@10≥0.85；隔离泄露0、verified错版/错span为0。 |
| 词法规模 | 1k/10k主件、总raw≤200MiB/chunks≤100k、单件2MiB只是首轮性能评估集。更大文超词法预算可标excluded/capacity，reader必须仍完整可达；不能声称lexical coverage_complete。 |

评估必须有真实 ingest 代码对脱敏Memory/local/fakeNAS写出映射及raw，不能全靠手写理想schema掩盖title/placeholder/version问题；禁止真实业务raw/API。gold记录题目/库/版本/raw SHA/人工关键span/无答案理由/reviewer，0题、漏类、未审gold不算通过。metadata/body-only/fusion同集消融，空召回计0，不剔除失败；verified只证明字节来源，不证明支持题意，人工复核仍需要。FTS5、网络/磁盘/权限竞态实验由开发交证据，未达目标先报告并调整工程方案，超成本找D02/D04而非偷偷放宽。

### C09 开发顺序、所有权与回滚

| 阶段 | 写面（均为未来） | 出口与停点 |
|---|---|---|
| P0 方案门 | 当前rt-lite与D01，核实main/worktrees/ownership，不写代码 | 用户明确开发授权；实验证据尚无，不声称已修 |
| P1 基础协议/I/O | gateway/client/storage有界stream、OPS preparation broker/builder、source锁/fence、metadata/refs/reader | 无lexical索引也能list/open/分页/read到EOF；fakeNAS与local验证全SHA/资源/覆写/撤权 |
| P2 真工具接线 | wizard受控read-side verbs、kb_access宿主包装、获准query Skill修改 | 真实工具调用链跑A01–A10，不只API200；token无NAS、旧服务识别、注入与预算；基础完整访问先交付 |
| P3 召回增强 | 独立lexical contracts/builder/reader、gateway显式融合、refresh最终点hook | 复用基础reader，C07消融/短词/span/代际与A11，不给reviewer扩权 |
| P4 脱敏验收/收口门 | tests/test_rt051_*.py及范围内既有测试；实际CLI/schema回填 | A01–A12证据+独立复核，用户收口；Issue只有实测验收及另行授权后才可关闭 |

所有权沿用kb_storage RT-042、kb_ingest RT-043/RT-050、gateway/wizard RT-044；新增模块登记RT-051，先按治理流程拿必要权限。PR-001/legacy wiki/entity/nightly/release flag不动，不把实现藏RT目录绕治理。当前main ingest已有vanished补丁，后续开发协调基线再接source fence，不能按effb1de覆盖main。

现有局部验证入口（未来执行，不代表本轮已跑）：`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_kb_gateway.py'`；同理test_kb_wizard.py/test_kb_ingest.py/test_kb_storage.py/test_kb_token.py；新增test_rt051_*.py必须实际case数>0。禁继承真实smoke启用变量、禁读.env。make ci是全量入口，局部通过不能称全CI绿。

回滚需要部署授权之后才操作：先独立禁lexical，基础reader继续可用；若reader故障显式disabled/read_not_ready，不退NAS直连/旧500假全文；保留旧v1兼容但标能力不足。停新派生发布、不回滚raw-index.prev/权限/源，不恢复已撤权token。清理snapshot/索引/配置是另获授权maintenance，不在本轮。

### C10 自查、风险与责任

- 分类：library/lineage/version/raw SHA定事实，view/parse定覆盖，cursor定读取进度，generation/engine仅定派生；没有把generation绑进文档授权。
- 编码：C01身份、C02接口/完整性、C03分页证据、C04兼容、C05工具、C06I/O、C07召回、C08预算、C09阶段、C10治理；A01–A12逐项映射Issue/合同/反例，D项只保留人门，P项按先访问后检索。
- SoR/状态机/责任：源owner对映射/权限/fence负责；OPS owner对broker/快照配额、任务失败与GC负责；开发对协议/工具/实测负责；独立复核人核实gold及行为；用户决定代码授权/成本/安全边界/部署。dirty/SHA错/撤权/配额/旧协议自动停，不让Agent“自行解决”权限。
- 工程未知：NAS流式TLS/error-envelope兼容、所有写入口屏障覆盖、元数据解析上限、长文续期/GC锁、实际宿主工具注册、Python性能与中文gold均待P1–P4实验；不是凭文档解决。问题卡住由owner给结构化reason与可重试条件，无限等待/热轮询不算恢复。
- 源ACL不能实时获知、单宿主拓扑不成立、磁盘无法批准是D02/D03启用停点；正文注入和Agent误称完整阅读由受控工具/任务覆盖与实际输出核对，不给零工具reviewer补权。

## 验证

- **工程判据**：本轮仅相对链接/围栏/空白、定向rtguard、Git精确范围与文档一致性检查，见 [validation.md](validation.md)。产品破坏实验未执行，不证明API/流式/SHA/鉴权已实现。
- **AI评审**：承接父会话静态Codex o40MC61R/effb1de，意见处理见 [evidence.md](evidence.md)；本轮作者按rigorous-plan-methodology及4份references自查，无再次委派、不是独立验收。
- **读产出**：本轮对照read/continue空文示例、版本/SHA/UTF8覆盖、prepare写宿主与gateway零写、main漂移、12项验收及handover；不读真实raw、不声称全44文件重核。开发后须读真实脱敏工具输出和Agent结论，不用字符串存在检查证明产品成功。

## 变更记录

- 2026-09-06 初稿：正文词法与元数据融合设计，历史证据保留。
- 2026-09-06 本轮：依用户20:39授权与Codex静态意见，统一为先完整受控访问/真实工具接线，再词法增强；纳入Issue #2，纠正generation绑定/每页全件下载/两三词零命中/大文硬拒绝等旧矛盾。只修RT文档，产品未修，仍待代码授权。

## 遗留事项

没有新建或转出DI，不用转出问题关闭本RT。非目标与启用决策见开头；本目标仍未开发/未验收，不关Issue、不合并推送部署。最短接手入口为 [developer-handover.md](developer-handover.md)。

- 2026-09-06 开发启动（Evan 21:19 授权 D01）：main（含 RT-050 vanished）并入
  feature 分支基线 f3fbfa5，273 测试绿。**P1a 落地**（提交 8ee5746）：
  `/v2/kb/*` 受控读取面 8 操作（capabilities/list/search metadata/resolve/
  inspect/read/continue/renew），HMAC 不透明句柄（ref 15 分钟 TTL + renew
  10 分钟宽限 + 签发 nonce）、read 三模式互斥 + UTF-8 边界 416 + 服务端游标
  + 页预算冻结、每请求全读全 SHA 复核（漂移 409）、list/search 游标绑快照
  digest（换快照 409）、lexical 显式 503 + allow_degraded、日志 URL 脱敏。
  测试 test_rt051_gateway_v2.py 38 例绿（含分页拼回逐字节 SHA、码点边界、
  句柄跨类型/跨库隔离、过期恢复）；make test 快车道绿；双门禁绿。
  P1a 诚实边界：传输级有界读与 OPS 快照 builder 留 P1b；本层每请求全读
  是 citation 同款退化形式，full_sha_verified=true 正因实读验证。

- 2026-09-06 P2 落地（提交见 feature 分支）：fakeNAS 出口判据收口
  （分页穿 FileStation 到 EOF 逐字节拼回+SHA 对账；源覆写漂移 409 拒读）。
  **真工具接线**：新增 scripts/kb_gateway_client.py（薄客户端：env 注入
  连接配置、错误不带 URL、exit 0/1/2）+ kb_wizard 8 个 read-side 动词
  （open=resolve 别名，无 backend 旗标，成功原样透传 v2 schema）。
  专项 40+14 例绿、回归 204 绿、make test 快车道绿、双门禁绿。
  剩余：P3 词法融合、P4 脱敏验收（A01–A12）。

- 2026-09-06 P3a 落地（提交 08b6d07）：scripts/kb_lexical.py 词法原语纯函数层——
  tokenize（CJK 1/2/3-gram；ASCII 小写整词项，AB-017≠017）、chunk_body
  （码点 800/硬 1200/重叠 ≤120、行边界优先、单块连续 span、双游标前向
  pass 字节映射）、BM25（k1=1.5/b=0.75、idf=ln(1+(N-df+0.5)/(df+0.5)) 空集
  0、每文档取最高块分）、best_spans（每文 ≤3 不重叠）、rrf=1/(60+rank)。
  零存储依赖；正文抽取（CWork envelope/frontmatter）属 builder 层。
  治理：code-ownership-manifest 增 R-runtime-rt051-controlled-read-lexical
  （认领 kb_gateway_client+kb_lexical，P2 的孤儿账一并还清；合成基线
  GA-STALE-RULE 抓到代表文件缺失——夹具清单同步 +2，交叉验证生效实证）。
  测试 28 例 + 治理 62 + RT-051 回归 232 + 快车道 + 双门禁全绿。
  剩余 P3b：builder（正文抽取→建代→原子发布）、gateway 融合路由
  （cwk.kb.search.v2/RRF/同资格域）、refresh 最终点 hook；P4 验收。

- 2026-09-06 P3b 落地（提交 3fddcbf）：**lexical_fusion_v1 正式开放**。
  kb_lexical.py 增序列化层（to/from_json_payload、generation_of 确定性
  建代=corpus 投影+引擎串无时间戳、chunk_id_of 域分离、extract_body
  保守 frontmatter 剥离、eligible_rows 资格域投影、引擎四段版本串）。
  新增 scripts/kb_lexical_builder.py（写面独立进程）：资格域逐件读回
  复核 SHA（漂移/缺件=build_refused 硬失败，不悄悄少件）、UTF8/空正文
  显式 excluded 计账、coverage_complete 口径（indexed==eligible）、同代
  幂等零写入、发布走账本（写后对账→留痕→重签）、publish 前重走 _collect
  断言 generation 一致。网关 _v2_load_lexical（missing/stale/corrupt 一律
  None：用当前 raw-index 资格域投影重算 corpus_digest 复核，过时代拒服务）
  + _v2_fusion_payload（cwk.kb.search.v2：两路同资格域、candidate_k=100、
  RRF=1/(60+rank)、matched_relation、hit 带 document_ref+候选 span 的 raw
  绝对字节坐标——同一条 read 路出引文、不另立证据路径）。
  capabilities lexical_modes=['lexical_fusion_v1']。测试 14 例（builder
  干跑/幂等/确定性/漂移拒发/坏行拒建/排除计账 + 融合 body-only 命中/
  RRF 双路/资格域隔离/span 字节读回/陈旧代 503+显式降级/重建恢复）；
  RT-051+治理回归 308 绿；快车道+双门禁绿。剩余 P3c refresh hook、
  P4 验收（A01–A12）。
