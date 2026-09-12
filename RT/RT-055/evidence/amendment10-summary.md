# RT-055 Amendment 10 — INVALID，失败收口完成

## 唯一结论与现场

唯一裁决 **INVALID**，未选中 A/B，不是质量 NO-GO。B 在首库构建阶段以 `CandidateError / REQUEST_FAILED` 终止；三库正式评分均未开始。没有生成正式 aggregate，也未用伪造分数运行质量裁决；本次 decision 来自协议的失败中止路径。不能依据公开同形 workload、Gateway health 或本地 258 项测试声称正式成绩。

- Source commit：`43207eefbe30557b53d556055fa5438c8afe47ee`。
- Run：`ac1ca0c7-6983-4f6e-91ce-8eb45e7673af`；migration：`ead5992d-fea7-4efb-b4c5-cedb1d7e8d12`；本窗：`be3ab168-0568-4d39-a06e-824b4cd26a58`。
- 冻结顺序 **B → A**，唯一原 coordinator 已退出，未重启。A attempts=0、B attempts=1；逐库 arm/exposure/query/score/complete/result 全0。
- 原 formal 文件及其 terminal 原样保留：`WAITING_RECONCILIATION / EXECUTION_ATTEMPT_FAILED / failed_phase=RUN_B / exit_code=2`，completed=[]。不伪造原 coordinator 已完成。
- 独立、仅失败收口 helper 唯一启动1次，追加 abort 与唯一 decision，终态 `INVALID_CLOSED`。未启动正式 coordinator 的 `--closeout-invalid` 分支，未启动任何新候选。
- B 首库构建最后计数：imported=30、completed=29、pending=1、failed=0；其它两库均0。这是导入状态，不是 query/质量指标。现存闭集证据只能归因为 `REQUEST_FAILED`，**底层原因未确定**；不诊断私有样例，不重放以求根因。

## 逐库 A/B 全20项

六个库/候选组合均 `NOT_MEASURED`。下列13计数（含 leak）、3质量比率、4资源指标共120格全部明确 null；不把保留题池42/31/42当成已测分母，也不把“没有检索”当成零错误/零泄漏成绩。

| 指标 | cwork-3m A | cwork-3m B | docdb-touqian A | docdb-touqian B | spbp-2027 A | spbp-2027 B |
|---|---:|---:|---:|---:|---:|---:|
| 题数 (`total_count`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| 有答案题数 (`answerable_count`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| Top10命中数 (`recall_hits_at_10`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| 精确题数 (`exact_count`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| 精确命中数 (`exact_hits`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| 无答案题数 (`no_answer_count`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| 无答案正确数 (`no_answer_correct`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| 系统错误数 (`system_error_count`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| 有答案题系统错误 (`answerable_system_error_count`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| 精确题系统错误 (`exact_system_error_count`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| 无答案题系统错误 (`no_answer_system_error_count`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| 超时数 (`timeout_count`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| 越界命中数 (`leak_count`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| Recall@10 (`recall_at_10`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| Exact (`exact`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| NoAnswer (`no_answer`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| P95 ms (`p95_ms`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| 完整数据面 bytes (`index_bytes`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| 完整构建 seconds (`build_seconds`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| 峰值 RSS bytes (`peak_rss_bytes`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |

## 机械复杂度与 Gateway 四能力

机械复杂度来自 source/freeze 绑定的 runbook 数组，A/B 已独立重数。它是方案步骤数，不是本轮实际服务性能，也不构成部署验收。

| 机械向量（冻结 runbook 逐项计数） | A | B |
|---|---:|---:|
| 组件数 | 1 | 2 |
| 升级步骤 | 6 | 7 |
| 备份恢复步骤 | 4 | 3 |

| Gateway 四能力 | cwork-3m A | cwork-3m B | docdb-touqian A | docdb-touqian B | spbp-2027 A | spbp-2027 B |
|---|---|---|---|---|---|---|
| HTTPS query API (`https_query_api`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| 逐 Gateway 身份 (`per_gateway_identity`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| 服务端逐库权限 (`kb_grants_server_side`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |
| 不直持 NAS/search 凭据 (`no_direct_nas_or_search_credentials`) | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 | null / 未测 |

四能力未完成正式候选 probe，六组均 null。生产三 Gateway 独立健康检查均 HTTP200、ok=true、read_only 字段存在且true；**生产健康不替代候选四能力**。

## 正式日志：保留失败，不涂绿

- B 6个流全部 EOF/closed/verified，9次脱敏替换；原 log-scan PASS，hit_files=0，6个归档日志与 firewall 清单相符，独立后置扫描命中文件=0。运行 workspace 已由原 runner finally 精确清理，归档日志保留 OPS。
- coordinator 日志 firewall **verified=false / CHILD_NONZERO**：子进程 exit2，不是全部通过。EOF/closed=true，原 post_scan_hits=0，独立后置扫描=0。将隐私扫描未命中与执行失败分开记录。
- 正式日志原文、私有 query/expected/题面/title/filename/path/doc_id/locator/body/片段及私有摘要均未导出。只导出协议固定库名、枚举、计数、布尔、空值及公开身份。

## cleanup → after → abort/decision

- 收口前已核验旧 PID 均退出、task_process_count=0、owned_runtime=0；启动器和 helper 再次拒绝活进程/有归属运行目录，未手工终止进程。
- 本窗 cleanup 回执1份：failures=0，temporary indices/services/containers 均0，related_processes=0，UUID容器/卷/网络资源=0，私有 holdout 保留。
- before=PASS；after=PASS，claim=1，已测库数=3，error_kind=None；不覆盖、不重试。after snapshot=True，comparison=True。
- 原本窗 37 个收口前文件逐字节保留；仅追加 audit/after/abort/decision。formal 原始失败状态未改写，追加的 abort 回执是本次最终终止权威。
- 独立审计完成后再次核验，无正式 coordinator、candidate、closeout helper 或本窗临时数据面；审计进程已退出。没有另一个观察器继续实验。

## 生产不变性与历史

本窗 before/after 实测比较：

```json
{
  "all_items_measured": false,
  "containers_unchanged": true,
  "existing_indices_unchanged": null,
  "existing_indices_unchanged_measured_projection": true,
  "gateway_health_content_unchanged": true,
  "gateway_unchanged": true,
  "nas_unchanged": null,
  "nas_unchanged_measured_projection": true,
  "production_config_unchanged": false,
  "production_config_unchanged_measured_projection": true,
  "services_unchanged": false,
  "volumes_unchanged": true
}
```

完整 NAS/index 不变性仍 **UNKNOWN / null**；all_items_measured=false。元数据/已登记索引投影相等不能证明全部内容不变，实测 false 也不会改成 true。历史比较原件与其 false/null 保留，公开 final 中逐旧窗保留闭集 `historical_comparisons`，不以当前健康消除历史漂移。

- 96材料、builder/verifier=1/1、题池42/31/42保持；未重建题池、未换seed/tier/floor/算法/质量门/7200秒budget。
- 独立核验历史 389835 文件、归档源码 27 文件及旧窗文件集合/字节一致；历史嵌套归档在完整清单内，不重新拷贝、不删除。
- Amendment9 window `7652dbee-3679-4886-a391-ffcf871e712c` 继续 INVALID/after FAIL；本次未重试旧after、未重开旧窗，旧claim/void/freeze全部保留。
- 未修改生产/NAS/index/alias/config或WeKnora core；不切流，RT-055选型目标未完成，不能按“失败收口已完成”关闭选型RT。

## 源码与准备证据边界

本次接管未修改部署源码。已有in-process legacy void proof修复及准备证据原样保留；准备READY是正式运行前的历史状态，不代表本次最终状态。公开同形数据不混入正式指标。

## 验证与交付

- [公开最终证据与120格指标](amendment10-final.json)、[唯一裁决](amendment10-decision.json)、[独立终态观察](amendment10-observation.json)、[最终QA](amendment10-qa.json)，均配闭集Schema。
- 接管后重新运行258项RT055脱敏回归，0failure/error/skip；142个Python文件编译。不宣称全仓CI。
- 判据：源码/冻结/旧文件重算、逐库账本、source-bound机械向量、日志归档扫描、Schema缺字段/注入/变值拒绝、秘密/隐私扫描、链接/锚点、AODW、governance、diff；详见QA实际回执。
- 评审：接管主工程师逐项检查helper成功/失败适用性，限制为失败路径，并新增防重入与无活进程门；未外派私有材料，未让执行worker自批。
- 读产出：独立对照原formal终态、build枚举/计数、firewall负值、after比较、120个null与Gateway四能力缺测，确认失败未被包装成通过。

只本地提交，不push、merge或清理worktree。最终commit与clean状态由提交后Git复核回执报告。
