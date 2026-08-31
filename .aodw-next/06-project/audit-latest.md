# AODW Latest Audit

- generated_at: 2026-08-31 00:00:00 +0800
- scope: AODW v0.6.1 framework adoption
- source: `bd-eval-loop` commit `08b0523c9457da9f59235e48e22227f369502ac6`
- result: pass
- production_change: none

## 接入范围

- 已复制完整 `.aodw-next/` 通用框架及自带 fixture；
- 已将 `06-project/`、`project.yaml`、`tools-status.yaml` 改写为 CWK 项目事实；
- 已建立根目录 AODW loader（`AGENTS.md`、`CLAUDE.md`）；
- 已保留既有 CWK RT、PR-001、`.spec-workflow/`、精编和运行产物，不做历史重写。

## 验证摘要

- AODW framework fixture：79/79 通过；
- 源/目标 `.aodw-next/` 文件数：101/101；仅 7 个项目专属文件与源不同，另有 3 处
  不改变行为的尾随空白规范化；
- manifest、项目模块索引和声明的 skill 路径均可解析且存在；
- `handover-pack` 的源随 `.aodw-next/skills/` 入库；宿主入口（`.agent/skills/`）
  是**本机状态**、已在 `.gitignore` 里，安装与否由 `make aodw-check` 第 4 项当场
  测量并如实报出，不再靠这份报告里的一句断言；
- `rt-guard.sh --root . --list-gates` 已加载 14 条判据；
- `make doctor` 通过；未执行真实采集、DocDB 写入、真实模型调用或生产配置修改。

## 后续边界

- 新建的 AODW RT 需要再跑 scoped `rt-guard`；历史 RT 不作为本次门禁基线；
- Work Agent 设计仍是未批准草案，必须另开实施 RT 后才能改精编运行路径。

---

## RT-029 收敛（2026-08-31）

接入报告写完后，RT-029 在收敛产品基线时发现三处「记录与实测对不上」，一并修正：

1. **skill 安装状态**：原文写「`handover-pack` 已通过符号链接安装」，而
   `install-skills.sh --check` 在 `main` 检出和各 worktree 里都报「未安装」——
   `.agent/` 根本不存在。断言与实测不符。改法不是把断言删掉，而是把它变成可复检
   的一项：`make aodw-check` 每次都真去测。
2. **门禁作用域只有散文、没有机制**：本文原说「历史 RT 不作为本次门禁基线」，
   但 `rt-guard.sh --root .` 照扫 27 个 RT，报出一百多条存量告警。现已把作用域
   写成配置（`.aodw-next/project.yaml` 的 `rt_gate_scope.managed_from`），并给存量
   条目打上 AODW 自带的 `backfill` 标记。
3. **`RT/index.yaml` 名不副实**：扩展名是 `.yaml`、内容是 Markdown 表格，
   `rt-guard` 的 G109 解析不了，导致仓库里每个 RT 都恒报一条告警、G111 的存量
   豁免也无从生效；同时索引漏掉了 RT-007/008/010，而编号规则要取「目录 ∪ 索引」，
   漏号就会撞号。现已改成真 YAML 并补齐，未改动任何 RT 自己的文档。

未做的一项：`pre-commit` hook 仍未安装。本仓库的 `main` 检出、RT worktree 与
原 PR-001 工作树共用同一个 git common dir，hook 装一次对所有 worktree 生效，
会写到用户明确要求保持原样的工作树的提交路径上。记为 DI-003，等落点定了再议。

统一检查入口：`make ci`（= `make test` + `make aodw-check`），GitHub CI 跑同一条。
