# Amendment 9 — 准备门执行中，尚非 READY_TO_RUN

仅修复公开 synthetic B 并发导入问题并建立新准备窗口；正式 A/B 未授权、未运行。

公开根因已从 Am8 synthetic 日志只读确认：native `chunks.seq_id` 并发分配竞态导致 UNIQUE 冲突，多行 INSERT 可见；不属于内容上限或 timeout，不推断旧私有 B 子类，不复制长日志。

实现每文档一次 POST → 同 ID pending 轮询 → completed 后下一 POST；failed 立即停止，transport 独立拒绝提前/重复/重入，7200 秒总 deadline 包含串行等待。所有历史回执保留。

最终状态以本页随后追加的验收结果及机器可读回执为准；当前不是运行正式 A/B 的凭据。

---

# Amendment 9 最终收口 — BLOCKED，未执行正式 A/B

## 结论与失败点

**本轮不能发布 READY_TO_RUN。** 已完成的串行公开同形与隐私门仍 PASS；失败发生在后续强制的无 Popen/socket 预检，不是再次发生 B 导入失败，也不是把 Gateway 健康当作完整准备门。

真实调用链：`coordinator_precheck → holdout_unexposed → void_paths → validate_void → validate_evidence → _proof → subprocess.run/Popen`。旧 claim/void 校验需要启动一个仅含公开合成案例的独立 Python 证明进程；禁止 Popen 的 guard 在启动之前抛出 `AssertionError: NO_POPEN`。不是正式候选进程，正式 query 未发生，但它确实违反本轮纯只读、无子进程合同。源码已绑定的 upstream 无 Popen 清单不足以覆盖这条旧 void 依赖。

常规 freeze verify 和两种 spawn policy precheck PASS，不能掩盖 coordinator_precheck 失败。独立只读复核重现相同拒绝；没有 mock 掉旧 void、删除 claim、放松 guard、重启 readiness 或改 source scripts。254 项本地回归和13项既有变异均不能替代这个真实 OPS 门。

机器权威：[readiness](amendment9-readiness.json) / [Schema](amendment9-readiness.schema.json)、[失败调用链](amendment9-readiness-failure.json) / [Schema](amendment9-readiness-failure.schema.json)、[独立 OPS 终态](amendment9-ops-verification.json) / [Schema](amendment9-ops-verification.schema.json)。

## 冻结与失败收口

- 部署源码：`6b32584023cd27096e7c2167c0c28f47b380f922`。证据提交由本文件 Git 历史定位，与部署源码分开。
- Run：`ac1ca0c7-6983-4f6e-91ce-8eb45e7673af`；原 migration：`89edbd10-2905-41f1-9037-62b502890856`，公开控制器终态仍 `READY_TO_FREEZE`。
- 使用唯一预留 window：`7652dbee-3679-4886-a391-ffcf871e712c`。新随机顺序 **B → A** 来自本次已有 freeze receipt，没有重新抽取；**此顺序不是正式运行授权**。
- main runtime/workspace/scoring-input → before → freeze → freeze verify 均已完成，才在最后无 Popen 预检发现失败。因此不能虚写“未 freeze”，也不能删除 freeze。原 before/freeze/verify claim 各1且原件保留。
- 按失败协议仅执行一次本 window 的 cleanup/after：cleanup PASS；after claim=1，但NAS读取在完成1库后返回 `TransientStorageError`，after状态FAIL，未产生完整after快照或comparison。原失败claim/status与控制器终态保留，**没有重试或覆盖after**。窗口追加标记为 **INVALID/after-failed，永久关闭**，只记录真实失败，不声称after验证完成。没有调用formal coordinator，没有formal controller/candidate attempt，READY receipt未创建。
- 两次 SSH 观察中断均先对账原 launch PID/claim，再恢复只读观察；readiness controller 与失败 closeout 各只启动1次，未重启。

## 已完成公开同形（本轮未重跑，不是正式成绩）

A/B 输入42/31/42、512KiB/8KiB字节上界不变，单库总 timeout7200秒不变。

- cwork-3m: A build **2.128s**；B build **62.924s**；A/B completed=42/42，pending/failed=0；B max_inflight=1、POST=42、completed→下一POST屏障=41。
- docdb-touqian: A build **1.667s**；B build **54.406s**；A/B completed=31/31，pending/failed=0；B max_inflight=1、POST=31、completed→下一POST屏障=30。
- spbp-2027: A build **2.076s**；B build **62.913s**；A/B completed=42/42，pending/failed=0；B max_inflight=1、POST=42、completed→下一POST屏障=41。

每候选各库真实 public search 1次（各3次）；B native UNIQUE constraint事件0。公开 SQL canary 在正常 build/search 后执行，非重试、非正式 query。[独立 native/firewall 重算](amendment9-native-workload.json)。

A3/B6 streams：verified、EOF、closed、caps全PASS，redactions各18，postscan/firewall errors0。B input/output18237/18048 bytes。main startup另外3A/3B/3sidecar、私有读取0/正式query0、最终drain与清理PASS。私有全集Filter、源绑定privacy与scoring-input均PASS；私有值、路径和digest未导出。

## 零正式暴露、清理与历史

新 attempts/arms/exposure/query/score/result **全0**；global formal exposure/query/result **全0**。唯一不再为0的是按失败协议创建的 **after claim=1，after FAIL**，不能与READY的after=0混写，也不能把失败claim当成完整after证明。旧d408窗口保留INVALID/after/logFAIL，B attempt1/A0、旧claim/void/arms及所有历史窗口原样保留；Am8未创建的预留窗仍未创建。

builder/verifier=1/1；三库42@T3、31@T3、42@T2。96材料、5938原归档、97376 Am8归档及本次归档 `1b9f56b2-688f-4b94-a95d-e1fa55d074c5` 的194848文件均按字节复核；历史文件集合也未变。新失败controller、before/freeze/verify原件另按收口前清单逐字节核对。

Gateway：**before 与独立最终均 3×HTTP200、ok=true、read_only 字段存在且 true**。候选/public/main-readiness/closeout进程、runtime/data plane、新tmp/TLS、run-scoped container/volume/network/image/service均0，cleanup failures0。native core/冻结镜像/依赖不变；未改生产/NAS/index/alias/config/沙箱/egress。**完整NAS/index不变性仍UNKNOWN**；before完成，但after的NAS读取失败，没有完整comparison，不把缺测填为True，不清除历史漂移。

## QA与后续边界

本轮重新运行完整RT055 **254 tests，0fail/error/skip，142文件编译**；源绑定的13项已检出变异及其恢复绿保留，未重跑修复/变异。闭集Schema反例、private/secret/links、历史前缀、AODW/governance/diff见[最终QA](amendment9-qa.json)。这些绿灯不把失败的OPS硬门升为PASS，不冒称全仓CI。

仅本地提交公开RT055证据/文档；source scripts/tests/contracts逐字节不变，runs/与任何私有/交接材料不入Git，不push/merge。后续若要READY，须另行授权修复纯只读校验的旧void依赖并重新建立准备门；本轮不修、不新建migration、不复用此已关闭窗口，更不执行正式A/B。
