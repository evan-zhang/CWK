目标：根据 CWK 的当前代码和已批准文档，维护 AI 长期需要的项目知识；不得编造，
不得读取、复制或输出真实工作协同原文与凭据。

先读：

- `.aodw-next/01-core/aodw-constitution.md`
- 根目录 `AGENTS.md`
- `.aodw-next/06-project/ai-overview.md`
- `.aodw-next/06-project/modules-index.yaml`
- `README.md`、`docs/AI-PILOT.md` 和与本次改动直接相关的代码/测试

允许：在行为、数据含义、对外合同或模块职责确实变化后，更新项目概览、模块索引和
对应文档；为新研发事项新建 AODW RT。

必须先取得授权：修改生产路由、模型权限、数据范围、cron、Gateway、DocDB、外部发送、
推送、合并、删除或任何真实 CWork 数据。

要求：

1. 区分已核实事实、设计建议和待确认项；
2. raw、summary、manifest 的事实边界以代码与核验器为准；
3. 不把历史 RT、`.spec-workflow/` 或旧生成文件当作当前规则；
4. 先报告结论与验证证据，再列待人工确认项。

## 首次安装后的自检

`.aodw-next/` 已携带可分发的 AODW 框架。宿主入口为根目录 `AGENTS.md` 与 `CLAUDE.md`。
自带 `handover-pack` skill 的源码在 `.aodw-next/skills/`；若当前 Agent host 需要发现它，
使用：

```bash
bash .aodw-next/tools/install-skills.sh --check
bash .aodw-next/tools/install-skills.sh
```

安装器默认建立 `.agent/skills/handover-pack` 符号链接。该入口应由 `.gitignore` 忽略，
源码只保留在 `.aodw-next/skills/handover-pack/`。在不支持符号链接的环境使用 `--copy`。

门禁脚本对 AODW 接入之后新建的 RT 生效；接入前的历史 CWK RT 仅用于追溯，不能以
“补跑门禁”的方式改写其历史状态。
