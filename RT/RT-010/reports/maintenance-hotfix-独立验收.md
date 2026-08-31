# RT-010 Maintenance Hotfix — 独立验收报告

- Verify agent: `agent/cwk-rt010-maintenance-verify-opus`（未参与实现，冷启动）
- 被验收对象：`agent/cwk-rt010-maintenance-impl-opus` @ `4997932`
- 基线父提交：`eba42cc`（RT-011 remediation）
- 验证工作树：`/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK/.worktrees/openclaw-worktree-cwk-rt010-maintenance-verify-opus`
- 真实镜像：`/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK/knowledge/工作协同镜像`（8315 summaries）
- 运行环境：`python3.11` (3.11.14)
- 验收时刻：2026-08-19

> 本报告只“新增”一个文件（本文件）到验证分支；本 Agent 未修改任何实现、测试、
> registry、catalog、PR-001 或其它生产文件。为运行验证，验证分支通过 `git merge
> --ff-only 4997932` 将实现分支 fast-forward 到当前 HEAD，工作树因此与被验收对
> 象 4997932 完全一致，随后仅新增本报告。

---

## 1. 变更范围硬确认

`git diff eba42cc..4997932 --name-only` 输出（仅 3 个文件）：

```
RT/RT-010/reports/maintenance-hotfix-记录.md
scripts/cwk_wiki_query.py
tests/test_wiki_entity_scope.py
```

以下路径的 diff 均为**空**（`git diff eba42cc..4997932 -- <路径>` 无任何输出）：

- `config/`（含 `entity-family-registry.json`）
- `scripts/cwk_entity_catalog.py`
- `scripts/cwk_nightly_pipeline.py`
- `scripts/cwk_pr001_*.py`
- `scripts/cwk_docdb_cloud.py`、`scripts/cwk_cloud_*.py`
- `RT/RT-011/`、`PR/`

**结论：改动严格限于 `cwk_wiki_query.py::_find_entity_matches` 补充“contextual-compound
合并阶段”、`tests/test_wiki_entity_scope.py`（新增合成测试 + 收紧真实语料采样池）、
与实现方交付记录。目录范围符合约束。**

`scripts/cwk_wiki_query.py` 中 `TBS/tbs/训战` 出现 14 处全部落在注释/docstring 里
（`grep -n` 校验：行 625/676–685/943/1207/1698 全部以 `#` 或位于三引号内），不存
在按实体字面量分支的代码。

---

## 2. 契约再审：contextual-compound 合并规则

`scripts/cwk_wiki_query.py` 行 673–759 新增段落。合并条件（按代码原文，非注释）：

1. `kept_sorted = sorted(kept, key=lambda t: (t[0], t[1]))` — 稳定按 normalised span 排序。
2. `while idx < len(kept_sorted)`：只查相邻两条命中 `(h1, h2)`。
3. `n1e <= n2s`：严格无 overlap（overlap 已由 `suppress_shorter_overlaps` 处理）。
4. `gap = normalised_question[n1e:n2s]; gap and gap.isspace()` — 中间必须**非空且全部为
   Python `str.isspace()` 意义下的空白字符**。ASCII space、连续多个 space、以及 NFKC 折
   叠后成 ASCII space 的全角空格 U+3000 均通过；`,`/`.`/`、`/`-`/`;`/`(`/`)`/`/`/`;`/
   `和`/`与`/顿号 U+3001 等一律阻断。
5. 遍历所有 family，找唯一同时满足下述三条的 family `F`：
   - `h1.norm` 在 `F` 的 `surfaces` 里 `scope_role == "hard"`；
   - `h2.norm` 在 `F` 的 `surfaces` 里 `scope_role == "generic_candidate"`；
   - `h1.norm + h2.norm` 在 `F` 的 `surfaces` 里 `scope_role == "hard"`（即预先声明的
     hard compound）。
6. 满足家庭数 `!= 1` 一律拒绝（uniqueness）。
7. 命中时合并成 `(n1s, n2e, (F_id, h1.norm+h2.norm, display))`；否则保留原命中。

关键点：
- `h1` 与 `h2` 都来自 `_find_entity_matches` 中通过了 `scope_role == "hard"` 过滤（行 648）
  的 `raw_hits`，因此 h1 天然是 hard；h2 是自身族的 hard，但代码要求它在**目标 family**
  内是 `generic_candidate`。这正是 “h1 hard / h2 generic_candidate / concat hard”的三段
  同族条件。
- 判定完全从 catalog 的运行时 `scope_role` 与 `surfaces` 集合读取，无任何 TBS/训战/其它
  实体字面量分支。任何未来遵循相同注册模式（acronym + 括号内 full form + 显式声明
  compound 成员）的实体都会自动获得同样的行为。
- 合并跨度 `(n1s, n2e)` 映射回原始字符偏移后进入 `results`；`_remove_entity_tokens` 因此
  会剥离整个 compound 段（含中间空白），BM25 残余不含 full form 描述词。

---

## 3. 独立攻击矩阵（合成、无 TBS/训战 字面量）

本 Agent 使用独立合成实体 `GAMMA` + `评审平台`（真实语料中无此实体）构造 fixture，
直接调用 `scripts/cwk_wiki_query.py::_find_entity_matches` 与 `resolve_entity`。脚本仅
写入临时目录，不修改被验收代码或生产 mirror；脚本文件不提交（`/tmp/rt010_attack.py`
与 stdin 一次性 Python 片段）。

| # | 场景 | 期望 | 实际 |
|---|------|------|------|
| P1 | `GAMMA 评审平台 风险` | resolved → GAMMA family、单个 matched_surface | ✅ resolved fid=b8c7… n=1 |
| P2 | 合并后 span 严格 = `GAMMA 评审平台` | 精确覆盖原字符 | ✅ span='GAMMA 评审平台' |
| P3 | `gamma 评审平台 进展` / `ＧＡＭＭＡ 评审平台 进展` / `G A M M A 评审平台 进展` | resolved → GAMMA family | ✅ 三例全通过 |
| P4 | `评审平台 进展` 独立查询 | resolved → 独立评审平台 family（NOT GAMMA） | ✅ fid=a3b5…（独立 family） |
| N1 | `GAMMA 与 评审平台` / `GAMMA 和 评审平台` / `GAMMA、评审平台` | 拒绝合并 → unsupported_multi_entity | ✅ 三例均 unsupported |
| N2 | `GAMMA, 评审平台` / `GAMMA/ 评审平台` / `GAMMA - 评审平台` / `GAMMA (评审平台)` / `GAMMA (评审平台) 风险` / `GAMMA ; 评审平台` / `GAMMA - 评审平台 风险` | 非空白间隔，两 hit 明显存在，必须拒绝合并 | ✅ 7 例全 unsupported_multi_entity（nhits=2） |
| N3 | `评审平台 GAMMA 风险` — 反向顺序 | concat=`评审平台gamma` 未注册，必须拒绝 | ✅ unsupported_multi_entity |
| N4 | `GAMMA · 评审平台` — CJK 中点非空白 | 拒绝合并 | ✅ unsupported_multi_entity |
| P5 | `GAMMA  评审平台 进展` — 双 ASCII 空格 | `str.isspace()` 通过，合并 | ✅ resolved → GAMMA family |
| P6 | `GAMMA　评审平台 进展` — 全角 U+3000（NFKC→ASCII 空格） | 合并 | ✅ resolved → GAMMA family |
| P7 | `GAMMA评审平台 风险` — 无空格直接命中 hard compound | 直接一 hit 单 family resolved（不走 merge 分支） | ✅ resolved → GAMMA family |
| N5 | 缺 hard compound（family 只注册 `alpha` 无 `alpha评审平台`） | 拒绝合并 → unsupported_multi_entity | ✅ unsupported_multi_entity |
| N6 | compound 只是 generic_candidate（无 hard 版本） | 拒绝合并 | ✅ unsupported_multi_entity；setup 侧确认 compound_role='generic_candidate' |
| P8 | 两 family 都满足三段条件但 h1 只被一 family 拥有（gamma 只在 A、delta 只在 B） | uniqueness 通过：h1='gamma' 时唯一 → 合并到 A | ✅ resolved → GAMMA family |
| N7 | 两 family 都同时满足 `(gamma hard, 评审平台 gc, gamma评审平台 hard)` — 通过 entity_type 差异保持不合并 | uniqueness 失败 → 拒绝合并 → ambiguous/unsupported | ✅ ambiguous；setup 侧 qualifying_families=2 |
| N8 | `ALPHA BETA 进展` 独立多实体 | fail closed → unsupported_multi_entity | ✅ unsupported_multi_entity |
| P9 | `GAMMA评审平台 GAMMA 风险` — 同 family 两 hit（h1=compound、h2=acronym） | 不触发跨 family 合并；两 hit 同 family → resolved | ✅ resolved → GAMMA family |
| Det1 | 采样池 stable-sort（重复两次构造 fixture 生成 pool JSON） | 字节相等且非空 | ✅ 一致 `[["GAMMA","gamma"],["GAMMA评审平台","gamma评审平台"]]` |
| Hyg1 | 生产脚本无 `TBS/tbs/训战` 代码分支 | 仅出现在注释/docstring | ✅ 14 处全部为注释 |

**结论：18/18 攻击用例全部通过。合并规则严格数据驱动、fail-closed 满足预期。**

> 说明：一个额外探测 `GAMMA. 评审平台` 返回了 `resolved→评审平台 family`。追踪原因
> 是 `entity_catalog._ASCII_WORD_RE = [A-Za-z0-9_\-.]` 把 `.` 归为 ASCII word 字符，
> 因而 `gamma.` 违反了 ASCII 边界，`gamma` 根本未产生命中；这条路径未进入合并逻辑，
> 属预先存在的 acronym 边界策略（`TBS.ADMIN` 与 `TBSADMIN` 区分设计），不由 4997932
> 引入，也不构成合并规则的漏洞。

---

## 4. 强制命令与结果

所有命令在验证工作树 `openclaw-worktree-cwk-rt010-maintenance-verify-opus` 内、HEAD=4997932
下执行；`CWK_TEST_MIRROR_ROOT` 指向真实镜像 8315 summaries。

| # | 命令 | 结果 |
|---|------|------|
| 1 | `python3.11 -m unittest -v tests.test_wiki_entity_scope.RealCorpusRegressionTests.test_query_variants_resolve_to_the_same_family tests.test_wiki_entity_scope.RealCorpusRegressionTests.test_dynamic_sampling_scope_precision_via_query_mirror` | **OK 2/2**（35.4 s） |
| 2 | 对照：同两测试在基线 `eba42cc` 的临时 worktree 下运行 | **FAIL 2/2**（`unsupported_multi_entity` 与 `cd89aba… != 9703e45…`）→ 确认为真实回归，非误报 |
| 3 | `python3.11 -m unittest -v tests.test_wiki_entity_scope.ContextualCompoundMergeTests` | **OK 6/6**（0.029 s） |
| 4 | `CWK_TEST_MIRROR_ROOT=… python3.11 -m unittest tests.test_wiki_entity_scope`（全量 133） | **OK 133/133**（52.4 s） |
| 5 | `PYTHON=python3.11 make test`（含 doctor / py_compile / discover / smoke / smoke-ai / smoke-ai-degraded） | **OK；unittest discover 报 Ran 399 tests OK；三段 smoke 全部生成 artifact；grep 校验 degraded 标志通过；总耗时 ≈ 46 s** |
| 6 | `python3.11 scripts/cwk_wiki_query.py --lint --mirror-root …/工作协同镜像` | **PASS**：summaries=8315；duplicate_ids=0；missing_raw=0；invalid_evidence_quotes=0；dangling_navigation_refs=0（29.8 s） |
| 7 | `python3.11 scripts/cwk_wiki_smoke_test.py --mirror-root …/工作协同镜像` | **PASS 14/14** |
| 8 | `python3.11 -m compileall -q scripts tests` | **无告警** |
| 9 | `git diff --check` | **无空白/冲突警告** |
| 10 | `python3.11 -m unittest tests.test_rt010_registry_binding tests.test_pr001_contracts tests.test_pr001_probes`（相邻套件回归） | **OK 75/75** |
| 11 | 独立攻击矩阵 `/tmp/rt010_attack.py` 与两段独立 `python3.11 -c` 探测 | **18/18 全通过**（详见第 3 节表格） |

---

## 5. 关键契约再校验

- **数据驱动、非实体特判**：`_find_merge_family` 仅读取 `families_by_id[*]["surfaces"][*]["scope_role"]` 与 `normalized`；无 `if norm == "tbs" / "训战系统"` 型分支。见 `scripts/cwk_wiki_query.py` 行 707–730。生产脚本内所有 `TBS/训战` 字符串全部处于注释/文档字符串。
- **相邻纯空白**：`gap and gap.isspace()`。空 gap（`n1e == n2s`）也被排除（`gap` 为 `""` → 布尔值 False）。因此“无缝相邻”不会进入合并分支（此时 h1+h2 拼接的字面命中会直接被 AC 匹配为一条 compound）。
- **fail closed**：连接词（顿号/和/与）、任意标点、反向顺序、缺 compound、compound 非 hard、多 family 歧义、两段 overlap 已经被 `suppress_shorter_overlaps` 抹掉、非 generic_candidate（h2 若在目标 family 是 hard，合并条件失败）、同 family 不唯一、独立多实体——**上述九类攻击矩阵全部落入 `unsupported_multi_entity`/`ambiguous`**（第 3 节 N1–N8 覆盖）。
- **动态采样确定性**：`pool = sorted(( (display, normalized) for s in family.surfaces if hard and surface_index[norm]==[family_id] ), key=lambda item:(item[0], item[1]))`。只取 `scope_role=="hard"` 且 `surface_index[norm] == [family_id]`（即唯一归属目标 family）；随后 `random.seed(2026)` + `random.choice`。经两次独立构造 fixture 生成 pool JSON 字节一致（第 3 节 Det1）。未对当前真实 corpus 写死任何 surface 字面量。
- **合成正例覆盖**：`ContextualCompoundMergeTests` 与本 Agent 独立合成的 GAMMA/评审平台 fixture 均**从未出现 TBS/训战 字面量**。第 3 节 P1 正例（GAMMA + 评审平台 → GAMMA family）为完全独立的合成正例；N1–N8 覆盖了 spec 中列出的全部 fail-closed 负例。

---

## 6. 发现（Blocker / Major / Minor / Nit）

- **Blocker**：无。
- **Major**：无。
- **Minor**：无阻断性；但记录两项观察供后续维护者参考——
  - M-1：ASCII acronym 边界策略（`_ASCII_WORD_RE` 含 `.`）使 `"GAMMA. 评审平台"` 在 h1 阶段就丢弃 `gamma` 命中，因此 fallback 到 `评审平台` family。这**不是**本次改动引入，也不影响合并规则的 fail-closed 属性，但如果未来运营出现 “TBS. 训战系统” 这类特殊标点使用，操作者需要注意结果族别可能是独立 family 而非 acronym 主家。属既有行为，不列 Major。
  - M-2：`_find_merge_family` 使用 O(F·|surfaces|) 线性扫描全部 family 判断 uniqueness。在当前 mirror（约几百个 family、每 family 数十 surfaces）耗时可忽略。若未来 catalog 规模 10× 增长可考虑用 `surface_index` 做常量级预筛。属性能建议，不影响正确性。
- **Nit**：无。

---

## 7. Verdict

| 项目 | 结论 |
|------|------|
| 两项真实语料回归（4997932） | **PASS**（且已在 eba42cc 处复现失败以确认属真实回归修复） |
| 合并逻辑数据驱动、无实体字面量 | **PASS** |
| Fail-closed 覆盖 spec 全部九类负例 | **PASS**（18/18 攻击用例通过） |
| 动态采样 hard+唯一归属+稳定排序 | **PASS**（deterministic pool 校验通过） |
| 独立合成正/负例（无 TBS 字面量） | **PASS** |
| `python3.11 -m unittest tests.test_wiki_entity_scope` 全量 | **OK 133/133** |
| `PYTHON=python3.11 make test`（含 doctor/py_compile/discover/smoke×3） | **OK 399 tests; smoke 3/3** |
| `wiki-lint` 真实 mirror | **PASS**（8315 summaries; invalid=0） |
| `wiki-smoke` 真实 mirror | **PASS 14/14** |
| `compileall` | **clean** |
| `git diff --check` | **clean** |
| 改动范围仅限 `cwk_wiki_query.py`+测试+维护记录 | **PASS** |
| PR-001 / RT-011 / registry / catalog / nightly / cron / Cloud / DocDB | **未触碰** |

**总体 Verdict：PASS。**

**是否建议合并 4997932：建议合并。**

理由：hotfix 在真实语料回归、独立合成攻击矩阵、全量测试套件、真实镜像 lint/smoke、
编译一致性与目录范围硬约束五个维度全部通过，且合并规则严格数据驱动，未出现任何 TBS
特判；未接触 PR-001 v1 契约、RT-011 引擎、registry 配置、catalog 构建、nightly/cron
或 Cloud/DocDB 生产文件。第 6 节列出的两条 Minor 均属既有行为或性能建议，不影响正确
性与安全属性，不构成合并阻断。

复现命令即第 4 节命令列，环境需 `CWK_TEST_MIRROR_ROOT` 指向真实 8315-summary
mirror；本 Agent 未修改任何被验收文件，仅新增本报告。
