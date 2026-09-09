# RT-054 要求去留合同

> 目的：把“有来源的快速问答”与早期过度严格实现分开。这里改变的是新搜索链要求，不静默改写 RT-051 已发布的 v2 原文读取合同。

## 保留（MUST）

1. CWork/NAS 源只读；检索派生物不得回写源。
2. 服务端按 service account 的 `tenant_id + kb_id` 授权，客户端不能扩权。
3. 回答证据至少定位到文档、源版本、章节，以及适用的页/Sheet/行。
4. 无足够资料返回 `no_evidence`；系统不可用返回明确错误，两者不能混淆。
5. 源版本和摄取 SHA 在构建时验证，索引能从已批准源和规范化对象重建。
6. 新版本未完整 ready 时旧版本继续服务；部分失败不能推进可见 epoch。
7. 外部 Gateway 只调用 HTTPS Query API，不持有 NAS、PostgreSQL、OpenSearch 或对象存储管理凭据。
8. v2 list/open/read/document_ref 在迁移期保持兼容；真实调用量归零前不删除。

## 简化（SHOULD）

1. v3 搜索证据使用人可读 locator；UTF-8 byte span 仅在数据源能可靠提供时附带，不是搜索成功前置。
2. 查询不重新读取整件并重算 SHA；使用摄取时绑定的源版本/SHA。
3. 撤权保证“下一请求拒绝”和“发送前再鉴权”；不承诺撤回已发送字节。
4. 允许 Query API 使用有界内存/本地派生缓存；缓存不是事实源，必须可丢弃。
5. 权限第一阶段为知识库级；逐文档 ACL 只有真实需求和权威源协议后再设计。
6. Parent 用于上下文且只存一次；Child 用于召回，不复制整份 Parent。

## 删除（MUST NOT）

1. 不再生成或在线加载全库 `lexical-index.json` 作为新生产检索面。
2. 正文不再全量生成 CJK 1/2/3-gram。
3. 不再用 Python 自研全量 postings/BM25 作为生产排名主链。
4. 不用增加 timeout、常驻整包 cache 或更大内存掩盖索引结构问题。
5. 不让 Gateway 直连 NAS、PostgreSQL、OpenSearch；不向客户端返回可绕过授权的 `object_uri`。
6. 不把 HTTP 200、测试字符串存在或 cache 命中当作业务验收。

## 延后（MAY，需新门）

1. 向量召回、RRF、reranker：只有同一 gold 集 Recall@10 提升至少 5 个百分点、精确查询不退化、存储和 P95 仍过门才立项。
2. Redis/消息队列：只有 PostgreSQL job queue 经压测成为瓶颈才引入。
3. 逐文档 ACL、历史版本查询、知识图谱、统一 Answer API：各自需要真实业务问题和单独方案门。
4. 正式公网云部署、数据上云策略和供应商选择：属于生产/合规决定，不由本阶段自动执行。
5. 原版 WeKnora 同场实验：单独 Experiment RT，不 fork、不修改其核心源码。

## 与 RT-051 的关系

- RT-051 v2 `read` 的 byte range、document_ref、授权和错误合同继续有效；本 RT 不删、不重定义。
- v3 Search API 不把“全文 SHA 每请求重算”作为召回条件；如用户随后打开原文，仍可走 v2 read 取得原合同的验证结果。
- v2 `lexical_fusion_v1` 在 v3 客户端迁移完成前保留旧后端；只有能无损满足既有响应和错误语义时才允许转接新 SearchBackend，否则明确保持 legacy，不能伪造兼容。
