# RT-Lite: RT-053 - 单库共享授权文件与 Agent 自助导入

> profile: Spec-Lite | execution_mode: collaborative

## 方案（给人看）

- 做什么：新增独立 `cwk-kb-authorize` Skill。具备 OPS token 登记写权限的管理 Agent 可为一个知识库生成一份授权文件，经现有 IM 由用户自行分发；接收方把附件交给 Agent，`cwk-kb-query` 自动导入、按库选择 token 并验证访问。
- 为什么：当前真实需求是“生成文件—IM 分发—Agent 导入”，不是在线申请审批系统。独立授权 Skill 可反复用于既有知识库，职责不应塞进只执行一次的建库 Skill。
- 代价：同一授权文件的所有持有人共用一支 token，只能整体撤销；IM 与接收端会持久保存明文授权文件；MVP 的授权管理员仍是“拥有 OPS token 登记写权限的 Agent”，不是由现有 `kb.json.owner_ref` 自动证明的创建者。
- 这次故意不做什么：不建设申请/审批服务、成员系统、Token Broker、HTTPS 管理面、个人接收者登记、单人撤销、设备签名；不修改查询 Gateway 或 NAS 知识库结构；不迁移生产库 owner。
- 用户怎样算成功：管理 Agent 一句话生成只含单个 `kb_id` 的授权文件；同一文件可经 IM 发给多人；接收 Agent 不打印 token，自动导入后能访问该库，其他库返回 403；撤销后所有副本下一次查询立即 401。
- 建议：**独立 Skill**。授权是可重复的管理动作，创建是一次性生命周期动作；接收侧继续归查询 Skill。

## 假设与现状

- 关键假设：MVP 接受 IM 附件保存明文 token 的风险；同一授权文件多人共用且整体撤销；授权 Skill 只安装/启用于具备 OPS token 登记写权限的管理 Agent。
- 现状依据：
  - `scripts/kb_token.py` 已支持摘要登记、单绑定 generation、即时 revoke 与 `kb_ids` 范围。
  - `scripts/kb_gateway.py` 每请求重读 token 登记并按目标库判 200/403/401；无需改动。
  - `scripts/kb_create.py` 的 `owner_ref` 由调用方传入，生产三库当前均为 `owner-ref-pending`，不能作为本次自动创建者授权的可靠依据。
  - 旧提交 `a6433aa` 仅实现签发端本地文件输出，不包含单库共享授权合同和接收端按库导入；不直接合并，必要代码只按本 RT 合同重写/择取。

## 实现备注（用户不问可不展开）

### 授权文件合同

- 文件 schema：`cwk.kb.access-file.v1`。
- 必填字段：`schema`、`kb_id`、`gateway_url`、`token`、`token_id`、`issued_at`、`expires_at`。
- `token` 是明文 bearer secret；文件创建与本地安装均为 0600，父目录 0700；CLI 成功回执、日志和错误中不得出现 token。
- 一份文件只能含一个 `kb_id`；拒绝空值、控制字符、未知 schema、额外权限字段和非 HTTPS/明确允许的当前内网 HTTP Gateway。

### 共享 token 语义

- token 绑定使用稳定的共享绑定标识 `share:<kb_id>`，范围固定为 `[kb_id]`。
- 首次导出创建 generation 1；已有 active 共享 token 但本地原文件不可恢复时，重新导出必须显式 rotate，旧文件全部失效。
- revoke 以 `token_id` 为入口；Gateway 每请求重读，撤销后下一请求 401。
- 不把共享 token 冒充个人 Agent token；登记记录需要可识别的 `token_kind=shared_kb`，旧记录兼容视为 `agent`。

### 接收端导入

- 本地权威目录：`~/.openclaw/cwk/access/`（0700），每库一个 0600 文件；文件名使用 kb_id 的安全编码/摘要，不能路径穿越。
- 导入工具仅输出非秘密回执；同库已有不同 token 时默认拒绝，显式 replace 才覆盖。
- `kb_gateway_client.py` 在显式 `CWK_KB_GW_TOKEN` 缺失时，按请求参数 `kb` 从本地授权库取 token；显式环境变量保持最高优先级，兼容现有流程。
- 导入后用 `/v2/kb/libraries` 或 capabilities + 目标库只读查询验证；不枚举或试探未授权库。403 判据由脱敏测试覆盖。

### 计划改的文件

- 新增 `scripts/kb_access_file.py`、`tests/test_kb_access_file.py`。
- 新增 `skills/cwk-kb-authorize/SKILL.md`。
- 修改 `scripts/kb_token.py`（共享 token 类型/稳定绑定/导出与撤销所需窄接口）。
- 修改 `scripts/kb_gateway_client.py` 与 `skills/cwk-kb-query/SKILL.md`（按库本地导入和选择）。
- 修改治理 ownership manifest，为新文件和新 Skill 建立归属。

### 不能破坏的约定

- 查询 Gateway 保持纯只读；管理 token 与现有 Agent token 兼容。
- token 原文不得进入登记表、日志、回执、测试夹具或 Git。
- 同一授权文件只开放一个库；不存在通配符或静默扩大范围。
- 不自动发送 IM；用户自行分发文件。

### 内部阶段

1. 冻结授权文件 schema、共享 token 代际和本地导入合同。
2. 实现共享 token 导出/撤销及 0600 原子文件。
3. 实现接收端导入和按库选择。
4. 编写两端 Skill 文档与端到端脱敏测试。
5. 跑专项、AODW、governance 和可承受的仓库质量门；独立复核。

## 验证

- `tests/test_kb_access_file.py`：schema、单库范围、原子权限、无回显、路径攻击、覆盖、rotate、revoke、导入、多库并存。
- `tests/test_kb_token.py`：旧 Agent token 合同不回归。
- `tests/test_kb_gateway.py`：共享 token 目标库 200、其他库 403、撤销后 401；Gateway 仍无写面。
- `tests/test_rt051_wizard_readside.py`：显式 env token 兼容；本地按库 token 选择。
- `make aodw-check`、`make governance-audit`、`git diff --check`。
- 不接触真实 token；生产部署和真实 IM 发送不属于本次本地实现验收。

## 变更记录

- 用户将能让管理 Agent 生成单库授权文件，经 IM 自行分发；接收 Agent 导入后直接查询该库。

## 遗留事项

- 生产三库 `owner_ref` 仍为 `owner-ref-pending`；创建者身份自动绑定与普通创建者自助授权另立后续，不在 MVP 冒充完成。
- 共享文件不支持按接收人单独撤销；需要时再增加“一人一文件”，不改变本次文件导入流程。
