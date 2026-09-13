# Amendment 8 — BLOCKED，不是 READY_TO_RUN

## 结论与授权边界

正式日志防火墙和 B build 计数诊断已实现、提交并在 OPS 部署。公开同形 workload 未完整通过，故本轮止于 **BLOCKED**；新 window、before、freeze、freeze verification 均未创建，未运行正式 coordinator。不能以本地测试或单库成功替代完整 readiness。

- 源码提交：`a475b048c235b2f4a8b92d845760462b4879c83c`。
- Run：`ac1ca0c7-6983-4f6e-91ce-8eb45e7673af`。
- 新 migration：`387d6d9e-5ae4-4ebb-96b7-d993275b6111`。
- 预留但**未创建**的 window：`16e5d3f2-0963-4b70-a202-c197c3b8958a`；run order 未抽取。
- 本次历史归档：`42b7617e-3769-4eda-99df-b1f84032005c`，97376 文件。所有先前迁移及失败链保留。
- 旧 window `d408cfab-40f7-41d3-8ccb-75fb0aaa2b65` 永久 INVALID/after；旧 log gate 保持 FAIL，不重开、不改写。
- 正式新 attempt/arm/exposure/query/score/result/after 全 0；全局 holdout exposure、正式 query/result 仍 0。公开合成查询不是正式评分。

机器可读权威入口：[readiness](amendment8-readiness.json) / [Schema](amendment8-readiness.schema.json)、[OPS 独立复核](amendment8-ops-verification.json) / [Schema](amendment8-ops-verification.schema.json)。本地 evidence commit 可由本文件的 Git 历史定位；不将证据提交冒充 OPS 部署源码。

## 根因与 firewall 保证范围

[根因证据](amendment8-root-cause.json)：GORM 独立默认 SQL logger 绕过应用 `LOG_LEVEL`，且未配置参数化日志；旧 stdout 直接写文件，后置 scan 只能发现已经落盘的泄漏。旧 6 日志中 1 文件命中，类别为 2 个 case_query 字段、6 个 filename 字段、共 20 行；未导出私有值、原文或内容 digest。旧 build 约 7720.6 秒，但其具体失败子类仍 **UNPROVEN**。

本次不 patch WeKnora core。controller-owned firewall 覆盖候选 stdout/stderr：

- 全部私有 string leaf 及 JSON-escaped 变体只在父进程 pattern bank 中；共享 bank 上限 128MiB，单 leaf 上限 8MiB。私有全集容量预检通过，未通过参数、环境变量或文件把 needles 传给子进程。
- pipe + 专用 drain 线程按 64KiB 读取，不依赖换行。未决尾部留在内存，跨 chunk、Unicode、转义匹配后替换为固定 `[RT055_REDACTED]`。
- 每路 input/output 各封顶 64MiB；超限块不写入日志。input 按实际读取计量（发现溢出时最多多读一个 chunk），output 按实际 write 返回字节计量。
- 原始候选 stdout/stderr 不落盘；只有净化日志写入 0700 candidate workspace。日志回执仅含 log identity、字节数、替换次数、overflow/error/eof/closed/verified，不含 needles 或其 digest。
- 容量、drain、EOF、关闭、异常子进程状态失败即停止所管候选、拒绝 gate。只有完整 verified 后才允许 scan；scan 必须 0 命中，只有合格净化日志进入归档。

[本地回归](amendment8-local.json) 241 tests、0 fail/error/skip、141 文件 compile；含跨 chunk、长行/无换行、Unicode/JSON、64MiB cap、输出膨胀、drain 异常、missing EOF、child nonzero、关闭与 fd/thread 清理。7 项[行为破坏测试](amendment8-mutations.json)均检出，恢复后绿色。保证针对本次受管日志边界，不宣称解决任意应用侧文件、主机外部遥测或未执行的正式 workload。

## B build 诊断与公开 workload

新诊断逐库记录 imported/completed/pending/failed、deadline phase、耗时与封闭 error enum；原生 `failed` 与 `pending/processing` 分开处理，terminal failed 不再睡眠轮询至 deadline。原 7200 秒/库对称 build timeout 不变。

公开文档独立编写，不从私有文本抽样：42/31/42 documents 与同数 trial 合同校验，每库一份不超过 512KiB 文档，其余不超过 8KiB；中英文 UTF-8 文本，canonical 字符数不超过 native 200000 上限。A/B 使用同一文档输入。trial 被构造并验证；不是执行了全部 trial 的正式评分。

第四轮实际结果：

- A：三库分别 42/31/42 completed，0 pending/failed；build 2.081 / 1.541 / 1.947 秒，真实公开 search 各 1 次。firewall、scan、cleanup 通过。
- B CWork：42 imported/completed，0 pending/failed，build 56.203 秒，真实公开 search 1 次。
- B 投前库：31 imported，0 completed，1 failed，30 pending；在 IMPORT 阶段 0.188 秒观察到 `NATIVE_TERMINAL_FAILED` 并停止。这是观测到的子集状态，不代表剩余 30 份永远不会完成。
- B SPBP：尚未导入、build 或 search；不得记为 PASS。
- 公开净化 native 日志：投前库 `UNIQUE_CONSTRAINT` 1 次；这是诊断线索，未证明具体列、触发顺序或底层根因。CWork 的 `SQLITE_BUSY` 1 次来自成功 build/search 后的刻意 SQL canary 探针，不能归为投前失败原因，更不能归为旧私有 B 的原因。

失败路径的 workload 总回执未回填 B 的 `firewall_verified`，原字段为 false，**原件保持不变**。另行关联相同 build-status 所在 workspace 的最终 drain 回执：6 streams 全部 verified/eof/closed，input 34991、output 34776 bytes、20 次替换，overflow/error 0、post-scan 0。两个事实均载入 readiness，不用后置证据把 workload 改成 PASS。

前三次公开失败分别保存在 [migration1](amendment8-migration1.json)、[migration2](amendment8-migration2.json)、[migration3](amendment8-migration3.json)。包含探针干扰正常导入和 ASCII 字节夹具超过原生字符上限；不是正式 A/B attempt，也不是正式成绩。

## 其余 gate、清理与不变性

- source-bound 公开隐私原证据独立重算 PASS：正常请求与认证错误路径、实际 embedding、日志 canary、网络与禁止私有读取检查通过。私有全集 Filter input receipt 重新验源与文件绑定 PASS，无 Popen、无 query。
- workload 未通过，迁移级 privacy binding 未发布；main runtime/workspace/scoring readiness、before、freeze/verify、随机顺序、formal precheck 未运行。不借用旧窗 READY。
- Gateway 三路均 HTTP 200、ok=true；但一路 `/health` 缺少 `read_only` 字段，故 **Gateway read-only contract 未验证**。未修改 Gateway、配置或 baseline 的 READY 断言；最终审计只对 BLOCKED 状态记录 HTTP 健康，不把它等同完整合同通过。
- 首次 final audit 在健康断言失败，失败证据保留；题池与 builder/verifier 并无变动。后续只读复核分别列明健康事实与缺口。
- builder/verifier 仍 1/1，42@T3 / 31@T3 / 42@T2；96 原材料、5938 初始归档、全部迁移历史字节比对保持。旧 attempt/claim/arm/window/log gate 未删改。
- WeKnora pinned core、冻结镜像与依赖不变；候选/合成/controller 进程、runtime/data plane、新 tmp/TLS、run-scoped container/volume/network/image/service 均 0，cleanup failures 0。
- 未放宽 sandbox/egress，未改生产/NAS/index/alias/config。完整 NAS/index 不变性仍 UNKNOWN，未清除历史漂移；HTTP 健康不能替代这些证明。

## 下一步边界

本轮不再重跑。若后续另行授权继续，应先隔离解释公开投前库 terminal failure 与失败路径回执回填缺口，并核实 Gateway health 合同；需新的 source-bound 证据，不能重开旧 window、复用失效 claim 或消费 holdout 来诊断。当前状态是 **BLOCKED，无后台任务**。
