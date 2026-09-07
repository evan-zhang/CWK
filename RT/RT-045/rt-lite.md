# RT-045 端到端验收（两真库 + 黄金题）

- Case 1 cwork：镜像 raw 近 3 个月 ≈440 篇 → 向导建库 + 摄取（route=timeline 缺省）。
- Case 2 docdb：空间 2082734860367093762（投前流程系统建设，31 目录/133 文件）→ 向导建库 + 摄取（route=classify 缺省）。
- 黄金题：每库 ≥3 问经网关实时引文（字节比对）作答；doctor 全绿；覆盖率对账零缺件（或差异全解释）。
- 回执：本目录 receipts/ 验收 JSON + 报告。
- 判据：反空转——无操作桩必红、NAS 断网必红、引文必须经网关实时拉取字节比对。

## 2026-09-07 归属清单补登记（RT-051 配套收尾）

- RT-051 的 cwk-kb-query skill v2 升级新增 `skills/cwk-kb-query/references/v2-read-chain.md`
  （v2 读链参数表/三模式/易错点速查），4134bdc 推送时漏登记归属清单，
  CI run 34069821059 三个 governance 测试红（GA-ORPHAN）。
- 处置：并入 R-runtime-rt045-kb-skills exact_set（同 owner RT-045、同变更入口，
  安装副本同步口径不变）。`make governance-audit` 复验通过（730 文件全有主）。
