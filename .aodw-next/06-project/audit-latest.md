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
- `handover-pack` 已通过符号链接安装，验证脚本可编译；
- `rt-guard.sh --root . --list-gates` 已加载 14 条判据；
- `make doctor` 通过；未执行真实采集、DocDB 写入、真实模型调用或生产配置修改。

## 后续边界

- 新建的 AODW RT 需要再跑 scoped `rt-guard`；历史 RT 不作为本次门禁基线；
- Work Agent 设计仍是未批准草案，必须另开实施 RT 后才能改精编运行路径。
