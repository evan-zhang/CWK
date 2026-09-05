# KB Skill 统一架构（SKILL-ARCHITECTURE v1.1）

- 日期：2026-09-04（v1.1：三引擎评审修复——仅采纳经复核确认项）
- 定位：平台对用户的唯一门面。一个 Skill（`cwk-kb`），三个动词【建/维/查】。

## 一、进程模型（v1.1 核心修订：两进程）

```
OPS 91.72 两个独立 OS 进程：
  kb-factory   唯一写者：持 NAS 服务账号凭据；管理 API（/api/kb/*）挂这里
  kb-gateway   只读查询：token 鉴权 → NAS 读；持**专用只读 NAS 账号**（FileStation HTTP，
               不用 OS 挂载）；不同端口、专用系统用户 _kbquery
验收（RT-044）：ps 断言两进程两系统用户（_kbfactory/_kbquery）+ gateway PID 扫描
无**工厂写者凭据**（factory 必须有）。「存储层凭据」≡ 工厂写者账号；
gateway 只读账号不得进 factory 进程、永不传播
```

v1.0 的「同进程不同路由组」作废——与两层分离宪法互斥（工厂必须持凭据，同进程即网关间接持有）。

## 二、三动词 → 接口映射（v1.1 补全）

```
【建】对话式向导（USER-MANUAL v2.1 全流程）
  起名/选型            → POST /api/kb/draft      （鉴权前草稿仅驻内存，TTL 30min 自动清理）
  验 Key              → POST /api/kb/verify-key  （whoami；返回短时单次签名授权绑定 draft_id+nonce）
  配源/窗口           → PATCH /api/kb/{id}/sources（须持 verify 授权）
  预查询 cwork        → POST /api/kb/{id}/preview （count-only：篇数；返回实际生效 filter 清单）
  预查询 docdb        → POST /api/kb/{id}/preview （文件数+类型直方图；另有 browse 列目录）
  正式拉取            → POST /api/kb/{id}/ingest  （后台异步；含 refine 阶段字段）
  专属建议            → POST /api/kb/{id}/taxonomy/propose（载荷含 taxonomy + entity/topic candidates）
                      → POST .../confirm
  频率                → PATCH /api/kb/{id}/schedule（含 timezone）
  发 token            → POST /api/kb/{id}/token

【维】（一律须 Key：whoami + Key↔库主匹配；token 调管理 API 一律 403）
  改名/分类/关注点/频率/源 → PATCH 对应端点
  换 Key              → POST /rotate-key（二次确认：旧 Key 或已验证设备）
  状态                → GET /status
  token 吊销/重发      → POST /token/revoke | /reissue（reissue 原子递增代际，旧代全失效）
  归档                → POST /archive --mode freeze|purge（purge 需离线审批对象：工单+清单哈希先落盘）
  恢复                → POST /restore

【查】（token 鉴权，走 kb-gateway 进程）
  提问 → 导航→回读权威 snapshot→引用/拒答（query-contract 纪律不可参数化）
```

**预留参数 v1 拒收**：API 收到 keywords/senders 等未启用字段一律 400，不得落盘（防「可写不生效」假配置）。

## 三、鉴权三档（v1.1 强化）

| 档 | 凭据 | 范围 | 存储 |
|---|---|---|---|
| 自助管理 | cwork Key | 建/维自己的库；高危动作二次确认 | OPS `secrets/<kb>.enc` 加密；Agent 侧仅对话内存中转 |
| 查询 | token（绑 owner_ref+kb_ids+代际+agent_binding_id，TTL，每 Agent 实例独立，2026-09-05 RT-047 P2 修订） | 只读自己圈定的库 | host 侧 gateway 配置槽；沙箱内无凭据 exec 通道 |
| 平台管理 | admin 凭据 | 跨库运维 | 只存 OPS 本机，永不出 OPS |

- token 登记表只存 HMAC 摘要；审计只记 token_id+指纹，禁明文/可逆密文
- Key 生命周期：「不落盘」精确化为——不落 Agent 磁盘、不进 NAS 库目录、不进聊天记录留存；OPS 加密存储供 nightly 无人值守采集
- TLS + 证书指纹固定（内网 v1；mTLS/OAuth 列 v2 不采纳进 v1，成本不匹配）

## 四、审计架构（v1.1 新增）

- 网关不写库内文件：查询审计 = 结构化事件 → factory 审计接收器（唯一写入者，哈希链防篡改）
- 平台级预建库审计：鉴权成败/preview/建库/Key 轮换/token 发吊/归档（不依赖库内文件先存在）
- 管理操作须收到持久化回执才算成功

## 五、解耦与验收

- Skill 是薄壳（对话引导 + HTTP），重活全在 OPS；解耦判据（RT-043）：
  停全部 OpenClaw → 用已落盘草稿 + CLI 续跑同一用户库到 active；负例：向导中途杀 Agent，草稿仍在、Key 不在 Agent 盘
- cwk-query 保留为弃用别名；统一 `cwk-kb`
- 安装 = 填 1 个 token（OPS 地址为安装常量）

## 六、执行计划落点

RT-043 管理 API 全组 + 向导 Skill；RT-044 两进程 + token 生命周期 + 审计架构 + 维权鉴权验收；RT-045 用 cwk-kb 走完整向导建真库。完整参数见 KB-PARAMETERS v1.1。


## 七、第二轮复审闭合（v1.2 补丁）

1. **verify-key 授权完整规格**：返回短时签名管理授权 `{owner_ref, draft_id, allowed_actions[], exp≤30min, nonce}`；向导后续【建】步骤持此授权调用，不重传 Key；【维】每次会话重新 verify。
2. **二次确认覆盖面**：rotate-key / token reissue / archive（freeze|purge）/ member 变更，全部需 Key + 二次确认。
3. **草稿防滥用**：每 IP/每 Key 并发草稿上限 3、速率限制、内存上限 + 对应负向验收。
4. **日志脱敏红线**：网关/工厂/代理日志禁记 Authorization/Cookie/Key 请求体（verify-key 路由不落 body）；RT-044 验收含全链路日志凭据扫描。
5. **审计韧性**：gateway 本地有界 spool（溢出告警）；管理操作 fail-closed（接收器不可用即拒绝）；**每日审计链头哈希推送外部锚点**（管理员告警渠道），防工厂被攻陷后整体重写哈希链。
6. **统一 404 防枚举**：有效 token 访问非成员库与非存在库一律 404；403 仅用于权限域错误（如 token 调管理 API）。
7. **管理 API 网络面**：仅监听内网地址 + 源 IP allowlist（v1 先限运维网段）。
8. **v1 残余风险声明（已接受）**：Key 经向导对话进入模型上下文，为内网单用户验证阶段的已接受残余；补偿控制 = OPS 日志脱敏 + Agent 盘零落盘验收；多用户/跨部门时升级为直传表单或 OAuth（v2 硬门槛）。
