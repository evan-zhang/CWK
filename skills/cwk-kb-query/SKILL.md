---
name: "cwk-kb-query"
description: "知识库问答或导入 CWK 授权文件：按库选择本地 token；v2 读链 + 融合搜索，实时引文，无命中直说"
---

# cwk-kb-query — 知识库问答（实时引文）

对已建知识库提问。铁律：**每个事实性回答必须带实时引文**——引文来自网关 v2 read 链（或 citation），sha256 是网关现场从存储后端拉字节算出来的；没有命中就直说没有，不许拿记忆或猜测作答。

## 网关拓扑（RT-049 合一，2026-09-06 起；RT-051 升级 1.2.0）

**8787 单进程多库网关（v1.3.0）**（192.168.91.72，launchd 常驻、开机自愈）：先用 discovery 取授权库，再以 `?kb=<kb_id>` 选库，kb_id 即 NAS prefix。

第 0 步（每次新接入或库变更后）：`GET /v2/kb/libraries`，携带绑定 token；只会返回该 token 的已挂载授权库。200 空列表不是越权，表示暂无交集；不再依赖本文的静态库清单。

- `cwork-3m`（个人工作协同近 3 个月）→ `http://192.168.91.72:8787`（不带 kb 参数的默认库，v1 兼容）
- `docdb-touqian`（投前流程系统建设）→ 同 8787 + `?kb=docdb-touqian`（8788 过渡期别名保留）
- `spbp-2027`（2027集团SP&BP）→ 同 8787 + `?kb=spbp-2027`（8789 过渡期别名保留）
- 新库不再开端口、不再写 plist：摄取 + 登记表一行后直接 `?kb=<prefix>` 查

```bash
curl -s -m 6 http://192.168.91.72:8787/health   # /health 免鉴权；ok=true 即用 OPS
```

语义要点（RT-049 定死，有单测钉住）：不带 kb = 主库 cwork-3m；kb 未挂载 → admin 见 404 `unknown_kb`（不回落主库、不回显挂载面），绑定 token 见 「03（scope 判定在挂载面之前）。

## 鉴权（绝不打印 Key/token 本身）

**通道 1：单库共享授权文件（接收方默认）**。

用户把 `.cwk-access.json` 附件交给 Agent 时，先本地导入，不读取或转述其中的 token：

```bash
cd <CWK仓库>
python3 scripts/kb_access_file.py import --file <附件本地路径>
```

- 本地权威目录是 `~/.openclaw/cwk/access/`（0700），每库一个 0600 文件；文件名由 `kb_id` 的 SHA-256 安全派生，不使用附件名或库名拼路径
- 同库已有不同授权时默认拒绝；用户明确同意替换后才加 `--replace`
- 导入成功后只对文件声明的 `kb_id` 执行一次 `capabilities --kb <kb_id>` 验证；不枚举或试探其他库
- `kb_gateway_client.py` 在没有显式 `CWK_KB_GW_TOKEN` 时，按每次请求的 `--kb` 自动选择对应本地授权；不跨库回落
- 401 表示文件已过期、撤销或换代；403 表示 token 有效但目标库不在其唯一授权面

**通道 2：Agent 绑定 token（旧流程兼容）**——单 Gateway 多 Agent 的多租户通道。

```bash
set -a; source ~/.openclaw/cwk/kb-bind.env; set +a   # CWK_KB_BIND_TOKEN 在这里
TOKEN=$CWK_KB_BIND_TOKEN
```

- 绑定 token 只能查 kb_ids 名单里的库，查别的库 → 403（跨库隔离，属正常）
- 吊销即刻生效（网关每请求重读登记表）；过期/被吊销 → 401
- 领 token：操作者在有业务 Key 的机器上 `python3 scripts/kb_token.py issue --verify-env <Key变量名> --agent-id <Agent标识> --kb-id <库>` 签发；token 明文只在签发回执出现一次，随后落 0600 文件

**通道 3：管理 Key 模仿（运维/调试用）**：

```bash
set -a; source ~/.openclaw/gateways/life/.env; set +a   # CWK_KB_ADMIN_KEY 在这里
TOKEN=$(printf '%s' "$CWK_KB_ADMIN_KEY" | shasum -a 256 | awk '{print $1}')
```

⚠️ 派生坑（实测）：`printenv` 带尾换行会算出错误 token（表现为 401 假象），必须 `printf '%s'` 无换行。派生后可查长度是否 64。（绑定 token 不需要派生，直接用）

## v2 受控读链（1.2.0 起，正文可达的正式路径）

八个操作全在 `/v2/kb/*` 路由：`capabilities / list / search / resolve / inspect / read / continue / renew`。POST 禁用，全 GET。合同详见 CWK 仓 `RT/RT-051/rt-lite.md` 的 C01–C06；参数表与易错点在本 skill `references/v2-read-chain.md`。

**流程：搜到 → 拿句柄 → 分页读全文 → 每页带引文**。不透明 document_ref（15 分钟 TTL，renew 可续）→ read 三模式互斥（cursor 续读 / start+end byte 范围 / 行模式）→ 每页自带 identity（lineage/version/raw_sha256）+ full_sha_verified=true（每请求全读全 SHA 复核）→ 融合命中带 candidate_spans（raw 绝对字节坐标，直接喂给 read 的 start/end_byte）。

**推荐入口：kb_wizard read-side 动词**（有 CWK 仓的机器；连接走环境变量，argv 零凭据，错误不带 URL，exit 0/1/2）：

```bash
cd <CWK仓库> && set -a; source ~/.openclaw/gateways/life/.env; set +a
export CWK_KB_GW_URL=http://192.168.91.72:8787 CWK_KB_GW_TOKEN=<绑定token或派生token>
python3 scripts/kb_wizard.py search --kb spbp-2027 --q 亿元 --retrieval-mode lexical_fusion_v1 --page-size 10
python3 scripts/kb_wizard.py open --kb spbp-2027 --lineage docdb:2091816...   # = resolve，拿 document_ref
python3 scripts/kb_wizard.py read --kb spbp-2027 --document-ref <ref> --max-bytes 65536
python3 scripts/kb_wizard.py continue --kb spbp-2027 --document-ref <ref> --cursor <next_cursor>
python3 scripts/kb_wizard.py inspect --kb spbp-2027 --document-ref <ref>
python3 scripts/kb_wizard.py renew --kb spbp-2027 --document-ref <ref>
```

**直接 HTTP**（无 CWK 仓的机器）：

```bash
# 搜正文（推荐默认）：融合 = 元数据子串 + 正文 BM25，RRF 排序
curl -s -m 300 -H "X-KB-Token: $TOKEN" \
  'http://192.168.91.72:8787/v2/kb/search?kb=spbp-2027&q=亿元&retrieval_mode=lexical_fusion_v1&page_size=10'
# 响应: items[].{lineage_id,document_ref,body_rank,metadata_rank,rrf_score,candidate_spans[]}

# 已知 lineage 直取
curl -s -m 300 -H "X-KB-Token: $TOKEN" \
  'http://192.168.91.72:8787/v2/kb/resolve?kb=spbp-2027&lineage=docdb:2091816...'

# 分页读正文
curl -s -m 300 -H "X-KB-Token: $TOKEN" \
  'http://192.168.91.72:8787/v2/kb/read?kb=spbp-2027&document_ref=<ref>&max_bytes=65536'
# 响应: {identity, full_sha_verified, returned_bytes, text, eof, next_cursor}
```

## 融合搜索语义（RT-051 C07）

- 两路并行：metadata 子串（标题/路径/lineage）+ 正文 BM25（中文 1/2/3-gram，ASCII 整词）；RRF=1/(60+rank) 融合，缺路 0
- **正文-only 词**（如「亿元」只出现在正文、标题没有的）融合模式可召回；metadata_rank=null = 纯正文路命中
- 编号查询是单一 ASCII 词项：查「017」不会命中「AB-017」
- 词法代未建/过时 → 503 lexical_unavailable：显式传 `allow_degraded=metadata` 降级，或通知运维跑 builder。**不要静默降级**
- 词法代与源同步：夜间 refresh（23:30）钩子自动重建；refresh 换代后旧候选自动失效
- 融合查询对高频词在百件库上需 60s+（P1a 边界）：curl 超时给 300s；交互场景先 metadata 后融合
- 零命中 ≠ 库里没有：换 2–3 个同义/变体词仍零才明说「库里没有」

## 引文与自检

- 每条引用的事实都要有 v2 read 页或 citation 支撑：核对 `full_sha_verified=true`（v2）或 `matches_index=true`（v1）；否则标「账本不一致」并停止引用该条
- 回答格式：结论先行 + 每条证据一行（文件名、原文摘录、sha256 前 12 位）
- citation（v1 兼容面保留）：`/citation?lineage=<id>&kb=<kb>` 实时全件 SHA + excerpt 前 500 字——短引文最省事
- placeholder 占位件（readable=false 或 reason=placeholder）：如实说「只存档未转正文」，不假装读过
- 401 = token 问题（管理通道先查尾换行坑）；403 = 跨库隔离；405 = 写动词被拒；503 lexical_unavailable = 词法代问题；连接被拒 = OPS 不通
- OPS 不可达（内网隔离/维护窗）才**兜底本机起网关**（仅限本机装有 CWK 仓库与凭据时）：

```bash
cd <CWK仓库> && set -a; source ~/.openclaw/gateways/life/.env; set +a
python3 scripts/kb_gateway.py --admin-key-env CWK_KB_ADMIN_KEY --backend nas \
  --prefix <prefix> --host 127.0.0.1 --port <port> &
```

（Agent 不直连 NAS 读全文——RT-051 起 v2 read 链是唯一正文路径，凭据不出 OPS 的红线不变。）

## 已知边界（如实交代，不冒充）

- 历史版本不可读：resolve 带 version≠当前 → 409（RT-051 合同：仅当前版）
- 非法 UTF-8 → 422；显式 end_byte 落码点中间 → 416 不静默前推
- 续读必须用 continue 动词（read+cursor → 400 invalid_cursor）
- cwork-3m / docdb-touqian 词法代**未建**（截至 2026-09-07）：融合查询 503，用 metadata 模式或找运维
- A04 大档（32/128MiB）/累计带宽优化属 P1b 快照架构，未交付
