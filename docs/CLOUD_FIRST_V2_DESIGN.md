# CWK Cloud-First v2 重新审核与目标设计

> 文档状态：Target Design（目标架构，不等同于当前生产默认）
> 复审日期：2026-08-04
> 适用范围：工作协同原文证据、Wiki、检索索引、nightly、云端恢复与可信问答

## 1. 结论

Cloud-First 方向可行，且现有 P0/P1 工程已经证明以下能力成立：

- 私有 DocDB 可保存 raw、Wiki、索引和对象目录；
- 3,452 个云端逻辑对象可以逐对象回读并通过 SHA-256 核验；
- 1,450 篇 raw 与 summary 可以从空目录恢复；
- 云端查询可以返回经 raw 验证的证据及预览链接；
- 当前云端查询 P95 约 3.49 秒，满足内部交互使用的基础要求。

但系统**尚未达到“可以删除本地持久 raw/Wiki”**的状态。当前生产 nightly、默认查询入口和增量编译仍依赖本地完整镜像；现有云端 catalog 也没有真正固定 DocDB 文件版本。正确判断是：

> 云端静态快照和恢复底座已经验证，cloud-only 持续运行链路尚未完成。

新版目标是：**个人私有 DocDB 成为唯一知识内容持久主库；Agent 本地只保留代码、非内容型配置，以及可清理的运行时缓存。**

## 2. 本次复审证据

### 2.1 已验证能力

- raw：1,450 篇；Wiki summaries：1,450 篇；主题页：92；实体页：439。
- 云端 active tree：3,452/3,452；missing、extra、hash mismatch、duplicate 均为 0。
- 云端 catalog：`index_version=10`，对象目录约 1.14MB。
- 搜索索引：压缩后约 5.3MB，拆为 4 个不超过 1.5MB 的分片。
- 20 问 Top-8 本地/云端排序一致率：100%。
- 云端查询实测可返回 `evidence_status=verified`、DocDB file ID 和预览链接。
- 空目录恢复：3,452/3,452，失败 0。

### 2.2 与目标不一致的当前状态

1. **生产 cron 仍是 Local-First。** 当前命令没有 `--cloud-first`，启用了 `--wiki-best-effort`，并在任务说明中明确“raw 禁止同步”。因此下一批新增数据不会自动形成新的云端完整快照。
2. **默认查询仍是本地。** `cwk_wiki_query.py` 的 `--mode` 默认值仍为 `local`，与使用说明中的 Cloud-First 入口不一致。
3. **nightly 仍要求完整本地镜像。** 摘要编译、主题/实体重建、source coverage 和候选 catalog 都从本地完整树生成；空本地会安全失败，而不是从云端继续增量运行。
4. **当前 catalog 不是不可变快照。** DocDB 更新使用同一 `file_id` 的新版本，而 catalog 只保存 `file_id + SHA`，没有固定 `version_id`。旧 catalog 回读时可能得到文件最新版本并触发哈希失败，能发现问题，但不能可靠恢复旧快照。
5. **提交指针过大。** `cloud-objects.json` 同时承担 HEAD 和全量对象目录，当前约 1.14MB；对象数增长后，每次查询都必须重新下载更大的文件，且会接近 DocDB 单文件可靠边界。
6. **来源内容信任边界需要统一。** 工作协同可读取内容应按非涉密知识源处理，不能再由字符串形态触发脱敏、隔离或整批阻断。
7. **检索相关性仍是词法模型。** 当前能保证证据正确，但复杂自然语言问题会出现排序噪声；“可核验”与“召回最优”需要分别验收。

## 3. 目标架构

<div style="width:1200px;box-sizing:border-box;background:#f8fafc;padding:18px;border:1px solid #cbd5e1;border-radius:8px;"><style scoped>.cf-title{text-align:center;font-size:22px;font-weight:700;color:#1e3a5f;margin-bottom:14px}.cf-row{display:grid;grid-template-columns:1.05fr 28px 1.4fr 28px 1.15fr;align-items:stretch}.cf-col{border:2px solid #64748b;border-radius:7px;padding:10px;background:white}.cf-col.source{border-color:#3b82f6;background:#eff6ff}.cf-col.cloud{border-color:#0f766e;background:#f0fdfa}.cf-col.runtime{border-color:#7c3aed;background:#f5f3ff}.cf-head{text-align:center;font-size:13px;font-weight:700;color:#1f2937;margin-bottom:8px}.cf-box{background:white;border:1px solid #cbd5e1;border-radius:5px;padding:7px;margin:5px 0;text-align:center;font-size:10px;line-height:1.35}.cf-box.strong{border-width:2px;border-color:#0f766e;font-weight:700}.cf-arrow{display:flex;align-items:center;justify-content:center;color:#64748b;font-size:20px}.cf-bottom{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:12px}.cf-note{font-size:10px;line-height:1.35;text-align:center;padding:8px;border:1px solid #cbd5e1;border-radius:5px;background:white}.cf-danger{color:#991b1b;font-weight:700}</style><div class="cf-title">CWK Cloud-First v2 目标架构</div><div class="cf-row"><div class="cf-col source"><div class="cf-head">源端与采集</div><div class="cf-box">工作协同只读 API</div><div class="cf-box">按业务日完整分页</div><div class="cf-box">source ID 集合与水位</div><div class="cf-box">单写者租约</div></div><div class="cf-arrow">→</div><div class="cf-col cloud"><div class="cf-head">个人私有 DocDB｜唯一持久主库</div><div class="cf-box strong">小型 HEAD：当前 snapshot/version/SHA</div><div class="cf-box">不可变 raw objects（内容寻址）</div><div class="cf-box">不可变 summary/build-state/index</div><div class="cf-box">分片 snapshot catalog + runtime state</div><div class="cf-box">版本历史、回收站、异地云备份</div></div><div class="cf-arrow">→</div><div class="cf-col runtime"><div class="cf-head">Agent 运行时</div><div class="cf-box">下载 HEAD + 热索引</div><div class="cf-box">Top-K raw 并发回读</div><div class="cf-box">临时构建工作区</div><div class="cf-box">证据验证与云端链接</div><div class="cf-box cf-danger">禁止静默回退旧本地副本</div></div></div><div class="cf-bottom"><div class="cf-note">事实边界<br>回答只引用当前 snapshot 中 verified raw</div><div class="cf-note">提交边界<br>先上传、再审计、最后原子切 HEAD</div><div class="cf-note">本地边界<br>只允许 TTL 缓存和临时构建产物</div><div class="cf-note">恢复边界<br>代码 + 云端即可从空目录恢复</div></div></div>

## 4. 存储模型

### 4.1 云端唯一持久内容

建议将云端目录从“可变文件树”升级为“不可变对象 + 逻辑视图 + 小型提交指针”：

```text
工作协同镜像/
├── _system/
│   ├── HEAD.json                         # 很小；唯一当前提交指针
│   ├── snapshots/v000123/meta.json       # 快照元数据
│   ├── snapshots/v000123/catalog-*.json.gz
│   ├── runtime/state.json                # 采集水位、失败队列、最后成功版本
│   └── runtime/lease.json                # 单写者租约
├── objects/
│   ├── raw/sha256/ab/<sha>.md.gz         # 不可变原文对象
│   ├── summary/sha256/cd/<sha>.md.gz     # 不可变摘要对象
│   ├── build-state/<sha>.json.gz         # 增量编译所需规范化状态
│   └── index/v000123/part-*.bin           # 版本化检索索引
├── views/
│   ├── raw/YYYY-MM/YYYY-MM-DD/...         # 可浏览逻辑视图，可重建
│   ├── summaries/...
│   ├── topics/...
│   └── entities/...
└── backup/                               # 周期性加密云端导出，独立故障域优先
```

关键规则：

- 证据对象以内容 SHA-256 命名，发布后不更新版本、不覆盖。
- `report_id` 是业务主键，SHA 是内容版本键。
- 一个汇报更新时创建新 SHA 对象；新 snapshot 指向新对象，旧 snapshot 仍能回读旧对象。
- `views/` 仅供人浏览，不是提交真相；丢失后可由 snapshot catalog 重建。
- 现有 `cloud-objects.json` 拆为小型 `HEAD.json` 和分片 catalog，避免每次查询下载全量对象表。

### 4.2 本地允许保留的内容

本地可持久保存代码、测试、非敏感部署配置和不包含知识正文的节点标识。完整 raw、完整 Wiki、长期 search index 和无 TTL 的证据缓存不得作为第二持久知识库保存。

运行时允许使用独立临时目录，默认 TTL 24 小时并设置容量上限；缓存命中必须校验 snapshot version 与对象 SHA。

## 5. 写入与提交协议

### 5.1 单写者约束

DocDB 当前没有可用的条件写/CAS 证据，因此 v2 先明确为**单写者系统**。nightly 在云端取得带过期时间的租约；存在未过期租约时，其他写入任务必须失败，不允许并发发布。

### 5.2 六阶段提交

1. **读取基线**：读取 `HEAD`、当前 snapshot catalog、runtime state，并核对个人私有 project ID。
2. **完整采集**：按业务日期完整分页，得到 source ID 集合；新增/更新 raw 写入临时目录。
3. **不可变上传**：按内容 SHA 上传 raw/summary/build-state/index 对象；已存在同 SHA 时复用。
4. **候选构建**：生成新 snapshot catalog，执行 `source IDs = cloud raw IDs = cloud summary IDs`、对象 SHA、隐私标签和 retry queue 门禁。
5. **候选审计**：从云端回读候选关键对象；执行固定问答、拒答、恢复和 read-after-write 验证。
6. **原子提交**：最后只更新小型 `HEAD.json`，指向新 snapshot 版本与 catalog SHA。任何前置步骤失败都不得改变 HEAD。

### 5.3 删除与垃圾回收

- HEAD 切换后保留最近 30 个快照或不少于 30 天。
- 未被任何保留快照引用的对象进入候选回收清单。
- 回收必须先 dry-run，再逻辑删除；禁止 nightly 直接永久删除。
- 删除本地持久镜像后，云端对象清理必须保持至少一个独立云备份可恢复。

## 6. Cloud-Only nightly

目标 nightly 不再依赖完整本地镜像：

1. 获取云端单写者租约；
2. 读取 HEAD、runtime state 和压缩 build-state；
3. 从工作协同完整分页采集当天及延续变化；
4. 仅在临时目录保存新增/变化 raw；
5. 生成变化 summary，增量更新规范化 build-state；
6. 从 build-state 重建 topics/entities/search index，不需要下载全部 raw；
7. 上传不可变对象和候选 catalog；
8. 执行完整性、安全、查询和恢复门禁；
9. 更新 HEAD；
10. 持久化 runtime state，释放租约并清理临时目录。

`build-state` 至少记录 report_id、业务日期、发送人、标题、source lane、当前 raw SHA、summary SHA、summary quality、evidence quotes、topic/entity candidates、搜索词项统计和云端对象 ID；不得包含完整 raw 正文。

## 7. 查询链路与性能设计

### 7.1 默认行为

- 默认模式必须是 `cloud`；`local` 仅限开发测试显式指定。
- 云端不可用、HEAD 低于要求版本或证据 SHA 不匹配时明确失败。
- 禁止静默使用旧本地副本生成答案。
- 最终回答返回 report_id、业务日期、原文引文和云端预览链接。

### 7.2 热路径

1. 使用配置中固定的 HEAD file ID，避免每次按文件名全局搜索。
2. HEAD 使用 15–60 秒 TTL；版本不变时复用内存索引。
3. 索引版本变化时并发下载分片并按 artifact SHA 重组。
4. 在内存完成 Top-K 召回。
5. 并发回读 3–8 篇 raw；按 SHA 验证后提取证据。
6. 预览链接异步或并行生成，不阻塞证据排序。

### 7.3 性能 SLO

- 热查询 P50 ≤ 1.5 秒，P95 ≤ 4 秒；
- 冷查询 P95 ≤ 10 秒；
- 5–10 篇综合查询 P95 ≤ 15 秒；
- 云端调用错误率 < 0.5%；
- 证据 SHA 验证成功率 100%；
- HEAD/catalog 读取不应随 raw 对象总数线性增长。

当前 3.49 秒 P95 可以作为迁移基线，但不能替代并发、冷启动和限流测试。

## 8. 检索质量

当前持久索引是词法 BM25/term statistics，证据正确但排序可能有噪声。改进顺序：增加同义词和结构字段加权；建立固定评测集；对 Top-20 使用轻量 reranker；raw 引文验证继续作为最终硬门禁。

上线门槛：Recall@8 ≥ 0.95、关键测试集 nDCG@8 ≥ 0.85、拒答准确率 ≥ 0.98、引文验证正确率 100%。

## 9. 安全与隐私

- Cloud-First 只能写入当前鉴权用户的个人私有 DocDB；每次提交持续断言 project ID。
- 所有 raw 和 raw 分片使用 `isSensitive=0`；catalog 继续记录对象哈希和版本，用于完整性而非内容分类。
- 凭据不进入命令行、日志、manifest、索引和模型提示词。
- 模型精编必须忠实接收工作协同原文；不得因文本类似 Key、Token 或凭据而省略、改写或跳过。
- preview URL 视为短期授权链接，不写入持久索引。
- 本地临时缓存权限至少为用户私有，过期后安全清理。
- 需要独立故障域的加密云备份；“仅云端主库”不应等于“仅一个云端副本”。

## 10. 必须修复的问题

### P0：切换前阻断

1. 将生产 cron 改为真正的 `--cloud-first` 严格模式，去掉 `wiki-best-effort` 和“raw 禁止同步”的旧说明。
2. 实现 cloud-only 增量构建，不依赖长期本地完整 raw/Wiki。
3. 将默认查询切换为 cloud，禁止静默本地回退。
4. 将可变 fileId 引用改为不可变内容对象，保证旧 snapshot 可真实恢复。
5. 拆分 HEAD 与 catalog，并为 catalog 分片和 SHA 校验。
6. 将 source/cloud raw/cloud summary 的 ID 集合门禁放在云端提交前。
7. 全量核验 raw 的 `isSensitive=0` 与内容哈希，确保云端对象未被内容级过滤或改写。
8. 将采集水位、失败队列和最后成功版本持久化到云端 runtime state。

### P1：删除本地前完成

1. 实现固定 HEAD file ID 与连接/鉴权复用，减少子进程和全局搜索开销。
2. 建立检索质量评测集与 reranker；修复日期/范围问题的排序噪声。
3. 建立缓存 TTL、容量、加密与清理策略。
4. 增加并发、限流、断网、超时和 DocDB 降级测试。
5. 建立 snapshot 保留、逻辑删除和对象垃圾回收机制。
6. 建立独立云备份和恢复演练。
7. 统一 DESIGN、USER_GUIDE、README、cron 与代码默认值，消除双重事实。

## 11. 迁移阶段与门禁

### Phase A：协议升级

- 实现不可变对象、HEAD/catalog 分离、cloud runtime state。
- 修复 cron、默认查询和文档漂移。
- 本地完整镜像仍保留，只作为迁移安全网。

### Phase B：双写与影子运行（至少 14 天）

- 同一批源端数据同时走旧本地构建和新 cloud-only 构建。
- source/raw/summary ID 集合一致率 100%。
- Top-8 一致率 ≥ 99%，证据 SHA 正确率 100%。
- nightly 连续 14 天无未解释失败、无 retry backlog。

### Phase C：云端默认（至少 30 天）

- 问答默认 cloud；nightly 只通过 cloud-only 路径提交。
- 本地完整镜像改为只读，不参与生产结果。
- 监控查询 SLO、DocDB 错误率、恢复时间和检索质量。

### Phase D：删除本地持久知识内容

只有同时满足以下条件才能请求删除授权：

- 两次不同日期、不同空目录的全量恢复成功；
- 一次新机器/新 Agent 的端到端恢复成功；
- 旧 snapshot 可恢复，证明版本真正固定；
- 云端主库与独立云备份均通过 SHA 审计；
- 连续 30 天 cloud-only nightly 和问答 SLO 达标；
- 已生成删除清单、回滚方案和恢复时间证明。

## 12. 验收标准

最终 cloud-only 交付必须同时证明：

- **完整性**：源端 ID = 当前 snapshot raw ID = summary ID；
- **不可变性**：任一保留 snapshot 均能回读其固定版本对象；
- **可信性**：所有事实引文均来自 verified raw；
- **隐私**：所有 raw 远端敏感标签正确，且仅位于个人私有项目；
- **性能**：满足热/冷查询 SLO 和 nightly 时限；
- **可恢复**：无需本地知识副本即可从云端恢复；
- **可运维**：失败不切 HEAD，retry queue 可恢复，单写者租约有效；
- **一致性**：代码默认、cron、文档和实际运行模式均为 cloud。

## 13. 决策建议

采用本设计作为 Cloud-First v2 的新目标基线，保留现有 P0/P1 工程作为迁移资产，但不再把“云端快照验收通过”表述为“cloud-only 已交付”。

近期最优先的不是删除本地，而是完成三个闭环：

1. **生产闭环**：cloud-only nightly 能持续生成并提交新 snapshot；
2. **读取闭环**：所有问答默认从云端 HEAD/index/raw 取证；
3. **恢复闭环**：旧版本与当前版本都能在无本地知识副本时恢复。

三个闭环通过后，再进入本地持久镜像删除阶段。
