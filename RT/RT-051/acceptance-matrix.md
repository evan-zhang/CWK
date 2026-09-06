# RT-051 验收映射（开发后使用）

本文件仅把 [rt-lite.md](rt-lite.md) 的C条款映射到需要观察的故障；参数/预算以该文件C08为唯一权威，不在这里复制数值。**本轮未实现或执行下列产品验收，全部未验收。** 不设集中测试登记表，开发测试仍按RT放tests/。

| 对应条款 | 脱敏行为验证 | 有效破坏/负例（开发阶段） | 必交证据 |
|---|---|---|---|
| C01 范围/引擎 | 同语料、同token、同chunk、同版本下比较metadata/body-only/fusion；如做FTS5对照，记录真实编译能力 | 替换成metadata-only，正文-only题必须失败；把FTS默认切词混进对照必须被实验配置核验发现 | 实际引擎/参数digest、输入集digest、全部成功/失败样本；无模型调用记录 |
| C02 身份/span | 主raw与索引映射、版本SHA；前500字外/重复段/emoji/CRLF/跨块边界定位 | 改raw一个字节、把span移到重复段另一处、换kb_code或version、乱UTF-8边界 | 原raw合成字节hash与返回span片段hash相等；不同库/版本chunk_id不碰撞，同输入可重现 |
| C02 覆盖边界 | placeholder/failed/skipped/缺SHA/空文本/XLSX目录与extras | 只索引标题/前部；把sheet目录算全文覆盖；丢掉一份合格raw却报ready | eligible/excluded/failed/source总数对账，主件范围明确，缺失不假绿 |
| C03 中文/编号 | 1字、2字、连续中文、英文单字母、混合编号、大小写、标点 | 仅保trigram导致短词漏召回；拆AB-017只匹配017；跨库df影响排名 | 独立人工gold、倒排tf/position向量、BM25手算小例与候选集合/排名；不是只查字符串在源码里 |
| C03 融合/无答案 | 两路重合/各自独中/无命中/截断/相同分/一长文多块 | 加和块分数让长文霸榜；所有result强塞一个非零分；把lower_bound当全量 | 文档去重、rank与score_kind正确、limit与matched_relation如实；无答案题保留在报告 |
| C04 metadata兼容 | 旧query_index、HTTP与wizard默认路径 | 默认偷偷切词法；篡改title/path/排序/limit含义 | 现有QueryRouteTests、QueryVerbTests及新旧diff；时间字段单列 |
| C04 段引文 | query拿chunk ref→同kb/version/generation请求v2 citation→现场读raw | 返回text[:500]代替命中span；只拿索引SHA而不读raw；source已变仍回cache | 现场read spy、full/segment SHA双核验、人工gold支持题意；无读backend桩必红 |
| C04 旧版/新旧接口 | timeline旧件留存、classify覆写；v1正常前500字符保留 | 用v2原文满足旧v1 source_version；chunk参数被忽略回v1头部 | v2对stale/missing/mismatch拒绝且不含excerpt；旧接口mismatch只作观测不能当verified |
| C05 库级隔离 | A/B同lineage异内容，同文异kb_code；未授权/未挂载/双库token | 全库召回后过滤；篡改generation指邻库；伪造kb路径；省kb读取邻库 | provider/backend spy未碰未授权库，返回正文/标题/chunk/词频0泄露，现有403/404顺序不变 |
| C05 撤权/在途 | retrieval中、citation读后、fallback前revoke/expire/reissue；挂载变化/registry损坏 | 去掉返回前auth、接受旧epoch或旧cache；客户端自报身份 | 整请求拒绝，不吐部分证据；日志/响应无敏感内容。明确最后校验为线性化点 |
| C05 删除边界 | 已授权库映射移除、源列表暂空、旧代仍存 | 当前映射缺失却从prev/cache复活；把源扫描0件自动视删除/安全撤权 | 旧词法失效；源故障不擅删；上游实时ACL能力不虚报为通过 |
| C06 代际/并发 | builder staging→验证→切pointer，读者固定旧代；两个builder；refresh与run相撞 | 在任一步注入崩溃、交叉写pointer、读取混代、把未完成代标ready | 每kill点重启结果，active指针旧/新二选一、无半代；source fence dirty不自动放行 |
| C06 完整性/资源 | missing/corrupt/unknown-engine/坏postings/disk-full/超大raw/NAS fake transient | len检查前无界读造成超预算；持续retry阻塞HTTP；悄悄跳合格件 | 真实peak RSS/总read字节/超时与retry计数；指定错误而非空success；所有输入数对账 |
| C06 只读/隐私 | gateway依赖图与写陷阱覆盖新reader/所有路由；记录服务日志 | query触发建索引/SQLite WAL、HTTP日志带原query、错误带raw或绝对路径 | 零NAS/local持久写（由query触发）、无write模块依赖、日志脱敏断言，builder独立运行 |
| C07 refresh挂接 | dry/unchanged/update/guard/known-failed/new-failed/多source最后收尾 | 每件publish即建索引、dry写job、丢hook、guard仍切新代；仅看source ok | 事件顺序、幂等request key、source与lexical分别状态、failed永不被当正文合格 |
| C08 评估有效性 | 最小集类别数、人工review、macro doc/证据span Recall、错误/无答案分母 | 0测试返回绿；漏一类；空召回从分母删掉；LLM自签gold | 数据集/运行配置/hash、逐题结果、审gold记录、失败样本及指标计算复核 |
| C09 回滚 | 禁词法/旧metadata继续，旧engine与generation兼容检查 | 回滚已撤权token/旧raw-index，拿不兼容代回包，清掉读者仍pin的代 | 无源/权限事实回退，显式disabled/degraded，旧v1正常行为；不需真实部署才能模拟 |
| C10 边界复核 | 作者/开发Agent/用户分别确认未批准项与遗留风险 | 把历史133件/旧回执/本地单文件测试当全容量/生产事务证据 | 独立审查意见、未批准清单与明确停止点 |

## 人工gold复核签收字段（模板，不是签收结果）

- 数据集版本及commit/hash：待开发阶段生成。
- 合成文本作者、题目来源/授权：待记录；不得复制真实业务raw。
- reviewer / reviewed_at：待人工填写，不能代签。
- 每题gold span支持题意、版本/库一致：待逐题核实。
- 无答案域与预期错误的理由：待逐题核实。
- 冻结后变更：每次变更记原原因、新hash和复核人，不让实现自动重算gold洗掉失败。

## 验证三格（当前设计阶段）

- 工程判据：本轮只做文档检查，见 [validation.md](validation.md)；上表的行为破坏实验尚未执行。
- AI评审：本会话直接按方法论做方案自查，不是独立实施验收，也未委派评审Agent。
- 读产出：设计正文与源码摘取范围已对照；真实gold和产品回答未产生，不可填PASS。
