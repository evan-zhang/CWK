# RT-054 阶段 A 基线（2026-09-09）

## 结论

当前首要问题不是“JSON 这种扩展名”，而是索引投影本身过度放大：三个库的词法索引占总存储 77.6%–90.8%。cwork-3m 在 66,500 chunks 时已形成 1.495GB 词法对象，旧链路还会整包读取、解析并在 Python 中排名。阶段 B 必须同时改变拆片/字段复杂度和检索载体，不能把旧 payload 原样导入 OpenSearch。

## 三库冻结值

- cwork-3m：1,100 文件、1.646GB；21 个 JSON 共 1.498GB；词法索引 1.495GB；500 文档、482 入索引、66,500 chunks、约 22.5KB/chunk。
- docdb-touqian：315 文件、59.95MB；22 个 JSON 共 47.20MB；词法索引 46.51MB；133 文档、114 入索引、1,008 chunks、约 46.1KB/chunk。
- spbp-2027：317 文件、324.85MB；22 个 JSON 共 265.23MB；词法索引 264.47MB；128 文档、90 入索引、3,984 chunks、约 66.4KB/chunk。

投前与 SPBP 文件数几乎相同，词法索引相差约 5.7 倍。这证明容量不能按文件数估算，必须按规范化正文、chunks、terms、postings 和 bytes/chunk 计量。

## 性能冻结值

- 已有生产观察：cwork-3m 短中文查询、`lexical_fusion_v1`、`page_size=1`，一次功能成功响应耗时 425.685 秒。该值是运维观察，不是本轮重放结果。
- 受控 P0 冷失败：24.122983625 秒；读 1,495,220,459 字节词法 payload 和 1,599,012 字节 raw-index；JSON decode self 4.5205685 秒；峰值 RSS 5,461,835,776 字节；未进入 BM25/span，最终 `lexical_unavailable`。
- 该单个失败样本不能计算 P50/P95，也不能给成功链路定根因；它足以否定旧链路的内存与整包读取方式。

## 刻意保留的未知项

真实三库的 unique terms/postings、完整成功冷/热分布、全量 builder 耗时和全部物理 wire bytes 没有可信证据。本阶段不为填表再次下载 1.495GB 对象；这些值保持 `null`，由阶段 B 新 PoC 在本地构建时原生输出。

## 可重复口径

机器可读权威为 `stage-a-baseline-20260909.json`。所有容量比较都使用原始字节，不使用压缩包；OpenSearch 后续使用 primary store，不用 replica、snapshot 或删掉必要字段的半成品冒充主索引。
