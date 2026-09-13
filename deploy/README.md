# RT-055 OpenSearch 双通道检索部署

本目录只提供可移植部署工件和上线步骤，不执行任何生产部署。服务默认只绑定
`127.0.0.1`；只有显式修改 compose 端口映射后才会对外暴露。

## 组件与配置

- OpenSearch 2.15 单节点镜像在构建阶段安装 `analysis-icu`，运行时不联网安装插件。
- 检索服务只使用 Python 标准库，提供 `POST /query`、`GET /healthz`、`GET /readyz`。
- 端点、索引名、租户、库注册表、端口和 `top_k` 由环境变量配置，模板见
  `adapters/opensearch_retrieval/config/example.env`。
- OpenSearch 用户名和密码只能通过环境变量注入；不要写入 compose、镜像、命令行或仓库。
- `OPENSEARCH_JAVA_OPTS` 默认 1GB heap；目标机至少预留 1–2GB heap 对应的内存，并按机器余量调整。

## Mac 路径（Docker Desktop + compose）

```bash
cd <repo>
cp adapters/opensearch_retrieval/config/example.env .env.retrieval
# 按环境变量方式加载 .env.retrieval；不要提交该文件
export $(grep -v '^#' .env.retrieval | xargs)
docker compose -f deploy/docker-compose.yml up -d --build
python3 -m adapters.opensearch_retrieval build-index --input tests/fixtures/rt055_retrieval_synthetic.json
python3 -m adapters.opensearch_retrieval smoke-test --expect-doc-id synthetic-rt055-001
```

Docker Desktop 需要给 OpenSearch 足够内存；`docker compose ps`、
`curl http://127.0.0.1:9200/_cluster/health` 和
`curl http://127.0.0.1:8787/readyz` 是本机确认路径。若需要登录即启动检索服务，Mac 可将
同一条 `python3 -m adapters.opensearch_retrieval serve` 命令放入用户级 `launchd` plist，
并把 `WorkingDirectory` 和环境变量指向本地私有配置；plist 不应进入仓库，本次交付不加载
或重启任何 launchd 服务。

## Linux 路径（compose）

```bash
cd <repo>
# OpenSearch 宿主机通常需要：sudo sysctl -w vm.max_map_count=262144
cp adapters/opensearch_retrieval/config/example.env .env.retrieval
# 以 systemd/environment-file 等受控方式加载，不把凭据写入仓库
export $(grep -v '^#' .env.retrieval | xargs)
docker compose -f deploy/docker-compose.yml up -d --build
python3 -m adapters.opensearch_retrieval build-index --input tests/fixtures/rt055_retrieval_synthetic.json
python3 -m adapters.opensearch_retrieval smoke-test --expect-doc-id synthetic-rt055-001
```

Linux 目标机先确认内存、磁盘和 `vm.max_map_count`；本工件不改远端主机、不改 NAS、
不改 OPS，也不重启既有服务。

## 三阶段发布路径

1. **Shadow（影子并跑）**：摄取经过授权的库快照，新旧检索同时接收同一批查询，只记录对账，
   不切流。连续运行至少 24 小时且错误数为 0；后端不可用必须计为错误，不能折算成
   `no_answer`。
2. **灰度**：显式指定一小部分查询进入双通道，逐库对账 `recall`、`exact`、`no_answer`、
   错误率和延迟。灰度指标不得低于考试基线：`cwork-3m` 为 `92%/100%/100%`，
   `docdb-touqian` 与 `spbp-2027` 三项均为 `100%`；另满足全局 recall 不低于 90% 的等同门槛。
3. **切换**：只有 shadow 和灰度均达标、回滚演练完成后才全量切换。旧检索路径保留一个版本，
   以便切回；本次代码交付不执行切换。

索引重建使用稳定的 parent/chunk ID 和 `index` bulk 操作，可重复执行；父文档展开前会校验
租户、库、文档身份。exact 查询零命中直接返回 `no_answer=true`，不把 lexical 失败静默
转换为空答案；OpenSearch 或索引未就绪统一走 503。

## 验收记录

生产验收由部署窗口另行记录。至少保留：shadow 运行起止时间与零错误计数、三库灰度逐项
指标、旧路径回滚结果、`/healthz` 与 `/readyz` 结果，以及索引模板和配置版本。测试只能证明
合成夹具和 API 契约，不能替代真实语料上的 24 小时 shadow 或灰度验收。
