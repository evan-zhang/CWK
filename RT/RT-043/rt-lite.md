# RT-043 摄取管道 v1（MVP）

- 目标：统一摄取管道：拉取 → originals(write-once) → 格式工厂 → 路由 → raw/分类树 + raw-index(lineage 寻址) → provenance → ingest-state 状态账 + 覆盖率对账。
- 上游合同：references/（DOCDB-INGEST-DESIGN v1.3、INGEST-AND-TAXONOMY v1.1、KB-PARAMETERS、AI-FIRST-MAP）；存储基座 = scripts/kb_*.py（RT-042，已合并 main，复用其 FileStation 客户端与后端抽象）。
- MVP 裁剪（Evan 2026-09-04 20:37 MVP 定调 + 2026-09-05 09:30 提速令）：
  - AI 理解回合降级为「路由智能缺省 + 确认卡」：route.mode 按源缺省（cwork=timeline、docdb=classify），确认卡以 JSON 输出可改；AI 落位提议/精编产物列 v1.1，不阻塞今日验收。
  - 格式工厂 v1：md 直通；docx→md（本机 md2md 可用则满做，否则占位+待审状态）；xlsx→每 sheet CSV；pptx/图片/zip→占位；rar/7z→跳过并记状态。
- 交付：scripts/kb_ingest.py + tests/test_kb_ingest.py（离线 fake 全覆盖）。
- 判据（每条必须有负例红）：
  - J1 originals 只写一次：同源同件重复摄取幂等（sha 相同跳过、账不动）。
  - J2 覆盖率对账：originals 有而 raw/index 无的件，对账必红（供 doctor 硬阻塞项取账）。
  - J3 raw-index 原子写：tmp+fsync+rename；中断后旧 index 完整可用。
  - J4 状态账：每件 pending|converted|placeholder|skipped|failed，failed 带原因；断点续跑跳过已完成。
  - J5 源故障必红：源 5xx/网络错→该批次红并保留已完成件，不许静默吞。
  - J6 lineage 寻址：raw-index 键=<source>:<稳定ID>，永不含 rev/seq；同 ID 新版本→supersedes 版本链记账。
- 源适配器 v1：cwork-mirror（本地目录扫描）；docdb（经 CMS_DOCDB_SKILL_DIR 下脚本列目录+下载）。
- 红线：不改 scripts/ 既有脚本；凭据只走 env；raw 只增不改。

---

## 实现记录（2026-09-05）

交付：`scripts/kb_ingest.py`（plan / run / status / reconcile，输出一律 JSON）+
`tests/test_kb_ingest.py`（93 条，纯标准库，离线 fake，1 条真 NAS 冒烟无凭据时 skip）。
存储地基全部复用 RT-042：`StorageBackend`、`record_write`、`refresh_manifest`、
`record_changed_paths`、`batch_id_for`，没有重写任何一块。

几个落地时定死的判断：

- **originals 路径只由 `(source, 稳定ID, sha256)` 决定**，不掺日期。掺了就会让
  "写一次"退化成"每次 mtime 漂移写一次"，而幂等判据照样全绿。
- **确认卡是可执行输入，不是装饰**：`run` 从卡上读 `route_mode` 和
  `proposed_raw_path`，改卡真的会挪产物；`load_plan` 把改过的卡再过一遍
  `normalize_path`，防止编辑把产物送出库根。
- **同 ID 第二版怎么落**：timeline 库落在旁边（`x.v2.md`，raw 只增不改）；
  classify 库原地更新（§II 活文档模型），两种都在版本链里记 `supersedes`。
- **三账发布顺序固定**：index → provenance → state。state 是续跑时唯一被信任的账，
  必须最后才成为事实。
- **无稳定ID 的源文件**（文件名没有 ≥8 位数字前缀）不摄取，但进 plan、
  ingest-state 和 reconcile 的 `unidentified` 清单，不计缺件，也不许消失。
- **AI 落位提议 / 精编产物**仍列 v1.1，未实现；`model_version` 字段写 `none`
  而不是省略，换模型时可以定向重跑。

## 验证（按判据纪律三格）

**判据**（`cd tests && python3 -m unittest test_kb_ingest`，93 条全绿）——
每条都做过破坏实验，破坏后确认变红、还原后确认变绿：

| 判据 | 抓的真故障 | 破坏实验 |
|---|---|---|
| J1 | 重复摄取把同一份字节在 originals/ 下写第二遍 | 拆掉 `archive_original` 的存在性检查 → 3 条红 |
| J2 | 手工删掉 raw 产物后三账仍自洽 | 让 reconcile 只比账不看字节 → 1 条红 |
| J3 | 发布 index 时中断，旧 index 被截断 | monkeypatch `os.replace` 在 index 目标上抛错 → 旧 index 逐字节完整、前代备份在位 |
| J4 | 续跑重做已完成件 / 全批失败后账本失真 | 让 `already_done` 恒假 → 3 条红；把 manifest 重签条件缩回 `touched` → 1 条红 |
| J5 | 适配器 5xx 被吞成"这批没有新件" | 把源故障记成 unchanged → 3 条红 |
| J6 | 第二版另开条目、版本链断掉 | 让 upsert 永远当作首版 → 4 条红 |

J1 第一版判据是**假绿的**：只跑"第二遍零写入"，走的是状态账短路，把
write-once 检查整段拆掉照样全绿。已补两条从状态账丢失和从
`archive_original` 直接进的判据，破坏实验才真的红。

**读产出**：用 `kb_create` 建了一个真库，摄取了 md + rar + zip + 一个无 ID 的
散文件，逐样读过：zip 占位件带中央目录清单且不含任何时间戳、`status` 汇总、
`reconcile` 绿→手工删一件→exit 1 并指名缺的是哪条 lineage 哪个路径；
`kb_doctor verify --manifest --raw --tree` 在摄取之后仍全绿。

**AI 评审**：本轮**没有做**。要做的话题面应该具体问两件事——
（1）这批判据里有没有"东西坏了还全绿"的（J1 已实证踩过一次）；
（2）`reconcile` 的红字段划分和 §IV/§VI 的语义对不对得上。

## 遗留

- AI 落位提议 + 精编产物（v1.1）。
- docdb 拆分件（`<fileId>#<锚点>`）的切分器：键的形状已放行并有判据，切分本身未做。
- 成本护栏（并发上限、限流退避、单篇超时、`raw/_unrouted/` 隔离区）：v1 单线程顺序跑，未实现。
