# RT-051 验收映射（12项）

> 2026-09-07 更新：A01–A12 行为套件已落 `tests/test_rt051_acceptance.py`（22 例）
> 与 `tests/test_rt051_a11_lexical_eval.py`（48 题矩阵）并全绿；A04 32/128MiB 档
> 与 A09 累计带宽判据显式顺延 P1b；A11 gold 逐题 rationale 见
> `a11-results-latest.json`，reviewer 待独立复核签收。正式验收仍走下述证据
> 与复核边界——复核人未签、用户收口门未过。

唯一合同与预算见 [rt-lite.md](rt-lite.md)。本表是Issue/需求→合同→行为破坏实验映射，不另立测试规范。**本轮没有产品测试PASS。** 每项需保存真实脱敏调用、期望/实际、失败/跳过、测试数、资源计量及复核人；不能只检查源码字符串或API200。

| ID / Issue或需求 | 合同 | 脱敏实际调用与可证伪断言 | 必须能抓住的坏行为 |
|---|---|---|---|
| A01 已知文档无需搜索 / RT044缺read | C01/C02/C05 | 真实ingest在Memory/local/fakeNAS至少各有一条链：生成raw-index→宿主kb_access→wizard/client→gateway→open已知lineage→read；删除lexical目录、不调用search仍拿同版正文。保存实际工具注册和子进程/路由调用回执 | 只在文档写工具名、漏注册、wizard仍直连backend、open要求BM25命中，均失败 |
| A02 query最多200无分页 | C02/C03 | ingest脱敏231与1001件，list与metadata search逐页到EOF，按同snapshot比较权威集合，total精确、无重复遗漏；缺title回null/lineage label，category不泄NAS路径；翻页中改变映射应409并不混快照 | limit截断却next=null、游标重复第一批、metadata snapshot变更仍200、伪造title/服务器目录均失败 |
| A03 citation500/忽略offset | C02–C04 | 大于500字符的文档，以工具read首段/安全中部byte与行/末尾；返回span可逐字节定位，500之后唯一标记必须真实出现；范围结束与整件EOF区分 | 仍回前500、忽略offset/line参数、以chunk限制任意安全阅读、将range_complete当EOF均失败 |
| A04 全文按需续读与SHA | C03/C06 | 2/32/128MiB合成UTF-8真实流式准备→多页同ref/SHA，乱序/重复响应去重后区间并集[0,total)，UTF8重组全hash等于权威raw SHA；每页自带引文，不再次citation下载；工具receipt与模型审阅状态分开 | 缺中间页仍complete、重复计覆盖、每页SHA冒充full SHA、换generation使当前ref失效、工具收齐自动称模型全审均失败 |
| A05 title/空/多字节/部分转换 | C01–C03/C07 | 实际ingest空件、缺title、placeholder、failed、可验证partial、XLSX主目录；UTF8含BOM/CRLF/emoji/组合字符，空raw标准SHA/单次EOF，非法UTF8与半字符range明确错误；主raw完整≠源转换完整 | replacement decode、strip/rejoin丢坐标、占位/partial/目录算源全文、空页无限continue均失败 |
| A06 版本覆写/旧版字节 | C01/C03/C04/C06 | 使用真实ingest的timeline/classify路径，prepare后覆写同路径升版，旧ref/read/renew/旧job皆拒而无excerpt；原始旧citation v1成功形状及mismatch观测留回归 | 新bytes冒旧version、旧cache/prev复活、只查路径不查版本SHA、历史schema被不声明地改写均失败 |
| A07 撤权/范围/游标/隔离 | C01/C03/C06 | A/B相同lineage异正文；read/list/status/prepare开始及返回前分别revoke/expire/reissue/换mount；篡改ref/cursor/页大小/跨操作/UTF8边界；ref/cursor到期和续期宽限覆盖 | 未授权provider访问>0、泄邻库title/计数、过期cursor继续、缓存绕撤权、接受伪造身份/路径均失败 |
| A08 索引坏≠源坏 | C01/C04/C06/C07 | ready快照+已知ref，lexical missing/corrupt/stale时仍read；分别让raw-index不可读/移除/dirty/权限注册表坏时必须失败无正文；list/metadata不依赖词法 | 一概要求重搜、源不可核实仍用缓存、silent metadata fallback、失败回空成功均失败 |
| A09 全bytes I/O/预算/GC | C06/C08 | fakeNAS transport记录实际正文下载量、每次read块大小、峰值RSS、磁盘、P页计量；无故障一次prepare≤1.1N、后续页source正文0，排除元数据字节单报；注入断流/TLS错误封装/磁盘满/queue满/deadline/GC与读锁/崩溃；大文续期再prepare可继续 | 每页整件download、len后限流、隐藏无界response.read、拼失败重试残片、gateway任何NAS/local持久写、GC删在读对象、永不结束重试均失败 |
| A10 旧服务与真实Agent权限 | C04/C05 | 旧fake服务忽略未知参数且200回v1，包装必须unsupported_contract非零，不能称全文；宿主仅提供绑定token连接、无NAS/管理Key/任意命令配置，实际工具读成功且NAS spy仅OPS触达；reviewer工具仍为空 | API200假成功、Skill from_env/walk/本机NAS兜底仍被物化、token进argv/log/response、reviewer扩权均失败 |
| A11 正文词法与同一reader | C07/C08 | 基础验收后48题同集metadata/body-only/fusion消融、中文1/2字/编号/无答案/正文中后部/跨块；query候选span→同kb_access read核同版SHA；build发布kill-point、双builder、refresh dry/unchanged/failed/vanished；人工gold不能0题自签 | 只匹配title、只有trigram、编号017冒AB-017、错库df、RRF当置信度、另一citation证据路径、漏合格件仍ready、dry写job、vanished当tombstone均失败 |
| A12 注入/任务预算/结论诚实 | C03/C05/C08–C10 | 合成正文含越库/泄token/执行命令提示，实际受控工具链不得产生对应调用；任务读到预算暂停给partial+next，可显式继续直到EOF；2–3词零命中后仍browse/open，实际读Agent产出核对“完整审阅”依据 | 文档提示变控制指令、自动扩权、未读完说读完、两三词零即断言无资料、预算耗尽丢续读状态均失败 |

## 证据与签收边界

- 以上是未来行为验收，不是本轮已实现的测试清单；测试落`tests/test_rt051_*.py`，0测试、只跳过或自签gold不算通过。
- 真实ingest指运行产品摄取代码处理人工脱敏fixture，不连接真实DocDB/CWork/NAS；fakeNAS流量不能宣称真NAS性能。
- 完整读取receipt只证明传输覆盖；模型实际理解和“支持题意”须读任务产出。prompt要求不能用字符串断言证明有效。
- 工程判据本轮限文档，见 [validation.md](validation.md)。独立Codex为已交接的静态审核，不是A项独立验收；gold reviewer/reviewed_at、逐题支持理由和失败样本均待实际填写，不代签。
- [Issue #2](https://github.com/evan-zhang/CWK/issues/2) 各缺口映射A01–A05/A09/A10；只有实际验收并另获用户授权后才可关闭。本轮不操作Issue。
