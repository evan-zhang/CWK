# RT-010 Maintenance Hotfix — 实现与验证记录

- 分支: `agent/cwk-rt010-maintenance-impl-opus`
- 基线 commit: `eba42cc` (`RT-011 remediation: strict Draft-2020-12 engine …`)
- 目标: 修复真实语料镜像下 RT-010 出现的两项回归，不新增 TBS 特判、不改动 PR-001/RT-011 契约与 nightly/cron/生产镜像。

## 1. 命中的回归

在同一份真实 mirror（`knowledge/工作协同镜像`, 8315 summaries）下，`eba42cc` / `8720906` / `6636a33` / `8d547e7` 均出现下列失败，属真实语料漂移暴露的回归而非 RT-011 引入：

| 测试 | 现象 |
| --- | --- |
| `tests.test_wiki_entity_scope.RealCorpusRegressionTests.test_query_variants_resolve_to_the_same_family` | `TBS 训战系统 风险` 被判为 `unsupported_multi_entity`：`tbs`（TBS family, hard）与独立 `训战系统` family (`cd89aba47581969d`, hard) 落在不同 span，触发 fail closed。|
| `tests.test_wiki_entity_scope.RealCorpusRegressionTests.test_dynamic_sampling_scope_precision_via_query_mirror` | 采样从 family 所有 surfaces 中抽取，包含 `scope_role=generic_candidate` 的 `训战系统`，其 `surface_index` 同时映射到 TBS 与独立 `训战系统` 两个 family，导致模板 `训战系统 项目进度` 解析成 cd89 而不是 TBS。|

## 2. 泛化修复（无 TBS 特判）

### 2.1 `scripts/cwk_wiki_query.py::_find_entity_matches` 新增 contextual-compound 归并

在 `suppress_shorter_overlaps` 之后、进入 `results` 之前加入一段严格窄口径的归并逻辑：

- 按 `(n_start, n_end)` 稳定排序后仅考察相邻两个命中 `(h1, h2)`，且要求 `h1.n_end <= h2.n_start`；
- `normalised_question[h1.n_end : h2.n_start]` 必须仅由 ASCII 空白构成（`isspace()`），因此 `和`、`与`、`、`（U+3001）等连接词天然会拒绝归并；
- 枚举全部 family，若唯一存在一个 family `F` 同时满足：
  - `h1.norm` 在 `F` 中 `scope_role == "hard"`；
  - `h2.norm` 在 `F` 中 `scope_role == "generic_candidate"`；
  - `h1.norm + h2.norm` 在 `F` 中 `scope_role == "hard"`（即预先声明的 hard compound surface），
  则合并成一个覆盖 `[h1.n_start, h2.n_end]` 的匹配，命中 family `F`，display 取 `F` 中该 compound surface 的 `display`；
- 否则保持两条命中不动，走原有 multi-entity fail-closed 路径。

归并后的匹配以完整跨度进入 `results`，`_remove_entity_tokens` 因此会一次性剥离整个 compound span（包含中间空白），BM25 残余中不会残留 `描述系统` 之类的 scope 描述词。

设计原则遵守 spec：不改 catalog 构建策略、不扩大 F postings、不绕过 hard/generic 门禁、不加任何 TBS 字面量，仅按运行时读取到的 `scope_role` / `surface_index` 做判定，可直接迁移到未来任何具有同形态歧义的 acronym。

### 2.2 `tests/test_wiki_entity_scope.py::test_dynamic_sampling_scope_precision_via_query_mirror` 采样池收紧

原实现在整个 family 的 surfaces 中随机抽样，暴露到 generic_candidate + 跨 family norm。改为：

- 从 `payload["surface_index"]` 读取每个 normalized 属于哪些 family；
- 只保留 `scope_role == "hard"` 且 `surface_index[normalized] == [family_id]`（唯一属于目标 family）的 surface；
- 用 `sorted(..., key=lambda item: (display, normalized))` 做稳定排序，再执行 `random.seed(2026)` + `random.choice`；
- 断言原有的 scope precision（每条结果 report_id 必须落在 family postings 里）保持不变，不放松、不删除。

## 3. 新增合成测试（全部非 TBS）

新增 `tests.test_wiki_entity_scope.ContextualCompoundMergeTests`，用 `ALPHA` + `描述系统` 双 family 场景覆盖归并契约：

- `test_alpha_and_generic_full_form_merges_to_alpha_family` — 存在 hard compound `alpha描述系统` 时 `ALPHA 描述系统 风险` 合并到 ALPHA family，matched span 精确覆盖 `ALPHA 描述系统` 原始子串；
- `test_bare_generic_full_form_still_resolves_independent_family` — `描述系统` 单独出现仍解析到独立 `描述系统` family；
- `test_connectives_refuse_merge_and_stay_unsupported` — `ALPHA 与 描述系统` / `ALPHA 和 描述系统` / `ALPHA、描述系统` 均保持 `unsupported_multi_entity`，连接词不参与归并；
- `test_missing_hard_compound_refuses_merge` — 无 hard compound 时不归并；
- `test_multiple_possible_primary_families_refuse_merge` — 至少两个 family 同时满足 hard/generic/compound 三条件时唯一性判定失败，`resolve_entity` 返回 `ambiguous` 或 `unsupported_multi_entity`；
- `test_merged_span_strips_description_tokens_from_bm25` — 归并后 `_remove_entity_tokens` 移除整个 compound span，BM25 残余不再包含 `描述系统` / `ALPHA`，且结果不会泄漏到独立 `描述系统` family 的 postings。

同时恢复的两项真实语料测试均通过。

## 4. 验证结果

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 原两项失败测试 | `CWK_TEST_MIRROR_ROOT=… python3.11 -m unittest tests.test_wiki_entity_scope.RealCorpusRegressionTests.test_query_variants_resolve_to_the_same_family tests.test_wiki_entity_scope.RealCorpusRegressionTests.test_dynamic_sampling_scope_precision_via_query_mirror` | OK (2/2, 12–40 s) |
| 新增合成测试 | `python3.11 -m unittest tests.test_wiki_entity_scope.ContextualCompoundMergeTests` | OK (6/6, 0.024 s) |
| 全量 `test_wiki_entity_scope.py` | `CWK_TEST_MIRROR_ROOT=… python3.11 -m unittest tests.test_wiki_entity_scope` | OK (133/133, 37 s) |
| `make test` | `PYTHON=python3.11 make test` | PASS（doctor / py_compile / discover / smoke / smoke-ai / smoke-ai-degraded 全通过） |
| `make wiki-lint` | `python3.11 scripts/cwk_wiki_query.py --lint --mirror-root …/工作协同镜像` | PASS（summaries=8315, invalid=0） |
| `make wiki-smoke` | `python3.11 scripts/cwk_wiki_smoke_test.py --mirror-root …/工作协同镜像` | PASS（14/14） |
| `compileall` | `python3.11 -m compileall -q scripts tests` | 无告警 |
| `git diff --check` | `git diff --check` | 无空白冲突 |

## 5. 风险与回滚

- 归并逻辑严格要求 `hard + generic_candidate + hard compound` 三段同时命中且唯一 family，理论上不会误合并任何未预先声明 compound 的 surface；连接词经 `isspace()` 天然排除。
- 不改动 catalog 构建、`entity-family-registry.json`、PR-001 contracts、RT-011 引擎、nightly、cron、Cloud/DocDB；文件改动限于 `scripts/cwk_wiki_query.py` 与 `tests/test_wiki_entity_scope.py`，本报告新增于 `RT/RT-010/reports/`。
- 若合并逻辑将来出现意外副作用，可通过 revert 相应 diff（`_find_entity_matches` 内 `kept_sorted / _find_merge_family / merged` 段落 + 测试改动）快速回滚，其它模块无耦合。
- 测试改动仅为 read-only 消费真实 mirror 与合成 fixture，未写入镜像；`.serena` / `.spec-workflow` 未触碰。

## 6. 交接

- 需另一个新 Agent 独立复核，本 Agent 不自我宣称 PASS。
- 复核清单建议：
  1. `CWK_TEST_MIRROR_ROOT=…/工作协同镜像 python3.11 -m unittest tests.test_wiki_entity_scope`；
  2. `python3.11 -m unittest tests.test_wiki_entity_scope.ContextualCompoundMergeTests -v`；
  3. `PYTHON=python3.11 make test`；
  4. `python3.11 scripts/cwk_wiki_query.py --lint --mirror-root …/工作协同镜像`；
  5. `python3.11 scripts/cwk_wiki_smoke_test.py --mirror-root …/工作协同镜像`；
  6. `python3.11 -m compileall -q scripts tests`；
  7. `git diff --check`；
  8. `git diff eba42cc -- scripts/cwk_wiki_query.py tests/test_wiki_entity_scope.py`：确认改动限于 `_find_entity_matches` 归并段与新测试。
