# v2 读链参数表（KB Gateway 1.2.0）

## 操作总表（全 GET，/v2/kb/* 前缀）

| 操作 | 必选参数 | 可选参数 | 说明 |
|---|---|---|---|
| capabilities | kb | - | 能力卡：lexical_modes、max_bytes 上下限、schema 版本 |
| list | kb | page_size(≤100), cursor | 库枚举，游标绑快照 digest（索引变→409） |
| search | kb, q | retrieval_mode, allow_degraded, page_size, cursor | 融合/元数据双模式；lexical 模式无游标 |
| resolve | kb, lineage | version | 签发 document_ref（15min TTL）；version≠当前→409 |
| inspect | kb, document_ref | - | 元数据视角：bytes/encoding/status，不读正文 |
| read | kb, document_ref | max_bytes / start_byte+end_byte / line_start+line_end（互斥）, cursor | 读正文页；每页带 identity+full_sha_verified |
| continue | kb, document_ref, cursor | - | 续读（read 返回的 next_cursor 喂回来） |
| renew | kb, document_ref | - | 句柄续期（宽限 10min；换发新句柄） |

## read 三模式（互斥，同给多个→400）

1. **cursor 续读**：continue 动词 + cursor
2. **字节范围**：start_byte + end_byte（含头不含尾）；融合命中的 candidate_spans 直接可用
3. **行模式**：line_start + line_end

默认（无参数）：从 0 读到 max_bytes 页。

## 三个易错点

1. **max_bytes 上限 65536**（64KiB/页）。给大了→400。全文就 continue 链翻页
2. **span 坐标是字节不是字**。candidate_spans 的 start/end_byte 是 UTF-8 字节偏移，直接喂给 read；当字符数用会错位
3. **续读必须用 continue 动词**。read+cursor→400；三模式互斥，continue 只带 cursor

## 错误码速查

| HTTP | code | 含义与处置 |
|---|---|---|
| 400 | bad_request / invalid_cursor | 参数错；cursor 类型不符 |
| 401 | unauthorized | token 错/过期/吊销（先查尾换行坑） |
| 403 | forbidden | 跨库隔离（正常） |
| 404 | not_found | lineage 不在索引 / 未知操作 |
| 405 | method_not_allowed | 写动词被拒（网关只读） |
| 409 | stale_reference / metadata_changed | 源升版（重 resolve）/ 索引变（重新 list） |
| 410 | reference_expired | 句柄过期（重 resolve 或 renew） |
| 416 | invalid_range / invalid_utf8_boundary | 空区间/超范围 / 码点中间切断 |
| 422 | unsupported_encoding | raw 非法 UTF-8 |
| 503 | lexical_unavailable | 词法代缺失/过时/损坏：显式降级或找运维 |

## 引文纪律

- v2 read 的 `full_sha_verified=true` 等价 v1 citation 的 `matches_index=true`——网关现场全读全核
- 回答里的 sha256 用 identity.raw_sha256 字段，不要自己重算
- 跨页拼接：next_cursor 链到 eof=true，拼回 bytes 的 SHA 应等于 identity.raw_sha256（需要时自证）
