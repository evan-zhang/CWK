---
name: "cwk-kb-create"
description: "建库向导：「建知识库」「建库」触发；对话收集库名/源/窗口/路由，AI提议用户拍板，CWK引擎建库+摄取，doctor回执"
---

# cwk-kb-create — AI 知识库建库向导

把用户的「帮我建一个 AI 知识库」变成 NAS 上的一个真库。引擎 = CWK 仓库的 `kb_wizard.py` + `kb_ingest.py`（已通过 RT-042/043/044 验收）。你是对话皮：信息你收集、建议你出、**拍板归用户**、执行归引擎。

## 硬约束
- AI 提议、用户拍板：库名/前缀/窗口/路由每一项都给建议值并等用户确认，不许静默取默认直接跑
- 凭据只走 env：先 `set -a; source ~/.openclaw/gateways/life/.env; set +a`（CWK_NAS_KB_*）；不回显、不进转录
- 目的地不干净必拒（引擎自带保护）；raw 只增不改；CLI 输出全 JSON，逐条给用户念关键计数
- 摄取前必须先出 plan 让用户看件数与 expected_status_counts，确认后才 run

## 已知锚点
- CWK 仓库（所有命令在此跑）：`/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK`
- cwork 镜像 raw：`/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK-20260708-001/knowledge/工作协同镜像/raw`
- NAS：`/ai-knowledge/` share，prefix 即其下目录名

## 第一步 收集（每项给建议值）
1. **库名与 prefix**：建议库名口语化（「投前流程系统建设」），prefix 从库名派生小写短横线（touqian）；两者都让用户确认
2. **数据源**（三选一）：
   - cwork 工作协同镜像 → root=镜像 raw 目录（锚点见上），route 建议 `timeline`
   - DocDB 知识库 → root=空间或子目录的 fileId（数字），route 建议 `classify`，建库时带 `--docdb-root <fileId>`
   - 本地目录 → root=绝对路径（local 后端）
3. **时间窗口**：cwork 建议 `--since`（近 3 个月=当天往前 90 天）；docdb 一般全量不传
4. **类型/可见性**：默认 personal / private

## 第二步 建库（用户确认后）
```bash
cd <CWK仓库> && set -a; source ~/.openclaw/gateways/life/.env; set +a
python3 scripts/kb_wizard.py create --kb-root nas://<prefix> --backend nas --prefix <prefix> \
  --name "<库名>" --source <cwork-mirror|docdb> --route-mode <timeline|classify> [--docdb-root <fileId>] --yes
```
完成判据：JSON `ok=true`；把 kb_code 前 16 位给用户。

## 第三步 计划 → 拍板 → 摄取
```bash
python3 scripts/kb_ingest.py plan --source <cwork-mirror|docdb> --root <root> \
  --kb-root nas://<prefix> [--since YYYY-MM-DD] --backend nas --prefix <prefix> --out /tmp/<prefix>-plan.json
```
把 `item_count`、`expected_status_counts`、`unidentified` 条数念给用户；有 unidentified 要逐条说明（无稳定 ID 的文件不摄取但也不许消失）。用户点头后：
```bash
python3 scripts/kb_ingest.py run --plan /tmp/<prefix>-plan.json --backend nas --prefix <prefix> --yes
```
大库（>300 件）放后台跑并记日志，期间用 status 查进度（`kb_ingest.py status --kb-root nas://<prefix> --backend nas --prefix <prefix>`）。

## 第四步 体检与回执
```bash
python3 scripts/kb_ingest.py status    --kb-root nas://<prefix> --backend nas --prefix <prefix>
python3 scripts/kb_ingest.py reconcile --kb-root nas://<prefix> --backend nas --prefix <prefix>
python3 scripts/kb_doctor.py verify --all --backend nas --prefix <prefix> --json
```
全绿判据：counts.failed=0、reconcile 各清单空、doctor 五项（raw/manifest/collection-state/changed-paths/tree）ok=true。有差异必须解释到件——参考 2026-09-05 Case 1 实例：基线 451 → 入库 453 = 月份目录 447 + unknown/ 目录 6 + 重复 ID 归并 − 3 件无日期 fail-closed 排除，差异全解释即零缺件。
最后告诉用户：库已就绪，用 cwk-kb-query Skill 提问。

## 故障速查
- FileStation code=400 且发生在建目录：DSM 拒「数字开头」目录名——引擎已自动加 `d-`/`c-` 前缀（commit 175d532 / 885d2fd）；复现说明有新形态，查 `kb_ingest.py` 的 `_device_safe_dir`
- 502：DSM 对不存在文件回裸 502，引擎已消歧为 NotFound；持续 502=服务忙，等 30s 重试
- plan 件数异常大：检查窗口（since）设置与是否扫进非源目录（`_system` 已排除）
- 400 一次定位法：backend 构造后包**实例级** `_transport`（类级 monkeypatch 无效——`__init__` 绑定了实例属性），包装里解析 CreateFolder/Upload 的 folder_path、name 并给 `success:false` 响应打标；单件重跑 `execute_plan`（items 只留一件）即可看到肇事调用
- plan 件数 ≠ 基线的对账法：取镜像文件名前 15+ 位数字 ID 与计划 stable_id 做集合差，差件按月份目录分桶——窗口前月份占大头=since 正常；其余三类逐一核：unknown/ 目录件（日期在窗口内应入）、同 ID 多文件（归并一条 lineage）、无 manifest 无日期件（fail-closed 排除，列出 ID）。差异全解释即零缺件
