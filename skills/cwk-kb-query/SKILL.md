---
name: "cwk-kb-query"
description: "知识库问答：「问知识库」「查知识库」触发；网关检索，回答必带实时引文（sha256现场比对），无命中直说"
---

# cwk-kb-query — 知识库问答（实时引文）

对已建知识库提问。铁律：**每个事实性回答必须带实时引文**——citation 的 sha256 是网关现场从 NAS 拉字节算出来的，`matches_index` 必须为 true；没有命中就直说没有，不许拿记忆或猜测作答。

## 已知库注册表（用户没说问哪个库就问一句，或两个都查）
- `cwork-3m`（个人工作协同近 3 个月）→ 端口 8787
- `docdb-touqian`（投前流程系统建设）→ 端口 8788
- 其他库：问用户 prefix，任选空闲端口起网关

## 启动网关（若未运行）
```bash
cd /Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK
set -a; source ~/.openclaw/gateways/life/.env; set +a   # CWK_KB_ADMIN_KEY 在这里
curl -s http://127.0.0.1:<port>/health   # ok=true 即跳过启动
python3 scripts/kb_gateway.py --admin-key-env CWK_KB_ADMIN_KEY --backend nas --prefix <prefix> --host 127.0.0.1 --port <port> &
```

## 查询（token 派生，绝不打印 Key 本身）
```bash
TOKEN=$(python3 -c "import os,hashlib;print(hashlib.sha256(os.environ['CWK_KB_ADMIN_KEY'].encode()).hexdigest())")
curl -s -H "X-KB-Token: $TOKEN" 'http://127.0.0.1:<port>/query?q=<关键词>&limit=20'
```
- 结果在 `results[]`：lineage_id / path / title / version / sha256；`matched` 是总命中数
- query 是子串匹配（匹配 lineage+title+path 小写），中文关键词直接可用；复杂问题拆多个关键词多查几次

## 引文（每条引用的事实都要拉）
```bash
curl -s -H "X-KB-Token: $TOKEN" 'http://127.0.0.1:<port>/citation?lineage=<lineage_id>[&version=N]'
```
- 核对 `matches_index=true`；`excerpt` 是原文开头；`bytes` 是全文长度
- 回答格式：结论先行 + 每条证据一行（文件名、原文摘录、sha256 前 12 位）
- excerpt 只从原文开头截取——回答细节若超出 excerpt 范围，就说明证据不足，降级为「库里能确认的部分」，不许脑补

## 自检后才交回答
- 每条引文 matches_index=true；否则标注「账本不一致」并停止引用该条
- 401 = token 错（重新派生）；405 = 写动词被拒（网关只读，正常）；503 = 后端不可达（查 NAS）
- 零命中 ≠ 库里没有：先换同义/变体关键词再下结论（实测问「S1」零命中，库里术语是「N1–N11」「立项会前」，内容其实全在）。试过 2–3 个变体仍零命中才明说「库里没有」
- 回答超出 excerpt 开头范围时，用存储后端直读原文补全文：`kb_storage.FileStationBackend.from_env(prefix=<库>).read(<raw 相对路径>)`（scripts 目录入 sys.path），引文纪律不变
- 全景/盘点类问题：关键词检索之外，用同一后端 `be.walk_files('.')` 拉 raw 全目录树一次，比逐词猜快且全
- placeholder 占位件（png/zip/无后缀未转换）没有正文可查：如实说「只存档未转正文」，按文件名与上下文定位并声明局限，不假装读过
- 引用的是多版本 lineage 时用 `version` 参数取具体版本，默认最新
