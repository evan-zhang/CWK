# CWK 平台 v1 执行计划（EXECUTION-PLAN v1.1）

- 日期：2026-09-04（v1.1 全量重写，吸收六份增补文档全部落点 + 三引擎评审修复）
- 基线合同：本文件 + KB-PARAMETERS v1.1 + SKILL-ARCHITECTURE v1.1（三份合称「验收合同」）
- 前置：AODW 判据纪律已同步；三引擎评审完成（REVIEW-SYNTHESIS.md，22 条修复清单全采纳/裁剪完毕）
- 状态：待 Evan 批准 → 批准即开工 RT-042

## 一、总目标

三机生产落地平台 v1：**NAS 主存 + OPS 中心工厂（双进程）+ AGENT 沙箱问答**。
对话式向导建库（cwk-kb Skill）、双源采集（工作协同 + 玄关 DocDB）、每库 taxonomy、
token 接入网关，端到端真实验收。

## 二、进程模型（v1.1 修订：两进程，宪法级）

```
OPS 91.72 上两个独立 OS 进程：
  kb-factory（工厂）   唯一写者：持 NAS 服务账号凭据；采集/精编/调度/审计归并/token 管理
  kb-gateway（网关）   只读：token 鉴权 → NAS 读返回查询；不持任何 NAS 凭据；
                       审计事件发往工厂的审计接收器（自己不写库内文件）
验收：ps 级进程分离断言 + gateway 进程环境/参数/打开文件扫描无 NAS 凭据（确定性）
管理 API（/api/kb/*）挂在 factory 进程，与 gateway 不同进程、不同端口。
```

## 三、编队标准打法（不变）

我写 spec + 判据（过判据纪律）→ Claude Code worktree 实现 → 我跑三门禁 →
Codex 安全评审 + Grok 红队对抗（评审不阻塞主线，失败降级双评审）→ 修复 → CI 绿 → 合并。
GPT-5.6 按需硬核回合。

## 四、阶段划分（v1.1：范围全吸收，工期重估 5–6 天）

### RT-042 存储层 NAS 化 + 建库基座 — D1–D2

范围：
1. storage 抽象（本地 FS / NAS FileStation 双后端，纯标准库）
2. 建库 CLI：display_name + 随机码升级为 128 位随机（防枚举）+ 完整库目录树生成（B 表 26 项为验收合同）
3. kb_members（OPS 集中索引为权威 + 库内副本审计留痕）
4. root-manifest 哈希账本 + docdb 源落盘规范（`raw/docdb/<fileId>@<rev>`、变更检测）
5. 向导状态机数据模型（draft→verified→sourced→previewed→ingesting→taxonomy→active，
   草稿 TTL + 未 verify 自动清理）
6. collection_state.json 采集游标（断点续采状态链）
7. 现有镜像数据迁移（路径映射表 + 内容哈希双向配对）

验证（含反空转）：
- 双后端一致性：「契约假后端」（Local FS）终态绿——排除时间戳类文件（log/audit/manifest 版本号），
  其余逐文件 sha256 零差异，排除集做结构等价断言
- **反空转**：换「无操作桩」后端（不真写）→ 全部测试必须红
- 越权路径（../、绝对路径）拒绝；断网重试 + 游标续采对账自愈
- 迁移判据：路径映射表内双向配对，未配对项必须落在「允许新增/重命名」清单内
- 判据纪律自检：删掉 NAS 后端换「无操作桩」，测试须能红（与「契约后端」Local FS 绿测严格分词，杜绝歧义）

### RT-043 工厂多库化 + 双源 + 管理 API — D3–D4

范围：
1. 多库注册表（幂等扫描）
2. 每库源配置 + 双源采集调度：**单 launchd 心跳（30 分钟）+ 工厂按 schedule.json 判到期**
   （不再每库生成 launchd；timezone 参数入 A3）
3. cwork 双通道 + DocDB 拉取（复用 RT-041 适配器；docdb 变更检测 mtime/hash/revision）
4. **管理 API 全组**（挂 factory 进程）：draft/verify-key/sources/preview（cwork count-only +
   docdb 文件数+类型直方图）/ingest/refine/taxonomy propose+confirm/schedule/token/status
   - verify-key 返回短时单次签名授权（绑定 draft_id + nonce），草稿鉴权前仅驻内存
   - **预留参数 v1 拒收**：收到 keywords/senders 等未启用字段一律 400，不得落盘
5. v2 刷新**冻结模型**：raw 永不改；回复只追加 snapshot；reply-state 指向当前权威 snapshot；
   summaries 声明 raw_ref+snapshot_id；查询回读权威 snapshot
6. taxonomy：提议器（含 entity_candidates/topic_candidates 载荷）+ 打标 + categories 索引页 +
   复利更新（compounding_scope 上限）
7. cwk-kb 向导 Skill（对话壳，调管理 API）

验证：
- 解耦（修订版）：停全部 OpenClaw → 用**已落盘的草稿** + CLI 续跑同一条用户库到 active；
  禁止另开测试专用捷径；负例：向导中途杀 Agent，草稿仍在、Key 不在 Agent 盘、可 CLI 续
- 幂等分账：raw-manifest 全量校验（存量逐文件 sha256 不变 + 零新增；log/audit 显式豁免）——锁死「v2 原地改 raw 集体改账」构造；同输入重跑 summaries 的
  raw_ref/snapshot_id 不得漂移
- 双源：两源归 raw 无重叠无丢失；库间隔离（确定性断言）
- 反空转：源 API 5xx / whoami 失败 / 空夹具对应用例必须红
- every6h 库：24h 测试钟断言 ≥3 次真实采集进程（非读配置）
- doctor 扩展：summary 指针/reply-state/最新 snapshot 三方一致；taxonomy 体检
  （孤儿分类/标签漂移/索引断链）；配置一致性（kb.json 引用 vs 子配置）

### RT-044 接入网关 + token 生命周期 — D5

范围：
1. kb-gateway 独立进程（纯标准库 ≤300 行）：token 鉴权 → 圈库 → 网关拼路径 → NAS 只读
2. token 强化：绑定 owner_ref + kb_ids + membership_epoch（非仅用户名）；TTL + 代际
   （reissue 原子递增使旧代全部失效）；每 Agent 实例绑定独立 token（2026-09-05 RT-047 P2 修订：单 Gateway 多 Agent、每 Agent 一人，粒度到 (owner_ref, agent_binding_id)，非机器级）；登记表只存 HMAC 摘要
   （审计只记 token_id + 指纹，禁明文/可逆密文）
3. 审计架构：网关发结构化事件 → 工厂审计接收器唯一写入（哈希链防篡改）；
   平台级预建库审计（鉴权成败/预览/建库/Key 轮换/token 发吊/归档）
4. TLS + 证书指纹固定（内网 v1；mTLS 列 v2）
5. Skill 改造：cwk-kb 查询走网关；token 存 host 侧 gateway 配置，
   沙箱内只有无凭据 exec 通道（统一 Key/token 同一隔离档）

验证：
- 越权矩阵：token A 访问非成员库 403（鉴权前统一 404 防枚举）；篡改路径 403/404；无 token 401
- token 生命周期：吊销即刻生效；并发 reissue 旧代全失效；token 调管理 API 一律 403
- 进程隔离：ps 断言两进程（专用系统用户 _kbfactory/_kbquery）；gateway PID 扫描无工厂写者凭据（factory 必须有；gateway 持专用只读 NAS 账号走 FileStation HTTP，不用 OS 挂载——「存储层凭据」定义为工厂写者账号）
- 反空转：断开 NAS 真连接 → 查询/采集用例必红；成功路径活依赖探针（whoami 返回真实身份、FileStation 写读探针经网关回读）
- 审计：随机操作回放逐条对上；哈希链篡改检测用例
- **维权鉴权**：改 taxonomy/rotate-key/改 schedule 各跑一次，断言 Agent 盘无 Key、
  token 只出现在声明过的 Skill 配置槽（会话库/日志/临时文件禁现）

### RT-045 三机部署 + 端到端验收 — D6

范围：NAS 正式目录+证书指纹；OPS 双进程 launchd 守护 + 心跳调度；AGENT 245 装 cwk-kb +
Py3.9 快测；**备份灾备**：NAS 快照策略 + 从备份恢复一个库并通过 manifest 对账（验收项）。

端到端验收单（全绿才算完）：
a. 用 cwk-kb Skill 走完整向导建真库（Evan 验证库：近 3 个月收发件箱 ≈440 篇）
b. 2 题黄金引文：答案 quote 必须是 raw 权威 snapshot 的字节子串且 report_id 命中
   （验证器在验证时刻经网关重新拉取字节比对，禁用预录副本）；
   1 题必拒答；禁止只查「答案里有编号」
c. **B 表 26 项按适用源逐项存在性+非空性**（cwork-only 项对纯 docdb 库豁免）；缺任一必检项 query 返回 503 + 审计
   （而非静默降级）
d. 扫沙箱盘：Key/token/NAS 凭据零残留（token 仅在声明过的 host 侧配置槽）
e. 夜间自动一轮：新增入库 + v2 刷新（指针不变量）+ 审计归并对上
f. 解耦负例：停 OpenClaw 全进程，更新照常（CLI 续跑同库）
g. 备份恢复演练通过

## 五、里程碑与工期

- M1（RT-042）：数据落 NAS + 建库基座 —— D2 末
- M2（RT-043）：双源多库 + 管理 API + 向导 —— D4 末
- M3（RT-044）：token 问答 + 安全验收 —— D5 末
- M4（RT-045）：三机生产 + 验收单全绿 —— D6
- 合计 5–6 个工作日（较 v1.0 +2 天：管理 API/向导/taxonomy/备份吸收进范围）
- 夜间既有任务照跑；13:40 类 v2 修复模式已回写为 RT-043 冻结模型

## 六、风险与对策（v1.1 增补）

| 风险 | 对策 |
|---|---|
| 编队产出不合治理 | spec 细 + 逐 diff 审 + 三门禁兜底 |
| FileStation 抖动 | 重试 + 游标续采 + 账本对账自愈 |
| 管理 API 面扩大攻击面 | 鉴权前置（verify-key 才建持久草稿）+ TTL + 限速 + 预建库审计 |
| 245 agent 名/Py3.9 | 前置探测项，不过不部署 |
| Codex/Grok 限速 | 评审不阻塞主线，降级双评审 |
| 数据丢失 | NAS 快照 + 异地副本 + 恢复演练（RT-045 验收项） |
| 单写者移交冲突 | 部署日冻结本地写 |

## 七、批准语义

Evan 批准本计划 = 验收合同三件（本文件 + KB-PARAMETERS v1.1 + SKILL-ARCHITECTURE v1.1）
定稿 + 编队定稿 + RT-042 即刻开工。修订完成后增量送三引擎复审（只审 delta），全 GO 才开工。
