## CWK 工作协同镜像

本机已安装 CWK（只读 CWork/工作协同 知识镜像）。需要建立、迁移、运行或排查
工作协同镜像、每日 Markdown/HTML 日报、事件与实体关联、DocDB 同步或 nightly
任务时，先读取下面这份 Skill 说明，再按其中的命令执行：

- `{{CWK_SKILL_DOC}}`

边界：CWK 对 CWork 只读，不回复、审批、完成、删除或标记事项。不要读取、回显或
转储 `.env` 与 `cwk-mirror.local.json` 的内容；凭据由使用者自己填写。真实采集与
DocDB 发布需要使用者明确确认。

本节由 CWK 的 `install.sh --integration router` 维护，重复执行会原样覆盖本节。
不要在起止标记之间手工编辑；标记之外的内容不受影响。
