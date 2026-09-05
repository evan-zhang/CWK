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
