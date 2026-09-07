# RT-052 方案稿独立评审（Codex，2026-09-07）

- 裁决：**GO-WITH-CHANGES**
- 评审对象：RT/RT-052/rt-lite.md @ cafca63（feature/RT-052-kb-discovery-ops-status）
- 方式：codex exec 只读评审（read-only sandbox），未修改文件、未提交、未运行产品测试
- tokens used: 177,469

## 必须改（合并入方案稿 v2）

1. **无目标库请求的独立鉴权流**：现有分发把不带 kb 的请求按主库鉴权——只授权了附加库的 token 会在进入 /libraries 前就 403。admin 直接返回全部挂载库不读登记表；绑定 token 单次读登记表验证有效性后取完整 kb_ids 与 mounts 求交集；禁止逐库 decide()（重复读+竞态+侧信道）。
2. **状态码**：未知/过期/吊销 token 与登记表不可读一律 401；有效 token 交集为空 → 200 + libraries:[]（无目标库不存在「越权」，不套 403）。
3. **kb_token.py 读侧扩展**：TokenDecision 不携带 kb_ids，网关拿不到完整授权域——允许只扩展读侧 API（一次扫描返回进程内授权集合）；签发/吊销/登记格式不变；禁止网关重新实现摘要/过期/吊销判定。
4. **先过滤授权域再读 NAS**：隐藏库的 raw-index/kb.json/词法文件一律不访问；测试要证「隐藏 backend 一被读即失败」。
5. **_v2_load_lexical 不是轻量读取**（全量下载 15MB+ 词法索引）：二选一——builder 原子发布小型 readiness 投影，发现端点只读 raw-index+小文件；或删掉动态词法可用性字段。不得加载全量索引后自称轻量。
6. **响应字段语义**：gateway_version 顶层一次；display_name 权威源是 kb.json（不愿加读就给 kb_id）；字段名 total/readable_total 且复用现有 readable 判定并说明 placeholder 计入；词法字段不得复用 lexical_modes（程序能力 vs 当前可用混淆），改 lexical_status；libraries[] 按 kb_id 排序。
7. **单库读取失败聚合**：该库行保留、计数 null、脱敏状态（source_unavailable）、顶层 complete=false；不回 NAS 路径或原始异常。
8. **补齐发现链路**：网关版本升 1.3.0；capabilities/启动路由卡声明 libraries；客户端 schema 校验；wizard 无 --kb 的 targetless libraries 动词；更新 skill 与接入文档——否则「摆脱写死清单」没交付。
9. **kb_ops 不得输出 agent-id**：登记表只存 HMAC 派生的 agent_binding_id，用它并如实标注不可逆；要人读别名是范围扩张需另立受保护账。
10. **OPS 输出字段白名单**：仅 token_id_suffix/agent_binding_id/kb_ids/created_at/expires_at/remaining_*/status；禁 token_sha256/owner_ref/owner_ref_salt/identity_probe/receipts/actor/reason/登记表原文；直接读取白名单投影，不经 kb_token list 再过滤；异常路径同样递归泄漏扫描。
11. **token_id 尾 4 只有 16bit**：改名 token_id_suffix、不得用于 revoke 定位；建议尾 8。
12. **降级矩阵与退出码**：tokens.json 缺失/不可读/损坏 = registry_unavailable 非零退出（绝不冒充 0 token）；单库 raw-index 坏只标该库继续；词法 missing/stale/corrupt 分开；无任何 refresh 日志 = never_run；只有旧日志 = 最后日期+stale；最新损坏 = error；active/expired/revoked 是账目状态不使命令失败。
13. **最近 refresh 确定算法**：按文件名合法日期取最新；只投影每库 ok/运行时间/必要状态，不带源路径/错误详情/vanished。
14. **只读可证伪**：文本与 --json、成功与损坏输入分别 WriteTrap；前后比对内容/mtime/目录树；静态检查禁 write/mkdir/remove/save_registry/子进程/shell；明确 0/1/2 退出码。
15. **三库名单不得成为第二份硬编码事实源**：repeatable --kb + 与网关挂载面差异检查（已挂载未盘点/配置未挂载都要显式告警）。
16. **治理**：kb_ops.py 创建时登记 ownership（文件创建前登记会触发 GA-STALE-RULE）；cwk-ops-deploy SKILL.md 更新是独立部署步骤与验收项。

## 建议改

- 风险清单给可执行阈值：记录当前三库与合成增长档的 NAS 请求数/下载字节/总耗时，规定超多少挂载库或时延改分页/快照；不得在单线程网关内并发复用 FileStation session 掩盖线性增长。

## 仅记录

- 严格授权过滤后，kb_id/显示名/件数/可读数/词法 readiness 不算过度暴露（同 token 经 list/search 本就可推导；网关版本 /health 已公开）。继续禁止 generation/corpus digest/engine/路径/token 登记信息。
- 评审核验 HEAD cafca6374346c814e0deb2e3ea1013895d8101b8，工作树干净。
