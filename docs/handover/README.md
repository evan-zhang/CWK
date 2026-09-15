# CWK 交接文档

本目录保存按 AODW `handover-pack` 规范编写的可接管现场。交接文档不是 RT 复盘，也不代表相关 RT 已完成。

## 现有文档

| 编号 | 文档 | 状态 | 关联 RT | 最后核验 |
|---|---|---|---|---|
| H001 | [RT-054 大库词法融合检索性能治理暂停开发与接管交接](H001-rt054-paused-development.md) | active | RT-054 | 2026-09-08 |

## 使用约定

- 接手前先读文档的“现状变化”和“接手清单”，再重查 Git、PR、进程与外部状态。
- 现实与记录冲突时，以现场为准，并更新交接文档。
- 新建文档前运行 `.aodw-next/skills/handover-pack/scripts/next-number.sh` 取号。
- 完成后运行 AODW handover validator 与 closure check。
