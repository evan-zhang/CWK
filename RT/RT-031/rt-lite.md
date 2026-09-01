# RT-Lite: RT-031 - CWK 内部小团队最小上手

> profile: Spec-Lite | execution_mode: collaborative

## 方案（给人看）

- 做什么：把入口 README、内部分发说明与 Agent Skill 的首次使用指引收敛为一条短路径：前置条件、安装、个人配置、本地自检、一次个人或已批准团队目标的只读试跑。
- 为什么：现有说明把安装、运行和历史材料混在一起，且未说明 `doctor` 需要先导入个人 `.env`，新同事容易把本机检查误当为可直接运行的步骤。
- 代价：只调整说明与 RT 记录；不改变生产行为、权限、数据处理或依赖。
- 这次故意不做什么：不发布、不新增许可证或支持体系、不启用 cron/AI/生产配置、不读写真实数据、不触及 PR-001 受保护工作树，也不实现内容扫描、脱敏或过滤。
- 用户怎样算成功：获授权员工可用自己的 Key 与配置在自己的 OpenClaw Agent 上按短步骤完成安装、无数据自检，并清楚知道如何进行一次个人或已批准团队目标的试跑。
- 建议：定论。现有安装脚本经隔离副本实际验证可用，最小改动应聚焦消除指引歧义，而非引入新依赖或安装体系。

## 假设与现状

- 关键假设：CWork 原始内容已在进入 CWK 前由上游授权链路处理；CWK 的职责是只读镜像，不做二次内容判定。
- 现状依据（文件 / 历史 RT / 提交）：`README.md`、`docs/INTERNAL_DISTRIBUTION.md`、`install.sh`、`scripts/cwk_doctor.py`、`skill/SKILL.md`；RT-030 的治理清单说明 `install.sh` 与安装模板是受管入口。

## 实现备注（用户不问可不展开）

- 计划改的文件：`README.md`、`docs/INTERNAL_DISTRIBUTION.md`、`skill/SKILL.md`、本文件。
- 不能破坏的约定：标准库依赖、私有配置不入库、CWork 只读、DocDB 仅在明确选择后写派生产物。
- 内部阶段：核实安装与依赖 → 收敛上手路径 → 隔离副本 smoke → 文档和治理检查。

## 验证

- 要跑的检查 / 要点的界面路径：临时隔离副本执行 `install.sh --install-skill`；检查生成私有模板、Skill 链接与脱敏 smoke；运行 Markdown/链接/空白检查、相关单测、`git diff --check`。
- 对照成功标准的结果：隔离副本以 Python 3.11、临时 HOME 和独立 skills 目录安装通过；生成 `.env`、`cwk-mirror.local.json`、正确的 Skill 链接和 smoke 的 Markdown/HTML。`tests/test_distribution.py`、RT-031 门禁、治理审计和空白检查通过。全仓 AODW 自检在未改动的基线脚本中因第 91 行把中文分号写入 shell 变量展开而失败；主工作树的相同基线复现，未纳入本 RT 修复。

## 变更记录

- README 给出首屏短路径和实际依赖；内部分发说明给出个人配置、导入 `.env` 后的 doctor 自检，以及个人/已批准团队 DocDB 试跑的分界；Skill 明确首次安装和隔离规则。

## 遗留事项

- 无。
