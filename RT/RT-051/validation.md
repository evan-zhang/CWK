# RT-051 文档验证回执

本轮检查对象仅RT设计资料，产品未实现/未验收。唯一设计见 [rt-lite.md](rt-lite.md)。

## 检查结果

检查日期2026-09-06；系统时钟读取到2026-09-06T11:12:35Z（非推测）。最终本地commit及提交后范围核验以原会话交付回执为准，不在同一commit正文里伪造自引用commit ID。

| 检查 | 实际结果 | 结论边界 |
|---|---|---|
| Markdown围栏/尾部空白 | 初检发现evidence.md一行尾空白，已修复；复查5份Markdown无错误 | 初检失败未隐瞒；不修改产品代码 |
| 相对文件链接 | 41处逐个解析，全部目标存在；无需要验证的内部anchor链接 | 不仅依赖AODW弱引用scanner；拟议新模块路径只写code文字，不伪造现有链接 |
| 源码证据 | sources.json的44份文件：工作树字节、固定commit的git show字节、SHA/行数/区间相符；5份methodology文件摘要相符 | 只证明取证引用有效；不是实现或线上状态证明 |
| `git diff --cached --check` | 精确暂存7份本RT文档后通过 | 覆盖未跟踪新文档加入后的diff，不仅检查meta |
| 授权范围 | 暂存项全在RT/RT-051，产品scripts/tests、规范.aodw-next/AGENTS相对80b8b85无diff | 本轮未写实现/测试/配置/规范/运行数据 |
| RT-051定向AODW | `bash .aodw-next/tools/rt-guard.sh --root . --rt RT-051 --format json`：pass12、error0、warn2、exit0 | G109索引快照未登记；G001共享pre-commit hook未安装 |
| AODW引用细节 | `--scan-refs` exit0；扫描5篇但该格式识别的file:line/§共0条 | **零匹配不等于所有引用已查**，相对链接和sources.json另行实核 |
| 只读花名册诊断 | 本worktree目录有RT-051，index快照无051；其余目录/index集合一致 | 不运行/声称全仓make aodw-check通过，也不修改index/规范消告警 |
| 主线漂移 | main已前进到bb414a63c78371e8aca57d38ed96a18dc32582a3，只改RT-050/rt-lite.md和RT/index.yaml，四个kb入口无改动；main暂存区/工作树干净 | 另一会话补了051索引；G109仅描述本分支快照。本轮未合并/rebase/撤销该提交 |
| 第三方工作树 | /tmp/yuxi-study在fd0d9c4f48ba0e4701457196a2f967232be6091c，复查工作树干净 | 未复制第三方实现，未安装依赖/跑模型 |

上述检查在最终暂存/提交前复跑。提交后另以 `git show --name-status`、commit-range `diff --check`、本worktree/main的status核对；精确commit写在最终回复，避免“文件声称已提交”作为唯一依据。

## 内容自查所得修订

- 区分库级快照许可与未实现的源侧实时逐文档ACL；已知源删除/撤权不能由旧缓存复活，未知上游变化不能假称可实时发现。
- 引文只核验原文真实性，不凭verified字段自动判定支持题意；人工gold仍必须核实。
- 固定byte span/双SHA，禁止classify旧版本指向新正文或只给前500字符。
- 排除generation ID和易变账本摘要的循环哈希/非确定性；相同logical源、相同引擎构建必须幂等。
- 写入屏障、独立builder与gateway只读界限清晰；refresh释放源锁后builder再取，避免嵌套死锁；源不可核实时不允许metadata fallback绕过。
- 所有性能与容量数字未测、未批准；不把历史133件、已有黄金题或旧原子性测试当生产证明。

## 未运行与禁止项

- 未运行make test/make ci、长全量产品测试或新RT产品测试；未启动网关/摄取、未生成运行数据。
- 未连接NAS/DocDB/CWork/Yuxi模型或任何真实API；未读凭据/私有raw。
- 未做FTS5可用性探针、中文token实验或BM25跑分，C08所有数字都是待批准建议。
- 未改共享hook、规范、业务配置、scheduler；未合并、推送、部署、清理任何worktree，未委派/spawn。
- 文档检查只证明格式、链接、证据字节和Git边界；人工gold、方案批准、产品行为与生产验收均未完成。

**终态：设计准备完成，等待方案门及代码开发授权；RT不关闭，无后台开发。**
