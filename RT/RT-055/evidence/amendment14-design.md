# Amendment 14 — 完整 bank 的有界、写盘前精确防火墙

## 授权与不变项

2026-09-12 19:14 用户明确授权 append-only 修复并在新 source/migration 执行一次当前隐私门。Am11 socket UNKNOWN、Am12 historical log overlap FAIL、Am13 CAPACITY/INVALID_CLOSED 与旧 after 全部只读保留。当前门失败不重跑。PASS 才进入公开同形、主 root readiness、fresh before、随机 freeze/verify/no-Popen、新窗正式 A/B；每候选最多一次，exposure>0 不重放。题池、seed、tier/floor、42/31/42、builder/verifier 1/1、7200 秒、A/B 对称和检索/评分算法不变；WeKnora core、production/NAS/index/alias/config 不改。

## 算法与完整性

1. controller 逐文件读取 builder/verifier 的全部 JSON，递归非空 string leaf（不把结构 dict key 当数据）；每叶原始 UTF-8 与 `json.dumps(ensure_ascii=False/True)[1:-1]` 的精确 UTF-8 均进入集合。仅按字节完全相同去重，不删叶、截断、采样、摘要替代或阈值豁免。
2. `MemoryBank` 只持久保存不可变 bytes；兼容 string view 按需解码，不另存一份 285MiB 字符串 bank。固定 16-byte prefix 建 trie 并因式分解为 C regex；regex 只作候选位置发现，完整长 needle 最终逐字节 `startswith` 比较。短于 16 bytes 的 pattern 是独立闭集，不能被 prefix 分支遮住。
3. 选择最左位置，同位置最长。长候选失败推进 **1 byte**，不跳过 overlap；同位置先长组再短组，各组按长度降序。无字符解码、换行或 pipe chunk 边界假设，Unicode/JSON escaped 是精确 bytes 匹配。
4. 同一 controller 的完整 bank 只编译一次，通过显式对象/不写盘的弱引用缓存供所有 workspace、stream、复核共享。正式 runner 补充既有派生 log needles 时，只编译真正新增的闭集 matcher，复用完整 bank 的 bytes 和 matcher；不会重编译全 bank，也不会污染当前隐私 receipt 的 bank 身份。新增集合与原集合的候选位置仍取全局最左，同位置统一最长。
5. drain 线程持续 nonblocking 读 pipe；每流最多保留一个固定 64MiB raw 内存 buffer。EOF 前只允许空 sink；EOF 后先完成精确清洗及 output cap 校验，再写 sanitized bytes。无 raw spool、磁盘队列、raw 临时文件或向 child 传 bank。
6. 独立 postscan 不复用 prefix matcher：逐个完整 byte pattern 对已清洗文件作精确包含判断；任何命中/未验证流/EOF 丢失/线程或关闭失败/child nonzero 都不能通过门。replacement 与其他 needle 偶然重叠也不能豁免 postscan。

## 固定资源预算与失败行为

- 每 bank：512MiB 精确 pattern bytes、64MiB 最大 leaf、16,384 个 pattern；JSON 输入总字节上限 512MiB、逐文件解析。每个上限均 fail-closed，不用部分 bank 继续。
- 每 stream：input/output cap 均保持 **64MiB**；固定 raw buffer 无动态超配，pipe read 临时块最多 64KiB。controller 最多 12 个同时占用的 raw buffer（768MiB）；EOF 清洗互斥，不阻塞其他 pipe 的读取。
- 常驻 full bank 不按 stream 复制；prefix 索引大小随 `16 × pattern_count` 而非 285MiB 全字节 automaton 增长。编码/去重至多随总 bank 字节线性读入；排序只移动引用。最多缓存两个仍存活的 root bank，释放的 controller 对象不会被缓存永久保留。
- regex trie 深度不超过 16；完整比较只发生在候选位置。为恶意重复 prefix 提供硬上限：每流候选位置 1,048,576、完整比较计费 8GiB，超过返回 **WORK_LIMIT**，不跳过剩余 pattern、不输出部分清洗结果、不放行。不是宣称任意对抗输入必定成功。
- 内存上界由 bank/leaf/count/JSON 总量、12 个 raw slot 和 64MiB output 共同限制；Python JSON/Unicode/allocator 有额外有界开销，不能把 pattern 字节数冒称 RSS。公开容量基准单进程 RSS 预算 1.5GiB；记录实测峰值，不能据此冒称包括 Java/模型的整机上限。
- 工作线程只负责内存和清洗写出；raw/error 不进入 exception 文本。receipt 仍为身份及 count/bool/enum/bytes/redactions，不新增 needle 值、分布、摘要、哈希或私有字段。64MiB+1 leaf、512MiB+ bank、output 膨胀、slot/工作量超限、线程、EOF、child nonzero 均须实测拒绝。

## 公开验证与读产出

可复现入口：`python3 scripts/rt055_log_firewall_qa.py`。只用内存构造六个 48MiB leaf 和 6475 个短 pattern（6481 total，302,125,863 bytes）；逐一命中六个巨型 leaf，再命中所有短 pattern，全部替换；64MiB 无换行无命中输入；真实 64MiB+1 leaf 与 >512MiB bank 拒绝。仓库不保存巨物。另用 unittest 覆盖每个跨 chunk/Unicode/JSON escaped 边界、随机 oracle、失败 prefix overlap、最长优先、对象共享、EOF 前空 sink、raw 不落盘、output cap、线程/EOF/child nonzero、FD/thread 回收，以及独立 postscan 的真正绕过故障。

工程判据、行为破坏后恢复绿、benchmark aggregate、源码及实际 gate/terminal 证据分别记录；主工程师复核算法与产出，不冒称独立第三方 Agent 或完整 `make ci` 已运行。任何真实新门失败按原失败收口，正式 120 指标与 24 Gateway 能力保持 null。
