# CWK 内部可分发版交付验证报告

## 已完成内容

- 镜像模板和批处理默认路径改为项目相对路径。
- 新增 `cwk_doctor.py`，检查 Python、配置、公司 Skills 与实采前置条件；默认不读取业务数据。
- 安装脚本支持 `PYTHON=python3.11` 与显式 `--install-skill`，仅创建本地 Skill 链接。
- 工作协同、DocDB、鉴权 Skill 的路径可通过环境变量覆盖。
- 增加内部试点安装、隔离与验收说明。

## 对照需求契约

- Python 3.10+ 门槛：通过；Python 3.9 的 doctor 返回非零并给出修复提示。
- 无 Evan 专属模板/文档路径：通过；可分发配置与操作文档使用相对路径或环境变量。
- Skill 链接安装：通过；临时目标目录创建链接成功。
- 非链接目标保护：通过；安装器拒绝覆盖普通目录。
- 不自动启用实采、DocDB、cron 或 Agent：通过；安装流程仅运行本地 smoke。

## 测试结果

- Python 3.11 单元测试：92/92 通过。
- rules-only smoke、AI dry-run smoke、AI degraded smoke：通过。
- `cwk_doctor.py --check-only`：通过。
- `cwk_doctor.py --require-live --require-docdb`：当前环境通过。
- 临时 Skill 链接安装与冲突保护：通过。

## 修改文件

- `install.sh`、`.env.example`、`Makefile`
- `scripts/cwk_doctor.py`、`scripts/cwk_collect_live.py`、`scripts/cwk_sync_mirror_to_docdb.py`
- `scripts/cwk_wiki_batch_driver.sh`
- `skill/templates/CONFIG.example.json`
- `docs/INTERNAL_DISTRIBUTION.md`、安装与测试文档
- `specs/internal-distribution/`、`tasks/internal-distribution.md`
- `tests/test_distribution.py`

## 风险与遗留问题

- 公司 Skills 的实际可用性、工作协同授权和 DocDB 写入权限仍由每位试点同事的环境决定。
- CWK 不代替团队数据范围、共享知识库权限或 cron 值守的治理决定。
- 当前仓库为私有仓库，需先授予试点同事访问权限。

## 最终结论

可以交付给具备相应公司权限的技术同事进行独立安装试点；不建议未经试点即向全员自动部署。
