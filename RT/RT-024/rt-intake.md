# RT-024 rt-intake：安全审计、可观测性与容量基准

- 状态：planned（未实现、未测量、未独立验收）
- Profile：Spec-Standard / Security-Critical
- 目标：建立可回放、可检测篡改的追加式审计与 tenant-scoped 健康指标，
  在 10 tenant × 10,000 report 合成负载上记录首个真实容量/延迟基线。
- 硬依赖：RT-018、RT-020～RT-023，且 **VG-B、VG-C、VG-D 均已独立
  PASS**。只满足 VG-D 不足以启动最终基准。
- 完成条件：审计/指标契约、日志脱敏、隔离与故障测试通过；真实记录 Broker
  core 和沙箱 E2E 基准，不预填目标数字；独立验收明确 PASS。

## 一、范围

1. binding/profile/grant/revoke/route/projection/query/backup-admin 等关键事件的
   追加式审计、hash chain、checkpoint 和 replay。
2. tenant health、队列/锁/授权/路由/空间/query/成本与恢复指标；tenant 只能
   看自身摘要。
3. 脱敏日志与扫描器；禁止 raw、AppKey、完整 query、临时 URL、原始 Agent
   ID 和宿主机绝对路径。
4. 10×10,000 合成数据基准：warm/cold、Broker core、真实沙箱 E2E、多个
   并发档、撤权逻辑拒绝与派生清理时间。

## 二、明确不做

- 不预设或伪造 P50/P95、容量、配额和撤权 SLA 目标；只产出可复现实测。
- 不实现备份/恢复（RT-025）、试点切换（RT-026）或生产 exporter 部署。
- 不把 metrics exporter 变成数据面硬依赖，不启用真实 tenant/cron/AppKey。
- 不记录完整 query/raw 以换取调试便利，不提供跨 tenant 管理查询旁路。

## 三、拟议代码所有权

- 新增：`scripts/cwk_audit.py`、`scripts/cwk_metrics.py`、
  `scripts/cwk_pr001_benchmark.py`。
- 新增 `contracts/rt024/schemas/` 和 `tests/test_rt024_*.py`。
- 仅通过已冻结 adapter 接入上游组件；不重写其权威数据和安全判断。

## 四、故障语义

- metrics/exporter/tenant dashboard 故障不影响 collector/router/projector/Broker。
- security audit 的本地 append 是关键操作的一部分；若本地安全 spool 也不能
  持久化，需审计的管理/授权操作 fail closed。export 失败只积压有界 spool。
- 审计或日志内容不合规时拒绝事件，不以“先记录再清洗”处理敏感正文。

## 五、回滚

关闭 exporter/benchmark runner，保留 append-only audit 与 chain checkpoint；
数据面不删除、不反向依赖指标服务。不得通过回滚抹除审计事实。
