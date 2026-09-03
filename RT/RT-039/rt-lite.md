# RT-039 查询专用 Skill（cwk-query）与两半架构定档

## 是什么

把知识库「应用侧」从全项目 Skill（cwk-mirror-workflow）中独立出来：一个只读查询 Skill 薄壳（`skill-query/`），教任何 OpenClaw 业务 Agent 定位镜像 → 调 `cwk_wiki_query.py` → 按证据包规矩作答。同时把「维护侧/应用侧两半架构」定档进 `docs/DESIGN.md`。

## 为什么

- 查询侧零依赖维护侧代码（接口是镜像数据，实测核实：`cwk_wiki_query.py` 纯标准库、不 import 任何管线模块）
- 现有 `skill/` 是建库/激活/运维说明书，不含查询入口（grep 证实：零处提及查询工具）
- 同事与其他业务 Agent 只需装查询 Skill 即可用知识库，无需理解维护全流程

## 做了什么

- 新增 `skill-query/SKILL.md`：定位 `CWK_PROJECT_DIR`/`CWK_QUERY_MIRROR` → 查询命令 → 引用/拒答/只读规矩
- `docs/DESIGN.md` 增「维护侧与应用侧两半」节（数据契约接口、形态 A/B、演进路径）
- `RT/index.yaml` 登记 RT-039；治理 code-ownership-manifest 为新路径补认领规则

## 边界

- 不触碰 `scripts/` 封闭命名空间；`cwk_wiki_query.py` 原样复用
- v1 为纯路由 Skill（不含脚本副本），与现有 skill/ 的「Skill 是说明书、命令在仓库里执行」模式一致

## 验证

- `make aodw-check` / `make governance-audit` 通过（含 skill-query/ 认领）
- 安装到本机 Skill 根实测两个查询（盖章行动项 / 公积金盖章）证据包正常
