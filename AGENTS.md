# AGENTS.md — CWK 开发规则

## AODW Foundation（必读）

本仓库采用 AODW v0.6.1。开始任何需求、设计、修复、RT、分支、提交或合并任务前，
先读 `.aodw-next/01-core/aodw-constitution.md`；它是唯一的流程与授权规则。

接着读：

1. `.aodw-next/01-core/ai-interaction-rules.md`
2. 本文件
3. `.aodw-next/06-project/ai-overview.md`
4. 需要时再读 RT、实现和 Git 的相应 AODW 文件

旧 Spec-Full、Autopilot、审计官流程、历史 RT 和 `.spec-workflow/` 仅用于迁移或证据
追溯，不是当前工作流。与 AODW 宪章冲突时，以宪章为准。

## 工程原则

你是主工程师：先调查、给出人话建议、在授权范围内连续做完并验证。用户决定产品取舍、
生产启用、外部发送、推送和合并，不需要阅读代码细节。

- 非琐碎工作先检查会改到的入口、相关测试和历史 RT；不要凭记忆描述现状。
- 改动只覆盖用户请求；不顺手重构、不改无关格式、不删除历史资产。
- 每个阶段都有可运行的验证；工具返回成功不等于目标完成。
- 五问任一为“是”时建 RT；轻量、局部、可逆变更走 AODW 轻量车道。
- 不要把 AODW 文件本身的修改误当产品代码；方法论文件按宪章直接改并验证即可。

## CWK 结构与边界

- `skill/`：OpenClaw 安装入口、配置模板与 Agent-facing 文档；
- `scripts/`：只读采集、规范化、精编、查询、同步和机械验证；
- `tests/`：脱敏 fixture 和回归测试；
- `docs/`：设计、运行、迁移与安全说明；
- `RT/`：研发轨迹；`PR/`：产品级合同与计划；
- `runs/`、`knowledge/`、`raw/`、`collected-raw/`：运行数据，默认不提交。

CWK 对 CWork 默认只读。真实 raw 是唯一事实源；任何摘要、实体、事件或模型输出都不能
回写 raw，也不能绕开逐条引文、SHA 和权限核验。

现有 `cwk-ai-reviewer` 是零工具 JSON transformer。不得给它添加 Skills、搜索、浏览器、
MCP、文件写入、subagent 或运行时调度权限。Work Agent 设计仍是未实施草案，见
`docs/design/cwk-work-agent-runtime-integration.md`。

## 常用检查

- `make ci`：**CI 跑的就是这一条**，等于 `make test` + `make aodw-check` +
  `make governance-audit`。判断「现在是不是绿的」以它为准；本地和 CI 命令不同的时候，
  CI 的绿灯不算本地证据；
- `make doctor`：检查可移植安装条件（含 Python 版本闸，低于 3.10 会直接失败）；
- `make test`：编译、单元测试和脱敏 smoke；
- `make aodw-check`：AODW **方法层**自检——框架 fixture、受管 RT 门禁、RT 花名册一致性；
  门禁面只含接入后新建的 RT，作用域配置在 `.aodw-next/project.yaml` 的 `rt_gate_scope`；
- `make governance-audit`：**代码层**自检——`git ls-files` 全集里每个文件归谁管、
  怎么改（RT-030 建立）。判据面是**当前代码树全量**，不是「新增文件才受管」：
  新增文件没有归属就是孤儿，直接红。判据写在
  `.aodw-next/06-project/governance/code-ownership-manifest.json`，
  脚本演化的前向叠加层在同目录的 `script-evolution-v2.json`，
  接管范围与例外边界见 `RT/RT-030/takeover-audit.md`。
  与 `aodw-check` 分工：那条管 RT 流程本身，这条管产品代码归属；
- `python3 scripts/cwk_wiki_query.py --lint`：检查本地 Wiki 证据完整性；
- `python3 scripts/cwk_cloud_wiki_compile.py --help`：确认精编命令合同。

`make test` 的单元测试跑满需要一个多小时（PR-001 的证据链测试大量做 KDF）。
不要因为「跑得慢」就以为卡住了，也不要用只跑几个文件的结果代替全量结论。

真实 CWork 采集、DocDB 同步或真实模型调用可能涉及受保护数据与费用；没有明确授权时，
优先运行本地、脱敏和 dry-run 检查。

## 代码、测试与 Git

使用 Python 3、UTF-8、显式路径与最小依赖。为行为、合同、路由、数据边界或错误恢复
写对应测试；只改文档时至少运行 Markdown/链接/空白检查。

不得提交真实原文、凭据、私有配置、模型提示、运行数据或 API 日志。`cwk-mirror.local.json`、
`.env`、`runs/`、`knowledge/` 和 raw 路径保持私有。

新 RT 的编号、worktree、方案门、收口门与合并流程均按 `.aodw-next/` 执行。没有用户
明确授权，不合并、推送、清理 worktree、改生产配置或执行任何外部写操作。
