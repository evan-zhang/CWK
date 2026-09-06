# RT-051 A11 词法评测 gold 独立复核报告

## 裁决

**PASS** —— 48 题矩阵、阈值断言、gold 完整性、指标真实性、突变可证伪性全部核验通过；未发现不合格项。gold 由独立复核人签收（本报告），`a11-results-latest.json` 的 `reviewer/reviewed_at` 字段仍为 null，按"gold 不自签"约定由本报告完成独立签收记录。

- reviewer: independent-reviewer-session
- reviewed_at: 2026-09-07 06:11 (Asia/Shanghai, GMT+8)
- 复核基线: worktree `RT-051-body-lexical-retrieval` @ `75e87f7`（复核全程 git status 干净，唯一新增产物为本文件）

## 复核范围与硬边界遵守

- 复核对象：`tests/test_rt051_a11_lexical_eval.py`（48 题矩阵）、`RT/RT-051/a11-results-latest.json`、规格 `RT/RT-051/rt-lite.md` 词法验收行 + `acceptance-matrix.md` A11 行。
- 仓库只读：未 commit、未 push、未改任何源码/测试/gold 的最终状态；突变用临时改文件 + `git restore`，dump 用 `git checkout -- RT/RT-051/a11-results-latest.json` 恢复。每步后 `git status --short` 确认干净（见突变记录）。
- 未触碰真实 NAS/DocDB/凭据：一切在合成语料、FakeDocdb/MiniDocdb、本地 LocalFSBackend 内。

## 1. 规格符合性（必做核查 1）

| 规格项 | 核对方式 | 结果 |
|---|---|---|
| 48 题 | dump JSON `results` 计数 = 48 | ✅ |
| 2 合成库 | setUp 建 liba/libb 各 14 件（28 件总量），两库均真实 ingest | ✅ |
| 8 类×6 | dump 按 category 计数：body_only/short_term/code/no_answer/version/isolation/index_fault/metadata_compat 各 6 | ✅ |
| macro doc Recall@10 ≥ 0.90 | `assertGreaterEqual(macro, 0.90, summary)`（test_a11_full_matrix） | ✅ 真实断言 |
| 正文-only / 短词各 ≥ 5/6 | `assertGreaterEqual(body_recall, 5/6)` / `assertGreaterEqual(short_recall, 5/6)` | ✅ 真实断言 |
| gold span Recall@10 ≥ 0.85 | `assertGreaterEqual(span_recall, 0.85)` | ✅ 真实断言 |
| 隔离泄露 0 | `assertEqual(iso_leaks, [])`（isolation+index_fault 12 题 ok 字段） | ✅ 真实断言 |
| verified 错版 0 | `assertEqual(wrong_version, [])`（version 题旧 ref 必须 409 stale_reference） | ✅ 眔实断言 |
| verified 错 span 0 | `_check_span` 要求 `full_sha_verified` 的 read 中 marker 命中，span_ok=False 的 version 题直接 fail_note 进 faults → `assertEqual(self.faults, [])` | ✅ 真实断言 |

阈值全部用 `assertGreaterEqual/assertEqual` 钉死，不是只打印。断言摘要 `summary` 随断言一起抛出，失败时可见全部字段。

## 2. gold 完整性——逐题独立核对（必做核查 2）

两条独立路径，均不 import 测试文件的 phase0 代码：

**路径 A（静态重建）**：`/tmp/a11_independent_check.py` 从测试源码提取语料定义重建 28 件正文/标题/路径模拟（path 用 `/synthetic/...` 模拟），按独立逻辑逐题核对，94 项检查全过：
- body_only/short_term/code（Q01–Q17 共 17 题）：marker 在期望件正文唯一出现、不在任何件标题/路径、对库正文亦无。
- no_answer（Q19–Q24）：查询字逐字不在该库标题/路径/正文。补充核对：查询字与 docdb_root `玄关/合同` 路径组件（真实 path 为 `raw/classify/玄关-合同/<name>`）零交集。
- code 零答案（Q18）：`017` 作为独立整词（正则 `[A-Za-z0-9-]+` 全匹配）不出现在任何正文/标题/路径；`017` 确实作为 `AB-017` 子串存在——失败样例前提成立，语义正确（整词 token 匹配下 017 不得命中 AB-017 所在件）。
- metadata_compat（Q43–Q48）：查询词只在期望件文件名、不在任何正文（含纯 ASCII 元数据件）。
- version（Q25–Q30）：marker 唯一性同源题复证。

**路径 B（真实 ingest 链）**：`/tmp/a11_real_ingest_check.py` 自建 MiniDocdb runner（不 import tests/ 任何代码，接口契约对照产品调用点 browse.py/download-file.py 的 --output 契约），直接调 `kb_create.create_kb` + `kb_ingest.refresh_library` 产品代码构建两库，读真实 `_system/raw-index.json`，title 实际为空、path 实际为 `raw/classify/玄关-合同/601-节点说明.md`。逐题核对全部通过（0 problems）。

isolation（Q31–Q36）与 index_fault（Q37–Q42）为行为断言题（403/401/410/400/503），语料层无可核对 marker，其正确性由 dump 指标重算（第 3 节）与突变检验（第 4 节）覆盖。

## 3. 指标真实性——dump 重跑与独立重算（必做核查 3）

命令：`CWK_RT051_A11_DUMP=1 PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_rt051_a11_lexical_eval` → `Ran 1 test ... OK`。重跑后 dump JSON 与提交版**逐字节一致**（`git status` 干净，`git diff` 空），复现性成立；重跑后执行 `git checkout -- RT/RT-051/a11-results-latest.json`（因 diff 为空，无实际变更），仓库保持干净。

独立重算脚本 `/tmp/a11_recompute.py`（不复用测试收口逻辑，从 dump 原始记录按规格公式重算）：

| 指标 | 独立重算值 | 阈值 | 判定 |
|---|---|---|---|
| macro doc Recall@10 | 36/36 = 1.0000（把 Q18 honest_zero 计入命中）；保守口径剔除 no_answer 的零命中分母 = 35/36 = 0.9722 | ≥ 0.90 | PASS（两口径均过） |
| body_only Recall@10 | 6/6 | ≥ 5/6 | PASS |
| short_term Recall@10 | 6/6 | ≥ 5/6 | PASS |
| gold span Recall@10 | 23/23 = 1.0000 | ≥ 0.85 | PASS |
| 隔离泄露 | isolation+index_fault 12/12 ok | 0 | PASS |
| verified 错版 | version 6/6 旧 ref 409 stale_reference、new_version=2 | 0 | PASS |
| verified 错 span | span_ok=False 的题进入 faults，faults=[] | 0 | PASS |

rank 分布：23 题命中位次全为 1–2（20 题 rank 1、3 题 rank 2）。

**口径审查**（越界检查，必做核查 6）：
- "honest_zero 计入 macro 命中"是合理设计（零答案题的"正确召回"就是诚实报零），且剔除该口径后 0.9722 仍 ≥ 0.90，不构成偷分。
- span 分母只含 recall 成功且有 marker 的题（23 题），题目设计内 marker 题全覆盖，无挑分母。
- 消融断言非平凡：突变 4b 证明 `ablation_body_clean` 会真实变 False；元数据件正文刻意纯 ASCII（测试文件注释明确说明防中文 1-gram 撞词），body_rank=None 断言在该语料上可证伪。

## 4. 突变检验（必做核查 4）

| # | 突变 | 改法 | 命令 | 结果 | 恢复确认 |
|---|---|---|---|---|---|
| a | BM25 失效 | `kb_lexical.py` `chunk_score` 开头插入 `return 0.0`（其余代码保留为死代码） | `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_rt051_a11_lexical_eval` | **红**：`macro=0.361 body=0.00 short=0.00`，23 题未进 top10（Q01–Q17 检索全灭、Q25–Q30 version recall 失败、Q33 红） | `git restore scripts/kb_lexical.py` → `git status --short` 空 |
| b | 元数据路全命中 | `kb_gateway.py` `haystack()` 恒附加全部查询词（metadata 子串路对所有 needle 命中全部合格件） | 同上 | **红**：`ablation_body_clean=False`（body_only 消融断言精确变红）+ 零命中题 meta=14 全命中 + Q32 跨库泄漏 | `git restore scripts/kb_gateway.py` → `git status --short` 空 |
| c | 还原 P4 缓存 bug | `_v2_load_lexical` 把 corpus_digest 复核挪回缓存命中之后（先查 `_v2_lexical` 缓存，gen 相同即返回） | 同上 | **红**：`Q39: before=200 stale=200`——陈旧代从进程内缓存冒充，未被 503 拒绝 | `git restore scripts/kb_gateway.py` → `git status --short` 空 |

三个突变全部使套件红且红在对应断言上（a→macro/检索、b→ablation_body_clean、c→Q39），证明套件非空洞、对被测代码的真实行为敏感。突变过程中 JSON/源码均已恢复，最终 `git status --short` 干净。

## 5. 全套绿跑（必做核查 5）

`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_rt051_a11_lexical_eval tests.test_rt051_acceptance` → `Ran 23 tests in 23.198s, OK`（A11 矩阵 1 例 + acceptance 22 例）。

## 6. 越界检查（必做核查 6）

- **自证检查**：阈值断言消费的是经真实 ingest→gateway→fusion 全链返回的检索结果与真实 read 回读的 span 验证，不依赖被测代码自报指标；复核人的独立重算（第 3 节）从 dump 原始字段重算，与测试收口独立。
- **消融非平凡**：突变 b 实证 `ablation_body_clean` 可被真实行为变化触发；元数据件纯 ASCII 设计使 body_rank=None 断言不退化为恒真。

## 发现的问题

无不合格项。两条观察（不影响裁决）：

1. `a11-results-latest.json` 的 `header.reviewer/reviewed_at` 仍为 null——按"gold 不自签"约定这是预期状态，本报告即独立签收记录；建议后续把签收事实回填进 JSON header（属锦上添花，非验收阻塞）。
2. 突变 b 第一版（改 `_v2_fusion_payload` 的 `meta_all` 恒真）使套件红在零命中诚实性而非 `ablation_body_clean`；改到 `haystack()` 后才精确命中消融断言。说明零命中诚实断言比消融断言更灵敏——是套件的优点，非缺陷。
