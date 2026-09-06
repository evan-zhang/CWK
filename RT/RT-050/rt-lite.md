# RT-050：定时增量摄取（refresh 子命令 + OPS 夜间挂载）

## 决策

2026-09-06 16:03 Evan 先拍板「夜间自动不做，只保手动增量」；16:53 改判
「值得做就做」。判断依据：spbp-2027 建库当天业务侧就推了 v0.9.43，
快照过时速度被实证；手动增量依赖用户记得开口；夜间管道已在 OPS 运行，
挂钩边际成本低。克制 v1：不碰 `cwk_nightly_pipeline.py`（PR-001 演化
回执管辖的重路径），新增独立 refresh 入口。

## 变更（scripts/kb_ingest.py，归属 R-runtime-rt043-kb-ingest）

- 新子命令 `refresh`：源参数从**库内 `source.json`** 读（单一权威，
  夜间跑不需要人再传一遍）；docdb 源用 root_folder，cwork 源用镜像
  raw 目录（`--cwork-mirror-root` 或 env `CWK_MIRROR_ROOT`）+ 库窗口
  （auto-3m = 当天往前 90 天）
- 增量语义全部复用 execute_plan（unchanged / 升版 / failed 补跑 /
  快照不删），refresh 只加三件事：
  1. **护栏**（`evaluate_refresh_guard`）：计划 0 件而库非空 → 像源
     故障，拒执行；件数 > 上次计划 ×3+50 → 像扫进非源目录，拒执行。
     宁可这一晚不刷新，不把可疑计划写进库
  2. **已知失败不计红**：refresh 前记下 failed 名单，只有**新**失败
     才让 ok=false——夜间不因存量失败（如 spbp 两个源侧空假 zip）天天红
  3. **refresh-state 账**（`_system/refresh-state.json`，历史 30 条）：
    记 last_plan_count（护栏基线）与每次执行摘要；写后走 changed-paths
     + manifest 重签（allow_replaced 含 CHANGED_PATHS_REL，与 execute_plan
     收尾同构）
- 不加 `--yes` = 只读刷新报告（零写入），确认后执行——与 plan/run 的
  确认门同款
- exit code：干跑恒 0；执行后仅「护栏触发或新失败」非 0

## 部署形态（OPS）

- `kb_ingest.py` 文件复制部署（同 RT-049 模式，旧版备份）
- 新 wrapper `~/CWK/ops/run-kb-refresh.sh`（0600）：source env-nightly
  （docdb 钥匙）+ ops/env（NAS 凭据），逐库 refresh --yes，JSON 报告落
  `~/CWK/ops/logs/kb-refresh-<date>.json`；单库失败不阻断他库
- 独立 launchd `com.cwk.kb-refresh`（每日 23:30，夜间管道 22:30 之后）；
  **不动** `com.cwk.nightly-pipeline` 的 wrapper 与 plist
- 手动与夜间同一入口：用户说「更新库」时也走 `refresh`（比手搓
  plan→run 少传一遍源参数）

## 验证

- tests/test_kb_ingest.py 新增 `Refresh*Tests` 16 例：护栏纯函数
  （空扫拒/空库放行/暴涨拒/界内放行/无基线不查暴涨/窗口换算）、
  apply 链（首摄取记账、二次全 unchanged 且业务件零写入、改件升 v2）、
  失败语义（新失败红、已知失败不红仍列名、恢复后无痕）、护栏集成
  （空扫/暴涨跳过执行）、干跑零写入、cwork 源（窗口生效/缺镜像拒）
- 全量回归 test_kb_ingest 164 tests OK；双门禁绿
- OPS 实测（部署后回填）：三库干跑 → spbp 可见增量 → launchd 定时验证
