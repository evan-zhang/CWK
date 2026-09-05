# RT-044 建库向导 + 查询网关 v1（MVP）

- 目标：① kb CLI 向导动词（create/ingest/status/query）包装 kb_create + kb_ingest + 查询；② 只读网关进程（HTTP JSON）。
- 上游合同：references/CLI-SPEC.md v1.1、KB-PARAMETERS.md。
- MVP 裁剪：向导=参数式（--yes 非交互 + JSON 确认卡输出）；对话式 cwk-kb Skill 包装层在 RT-045 验收时提供。网关 v1 单进程单端口；鉴权=管理 Key 派生 token（SHA-256 + 常时比较）；只读动词；NAS 专用只读账号 _kbquery 列 v1.x（今日现账号 + 网关侧禁写兜底）。
- 交付：scripts/kb_wizard.py、scripts/kb_gateway.py + tests/test_kb_wizard.py、tests/test_kb_gateway.py。
- 判据（负例红）：
  - J1 向导目的地脏必拒（复用 kb_create 预检），零写入。
  - J2 网关写语义/未知动词一律 405/404；只暴露查询与引文。
  - J3 引文实时性：citation 动词现场从 NAS 拉字节，返回 sha256+节选，不读本地缓存。
  - J4 token 错→401；token 只能查，拿不到任何管理面。
  - J5 向导输出一律 JSON（CLI-SPEC 合同）。
- 红线：两进程宪法——factory 写面（向导/摄取）与 gateway 读面严格分离；网关进程不实现任何写动词。
