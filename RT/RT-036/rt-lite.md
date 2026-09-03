# RT-036：精编范围策略（--refine-scope owner + --min-year）

## 决策

2026-09-03 13:27/13:36 Evan 拍板：个人镜像的 AI 精编改为选择性——只精编与镜像主人
相关的汇报（writer 或任一结构化角色字段含主人姓名/工号），请假/通知/公告等旁观
内容保留确定性 fallback 摘要、不做 AI 精编；同时把年份下限恢复为脚本能力
（此前丢失 --year 约束，队列混入 28 篇 2022-2025 历史文档）。两项合并为固定策略，
由宿主 cron 常态携带。

数据依据（2026-09-03 实测，2026-08 抽样 400 篇）：角色字段含镜像主人的汇报仅
~6%；过滤后 2026 待精编量 4658 → 约 300 篇。

## 变更（scripts/cwk_cloud_wiki_compile.py，script-evolution-v2 ord1）

- 新参数：--refine-scope all|owner（默认 all，行为不变）、--refine-owner-name、
  --refine-owner-emp-id、--min-year N
- owner_involved()：仅匹配 frontmatter writer 与角色标签行（汇报人/发件人/收件人/
  建议人/审批人/决策人/申请人/参与人/部门负责人/抄送/知会人），正文自由文本永不
  参与匹配——防重名误伤
- partition_year() + --min-year：自动选篇仅考虑 raw/YYYY-MM 分区年份 >= N
- scope=owner 时：missing 中范围外文档物化为 fallback 页（覆盖契约不变，计
  scope_excluded），fresh/retry 队列按 owner 过滤；--fallback-only 模式不过滤
- manifest-out 新增 refine_scope / min_year / scope_excluded

## 边界

- 默认 all：其他部署行为零变化；个人镜像 cron 携带 owner + min-year 作为固定策略
- 不触碰 DEFAULT_MODEL/DEFAULT_REPAIR_MODEL 字面量与模型允许清单（沿袭 RT-035 红线）
- 已精编内容不回退

## 验证

- tests/test_rt036_owner_refine_scope.py（角色匹配/正文中提及不算/分区年份/CLI 校验，11 例）
- make governance-audit + tests/test_governance_audit + GitHub CI
