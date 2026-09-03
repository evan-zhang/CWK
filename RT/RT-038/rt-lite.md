# RT-Lite: RT-038 - 测试两层法与 CI 提速

> profile: Spec-Lite | execution_mode: autonomous

## 方案（给人看）

- 做什么：把测试与门禁拆成两层车道。`make test` / `make ci` 是快车道——
  doctor + py_compile + 单测排除 PR-001 安全族（`test_pr001_*.py`）+ 三条脱敏
  smoke，本地实测约 2.5 分钟；`make test-full` / `make ci-full` 是全量车道
  ——全部单测，本地实测约 75 分钟，CI 每夜 schedule（ci.yml `nightly-full`
  job，北京时间 02:40）跑一遍兜底。退役 `test-lite` / `ci-lite`。
- 为什么：旧 `test-lite` 只排除 `test_pr001_release_gate_validation.py` 一个
  文件，但实测 PR-001 安全族其余 10 个测试文件同样是时长主体——全量里
  非 PR-001 部分只占约 2.4 分钟，PR-001 族约占 73 分钟。「轻量」车道其实
  并不轻，本地验证又慢又红（macOS 环境差异），大家只能等 CI。
- 测试两层法（写进 docs/FEATURES.md K 节）：AI 增强类产物只测**结构**
  （存在、非空、章节齐、引用在），不做内容值断言；确定性管线测**函数级
  输入→输出断言**。依据是 RT-037 的毫秒静默采零案例：search-list 接口拿
  13 位毫秒时间戳不报错、静默返 0 篇，同窗口秒级返 38 篇——「合法的空」
  骗过一切结构检查，只有值断言拦得住静默丢数。
- 这次故意不做什么：不碰 `scripts/` 封闭命名空间（无 receipts 需求）；
  不动 raw/；不新增 workflow 文件（.github/ 在 exact_only_zones，新文件会
  孤儿化）；不改任何测试代码本身；不做并行化/分层 mock——排除法已足够
  达标，先拿最朴素的证据。
- 怎样算成功：`make test` 本地 <5 分钟且全绿（干净 env + 正确 TMPDIR）；
  `make aodw-check` + `make governance-audit` 全绿（含 CC-1..CC-4 与重 pin）；
  CI push 车道 <10 分钟；每夜 schedule 跑全量。

## 车道与命令

| 车道 | 命令 | 单测范围 | 实测（本机） |
| --- | --- | --- | --- |
| 快 | `make test` / `make ci` | 排除 `test_pr001_*.py`（11 文件） | test ≈ 2:30，ci 三连 161s |
| 全量 | `make test-full` / `make ci-full` | 全部 | pytest 全套件 1:15:30（见下） |

- 排除机制沿用旧 test-lite 的 `find ! -name` 写法，直接写在 Makefile recipe
  里，新增/退役文件一目了然；
- 快车道单测面 = 64 文件 1640 tests（unittest 口径），三条 smoke 实测
  1s / 1s / 2s——时长主体就是单测本身，smoke 不是瓶颈。

## 实测数据（2026-09-03，Mac mini M 系，Python 3.14.5）

- 全量套件 pytest 快照：`71 failed, 2671 passed, 7 skipped, 2099 subtests
  passed in 4530.47s (1:15:30)`。71 个失败全部集中在 7 个 VGA 文件，根因
  是 macOS `/tmp → /private/tmp` 符号链接触发 `cwk_instance` 实例根链
  fail-closed（`InstanceRootError`，0.04s 即败）——从 `/private/tmp/...`
  路径进入后同批测试全过（例：`test_vga_revocation_isolation` 9 tests
  4.6s OK）。产品与 CI（ubuntu，无符号链接）不受影响；
- 非 PR-001 批次（unittest，64 文件）：`Ran 1640 tests in 143.652s`；
- 由此推算 PR-001 族 11 文件 ≈ 73 分钟，是全量时长的绝大头；
- 快车道验证：`env -u CWORK_APP_KEY make test TEST_TMPDIR=/private/tmp`
  → RC=0、0 FAIL/ERROR，含 aodw-check + governance-audit 三连共 161s；
- 计时口径注意：全量 pytest 前 13 分钟与一个同型孤儿进程并行（争抢约
  +几分钟）；快车道计时无争抢。

## 本地跑法（macOS 备忘）

- 仓库放 `/tmp` 下时必须从 `/private/tmp/...` 路径进入，否则 VGA 实例根链
  fail-closed；
- 默认 TMPDIR 路径过长会触发 rt032 socket 夹具 `AF_UNIX path too long`
  （macOS 104 字符上限），加 `TEST_TMPDIR=/private/tmp`；
- 网关/日常 shell 若带 `CWORK_APP_KEY`，rt032 凭据净化测试会按设计报警，
  跑测试前 `env -u CWORK_APP_KEY`。这三条都是环境差异，不是产品缺陷
  （CI 在 ubuntu 上全绿）。

## 治理动作

- Makefile 与 `.github/workflows/ci.yml` 走 RT-030 `cwk-governance-repin-v1`：
  重 pin（Makefile `932ca9f6… → ea0b94c1…`，ci.yml `e43e6f38… → 3e3235fa…`），
  理由即本 RT；CC-2 断言的 `ci` recipe 含 `governance-audit` 保持不破，
  `ci-full` 同样含；
- ci.yml 只改现有文件：push/PR 走快车道（`smoke` job id 不变，分支保护
  认它），新增 `schedule` 触发 + `nightly-full` job 跑 `make ci-full`，
  checkout 保持 `fetch-depth: 0`（PR-001 历史证据需要）；
- docs/FEATURES.md：K 节重写为两层车道 + 测试两层法，三处 ci-lite 引用
  同步更新；
- RT 花名册：RT/index.yaml 加 RT-038 条目，aodw-check 花名册一致性 36/36。

## 遗留与后续

- 每夜 `nightly-full` 的 Linux 实测时长以首次 schedule 运行为准（本地
  75 分钟不能直接外推）；若超 timeout 150 分钟再调；
- 快车道在 GitHub Actions 上的实测时长以本次 push 的 CI 为准（目标
  <10 分钟）；
- macOS 本地三个环境坑已记入 Makefile 注释与本文件，若将来要在本地常跑
  全量，可考虑把仓库挪出 /tmp 或给 cwk_instance 加显式 realpath 解析。
