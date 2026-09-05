# RT-047 三机生产部署

- P1 OPS 查询网关服务化：CWK 代码上 OPS（~/CWK）；launchd 双实例常驻
  （cwork-3m:8787 / docdb-touqian:8788，--host 0.0.0.0）；NAS env 与 admin key
  落 OPS 侧 600 权限 env 文件；验收 = 从另一台内网机器 /health ok=true
  + 错 token 401 + 写动词 405。
- P2 token 多租户（Evan 2026-09-05 17:50 定案：单 Gateway 多 Agent、每 Agent 一人）：
  身份层不发明新体系——用户唯一身份 = 现有业务 Key（工号级 AppKey，工作协同/玄关
  都在用的那把）；token 签发单位 = (owner_ref, agent_binding_id)，owner_ref 由
  verify-key 从该 Key 派生；绑定注册表复用 RT-013 模式（HMAC 哈希 + epoch +
  审计回执）；max_active 语义改为「每用户跨 Agent 实例」；KB-PARAMETERS A6 措辞
  同步修订（「每设备」→「每 Agent 实例绑定」）。
- P3 245 接入：SSH/凭据由 Evan 提供；装 cwk-kb Skill；Python 版本快测；
  走 OPS 网关查询验证（前置闸：不过不部署）。
- P4 备份演练：NAS 快照策略确认；从备份恢复一个库；manifest 对账通过。
- 反空转：OPS 网关停 NAS 连接必红；gateway 进程环境扫描无工厂写者凭据；
  非成员库访问 404/403。
