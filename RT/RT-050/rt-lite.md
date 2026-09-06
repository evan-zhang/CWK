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

## 部署与实测回填（2026-09-06）

- 提交 `80b8b85` 推送，CI smoke 绿（run 34027288178）；OPS 文件复制部署
  （旧版备份 `kb_ingest.py.bak-20260906-pre-refresh`，指纹与 HEAD 逐字一致）
- wrapper `~/CWK/ops/run-kb-refresh.sh`（700，双 env：ops/env + env-nightly
  ——单 env 不够，NAS 凭据与 DocDB 钥匙分居两个文件；`CWK_MIRROR_ROOT`
  指向镜像 raw）+ launchd `com.cwk.kb-refresh` 每日 23:30（不动 22:30 主管道）；
  逐库失败隔离，报告落 `~/CWK/ops/logs/kb-refresh-<date>.json`
- **真实增量实测**（手动触发，与定时同一入口）：
  - spbp-2027：109 基线 → converted 15（当天研讨会新资料：12 md +
    2 xlsx + 1 pptx，非 md 因 OPS 无 md2md 落 placeholder 3）/ unchanged 104 /
    failed 2（已知源侧空件，豁免不计红）→ ok=true
  - cwork-3m：453 基线 → converted 30（新周报）/ unchanged 384 → ok=true
  - docdb-touqian：扫出 0 件，**护栏拦截**（empty）——源侧 root
    folder 2082734860367093762 直查返回空数组（库内 133 件快照不受
    影响，可正常查询）
    - **更正（2026-09-06 19:28 考古）**：当时疑「目录被清空/移走」，
      随后逐件 fileId 直连 + get-level1-folders 复核推翻——实为**源侧
      按部门重组**：08-12 建了两个新顶层目录（投前系统_项目管理部
      2087521342634180609 / 投前系统_玄关开发 2087521796046831618），
      09-05 摄取后老结构被整体搬入、项目根清空。两新目录递归 133 件
      与库内 133/133 逐 fileId 完全对账，零缺失零新增。教训：建库
      root 应指稳定子目录而非项目根。修复走 Evan 拍板的 A 方案
      （source.json 双源改指两新目录），见下方补丁二
- 增量后 doctor 两库五项全绿（raw/manifest/collection-state/changed-
  paths/tree）；网关无重启可见新件（全天研讨会 matched=2、主持人手册
  matched=1、周报 matched=22）
- 注：本次实测中 18:25 网关重启杀掉的是本地轮询连接，远端 refresh
  进程照常跑完（结果文件完整落盘）——逐件追加+账本级联的中断安全
  语义得到一次意外实证

## 补丁：源侧消失报告（vanished，2026-09-06 19:48 Evan 批）

- 动机：touqian 重组事件暴露的盲区——暴涨有护栏、缩水无人知晓（133 件
  被删 20 件 → 下次计划 113 件全 unchanged，静默）
- `refresh_library` 增 vanished 清单：库里已知、本次**全量**扫描没看见
  的件，按源 label 聚合上报（count + 前 50 件 + truncated 标记）
- 三条防误报边界：带 since 窗口的计划不判定（窗外件看不见 ≠ 消失）；
  多源聚合到 label 级再判（同库多根不互报）；护栏命中的轮次整轮跳过
  （清单只会是噪声，护栏自己会红）
- 语义：**只报告、不删库、不计红**——快照语义下源侧删除是业务常态，
  库保留原件正是审计价值；但「悄悄少了 N 件」必须可见
- 测试：RefreshVanishedTests 4 例（部分删除上报且库内保留 / 窗口源
  不判 / 截断标记 / 护栏轮次零清单）；全量回归 168 OK；双门禁绿
