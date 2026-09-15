# RT-Lite: RT-056 - AI 知识库管理台与管理 API MVP

> profile: Spec-Lite | execution_mode: collaborative

## 方案（给人看）

- 做什么：提供默认关闭、仅监听本机的管理台，展示脱敏库状态、服务健康与管理审计，并为建库/摄取保留安全占位接口。
- 为什么：让管理员有一个可核验的观察入口，同时把写入边界锁在后续明确实现之前。
- 代价：需要独立管理密钥与审计文件；本次不提供真实写入能力。
- 这次故意不做什么：不修改 8787/8790，不创建/摄取/删除 KB，不读取真实语料，不部署生产服务。
- 用户怎样算成功：无开关时不可用；启用后无密钥为 401；概览、探测、审计可用且不泄露敏感字段；写接口只返回拒绝或 501 占位。
- 建议：定论——先交付只读观察面，真实写入另立合同与验收。

## 假设与现状

- 关键假设：本地库由 `kb_ops` 的 local backend 与 registry 结构提供；管理审计不属于 KB 内容树。
- 现状依据：`scripts/kb_ops.py` 的 `registry_projection` / `status`；RT-044/052/053 的只读、鉴权和脱敏约定。

## 实现备注

- 计划改的文件：`scripts/kb_admin.py`、`tests/test_rt056_kb_admin.py`、`docs/KB-ADMIN.md`、治理 manifest。
- 不能破坏的约定：密钥只从环境变量读取；不输出 token 摘要、owner 引用/盐、明文 token、路径或业务凭据；管理作业不调用写脚本。
- 内部阶段：实现 stdlib HTTP 面 → loopback 真实 HTTP 回归 → 编译、空白、治理审计 → 本地提交。

## 验证

- 目标测试：`python3 -m unittest tests.test_rt056_kb_admin`。
- 目标检查：`python3 -m py_compile scripts/kb_admin.py`、`git diff --check`、`make governance-audit`。
- 界面路径：真实 loopback 请求 `/`，确认原生 HTML/CSS/JS 返回。

## 变更记录

- 用户能感到什么变化：获得一个默认关闭的脱敏管理观察面；写操作明确显示为未实现，而不是假装完成。

## 遗留事项

- 真实建库/摄取作业、生产部署和更细的审计保留策略不在本次范围，需另立 RT 与安全评审。
