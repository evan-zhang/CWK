# RT-049：网关合一（8787 单进程多库路由）

## 决策

2026-09-06 14:25 Evan 拍板「同意开工」。背景：spbp-2027 上线暴露了 v1
「一库一进程一端口」的运维税——每建一库就要占一个端口 + 写一个
launchd plist + 更新拓扑文档；而鉴权层（kb_token 登记表）早已多库感知
（token kb_ids 携带多库），错位靠「连不同端口」弥补。合一后建新库 =
摄取 + 登记表一行，零端口零 plist。

原四批改造（摄取筛选 / 夜间增量 / NAS 直写 / 人读页面）编号顺延，各自
立项时再入册。

## 变更（scripts/kb_gateway.py v1.0.0 → v1.1.0）

- `GatewayApp` 增挂载表 `mounts: kb_id → backend`；主库自动挂载于自身
  kb_id，`kb_mounts` 构造参数承载附加库
- `?kb=` 在 auth gate 之前解析为 target_kb 并传入 `authorize`：token 的
  scope 按「目标库」判——授权面不含即 403；先鉴权后查挂载面，探测未挂
  载的库不比鉴权便宜（admin 问未挂载库 → 404 unknown_kb）
- `_backend_for_kb`：空 kb 回落主 backend（v1 行为不变）；显式 kb 未挂
  载 → 404 `unknown_kb`，**不回落主库**——「问一库答另一库」正是本路
  由要防的跨库泄漏；404/403 响应体不含挂载面（不可枚举）
- `/query` `/citation` 全程走 `_backend_for_kb`；query 成功回执带 `kb`
  字段
- CLI `--kb`（可重复、去重、排除主库）仅支持 nas 后端——local 一进程
  一根目录，直接拒绝启动；`main()` 组装 kb_mounts，finally 关闭
  extra_backends
- startup card：多库（>1）时顶层 `mounted_kbs`（与 routes/methods 同级
  ——路由面事实，不塞进 backend 存储块）；单库卡片形状与 v1 一致

## 边界

- 治理归属：`R-runtime-rt044-kb-wizard-gateway`（exact_set，无指纹
  pin）→ 标准 RT 流程，无 script-evolution 回执
- 两进程宪法不动：网关仍只读、无写动词（import 图 + WriteTrapBackend
  双重保证）
- 8788/8789 过渡期保留为别名，验证后另行退役；不在本 RT 内删端口
- kb_id 即 nas prefix，沿用 RT-047 语义，未引入新身份体系

## 验证

- tests/test_kb_gateway.py 新增 `MultiLibrary*Tests` 13 例——路由：省
  略 kb=主库、?kb= 选库、主库按自身 kb_id 可达、citation 跨库、
  unknown_kb 404 且不泄挂载面、真实 socket 端到端；鉴权：单库 scope
  token 对挂载邻居 403、双库 token 通读两库、admin 通读全部、错 token
  401 与 kb 无关；CLI：local 拒绝 `--kb`、启动卡 mounted_kbs 顶层 +
  重复去重、`--kb`=prefix 不重复挂载
- 全量回归：test_kb_gateway + test_kb_token 共 190 tests OK；v1 既有
  177 例零改动通过（单库兼容）
- OPS 部署（8787 挂三库升级重启，8788/8789 保留）+ 245 端到端验证：
  部署后回填本节

## 部署与端到端回执（2026-09-06）

- OPS：`~/CWK` 非 git 仓（文件复制式部署），旧版先备份
  （`kb_gateway.py.bak-20260906-1.0.0`）；plist 备份
  （`.bak-20260906-single`）后加 `--kb docdb-touqian --kb spbp-2027`，
  bootstrap 重启；`--check` 预检：v1.1.0、mounted_kbs 三库、NAS 可达、
  登记表 2 条
- OPS 路由矩阵 10/10：spbp/touqian 经 `?kb=` 可查且回执带 kb；默认
  kb=cwork-3m；unknown_kb→admin 404 不回显挂载面；错/无 token 401；
  citation 跨库 200 matches_index=true、同 lineage 不带 kb 404；8788/8789
  旧实例健康存活（过渡期别名）
- 245 端到端（绑定 token）：三库通查、引文链 matches_index=true。验证
  脚本曾预期「绑定 token 问未挂载库→404」，实测 403——这是正确语义
  （scope 判定在挂载面之前，不泄露挂载面），已补单测钉住：
  `test_a_binding_token_asking_for_an_unmounted_kb_is_403_not_404`
- 结论：建新库 = 摄取 + 登记表一行，零端口零 plist（RT-049 目标达成）
