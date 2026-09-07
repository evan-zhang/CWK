# CWK 知识库检索接入（cwk-kb-query）

> 本文件由知识库管理员（Evan）分发。把它整段发给你的 AI 助手即可。
> 全程无秘密交换：本文件不含任何 token/密码；token 由管理员审核后单独下发。

【任务】在本机 OpenClaw Agent 上接入 CWK 知识库检索（安装/升级 cwk-kb-query Skill，配置查询，完成验证）。按下面的阶段判断自动选择分支执行。

## 阶段判断

检查 `~/.openclaw/cwk/kb-bind.env`：
- **不存在、为空、或 CWK_KB_BIND_TOKEN 不是 64 位十六进制** → 执行【阶段 A：安装 + 登记】
- **存在且有效** → 执行【阶段 B：配置验证】

---

## 阶段 A：安装 + 登记信息收集（无 token 时）

### A1. 安装/升级 Skill（幂等，重复执行即升级）

1. 拉取仓库（公开仓，无需凭据）：
   ```bash
   git clone --depth 1 https://github.com/evan-zhang/CWK.git /tmp/CWK 2>/dev/null || git -C /tmp/CWK pull --ff-only
   ```
2. 定位本网关工作区的 skills 目录（选自己网关对应的那个）：
   ```bash
   ls -d ~/.openclaw/gateways/*/state/workspace*/skills
   ```
   （多级工作区拿不准时，选当前 Agent 所在 gateway 的；仍不确定就列出全部让用户指认）
3. 覆盖安装：
   ```bash
   rm -rf <skills目录>/cwk-kb-query && cp -R /tmp/CWK/skills/cwk-kb-query <skills目录>/cwk-kb-query
   ```
4. 校验：目标目录下应有 `SKILL.md` 和 `references/v2-read-chain.md`；通读 SKILL.md 后再继续。

### A2. 收集登记信息（纯只读）

1. 机器名：
   - macOS：`scutil --get ComputerName && hostname`
   - Linux：`hostname`
2. 本 Gateway 的 agent-id：查 `~/.openclaw/` 下当前网关配置 agents 段的 id 字段，或运行 `openclaw agents list` 取默认聊天 Agent 的 id
3. 网关可达性预检：
   ```bash
   curl -s -m 6 http://192.168.91.72:8787/health
   ```
   期望 `ok=true` 且 `version ≥ 1.2`（连不上也如实报，不要猜原因）

### A3. 回报（严格按模板，不附加其他内容）

```
--- CWK-KB 接入登记 ---
机器名: <...>
agent-id: <...>
网关可达: <ok=true / 不可达 + 原始报错>
申请库: <从下面勾选>
  [ ] docdb-touqian（投前流程系统建设）
  [x] spbp-2027（2027集团SP&BP）
Skill 安装: <成功/已升级 路径 / 失败原因>
---
```

把以上内容发回给分发者（Evan）。等 token 下发后，重新执行本文件进入阶段 B。

⚠️ 纪律：以上全是明文标签。不要在回复中附带任何 token、密码、业务 Key 或配置文件原文。

---

## 阶段 B：配置 token + 四步验证（拿到 token 后）

### B1. 写入 token（token 值由 Evan 单独发给你，不经过群聊/仓库）

```bash
mkdir -p ~/.openclaw/cwk
cat > ~/.openclaw/cwk/kb-bind.env <<'EOF'
CWK_KB_BIND_TOKEN=<Evan发给你的64位token>
EOF
chmod 600 ~/.openclaw/cwk/kb-bind.env
```

⚠️ 铁律：token 绝不打印到对话/日志，绝不进命令行参数（history 会留痕）或 git；绝不向任何人索要管理 Key（CWK_KB_ADMIN_KEY 不会下发）。

### B2. 第 0 步：先发现授权库

不要再从本文复制库名。配置 token 后先运行：

```bash
curl -s -m 30 -H "X-KB-Token: $CWK_KB_BIND_TOKEN" http://192.168.91.72:8787/v2/kb/libraries
```

从返回的 `libraries[].kb_id` 选择后续查询目标。200 空列表表示 token 有效但当前没有已挂载授权库；401 才是 token 或登记表问题。

### B3. 四步验证（全绿才算接入完成）

```bash
set -a; source ~/.openclaw/cwk/kb-bind.env; set +a
```

1. **服务健康**：
   `curl -s -m 6 http://192.168.91.72:8787/health` → `ok=true`
2. **元数据查询（旧通道）**：
   `curl -s -m 30 -H "X-KB-Token: $CWK_KB_BIND_TOKEN" 'http://192.168.91.72:8787/query?q=立项&kb=docdb-touqian'`
   → HTTP 200（401=token 问题找 Evan；403=该库不在你的授权名单，找 Evan 调整）
3. **融合正文搜索（新通道，超时必须给 300 秒）**：
   `curl -s -m 300 -H "X-KB-Token: $CWK_KB_BIND_TOKEN" 'http://192.168.91.72:8787/v2/kb/search?kb=spbp-2027&q=亿元&retrieval_mode=lexical_fusion_v1&page_size=5'`
   → 200，total ≥ 1，top1 带 body_rank 和 candidate_spans
4. **引文链闭环**：拿第 3 步 top1 的 document_ref 与第一个 candidate_span 的 start_byte/end_byte：
   `curl -s -m 300 -H "X-KB-Token: $CWK_KB_BIND_TOKEN" "http://192.168.91.72:8787/v2/kb/read?kb=spbp-2027&document_ref=<ref>&start_byte=<s>&end_byte=<e>"`
   → `full_sha_verified=true` 且返回 text 含查询词

### B3. 使用守则（日常使用）

- 检索用法、参数表、易错点（64K 页上限 / span 是字节不是字 / 续读必须 continue）、错误码速查：以已安装的 `cwk-kb-query/SKILL.md` 和 `references/v2-read-chain.md` 为准
- 每个事实性回答必须带实时引文；零命中先换 2–3 个同义词再下结论
- 融合搜索单次可能 60 秒以上（正常），`503 lexical_unavailable` = 词法代问题，显式传 `allow_degraded=metadata` 降级或找运维
- 完成验证后汇报：四步验证的输出摘要 + skill 安装路径
