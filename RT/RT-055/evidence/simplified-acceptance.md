# RT-055 简化版考试收口 — 唯一裁决 A

## 授权与边界

授权依据：Evan 2026-09-12 21:18「直接开考」；Evan 2026-09-13 07:19 问结果即收尾授权；本轮 07:24 明确要求独立完成评分、隐私抽查、裁决、清理核验和本地提交。前两项由本轮明确确认，不冒称重新检索了原始会话。

PRIVACY_MODE=SIMPLIFIED。这里只接续终态，不重跑 A/B、builder/verifier 或候选查询，不修改冻结题池、历史失败和已消费记录。只提交必要本地改动，不 push、merge、切流或修改生产/NAS/他人资源。

## 真实成绩与裁决

OPS status 为 DONE / MEASUREMENTS_COMPLETE。冻结题数 cwork/docdb/spbp=42/31/42；A 对账115、A新增0、B新增115。A 原成绩与冻结 receipt/exposure 绑定，B 成绩与计数器绑定；不是逐题响应日志完整性证明。

下列顺序为 Recall@10 / Exact / NoAnswer / P95(ms)，计数和完整精度以 [aggregate v3](simplified-aggregate-v3.json) 为准：

- cwork-3m：A 0.918919 / 1 / 1 / 9.251；B 0.621622 / 0 / 0 / 105.633。
- docdb-touqian：A 1 / 1 / 1 / 8.548；B 0.846154 / 1 / 0 / 63.319。
- spbp-2027：A 1 / 1 / 1 / 6.593；B 0.783784 / 0.875 / 0 / 94.663。
- A 三库 index_bytes / build_seconds / peak_rss_bytes 全为 null；不补零、不重跑、不宣称 A 的资源优势。
- B cwork：142390344 bytes / 4600.468 seconds / 13010763776 bytes。
- B docdb：28322872 bytes / 168.578 seconds / 4603314176 bytes。
- B spbp：71212984 bytes / 364.470 seconds / 3685761024 bytes。
- 两候选三库 system_error、timeout、leak 全为0。逐库保留现有合同全部13个计数字段（含总数、分类分母和分类错误），未为“十二项”删掉任何字段；每库共20项指标。

[唯一真实 CLI 裁决](simplified-decision.json)：A / PASS。A 三库质量及 Gateway 能力均过门；B 三库都有质量硬门失败，共8项失败，不能入选。简化模式的已知 A 资源缺测不构成 INVALID；未测资源不参与“B 显著更优”的证明。隐私 FAIL、隐私未核实、清理失败或必要测量缺失仍返回 NO-GO；合同错误仍拒绝。不改正式模式门，不把本次 PASS 解释为上线许可或完整隐私证明。

## 隐私、清理与不变性

[独立终审](simplified-final-audit.json)在 OPS 内重验冻结 query/title 集合与原确定性10条样本绑定，对 A/B 及 controller 全部12份留存日志执行固定字符串 grep，0份命中：privacy_check=SIMPLIFIED_SPOT_PASS。样本、私有字符串、私有摘要和绝对路径均未出 OPS；导出前另扫描全部私有 query/title/定位标识，拒绝值泄漏。抽查不是全量隐私门，也不撤销历史 FAIL/UNKNOWN。

原 before/after 对比重新计算，与原 summary 完全一致；另做最新本机只读清点。清理失败0，列示PID存活0、候选活跃进程0、临时数据目录0、自有容器/卷/网络/服务/搜索索引0；本轮无需删除资源，清理动作0。Gateway 8787/8788/8789 均 HTTP 200。历史记录与私有证据保留。

最新本机 Gateway、配置、容器、卷和已观测索引投影与 before 一致，但 services_unchanged=false；原 production_config_unchanged=false 如实保留，不能归因为本考试，也不改成 true。完整 NAS / 全部索引不变性证据不足，nas_unchanged 与 existing_indices_unchanged 保持 null；本轮未写 NAS，未补做完整 NAS 扫描。

## 必须随裁决保留的五项 caveat

- A_ALREADY_CONSUMED_RECONCILED_NO_REPLAY
- A_RESOURCE_MEASUREMENTS_MISSING
- B_ONLY_FRESH_RESOURCES
- NO_ANSWER_TO_CONFLICT_DEFAULT_PRESERVES_SINGLE_USE
- SIMPLIFIED_REPLACES_AMENDMENT_CONTROLS

补充证据边界也保留在 aggregate/decision：
- PER_TRIAL_RESPONSES_NOT_RETAINED_IDENTITY_AND_COUNTER_RECONCILIATION_ONLY
- FULL_NAS_AND_EXISTING_INDEX_INVARIANTS_UNMEASURED

## 验收证据

- 工程判据：[QA](simplified-qa.json)记录对应单测、全部相关 RT-055 回归、schema 反例、行为破坏及隐私/secret/path扫描；不冒称全仓 make ci。
- AI 复核：主执行者独立检查接续实现、OPS原始聚合字段、冻结绑定、缺测语义及本次真实裁决；未虚构独立 reviewer Agent。
- 读产出：已逐库核对分母、命中、错误计数、P95、资源 null 与原始结果；验收读 aggregate、唯一 decision 和 cleanup/baseline/隐私审计，而不是仅凭测试绿灯。
