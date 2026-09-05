---
name: "cwk-kb-query"
description: "知识库问答：「问知识库」「查知识库」触发；网关检索，回答必带实时引文（sha256现场比对），无命中直说"
---

# cwk-kb-query — 知识库问答（实时引文）

对已建知识库提问。铁律：**每个事实性回答必须带实时引文**——citation 的 sha256 是网关现场从存储后端拉字节算出来的，`matches_index` 必须为 true；没有命中就直说没有，不许拿记忆或猜测作答。

## 网关拓扑（生产优先，2026-09-05 起）

**先用 OPS 生产网关**（192.168.91.72，launchd 常驻、开机自愈）：

- `cwork-3m`（个人工作协同近 3 个月）→ `http://192.168.91.72:8787`
- `docdb-touqian`（投前流程系统建设）→ `http://192.168.91.72:8788`

```bash
curl -s -m 6 http://192.168.91.72:8787/health   # /health 免鉴权；ok=true 即用 OPS
```

OPS 不可达（内网隔离/维护窗）才**兜底本机起网关**（仅限本机装有 CWK 仓库与凭据时）：

```bash
cd <CWK仓库> && set -a; source ~/.openclaw/gateways/life/.env; set +a
python3 scripts/kb_gateway.py --admin-key-env CWK_KB_ADMIN_KEY --backend nas \
  --prefix <prefix> --host 127.0.0.1 --port <port> &
```

其他库：问用户 prefix；OPS 侧为新库加 launchd 实例后再用（新库上 OPS 是运维动作，不在会话里即兴做）。

## 查询（token 派生，绝不打印 Key 本身）

```bash
set -a; source ~/.openclaw/gateways/life/.env; set +a   # CWK_KB_ADMIN_KEY 在这里
TOKEN=$(printf '%s' "$CWK_KB_ADMIN_KEY" | shasum -a 256 | awk '{print $1}')
curl -s -H "X-KB-Token: $TOKEN" 'http://192.168.91.72:<port>/query?q=<关键词>&limit=20'
```

⚠️ 派生坑（实测）：`printenv` 带尾换行会算出错误 token（表现为 401 假象），必须 `printf '%s'` 无换行。派生后可查长度是否 64。

- 结果在 `results[]`：lineage_id / path / title / version / sha256；`matched` 是总命中数
- query 是子串匹配（匹配 lineage+title+path 小写），中文关键词直接可用；复杂问题拆多个关键词多查几次

## 引文（每条引用的事实都要拉）

```bash
curl -s -H "X-KB-Token: $TOKEN" 'http://192.168.91.72:<port>/citation?lineage=<lineage_id>[&version=N]'
```

- 核对 `matches_index=true`；`excerpt` 是原文开头；`bytes` 是全文长度
- 回答格式：结论先行 + 每条证据一行（文件名、原文摘录、sha256 前 12 位）
- excerpt 只从原文开头截取——回答细节若超出 excerpt 范围，就说明证据不足，降级为「库里能确认的部分」，不许脑补

## 自检后才交回答

- 每条引文 matches_index=true；否则标注「账本不一致」并停止引用该条
- 401 = token 错（重新派生，先查尾换行坑）；405 = 写动词被拒（网关只读，正常）；503 = 后端不可达（OPS 网关→查 NAS；本机网关→查本机与 NAS）；连接被拒 = OPS 不通，走兜底
- 零命中 ≠ 库里没有：先换同义/变体关键词再下结论（实测问「S1」零命中，库里术语是「N1–N11」「立项会前」，内容其实全在）。试过 2–3 个变体仍零命中才明说「库里没有」
- 回答超出 excerpt 开头范围时，直读原文补全文：`kb_storage.FileStationBackend.from_env(prefix=<库>).read(<raw 相对路径>)`（scripts 目录入 sys.path），引文纪律不变
- 全景/盘点类问题：关键词检索之外，用同一后端 `be.walk_files('.')` 拉 raw 全目录树一次，比逐词猜快且全
- placeholder 占位件（png/zip/无后缀未转换）没有正文可查：如实说「只存档未转正文」，按文件名与上下文定位并声明局限，不假装读过
