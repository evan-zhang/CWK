# 库与授权（RT-061）运维手册

给管理员看：库、成员、人员目录怎么管，现有令牌怎么迁过来，线上怎么切换、怎么回退。
设计理由见 `RT/RT-061/rt-lite.md`。

## 一句话模型

- **令牌证明"你是谁"**，**成员表决定"你能读哪些库"**。
- 身份只向玄关借（人员 ID、组织、真名），玄关空间的权限一概不用。
- 一个库可以有多位所有者；所有权只看成员表里 `role=owner` 的行。

## 文件

都在 OPS 的 `~/rt055-production/auth/registry/`，这个目录以只读方式挂进检索（8787）和问答（8790）两个容器。

| 文件 | 内容 | 谁写 |
|---|---|---|
| `rt055-tokens.json` | 令牌登记表（只存指纹，不存明文） | `scripts/kb_token.py` |
| `kb-authz.json` | 库、来源、成员、人员目录，带版本号和回执 | `scripts/kb_authz.py` |
| `kb-authz.json.lock` | 写入时的互斥锁 | `scripts/kb_authz.py` |

两个文件都是每次请求重读。改完之后等约 10 秒再验证（Docker 挂载同步有秒级延迟）。

## 三种角色

| 角色 | 能做什么 |
|---|---|
| `owner` | 查询；改来源；加成员、改角色、加别的所有者；归档库 |
| `writer` | 查询；往库里加内容（上传/摄取入口上线后才有实际用途） |
| `reader` | 只能查询 |

硬规则，代码里强制：

- 所有者不能降级或移除**别的**所有者，只能降级自己。管理员不受此限。
- 任何写入都不能让库没有所有者（管理员也不行）。转让 = 先加新所有者，再降级自己。
- 服务身份（如 `service:rag-answer`）只能是 `reader`，只有管理员能给它授权或移除它。
  **移除后该库的问答会中断**：问答服务用它去调检索。

命令行以"OPS 文件写权限持有人"的身份执行，也就是管理员。

## 主体（principal）的写法

- 人：`person:<组织ID>:<人员ID>`，两段都是数字字符串。
- 服务：`service:<服务名>`，如 `service:rag-answer`。

## 常用操作

以下命令都在 OPS 的 `~/rt055-production` 下执行。业务 Key 只通过环境变量名传入，绝不写在命令行上。

```bash
S=auth/registry/kb-authz.json

# 报到：用某人的业务 Key 经玄关核实身份，写进人员目录
python3 scripts/kb_authz.py enroll --store $S --verify-env <变量名>

# 看全貌 / 看某人能读哪些库
python3 scripts/kb_authz.py show --store $S
python3 scripts/kb_authz.py show --store $S --principal person:<组织ID>:<人员ID>

# 建库：同时指定第一位所有者，并给问答服务只读权限（别漏，漏了问答会中断）
python3 scripts/kb_authz.py bank-create --store $S --bank-id <库ID> --name <名称> \
  --owner person:<组织ID>:<人员ID> --service service:rag-answer --actor <你> --reason "<理由>"

# 加成员 / 改角色 / 移除
python3 scripts/kb_authz.py grant  --store $S --bank-id <库ID> --principal <主体> --role reader
python3 scripts/kb_authz.py revoke --store $S --bank-id <库ID> --principal <主体>

# 来源（目前只登记，不驱动摄取）
python3 scripts/kb_authz.py source-add --store $S --bank-id <库ID> --type docdb \
  --selector-json '{"space_id":"<空间ID>","path":"/某目录"}' --credential-env <采集Key的变量名>
```

基于某个版本做修改时加 `--expect-version N`。如果期间别人改过，这次写入会被拒绝，不会把别人的改动覆盖掉。

所有命令都只输出一个 JSON 对象。成功时返回码是 0；参数或权限错误返回 2；核对不通过返回 3。

## 令牌

`kb_token.py issue` / `reissue` 新增两个参数：

- `--authz listed`（默认）：只认令牌自带的 `--kb-id`，和 RT-061 之前一样。
- `--authz grants`：跟着成员表实时生效，加减权限不用重签。此时 `--kb-id` 只作为回滚快照；签发要求能从玄关解析出人员身份，解析不出就拒签。
- `--label`：持有人自取的显示名，不要用主机名。

服务令牌单独签，不占任何人的额度：

```bash
python3 scripts/kb_token.py issue-service  --registry auth/registry/rt055-tokens.json \
  --service rag-answer --kb-id cwork-3m --kb-id docdb-touqian --kb-id spbp-2027
python3 scripts/kb_token.py rotate-service --registry auth/registry/rt055-tokens.json --service rag-answer
```

现有的问答服务令牌（`rt055-internal-rag`）是 RT-061 之前按普通 Agent 令牌签的。迁移会把它认作
`service:rag-answer`，但记录形态不变，所以对它直接 `rotate-service` 会提示"还没有 token"。
要换成正式的服务令牌，按这个顺序：`issue-service` 签新的 → 写进 OPS 的 `deploy/.env`（`RAG_AUTH_TOKEN`）
并重建问答容器 → 确认问答正常 → 再 `revoke` 旧的那支。顺序反了问答会中断。

额度"每人 5 支"现在按人算：同一个人拿两把 Key 签，也只有一份额度；同一个 Agent 也不能用两把 Key 各持一支。

## 从现有令牌迁移

迁移只做一件事：把每支有效令牌归到"证明得了的人"名下，再按令牌自带的库生成成员条目。**不改变任何人今天能读什么。**

归属怎么证明（没有任何手填身份）：

- 令牌里的 `owner_ref` 是签发它的那把 Key 算出的指纹。迁移时把 Key 通过 `--verify-env` 交给脚本，脚本先经玄关核实这把 Key 是谁的，再重算指纹；对得上，才算"这支令牌是这个人的"。
- 服务令牌还要多一步：按给定的 Agent 标识重算绑定 ID，对得上才认作服务身份。
- 只要有一支有效令牌证明不了，迁移就整体不执行，两个文件都不动。

```bash
# key.env 里只有 Key 本身（一行裸值），不是 VAR=值 的格式，所以用 cat 读进变量，不要 source 它
export CWORK_APP_KEY="$(cat auth/key.env)"    # 签发现有令牌用的那把业务 Key
R=auth/registry/rt055-tokens.json; S=auth/registry/kb-authz.json

# 1. 先演练：只算不写，看每支令牌会归给谁
python3 scripts/kb_authz.py migrate --registry $R --store $S \
  --verify-env CWORK_APP_KEY --owner-env CWORK_APP_KEY --service rag-answer=rt055-internal-rag --dry-run

# 2. 正式执行（执行前备份两个文件）
cp -p $R $R.pre-rt061
[ -f $S ] && cp -p $S $S.pre-rt061
python3 scripts/kb_authz.py migrate --registry $R --store $S \
  --verify-env CWORK_APP_KEY --owner-env CWORK_APP_KEY --service rag-answer=rt055-internal-rag \
  --actor <你> --reason "RT-061 迁移"

# 3. 两道核对，都必须返回 0
python3 scripts/kb_authz.py check-equivalence --registry $R --store $S
python3 scripts/kb_authz.py check             --registry $R --store $S
```

`check-equivalence` 调用的就是线上的判定代码（`adapters/kb_auth.py`）。它逐支有效令牌、逐个库，比对两种模式放行还是拒绝，任何一处不同都会列出来。

## 切换到按成员表判定

前提：迁移完成，两道核对都通过，并且已获得产品负责人批准。

1. 在 OPS 的 `deploy/docker-compose.auth.yml` 里，给 `retrieval` 和 `rag-answer` 都加上：
   ```yaml
   RAG_AUTHZ_MODE: "grants"
   RAG_AUTHZ_PATH: /app/auth/kb-authz.json
   ```
2. 重建两个容器。注意必须带 `KB_BIND_ADDR=0.0.0.0`，否则服务会掉出局域网：
   ```bash
   cd deploy && KB_BIND_ADDR=0.0.0.0 docker compose -p rt055-production \
     -f docker-compose.yml -f docker-compose.rag.yml -f docker-compose.auth.yml \
     up -d --build --no-deps retrieval rag-answer
   ```
3. 复测：用现有令牌对三个库各查一次，结果要和切换前一样；不带令牌仍是 401。

`RAG_AUTHZ_MODE` 写错（比如写成 `grant`）时，所有请求都会被拒（401 `authz_mode_invalid`）。这是有意的：配置错了宁可全拒，也不静默套用某一种规则。

## 回退

- **最快的回退**：把 `RAG_AUTHZ_MODE` 改回 `scope`（或删掉这一行），重建两个容器。立刻恢复成只认令牌自带的库。
- **代价**：回退期间，成员表里的改动不生效。`--authz grants` 的新令牌会按签发时的 `--kb-id` 快照工作。
- **代码回退**：退回 RT-061 之前的镜像也可以。登记表 schema 版本没变，新增字段旧代码会忽略，已签发的令牌照常可用。

## 当前的边界

- **给别人报到还做不了。** `enroll` 需要本人的业务 Key 出现在 OPS 进程的环境变量里。局域网是明文 HTTP，不能让个人 Key 这样传。所以目前人员目录里基本只有管理员本人。这支 Key 签出的令牌照旧归管理员名下，靠 `--kb-id` 收窄范围。等有了加密的报到通道，才能真正"按人授权"。
- 建库目前只允许管理员执行。
- 来源只登记，不驱动摄取。摄取时的来源权限校验要到自助建库上线前才补。
- 检索和问答服务没有访问审计（为防正文泄露，服务端不记请求）。切换到 `grants` 前后，只能靠复测确认效果。
