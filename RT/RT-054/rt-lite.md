# RT-Lite: RT-054 - 大库词法融合检索快照/预计算性能治理

> profile: Spec-Lite | execution_mode: collaborative
> 本文件是 RT-054 的唯一方案权威。2026-09-09 起，旧的「继续承载 1.5GB JSON 快照」方案被本稿取代；历史调查、P0 evidence 与 bounded-read 实现仍保留为事实和回归资产，不再代表目标架构。

## 方案（给人看）

- **做什么**：把 CWK 从「Gateway 在线下载并解析整包 JSON、自研全量 BM25」改造成「内网只读 Connector + PostgreSQL 控制面 + OpenSearch 检索面 + NAS/S3 对象层 + 统一 Query API」。同时重做父子拆片和中文字段设计，从源头削减索引膨胀；当前内网先落地，公网只保留安全、可迁移的接口和数据身份。
- **为什么**：当前 cwork-3m 仅 482 个可索引文档、66,500 chunks，就产生约 1.495GB 词法 JSON，并出现 425.685 秒冷请求。数据库只能改变访问方式，不能自动消除全文 1/2/3-gram、固定重叠和长 ID 重复造成的膨胀。预计规模是当前 100–200 倍，并保留 1000 倍可能；SQLite 单文件不适合作为长期生产底座，继续自研 postings/排名/快照也会把 CWK 变成弱化版搜索引擎。
- **代价**：增加 PostgreSQL、OpenSearch、索引 Worker 和统一 Query API；需要容器化部署、容量压测、备份恢复、监控和版本升级纪律。公网阶段还需云端私有网络与对象存储。代价换来的是百万至数千万 chunks 的可扩展检索、增量更新、多 Gateway 统一访问和明确的故障边界。
- **这次故意不做什么**：不 fork 或修改 WeKnora；不把 WeKnora 部署并入本 RT；不在第一阶段加入向量、知识图谱、reranker、Redis 或逐文档复杂 ACL；不让外部 Gateway 直接访问 NAS、PostgreSQL 或 OpenSearch；不把现有 1.5GB JSON 原样灌入数据库；不在方案门前修改产品代码、生产配置或部署。
- **用户怎样算成功**：现有三个真实知识库不再生成或加载大词法 JSON；同等资料的主检索索引至少缩小 80%；当前规模和 100 倍规模检索达到本稿性能门；指定知识库权限零串库；新增/更新文档能增量可见且失败不破坏旧版本；外部 Gateway 只凭受限身份调用 HTTPS Query API；系统能从 NAS/S3 原件和 PostgreSQL 状态重建 OpenSearch。
- **建议（定论）**：生产目标锁定为 PostgreSQL + OpenSearch，不再把 SQLite、ParadeDB 与 OpenSearch 并列。SQLite 只用于单元测试/本地夹具；ParadeDB 随原版 WeKnora 的独立对照实验评估。若真实 100 倍压测证明 OpenSearch 的成本不可接受，回方案门重新评估 WeKnora，不回到大 JSON 或自研倒排表。

## 一、范围与需求边界

### 1.1 核心产品目标

CWK 的核心目标收敛为：

> 获得授权的用户或 Gateway，能够快速从指定的数据文件集合中找到足以回答问题的内容，并看到可理解的来源；资料不足时明确说明未找到。

### 1.2 必须保留

1. CWork/NAS 源保持只读；派生索引不能回写源。
2. 请求只能访问服务端授权的 `tenant_id + kb_id`，客户端参数不能扩大权限。
3. 回答来源至少包含文档、版本、章节，以及适用的页码、Sheet/行号或段落定位。
4. 无足够证据时返回明确的无答案状态，不用模型常识补成“资料结论”。
5. 索引是可丢弃派生物，能从批准的数据源与规范化对象重建。
6. 文档更新失败、索引构建失败或新版本未完成时，旧的已完成版本继续可用。
7. 公网访问只经过 HTTPS Query API；外部 Gateway 永不持有 NAS、PostgreSQL 或 OpenSearch 凭据。

### 1.3 明确降级或删除的过度设计

1. 搜索结果不再要求每条引文都精确到 UTF-8 byte span；保留人可理解的来源定位。已有 v2 `read` 的 byte 合同继续兼容，不作为新搜索链每次查询的前置。
2. 查询时不重新下载全文并计算整件 SHA；SHA 在摄取/规范化阶段验证并绑定版本。
3. 不承诺撤权能撤回已经发送的响应；保证撤权后的新请求拒绝，响应发送前再做一次授权检查。
4. 不把 GET/查询进程“绝对零磁盘写”当产品目标；允许有界、可丢弃、无正文日志的运行缓存。源系统仍零写。
5. 不继续建设多层 JSON generation、全量双 collect 和自研 CAS 来模拟数据库事务；版本、任务和切换状态进入 PostgreSQL。
6. 第一阶段权限粒度是知识库级，不实现逐 chunk ACL；未来出现真实逐文档权限需求时另过方案门。

## 二、已核实的现状与证据边界

### 2.1 当前代码事实

- `scripts/kb_gateway.py` 的词法路径会读取完整 `lexical-index.json`；当前 Gateway 仍基于单进程 `HTTPServer`。
- `scripts/kb_lexical.py` 保存 chunk、长度、term postings、term frequency、byte/code-point spans，并在 Python 中执行 BM25、best spans 与 RRF。
- `scripts/kb_lexical_builder.py` 从当前 `raw-index` 收集文档并发布 `_system/lexical-index.json`。
- 静态代码和 RT-054 P0 已确认旧评分存在重复求 `avgdl`、重复 query tokenization、全 chunk 扫描；旧大 JSON 还叠加网络、解析和内存成本。
- `scripts/kb_storage.py` 已具备 LocalFS/Memory/FileStation 抽象和 bounded-read；该能力继续服务安全读取与诊断，不再用于把巨大词法 JSON搬进 Gateway。
- RT-051 的 list/open/read/document_ref 合同已经存在；本 RT 新搜索面必须兼容其已授权文档读取能力，不能用新检索引擎替代事实源。

### 2.2 三个真实知识库的只读体量

| 知识库 | 文件数 | 总大小 | JSON 数/大小 | 词法索引 | 有效文档 | chunks | 词法索引占总量 |
|---|---:|---:|---:|---:|---:|---:|---:|
| cwork-3m | 1,100 | 约 1.65GB | 21 / 约 1.50GB | 约 1.495GB | 482 | 66,500 | 90.8% |
| docdb-touqian | 315 | 约 60MB | 22 / 约 47.2MB | 约 46.5MB | 114 | 1,008 | 77.6% |
| spbp-2027 | 317 | 约 325MB | 22 / 约 265MB | 约 264.5MB | 90 | 3,984 | 81.4% |

投前与 SPBP 文件数近似（315/317），词法索引相差约 5.7 倍，说明文件数量不能解释索引成本。当前主要放大器是正文全量 CJK 1/2/3-gram、最多约 15% 固定重叠、统一固定块、posting 中重复长 chunk ID、多份映射和 JSON 文本编码。

### 2.3 规模假设

以当前最大库为 1 倍：

- 100 倍：约 5 万文档、665 万 chunks；
- 200 倍：约 10 万文档、1,330 万 chunks；
- 1000 倍：约 50 万文档、6,650 万 chunks。

100–200 倍是目标容量；1000 倍是架构演进上限，不在当前 OPS 上直接承诺。任何“支持 1000 倍”的说法必须来自多节点完整或代表性压测，不能只线性外推。

### 2.4 外部参考事实

WeKnora 固定审查版本为 commit `8d7298fb5d759973cb1e481cadc5ecdf16dca599`：默认 PostgreSQL/ParadeDB，Redis 协调任务，存储驱动支持本地与多种对象存储，检索驱动可选 PostgreSQL、OpenSearch、Qdrant、Milvus 等；父块进业务库、Child 进入检索，关键词与向量结果在服务层做 RRF。CWK 采用其“数据库管状态、检索引擎管召回、对象存储管正文、父子块分工”的成熟原则，但不复制其完整产品代码。

### 2.5 证据限制

- 上述三库数据来自 2026-09-09 对 NAS 元数据和索引头部的有界只读统计，未下载三个完整索引。
- OPS 已核实有 64GB 内存和约 460GB 可用空间；这只足够当前 PoC 与有限放大测试，不构成 1000 倍生产容量证明。
- 本稿的磁盘和节点规划是候选预算，最终以阶段 E 的真实 OpenSearch 指标为准。

## 三、目标架构

```text
公司内网                                         云端演进
┌────────────────────────────┐                 ┌────────────────────────┐
│ NAS / CWork 原始资料        │                 │ S3 兼容对象存储         │
│          │ 只读             │                 │ PostgreSQL HA           │
│ CWK Connector              │──出站 HTTPS───▶│ OpenSearch 集群          │
│ Parser / Normalizer        │                 │ Query API + Worker       │
│          │                  │                 └──────────┬─────────────┘
│ PostgreSQL                  │                            │ HTTPS
│ OpenSearch                  │                 ┌──────────▼─────────────┐
│ Parent 对象缓存/NAS Adapter │                 │ 内外部 OpenClaw Gateway │
│ Query API                   │                 └────────────────────────┘
└──────────┬─────────────────┘
           │ 内网 HTTPS
     内部 Gateway
```

### 3.1 组件职责

#### Connector（源连接器）

- 唯一可以读取公司 NAS/CWork 的新组件；只读运行。
- 发现新增、修改、删除，形成稳定 `source_id/source_version/source_sha256`。
- 解析和规范化后，把 Parent 对象、元数据和索引任务交给内部控制面。
- 云端阶段只主动向外发起 HTTPS；云端不反向进入公司内网。

#### PostgreSQL（控制面）

保存需要事务和关联查询的“小而关键”状态：租户、知识库、文档、版本、Parent 元数据、服务身份、知识库授权、索引任务、活动 epoch、schema/chunker/analyzer 版本、失败/重试和审计。它不保存倒排 postings，不保存重复 Child 正文。

#### OpenSearch（检索面）

只保存可重建的 Child 检索投影：租户/库/文档/Parent/版本、标题、章节、正文分析字段、精确标识字段、来源定位和 epoch 可见区间。第一阶段只做 BM25；向量是后续有数据门的增强项。

#### ObjectStore（对象层）

- NAS 原件是当前事实源。
- 规范化 Parent 通过 `ObjectStore` 接口访问；内网实现可以是 NAS/受控本地缓存，公网实现是 S3/MinIO/COS。
- OpenSearch 命中后只批量读取 Top Parent，不扫描或下载全库。
- OpenSearch `_source` 不保存第二份完整 Parent；正文分析字段允许从 `_source` 排除，以索引倒排结构、Parent 对象负责展示上下文。

#### Query API（统一查询服务）

- 内外部 Gateway 的唯一知识检索入口。
- 负责身份验证、授权、OpenSearch 查询、Parent 批量取回、去重、来源组装和审计。
- PostgreSQL、OpenSearch、NAS/ObjectStore 均不直接暴露给 Gateway。

#### Worker（异步索引执行）

- 从 PostgreSQL 任务表以 `FOR UPDATE SKIP LOCKED` 领取任务。
- 完成解析后的 Bulk 写入、旧版本失效、计数校验和任务状态更新。
- 第一阶段不用 Redis；只有 PostgreSQL 队列经压测成为瓶颈才另立扩展决定。

## 四、权威数据模型

### 4.1 全局身份

所有身份独立于物理路径：

- `tenant_id`：租户稳定 UUID；
- `kb_id`：知识库稳定 UUID，另有可读 slug；
- `doc_id`：文档 lineage 的稳定 UUID；
- `source_version`：同一文档单调整数版本；
- `parent_id`：`doc_id + source_version + section ordinal + chunker_version` 的稳定 ID；
- `chunk_id`：`parent_id + start/end + analyzer projection version` 的稳定 ID；
- `index_epoch`：知识库可见版本的单调整数；
- `object_uri`：服务端 locator，不能返回 NAS 凭据或可绕过授权的物理地址。

### 4.2 PostgreSQL 核心表

```text
tenants
knowledge_bases
source_connectors
documents
document_versions
parents
index_jobs
kb_index_epochs
service_accounts
service_account_kb_grants
audit_events
```

关键状态机：

```text
discovered → parsing → normalized → indexing → validating → ready
                      ↘ failed / superseded / deleted
```

规则：

1. `documents.current_version` 只指向完整 ready 版本。
2. 新版本失败时旧 ready 版本仍服务。
3. 删除先取消控制面授权/可见性，再异步清理 OpenSearch 和对象。
4. 任务幂等键为 `(kb_id, doc_id, source_version, pipeline_version)`。
5. 同一文档同版本重复提交只复用已存在结果，不生成重复 chunks。

### 4.3 OpenSearch Child mapping

必需字段：

```text
tenant_id          keyword
kb_id              keyword
doc_id             keyword
source_version      long
parent_id           keyword
chunk_id            keyword
generation_schema   keyword
valid_from_epoch    long
valid_to_epoch      long/null
title               text + keyword
section_path        text + keyword
body                 text（不进 _source 或仅 PoC 对照）
identifiers          keyword[]
entity_names         keyword[]
date_values          date[]
page_start/end       integer
sheet_name           keyword
row_start/end        integer
char_start/end       integer（相对规范化 Parent，仅用于上下文窗口）
object_uri           keyword（服务端使用，不返回客户端）
```

约束：`dynamic: strict`；标识、租户、库、版本、epoch 使用 doc values；正文关闭 doc values；禁止把原始任意 metadata 整包写入 mapping。

### 4.4 增量可见性协议

单文档更新使用知识库 epoch，避免 PostgreSQL 与 OpenSearch 假装有跨库事务：

1. 在 PostgreSQL 预留 `next_epoch = current_epoch + 1`。
2. Bulk 写入新 Child，`valid_from_epoch=next_epoch`。
3. Bulk 更新旧版本 `valid_to_epoch=current_epoch`。
4. 复核新旧 chunk 数、失败项和抽样检索；任一步失败不推进 epoch。
5. PostgreSQL 原子推进 `current_epoch=next_epoch` 与 `documents.current_version`。
6. Query API 每次读取 current epoch，并过滤 `valid_from <= epoch <= valid_to/null`。
7. 后台清理超过保留窗的旧 Child；清理失败不影响当前 epoch。

mapping/analyzer/chunker 发生不兼容变化时，不走逐文档更新：建立新物理索引，完整验证后通过 alias 切换；旧索引保留到回滚窗结束。

## 五、解析、Parent/Child 与索引降复杂度

### 5.1 规范化规则

- 保留标题层级、段落、列表、表格、代码块、公式和页/Sheet/行定位。
- 去除可判定的重复页眉页脚、固定签名、导航和模板；无法确定是否正文时宁可保留。
- 不用模型改写索引正文；模型摘要不得替代原始规范化内容。
- 每个 Parent 保存 parser/chunker 版本和源 SHA；查询时不重新全件计算 SHA。

### 5.2 Parent

- 优先以标题章节、PPT 页、Excel 表格组、连续 PDF 小节形成 Parent。
- 候选上限：约 4,000 Unicode code points；超长按段落/表格组递归分割。
- Parent 仅作为回答上下文和来源单元，不建立全文或向量索引。

### 5.3 Child

初始冻结参数：

- 目标约 900 Unicode code points；
- 硬上限 1,400；
- 默认 overlap=0；
- 只有连续长段在安全边界无法切开时，最多重叠 80 code points；
- 标题和 `section_path` 作为独立字段，不复制进每个正文多次；
- 表格按表头 + 有界行组切分，代码/公式保持不可拆原子块，超限明确标记。

参数调整必须产生新的 `chunker_version` 并重跑同一质量/体积基准，不能在线静默变化。

### 5.4 中文字段设计

1. 正文取消现有全量 CJK 1/2/3-gram。
2. PoC 只比较 OpenSearch 官方、版本匹配的 `analysis-icu` 与 `analysis-smartcn` 两个候选；不引入第三方 IK 作为生产强依赖。
3. 同一 gold 集中，先满足质量门，再选主索引更小、升级依赖更少者；质量/大小接近时默认选择 ICU。
4. 编号、合同号、日期、金额、英文缩写和可识别主体名进入 `keyword` 精确字段，不依赖中文分词碰运气。
5. 标题/章节可保留有限辅助 analyzer；正文不恢复全量字符 n-gram。
6. 查询只分析一次；多字段 BM25 初始权重为 title 3、section 2、body 1，精确 identifier 命中作确定性提升。权重只能由 gold 集调优并版本化。

### 5.5 初始召回与上下文

1. Query API 授权并取得固定 `tenant_id/kb_ids/current_epoch`。
2. 解析精确标识与普通查询词；对 OpenSearch 注入服务端过滤。
3. 每路最多取 100 个 Child；按文档/Parent 限流，避免长文霸榜。
4. 默认每文档最多 3 个 Child、每 Parent 最多 2 个；最终取 8–12 个证据窗口。
5. 批量读取对应 Parent，仅展开命中 Child 周边的有界上下文。
6. 返回文档、版本、章节、页/Sheet/行与证据片段；不把 BM25 分数冒充置信度。
7. 无命中与系统不可用分开：零命中为成功的 `no_evidence`，索引/控制面不可核实为显式 503。

## 六、Query API 与兼容策略

### 6.1 新 API

优先稳定检索 API，回答 API 后置：

```text
POST /v3/kb/search
Authorization: Bearer <short-lived service identity>
{
  "kb_ids": ["..."],
  "query": "...",
  "top_k": 10,
  "request_id": "..."
}
```

响应至少包含：请求/生效库、current epoch、pipeline/analyzer 版本、检索耗时分段、`no_evidence`、hits，以及每个 hit 的 doc/version/section/locator/evidence excerpt。错误响应不含正文、物理路径、token、数据库地址或底层异常。

`POST /v3/kb/answer` 不进入第一批切换；先由外部 Gateway 使用 search 证据调用自己的模型。只有 Search API 稳定后，才决定是否由 CWK 统一提供回答。

### 6.2 旧接口

- 现有 `/v2/kb/libraries|search|read` 在迁移期继续存在。
- `/v2/kb/search?...lexical_fusion_v1` 通过兼容适配器调用新 SearchBackend，但响应形状保持；无法无损映射的字段明确标 deprecated，不能伪造。
- v2 `document_ref/read` 继续提供已批准的原文读取能力；新 v3 搜索不要求每个 hit 先完成全文 SHA 回读。
- 切换和退役必须有真实调用量与客户端升级证据，不能只凭代码存在删除旧接口。

### 6.3 服务实现

- 新 Query API 使用成熟 ASGI 服务栈与官方 OpenSearch/PostgreSQL 客户端，依赖固定版本并有锁文件/镜像摘要；不继续扩大单线程 `HTTPServer`。
- 当前 `kb_gateway.py` 先做兼容层；新服务稳定后再决定是否退役其旧 HTTP server。
- 搜索后端通过 `SearchBackend` 接口隔离，接口覆盖 health、search、bulk-index、invalidate-version、stats；不把 OpenSearch DSL 泄露给 Gateway/Agent。

## 七、权限、安全与公网演进

### 7.1 当前内网

- Connector、PostgreSQL、OpenSearch、ObjectStore 和 Query API 先部署在内网。
- Gateway 只访问 Query API；即便同一局域网也不直连数据库/NAS。
- 现有绑定 token 可作为兼容身份，但服务端必须映射为 service account + kb grants。

### 7.2 外部 Gateway 试用

- 通过零信任专网/VPN 访问内网 Query API。
- 只开放 HTTPS API，不开放 NAS、9200、5432 或对象存储管理端口。
- 适用于少量可信 Gateway；公司网络中断时外部查询不可用，必须如实显示。

### 7.3 正式公网

- Query API、PostgreSQL、OpenSearch 和允许上云的对象副本进入云端私有网络。
- 内网 Connector 仅通过出站 HTTPS 增量同步；云端不主动进入公司内网。
- API 前置 TLS、WAF/限流、请求体上限、短期身份、审计与密钥轮换。
- 每个 Gateway 使用独立 service account，绑定 tenant/kb，只授予 search/read-evidence 权限，可单独吊销。

### 7.4 知识库数据策略

每个知识库必须选择且可审计：

- `cloud_replica`：原件/规范化 Parent/索引可上云；
- `derived_only`：原件留内网，只同步批准后的规范化文本和检索投影；明确承认云端仍持有可读内容；
- `onprem_only`：资料和索引均留内网，外部只能经零信任网络调用内网 API，或被拒绝。

“资料不能离开公司”与“公司断网时外部仍可查询”不可同时满足；此冲突必须由业务数据 owner 选择，系统不能用技术措辞掩盖。

### 7.5 日志与隐私

- 默认不记录 query 正文、证据正文、token、路径和原始异常；记录 request_id、service account、tenant/kb、耗时、结果数、状态码和版本。
- 需要查询审计时采用受控开关、最短留存和脱敏策略，另行批准。
- 凭据只来自受控环境/secret store，不入 Git、RT、命令参数和日志。

## 八、容量、分片、存储与恢复

### 8.1 容量预算

当前目标是同等语料的 OpenSearch 主索引不超过旧词法 JSON 的 20%。若 cwork 1 倍主索引达到 300MB：

- 100 倍约 30GB 主索引；
- 200 倍约 60GB；
- 1000 倍约 300GB。

生产一份 replica 约翻倍；segment merge/升级再预留至少 50% 运行空间；快照另放对象存储。因此 1000 倍的候选在线+余量接近 900GB，不适合当前仅约 460GB 可用空间的 OPS 单机。

### 8.2 分片策略

- 不按每个知识库建立物理索引，避免大量小分片；同环境共享索引，以 tenant/kb 字段过滤。
- 物理索引按 schema/analyzer generation 管理，目标主分片约 20–40GB。
- PoC 单节点 `replicas=0`，不得称高可用生产。
- 内网生产是否单节点由可用性要求决定；正式公网至少一份 replica，并通过节点故障实测决定 3 个组合节点或专用 master/data 拓扑。
- shard 数不提前写死；阶段 E 用 100/200 倍数据确定。发现分片选择错误时重建新索引并 alias 切换，不在线拆补丁。

### 8.3 初始加载与增量

- 初始 Bulk 建库期间延长 refresh interval 或暂时关闭自动 refresh，按批次失败项重试；完成后恢复查询设置并 force merge 仅在实测证明有收益时执行。
- Bulk 的文档数/字节上限由 PoC 找到安全值，客户端同时限制请求字节和 in-flight 数。
- 增量目标：普通文档从发现到 ready P95 ≤5 分钟；大文/异常显式排队或失败，不阻塞其他文档。

### 8.4 备份与恢复

- PostgreSQL：生产使用定期全备 + WAL/PITR；恢复演练必须包含 grants、current epoch 和任务状态。
- OpenSearch：定期 snapshot 到独立对象存储；索引仍可从 Parent 对象和 PostgreSQL 重建。
- ObjectStore：版本化、生命周期和跨故障域备份由部署环境决定。
- 恢复顺序：PostgreSQL → ObjectStore 可读性 → OpenSearch snapshot/重建 → count/抽样/gold 验证 → Query API 放流。

## 九、可观测性与失败语义

必须采集：

- API QPS、P50/P95/P99、4xx/5xx；
- OpenSearch took、rejected、timeout、heap、GC、segments、shards、磁盘水位；
- PostgreSQL 连接、慢查询、锁等待、job queue depth；
- Connector 同步滞后、文档状态和失败原因类别；
- 索引 bytes/document、bytes/chunk、chunks/document、重复率；
- ObjectStore 命中、延迟、字节和缓存命中；
- current epoch、alias、pipeline/chunker/analyzer 版本。

失败必须区分：unauthorized/forbidden、no_evidence、source_stale、index_not_ready、search_timeout、control_plane_unavailable、object_unavailable、capacity_exceeded。禁止将系统错误降级为空结果或旧版本成功。

## 十、分阶段实施计划

### 阶段 A：冻结合同与基线（方案门通过后首先执行）

产出：

1. 现有三库 1 倍基准：原文/JSON/词法大小、文档/chunk/term 数、构建和查询分段耗时、RSS。
2. 50–100 题 gold 集：正文、标题、中文短词、精确编号、公司/人名、日期、表格、无答案、权限负例。
3. v2 兼容合同和 v3 Search API schema。
4. 旧要求保留/降级/删除的逐项表；RT-051 read 合同不被静默改写。

完成门：基准可重复；gold 由人工读过；方案中的成功标准能被真实行为判据证伪。

### 阶段 B：精简 Parent/Child 与 analyzer PoC

只用脱敏/批准数据离线执行：

1. 实现结构化 Parent/Child 投影，不接 Gateway。
2. 比较旧 1/2/3-gram、ICU、SmartCN；记录索引大小、term 数、Recall@10 和精确查询。
3. 比较 body 进入/排除 `_source` 的磁盘和取回代价。
4. 固定 chunker/analyzer/mapping v1。

完成门：三库等价语料的主索引相对旧 JSON 缩小 ≥80%；gold macro Recall@10 不低于旧实现且 ≥0.90；精确编号 Recall@10=1.00；无串库。

### 阶段 C：PostgreSQL 控制面与索引 Worker

1. 建 migration/schema、文档状态机、任务幂等和 kb epoch。
2. Connector 只读发现变更，Parent 对象与任务分开提交。
3. Worker 完成 Bulk、旧版失效、验证、epoch 推进、失败重试。
4. 新增脚本时同步 AODW ownership manifest 和模块索引，不能产生 GA-ORPHAN。

完成门：新增、修改、删除、重复提交、Worker 崩溃、部分 Bulk 失败、重试和旧版继续服务均有行为测试；破坏 epoch 接线时判据必须变红。

### 阶段 D：Query API 与影子流量

1. 实现 v3 Search API、SearchBackend、授权、Parent 批取和来源组装。
2. v2 search 兼容适配器接新后端；旧 JSON 仍是正式路径。
3. 同一请求执行影子检索，只记录脱敏差异和耗时，不改变用户响应。
4. 完成返回前授权复核、超时、限流和错误分类。

完成门：连续影子运行无未解释系统错误；固定问题集与人工产出复核通过；权限负例零泄漏；Query API 不暴露底层 locator/凭据。

### 阶段 E：容量与故障验证

分层执行：

1. 1 倍：三库真实脱敏数据完整构建与查询。
2. 100 倍：约 665 万 chunks 的代表性全量压测；并发 20/50/100，读写混合。
3. 200 倍：约 1,330 万 chunks，验证分片、恢复、增量和磁盘线性。
4. 1000 倍：在获批成本环境做 6,650 万 chunks 完整或足以证明瓶颈的多节点代表性压测；未执行前只能称“设计可演进”，不能称“已支持”。

完成门：满足第十一节容量/性能/可靠性门；节点数、磁盘、heap、shard 和副本形成实测部署表。

### 阶段 F：逐库切换

顺序固定：docdb-touqian → spbp-2027 → cwork-3m。

1. 每库先 shadow，再小流量，最后全量。
2. 切换只改变服务端 backend/alias，不改变 NAS 原件。
3. 旧 JSON 保留回滚窗；任何门失败切回旧路径。
4. 三库稳定后停止生成新大 JSON；旧文件归档/删除是独立维护授权，不在本 RT 自动执行。

### 阶段 G：公网准备与 WeKnora 对照

- 当前 RT 只把 Connector/ObjectStore/Query API/身份设计做到可迁移，并完成内网部署证据。
- 外网少量 Gateway 先经零信任专网验证。
- 正式云端部署、数据出境策略和生产成本需要新的生产授权。
- 原版 WeKnora 使用同资料、同问题、同硬件级别做独立 Experiment RT；禁止 fork，记录部署、升级、检索、存储和自研代码成本。若 WeKnora 总成本/体验明显优于 CWK，则停止继续产品化 CWK，而不是为保项目而扩大范围。

## 十一、验收门

### 11.1 存储

- cwork-3m 主检索索引 ≤300MB；
- spbp-2027 ≤53MB；
- docdb-touqian ≤10MB；
- 相对旧 JSON 均缩小 ≥80%；
- 100/200 倍 bytes/chunk 近似线性，偏离 >25% 必须解释；
- 统计 OpenSearch primary store，不能拿压缩 snapshot 或不含必要字段的半成品冒充。

### 11.2 性能（不含 LLM）

- 当前规模 Search API 暖态 P95 ≤500ms、P99 ≤1s；
- 100 倍规模 P95 ≤1s、P99 ≤2s；
- 200 倍规模不得超过 100 倍目标的 1.5 倍；
- API 系统错误率 <0.1%；
- 查询内存不随全库正文线性加载；
- 不再下载/解析 1.5GB 词法 JSON；
- 单文档增量 ready P95 ≤5 分钟（超大文单列）。

### 11.3 质量

- 固定 gold macro document Recall@10 ≥0.90 且不低于旧实现；
- 精确编号 Recall@10=1.00；
- 正文-only、中文短词、表格和无答案各自报告，不得用总平均掩盖失败类别；
- 每个返回证据能定位到当前文档版本与人可读位置；
- AI 评审不替代人工读至少 20 个真实脱敏查询产出。

### 11.4 权限和公网边界

- tenant/kb 跨域泄漏为 0；
- 客户端伪造 kb、filter、object_uri 不能扩大权限；
- 撤权后的下一请求拒绝，响应发送前再鉴权失败时不返回正文；
- 外部 Gateway 无 NAS/DB/OpenSearch 凭据；
- 公网扫描不能直达 5432/9200/对象存储管理面；
- 日志不含 query/正文/token/物理路径。

### 11.5 一致性与恢复

- 新版本未 ready 不可见，失败不破坏旧版；
- Bulk 部分失败不推进 epoch；
- alias/epoch 回滚经过真实行为测试；
- PostgreSQL 恢复、OpenSearch snapshot 恢复和从对象重建各完成一次演练；
- 单节点故障的用户影响与恢复时间有实测，不用“有 replica”替代演练。

### 11.6 AODW 与仓库门

- 每阶段跑对应定向测试；新增判据做真实破坏实验。
- `make governance-audit`、`make aodw-check`、`git diff --check` 通过。
- 完整实现收口前 `make ci` 通过；其耗时不能用局部测试替代。
- 独立 AI 评审检查：是否解决错问题、判据是否空、权限过滤能否绕过、epoch 是否假原子、容量结论是否外推。
- 人工读三库真实脱敏搜索结果、错误响应、同步状态和公网身份流；测试绿不等于用户目标达成。

## 十二、计划改动面与所有权

预计修改：

- `scripts/kb_gateway.py`：旧 v2 兼容适配与逐步退役单线程路径；
- `scripts/kb_gateway_client.py`：v3 Search API 客户端合同；
- `scripts/kb_lexical.py`：降为旧实现/等价基准，不再是生产排名引擎；
- `scripts/kb_lexical_builder.py`：过渡构建与旧 JSON 停产开关；
- `scripts/kb_ingest.py`、`scripts/kb_storage.py`：Connector/ObjectStore 接缝和源身份复用；
- `.aodw-next/06-project/governance/code-ownership-manifest.json` 与 `modules-index.yaml`：新运行文件逐条归属。

预计新增（名称在实现前再做冲突检查，不预登记不存在文件）：

```text
scripts/kb_search_backend.py
scripts/kb_opensearch.py
scripts/kb_control_db.py
scripts/kb_index_worker.py
scripts/kb_connector.py
scripts/kb_query_service.py
config/kb-search-*.example.*
tests/test_rt054_*.py
```

外部依赖必须单独锁定并进入供应链检查，候选包括官方 `opensearch-py`、PostgreSQL driver 和 ASGI 栈；在阶段 A/B 先确定最小依赖集合，不允许每个脚本各自实现 HTTP/连接池/重试。

## 十三、开发提交与推进节奏

建议按可回滚边界提交，不把整个重构压成一个提交：

1. `docs(rt054): converge search architecture and acceptance gates`
2. `test(rt054): freeze corpus and retrieval baselines`
3. `feat(rt054): add parent-child projection and analyzer benchmark`
4. `feat(rt054): add control-plane schema and index worker`
5. `feat(rt054): add opensearch search backend`
6. `feat(rt054): add v3 query service and v2 compatibility`
7. `test(rt054): add scale failure and recovery gates`
8. `perf(rt054): validate shadow traffic and staged cutover`

每个提交只在本 feature worktree 完成、运行相应验证并带 `Refs: RT-054`。未经收口门，不合并 main、不推送、不改生产、不清理 worktree。

## 十四、停止条件与改选条件

任一条件成立即停止继续堆 CWK 功能并回方案门：

1. 精简后当前三库索引仍不能缩小 80%，且质量要求无法解释该成本；
2. 100 倍规模需明显超出可接受节点/磁盘预算；
3. 为达到 WeKnora 已提供的基本能力，CWK 需要持续复制其账户、管理界面、任务和运维系统；
4. OpenSearch 官方中文 analyzer 无法达到 gold 门，而解决方案依赖高风险第三方插件；
5. 外部数据合规不允许任何可检索文本上云，但业务又要求公司断网时公网可查；
6. 原版 WeKnora 对照在质量、总成本、升级和用户体验上明显胜出。

改选顺序固定：先评估原版 WeKnora；不回到 SQLite 生产方案，不恢复大 JSON，不继续自研倒排引擎。

## 验证

### 当前方案阶段

- 已读取 AODW 宪章、交互规则、项目 overview、RT manager、Spec-Lite、Git 与 test discipline。
- 已打开当前 Gateway、词法 builder/原语、StorageBackend、RT-051 合同、RT-054 evidence 与治理所有权入口。
- 已核对 WeKnora 固定 commit 的数据库/检索/对象存储驱动和父子块/RRF 主链路。
- 阶段 A 已完成：三库基线、72 题 gold 与 fixture、v2/v3 合同和要求去留表已冻结，独立复核由初审 FAIL 修复至二审 PASS。
- 阶段 A 未修改产品运行代码、OPS/NAS、生产配置或部署；旧链缺失的 terms/build/成功延迟数据显式保留为未知，由阶段 B 新 builder 原生测量。
- 阶段 A 运行合同测试、JSON Schema、RT guard、AODW 和 governance 门；完整 `make ci` 仍留在产品实现收口，不用文档/合同门冒充产品验证。

### 实现收口必须补齐的三格证据

- **工程判据**：每个合同对应真实故障和破坏实验，覆盖权限绕过、epoch 不推进、Bulk 部分失败、旧版误可见、跨库、alias 回滚、对象不可用与容量超限。
- **独立 AI 评审**：具体审查架构是否做错问题、判据是否空、OpenSearch mapping/epoch 是否自洽、容量结论是否超出证据。
- **读真实产出**：人工读三库真实脱敏搜索结果、来源、无答案、错误与同步状态；公网试用时按真实 Gateway → Query API 路径验证。

## 变更记录

- 2026-09-07～08：完成旧大 JSON 性能诊断、bounded-read 合同与 pure-local 验证；P1b 快照实现保持 NO-GO。
- 2026-09-09：基于 WeKnora 源码审查、三库体量、100–1000 倍规模与公网 Gateway 目标，放弃“扩大/缓存 JSON”和 SQLite 生产路线；方案收敛为 PostgreSQL 控制面、OpenSearch 检索面、对象层、Connector 与 Query API。
- 2026-09-09：方案门通过；阶段 A 冻结三库证据基线、72 题 gold、v2/v3 合同和要求去留，独立复核初审 FAIL 后补齐 fixture/行为判据并二审 PASS。
- 后续按阶段 B→F 开发；阶段 G 只做公网准备与独立对照，不自动部署生产。

## 遗留事项

- 正式公网云端供应商、区域、节点规格、月度预算、数据出域审批：当前没有足够业务/合规输入，阶段 E 后作为生产部署决定，不阻塞内网架构实现。
- WeKnora 原版部署与同场比较：建议单独建立 Experiment RT，使用本 RT 固定的数据集和验收口径；不在 RT-054 内 fork 或修改其源码。
- 向量召回：只有 BM25 gold 结果显示语义问题确有缺口，且加入向量使 Recall@10 提升至少 5 个百分点、精确查询不退化、存储和 P95 仍过门时，才建立后续增强 RT。
