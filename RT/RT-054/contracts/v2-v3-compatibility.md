# RT-054 v2/v3 兼容合同

## 定论

v3 是新的检索合同，不是对 v2 原文读取合同的原地改义。迁移期两者并存：

- v2 `libraries/resolve/open/read`：保持现有行为与 schema。
- v2 metadata search：保持现有行为。
- v2 `lexical_fusion_v1`：阶段 D 前继续走 legacy JSON；只有完成逐字段等价验证后才可切到新后端。
- v3 `POST /v3/kb/search`：新的 PostgreSQL/OpenSearch 查询入口。

## v3 请求边界

客户端只可提供 `kb_ids/query/top_k/request_id`。禁止提供 `tenant_id`、OpenSearch DSL、任意 filter、物理路径、`object_uri`、index 名称、epoch 或数据库参数。租户、允许库和当前 epoch 全部由服务端身份与控制面注入。

## v3 成功响应

- `schema=cwk.kb.search.v3`、`ok=true`。
- `effective_kb_ids` 只包含获授权且实际执行的库；任一请求库无权时整个请求拒绝，不做“悄悄少查一个库”。
- `epochs` 逐库给出本次一致性视图。
- `no_evidence=true` 表示查询成功但零证据；系统故障不得返回该状态。
- hit 提供 `kb_id/doc_id/source_version/title/section_path/locator/excerpt/rank/score_kind`。
- `score_kind=bm25_rank_v1` 不是概率或事实置信度。
- 响应不含 `object_uri`、NAS path、OpenSearch index、token 或底层异常。

## v3 错误响应

统一 `cwk.kb.error.v3`：

- 400 `bad_request|unsupported_contract`
- 401 `unauthorized`
- 403 `forbidden`
- 409 `source_stale`
- 429 `rate_limited|capacity_exceeded`
- 503 `index_not_ready|control_plane_unavailable|object_unavailable`
- 504 `search_timeout`

错误必须有 `request_id`、`retryable`；可重试时允许 `retry_after_ms`。不得带正文、query、物理路径、token 或原始异常。

## v2 转接新后端的前置

必须全部满足后，才能将 v2 `lexical_fusion_v1` 从 legacy 切到新 SearchBackend：

1. 既有 48 题逐字段回归通过；
2. v2 `document_ref` 仍指向当前源版本；
3. 候选 span 能按旧 byte 语义无损生成；不能生成时继续 legacy，不填假 span；
4. `matched_relation/candidate_truncated/degraded/effective_mode/generation` 有确定映射；
5. missing/stale/corrupt/撤权错误码不退化；
6. 客户端协议测试和真实已安装 Skill 路径通过。

## 退役条件

v2 lexical 只有在以下事实同时成立后才可另行批准退役：

- 所有受管 Gateway/Skill 已支持 v3；
- 连续观察窗内 v2 lexical 调用量为零；
- v2 read 仍有独立替代或明确继续保留；
- 已验证回滚不会让旧客户端静默得到错误结果。
