# CWK 知识库接入 Runbook（v3 · RT-055 新栈）

> 本文件由知识库管理员（Evan）分发。把它整段发给你的 AI 助手即可。
> 全程无秘密交换：本文件不含任何 token/密码；token 由管理员审核后单独下发。
> 适用：与管理员 gateway 同一局域网、且以 OpenClaw 运行的 Agent。栈版本：RT-055（2026-09-14 灰度上线）。

## 服务坐标（只读备忘）

| 能力 | 局域网入口 | 鉴权 |
|---|---|---|
| 健康检查 | `GET http://192.168.91.72:8787/healthz` | 免鉴权 |
| 检索 | `POST http://192.168.91.72:8787/query` `{bank, query, top_k}` | X-KB-Token 必须 |
| AI 问答 | `POST http://192.168.91.72:8790/answer` `{bank?, query, top_k?}`（约 25–45s，超时给 ≥120s） | X-KB-Token 必须 |
| 读原文 | `POST http://192.168.91.72:8790/read` `{doc_id, offset?, length?}` | X-KB-Token 必须 |

当前可用库（bank）：`cwork-3m`（工作协同近 3 月）、`docdb-touqian`（投前资料）、`spbp-2027`（2027 集团 SP&BP）。
错误语义：401 = token 缺失/过期/被吊销（找管理员）；403 = token 有效但目标库不在其 scope（找管理员调 scope）；404 = doc_id 不存在；416 = offset 越界（按 total_chars 重算）。

## 阶段判断

检查 `~/.openclaw/cwk/kb.env`：
- **不存在、为空、或 CWK_KB_TOKEN 不是 64 位十六进制** → 执行【阶段 A：安装 + 登记】
- **存在且有效** → 执行【阶段 B：验证】

---

## 阶段 A：安装 + 预检 + 登记（无 token 时）

### A1. 安装/升级 Skill（幂等，重复执行即升级）

```bash
git clone --depth 1 https://github.com/evan-zhang/CWK.git /tmp/CWK 2>/dev/null || git -C /tmp/CWK pull --ff-only
ls -d ~/.openclaw/gateways/*/state/workspace*/skills   # 定位本网关工作区 skills 目录（多网关拿不准就列出全部让用户指认）
rm -rf <skills目录>/cwk-kb-query && cp -R /tmp/CWK/skills/cwk-kb-query <skills目录>/cwk-kb-query
```

校验：`SKILL.md` 存在，且内容含 `8787`、`8790`、`/read`（v3 特征），不含 `draft-pending-gray-gate`。

### A2. 加入 Agent 可见白名单（装了 ≠ 看得见，必做）

skill 目录就位只是文件层面；Agent 实际能否使用取决于该 agent 的 skills 白名单：

1. 找到本网关配置文件（通常 `~/.openclaw/gateways/<网关名>/openclaw.json`），先备份。
2. 在 `agents.entries.<本agent-id>.skills` 数组**末尾追加** `"cwk-kb-query"`（只加不删，保持既有条目不动）。
3. 验证：`openclaw skills info cwk-kb-query` → 应显示 `Visible to model: yes`。
4. 若显示 excluded/not visible：改完后让该 agent 新起一轮对话重载 skills；仍不行则回报登记信息，不要瞎改其他配置。

### A3. 预检 + 收集登记信息（纯只读）

```bash
curl -s -m 6 http://192.168.91.72:8787/healthz        # 期望 {"status":"ok"}
scutil --get ComputerName 2>/dev/null || hostname      # 机器名
openclaw agents list 2>/dev/null | head                # 本网关 agent-id
```

### A4. 回报（严格按模板，不附加其他内容）

```
--- CWK-KB 接入登记 ---
机器名: <...>
agent-id: <...>
网关可达: <{"status":"ok"} / 不可达 + 原始报错>
申请库（勾选）:
  [ ] cwork-3m（工作协同近 3 月）
  [ ] docdb-touqian（投前资料）
  [ ] spbp-2027（2027 集团 SP&BP）
Skill 安装: <成功/已升级 路径 / 失败原因>
白名单可见: <Visible to model: yes / 未通过 + 现象>
---
```

把以上内容发回分发者（Evan）。管理员签发 token 后会单独私发给你；拿到后重新执行本文件进入阶段 B。

⚠️ 纪律：全程明文标签，不附带任何 token、密码、业务 Key 或配置原文。

---

## 阶段 B：配置 token + 验证（拿到 token 后）

### B1. 写入 token（值由 Evan 私发，不经过群聊/仓库/日志）

```bash
mkdir -p ~/.openclaw/cwk
cat > ~/.openclaw/cwk/kb.env <<'EOF'
CWK_KB_TOKEN=<Evan私发你的64位token>
EOF
chmod 600 ~/.openclaw/cwk/kb.env
set -a; source ~/.openclaw/cwk/kb.env; set +a
```

⚠️ 铁律：token 绝不打印到对话/日志，绝不进命令行参数（history 留痕）或 git。签发/换发后等 ≥10 秒再验证（注册表同步有秒级延迟）。

### B2. 验证矩阵（全绿才算接入完成）

1. **服务健康**：`curl -s -m 6 http://192.168.91.72:8787/healthz` → `{"status":"ok"}`
2. **检索**（用你申请的任一 bank）：
   ```bash
   curl -s -m 15 http://192.168.91.72:8787/query -X POST \
     -H 'Content-Type: application/json' -H "X-KB-Token: $CWK_KB_TOKEN" \
     -d '{"bank":"<你的bank>","query":"立项","top_k":3}'
   ```
   → HTTP 200，`hits` 数组含 `doc_id/score/channel`，`took_ms` 毫秒级
3. **读原文**（拿第 2 步 top1 的 doc_id）：
   ```bash
   curl -s -m 15 http://192.168.91.72:8790/read -X POST \
     -H 'Content-Type: application/json' -H "X-KB-Token: $CWK_KB_TOKEN" \
     -d '{"doc_id":"<doc_id>","offset":0,"length":500}'
   ```
   → HTTP 200，返回 `text/eof/total_chars`
4. **AI 问答**（可选，按需）：
   ```bash
   curl -s -m 120 http://192.168.91.72:8790/answer -X POST \
     -H 'Content-Type: application/json' -H "X-KB-Token: $CWK_KB_TOKEN" \
     -d '{"bank":"<你的bank>","query":"<问题>","top_k":3}'
   ```
   → HTTP 200，返回 `answer` + `citations`；约 25–45 秒属正常；库里没有的内容会体面拒答

错误对照：401 → token 问题找 Evan 重签；403 → 该库不在你的 scope，找 Evan 调整。

### B3. 使用守则（日常）

- 检索用法、端点细节、易错点以已安装的 `cwk-kb-query/SKILL.md`（v3）为准；日常使用直接对 agent 说「用 cwk-kb-query skill 查/问 <bank> 库：<问题>」。
- 每个事实性回答必须带 citation 或 `/read` 原文页；`no_answer` 或零命中先换 2–3 个同义/变体查询再下"库里没有"的结论。
- 语料原文不要大段搬运到频道/群聊展示（引用与定位片段除外）。
- token 是按 agent 实例签发的：不要复制给其他机器/agent 共用；怀疑泄露立即让 Evan 吊销重签。
- 完成验证后汇报：验证矩阵各步 HTTP 状态摘要 + skill 安装路径 + 白名单可见状态。
