# CWK 使用说明书

> 适用版本：当前 mainline 实现（2026-08-04）
> 适用对象：知识库使用者、部署人员、日常运维人员、问答 Agent

## 1. 你可以用 CWK 做什么

CWK 支持以下典型工作：

- 查找某个日期范围内的工作汇报；
- 汇总某个人、部门、产品、项目或主题的进展；
- 分析 Token、AI 费用、预算、异常和行动项；
- 区分实际数据、估算、预算与目标口径；
- 为结论提供 `report_id`、原文路径和逐字引文；
- 补采遗漏日期并验证源端、raw、Wiki 是否完全一致；
- 将经过编译的 Wiki 和每日摘要同步到 DocDB；
- 通过 nightly 自动完成当天采集、编译、审计和同步。

CWK 默认不能修改工作协同内容，也不能代替审批、回复或标已读。

## 2. 最快使用路径

如果本机已经部署并完成 nightly，只需运行：

```bash
cd /path/to/CWK
python3 scripts/cwk_wiki_query.py "你的问题" --mode local --top-k 8
```

例如：

```bash
python3 scripts/cwk_wiki_query.py \
  "7 月 10 日 OpenClaw Token 异常用户是谁" \
  --top-k 6
```

命令返回的是证据包。回答时只使用 `evidence_status=verified` 的结果，并引用 `report_id` 与本地 raw 路径。当前 Cloud-First 和 cloud/shadow 查询已暂停；生产查询不得调用云端模式。保留的实验代码只有在明确重启评审后才能通过额外实验开关使用。

## 3. 安装与初始化

### 3.1 环境要求

- macOS/Linux；
- Python 3；
- Git；
- OpenClaw CLI；
- 本机可用的 `cms-cwork-workflow`、`cms-docdb` 和鉴权能力；
- 有权限读取目标工作协同数据的 appKey。

模型精编还需要专用 `cwk-ai-reviewer` Agent。纯采集、完整性审计和可信检索不需要模型，也不需要 Docker。

### 3.2 克隆与安装检查

```bash
git clone https://github.com/evan-zhang/CWK.git
cd CWK
./install.sh
```

安装脚本会创建本地配置模板、编译 Python 文件并运行脱敏 smoke test，不会创建真实凭据。

### 3.3 创建私有配置

```bash
cp .env.example .env
cp skill/templates/CONFIG.example.json cwk-mirror.local.json
```

`.env` 和 `cwk-mirror.local.json` 已被 `.gitignore` 排除，不要强制提交。

推荐在 `.env` 配置：

```bash
CWORK_APP_KEY=你的工作协同AppKey
CWK_DOCDB_PROJECT_ID=
CWK_DOCDB_ROOT_FILE_ID=
```

个人知识库使用场景可以把两个 DocDB ID 留空，系统会查找或创建默认的 `工作协同镜像` 目录。

## 4. 配置说明

`cwk-mirror.local.json` 的关键配置：

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `detail_cap` | 60 | 每轮日报采集处理上限，不代表源端完整数量 |
| `continuation_cap` | 15 | 续报处理上限 |
| `backfill_enabled` | true | 是否进行轮转历史回填 |
| `backfill_cap` | 20 | 每轮历史回填上限 |
| `source_completeness` | true | 是否完整分页当天数据并启用完整性门禁 |
| `source_backfill_max_parallel` | 6 | 完整补采全文并发数 |
| `mirror_root` | 私有镜像路径 | raw 和 Wiki 的本地根目录 |
| `sync_docdb` | false | 是否同步 daily/runs/派生知识 |
| `sync_wiki` | false | 是否启用 Wiki 编译/重建/同步组合 |
| `wiki_limit` | 80 | 每轮最多处理的 Wiki 页面数 |
| `wiki_max_parallel` | 1 | Wiki 模型并发，允许 1–8 |
| `wiki_refine_fallbacks` | false | 是否在缺失页之后继续精编 fallback |
| `wiki_best_effort` | true | Wiki 质量增强失败是否保持主任务可用 |
| `ai_enabled` | false | 是否启用旁路 AI 理解与聚类 |

配置优先级通常是：命令行参数 → 配置文件 → 环境变量/默认值。个别布尔项按代码中的显式合并规则处理；生产变更前建议使用 `--help` 和一次 no-publish smoke 验证。

## 5. 知识镜像目录怎么读

```text
knowledge/工作协同镜像/
├── raw/
│   ├── YYYY-MM/YYYY-MM-DD/   # 按业务日存原文
│   ├── unknown/              # 无法确定日期的原文
│   └── _system/              # raw manifest
├── wiki/
│   ├── summaries/            # 每个 report_id 一篇摘要
│   ├── topics/               # 主题导航
│   ├── entities/             # 人/组织/产品/项目/系统导航
│   └── _system/              # Wiki 状态与问答契约
├── daily/                    # 每日 Markdown/HTML
├── history/                  # 脱敏历史派生页
├── events/                   # 事件候选
├── entities/                 # 规则抽取实体
├── _index/                   # 派生索引
└── runs/                     # 可浏览的验收页
```

日常判断依据：

- 查原始事实：看 `raw/`；
- 看单篇理解：看 `wiki/summaries/`；
- 看跨篇主题：看 `wiki/topics/`；
- 按人/组织/产品查：看 `wiki/entities/`；
- 判断任务是否健康：看 nightly manifest 和 coverage manifest。

## 6. 可信问答

### 6.1 基础查询

```bash
python3 scripts/cwk_wiki_query.py "下半年大模型预算最新口径" --top-k 8
```

### 6.2 日期范围

```bash
python3 scripts/cwk_wiki_query.py \
  "Token 消耗" \
  --from-date 2026-07-15 \
  --to-date 2026-07-31 \
  --top-k 20
```

### 6.3 指定发送人

```bash
python3 scripts/cwk_wiki_query.py \
  "AI 费用与模型预算" \
  --writer 屈军利 \
  --top-k 12
```

### 6.4 限定导航类型

```bash
python3 scripts/cwk_wiki_query.py "云端虾" --kind topic
python3 scripts/cwk_wiki_query.py "李文俏" --kind entity
python3 scripts/cwk_wiki_query.py "财务审核" --kind summary
```

### 6.5 JSON 输出

```bash
python3 scripts/cwk_wiki_query.py \
  "Token 异常" \
  --format json \
  --output runs/query-token-anomaly.json
```

### 6.6 参数说明

| 参数 | 作用 |
|---|---|
| `--top-k` | 返回候选汇报数量 |
| `--max-evidence` | 每篇汇报最多返回的 raw 引文数 |
| `--from-date` / `--to-date` | 按业务日期筛选 |
| `--writer` | 按发送人精确筛选 |
| `--kind` | 限定 summary/topic/entity 或全部 |
| `--min-score` | 最低相关性阈值 |
| `--format` | Markdown 或 JSON |
| `--output` | 写入文件，不指定则输出终端 |

### 6.7 回答协议

上层 Agent 组织答案时必须遵循：

1. 只用 `evidence_status=verified`；
2. 核心结论至少带 `report_id`；
3. 重要数字附原文引文；
4. 说明检索时间范围和覆盖边界；
5. 区分实测、估算、预算、申请和目标；
6. 对互相冲突的材料分别陈述，不擅自合并；
7. `confidence=none` 时拒答或调整关键词重新查；
8. 不因为第一个结果置信度高，就自动采信后续低相关候选。

检索采用词法排序，结果中可能出现同词噪声。需要“所有相关汇报”时，应提高 `top-k`、加日期范围，并人工/Agent 逐条检查标题与 raw 引文。

## 7. 检查 Wiki 完整性

### 7.1 本地结构 lint

```bash
python3 scripts/cwk_wiki_query.py --lint
```

通过标准：

- 无重复 report ID；
- 所有 summary 都能找到 raw；
- 引文能在 raw 中验证；
- topic/entity 不存在悬空 summary 引用。

### 7.2 运行 smoke test

```bash
make wiki-smoke
```

### 7.3 指定日期源端对账

```bash
python3 scripts/cwk_source_coverage_audit.py \
  --start-date 2026-08-04 \
  --end-date 2026-08-04 \
  --strict \
  --manifest-out runs/coverage-20260804.json
```

`--strict` 下，只要存在缺失 raw 或 summary，命令就以非零状态退出。

## 8. 补采遗漏数据

### 8.1 按日期范围完整回填

```bash
python3 scripts/cwk_backfill_range.py \
  --start-date 2026-07-01 \
  --end-date 2026-07-31 \
  --run-name backfill-202607 \
  --max-parallel 6
```

脚本会：

- 完整分页日期范围；
- 校验源端总数；
- 只抓 raw 中缺失的 ID；
- 获取全文并暂存；
- 按业务日晋级 raw；
- 输出 `runs/backfill-202607/backfill-manifest.json`。

若 `remaining_missing > 0`，命令退出码为 2，不能认为回填完成。

### 8.2 手工晋级已有暂存文件

```bash
python3 scripts/cwk_raw_store.py \
  --source-dir runs/some-collect/collected-raw \
  --mirror-root knowledge/工作协同镜像 \
  --manifest-out runs/raw-promotion.json
```

不要手工把文件塞进某个采集日期目录；日期由原文字段解析。

## 9. 编译与维护 Wiki

### 9.1 补齐缺失 summary

```bash
python3 scripts/cwk_cloud_wiki_compile.py \
  --mirror-root knowledge/工作协同镜像 \
  --model newapi/BD-MiniMax \
  --repair-model newapi/BD-glm \
  --limit 80 \
  --max-parallel 4 \
  --manifest-out runs/wiki-compile.json
```

### 9.2 不调用模型，仅创建 fallback 导航页

```bash
python3 scripts/cwk_cloud_wiki_compile.py \
  --mirror-root knowledge/工作协同镜像 \
  --fallback-only \
  --limit 200
```

适用于先保证覆盖，再逐步偿还精编质量债务。

### 9.3 精编 fallback

```bash
REFINE_FALLBACKS=true \
LIMIT=8 \
MAX_PARALLEL=4 \
MAX_BATCHES=1 \
scripts/cwk_wiki_batch_driver.sh
```

默认只改本地。仅当页面已复核并希望同步 DocDB 时设置 `SYNC_WIKI=true`。

### 9.4 修复 Wiki manifest

```bash
python3 scripts/cwk_cloud_wiki_compile.py \
  --mirror-root knowledge/工作协同镜像 \
  --reconcile
```

它会以磁盘 summaries 为准修复 compiled/refined/fallback 分区，不会调用模型。

### 9.5 重建主题和实体

```bash
python3 scripts/cwk_cloud_wiki_topics_entities.py \
  --mirror-root knowledge/工作协同镜像 \
  --min-topic-reports 2 \
  --min-entity-reports 2 \
  --manifest-out runs/topics-entities.json
```

## 10. 运行 nightly

### 10.1 本地脱敏 smoke

```bash
python3 scripts/cwk_nightly_pipeline.py \
  --config cwk-mirror.local.json \
  --run-name nightly-smoke-$(date +%Y%m%d-%H%M) \
  --date $(date +%F) \
  --source-dir tests/smoke/raw \
  --no-publish-mirror
```

### 10.2 生产只读运行

```bash
python3 scripts/cwk_nightly_pipeline.py \
  --config cwk-mirror.local.json \
  --run-name nightly-$(date +%Y%m%d-%H%M) \
  --date $(date +%F) \
  --sync-wiki \
  --sync-docdb
```

默认 `source_completeness=true` 时会完整分页当天记录，并在 Wiki 编译后执行严格门禁。

### 10.3 带有限精编预算

```bash
python3 scripts/cwk_nightly_pipeline.py \
  --config cwk-mirror.local.json \
  --run-name nightly-$(date +%Y%m%d-%H%M) \
  --date $(date +%F) \
  --sync-wiki \
  --sync-docdb \
  --wiki-refine-fallbacks \
  --wiki-limit 8 \
  --wiki-max-parallel 4 \
  --wiki-model newapi/BD-MiniMax \
  --wiki-repair-model newapi/BD-glm
```

## 11. 判断 nightly 是否成功

打开：

```text
runs/{run-name}/nightly-pipeline-manifest.json
```

至少检查：

- `overall_pass=true`；
- `source_completeness_failures=[]`；
- `source_coverage.complete=true`；
- `source_coverage.missing_raw_count=0`；
- `source_coverage.missing_summary_count=0`；
- `sync_failures=[]`；
- AI 启用时根据用途判断 `degraded`；
- `mutating_cwork_commands_called` 为空；
- DocDB retry queue 为空或有明确补偿计划。

注意：Cloud-First 下 `wiki_best_effort` 默认为 false。AI 精编失败可以保留 fallback，但源端/raw/summary、云端对象覆盖和索引提交任一门禁失败，nightly 必须报告失败。

## 12. 同步 DocDB

### 12.1 dry-run

```bash
python3 scripts/cwk_sync_mirror_to_docdb.py \
  --mirror-root knowledge/工作协同镜像 \
  --only-prefix wiki/ \
  --dry-run \
  --manifest runs/docdb-wiki-dry-run.json
```

### 12.2 增量同步变化页面

```bash
python3 scripts/cwk_sync_mirror_to_docdb.py \
  --mirror-root knowledge/工作协同镜像 \
  --only-prefix wiki/ \
  --paths-manifest runs/wiki-changed-paths.json \
  --manifest runs/docdb-wiki-sync.json
```

已存在页面使用新版本更新。失败路径会进入 `runs/docdb-sync-retry-queue.json`，下轮自动补偿。

禁止用 `--only-prefix raw/` 进行生产或共享同步。Cloud-First 已暂停；受控实验必须同时满足：Evan 明确授权、个人 DocDB、显式 `--allow-raw --experimental-cloud-raw`、`--physical-prefix raw/`、`isSensitive=0`、同步回执与云端覆盖审计。同步器会拒绝把 raw 写入未授权项目；超过 2MB 的 raw 自动转成内容寻址 gzip 分片，目录提交后查询端再重组并核验逻辑原文 SHA。上述限制控制写入目标和完整性，不对工作协同正文做涉密判断或内容改写。

### 12.3 Cloud-First 查询与写后读（已暂停，仅保留实验说明）

下列命令不属于生产运维。只有 Evan 明确重启 Cloud-First 评审后，才允许在隔离实验中使用第二道解锁参数：

```bash
python3 scripts/cwk_wiki_query.py \
  "Token 消耗异常" \
  --mode cloud \
  --experimental-cloud \
  --min-index-version 9 \
  --top-k 8
```

- 云端启动时下载 3 个约 1–1.5MB 的索引分片，逐片校验后重组压缩索引；Top-K raw 按需下载并按 SHA-256 校验。
- 缓存只使用 `file_id + sha256` 命名，可随时删除并从云端恢复。
- `--min-index-version` 用于读后即见；云端版本较旧时命令直接失败。
- `--mode shadow --experimental-cloud` 返回本地/云端 report ID 排名差异，不改变答案证据边界。
- 若实验需要由 nightly 发布云端查询对象目录，必须同时使用 `--publish-cloud-query-catalog --experimental-cloud-query-catalog`；单独设置配置项或环境变量会被拒绝。

### 12.4 云端覆盖审计

```bash
python3 scripts/cwk_cloud_coverage_audit.py \
  --mirror-root knowledge/工作协同镜像 \
  --prefix raw/ --prefix wiki/ \
  --live \
  --retry-queue runs/docdb-sync-retry-queue.json \
  --output runs/cloud-coverage.json
```

`--live` 是默认模式，审计会逐对象下载并核验字节哈希，同时断言目标是当前鉴权用户的个人私有项目，并要求 retry queue 为空。`--no-live` 只用于诊断，硬门禁结果始终为失败。只有 `overall_pass=true` 才允许提交 nightly 成功。

### 12.5 空目录恢复

```bash
restore_dir=$(mktemp -d)
python3 scripts/cwk_restore_from_docdb.py "$restore_dir" \
  --prefix raw/ --prefix wiki/ \
  --min-index-version 9 \
  --max-parallel 8 \
  --output runs/cloud-restore-drill.json
```

删除本地持久镜像前必须完成两次不同空目录的恢复演练，且所有文件哈希一致。

此外必须验证本地 live scan 与持久索引在不少于 20 个固定问题上的 Top-8 聚合重合率：

```bash
python3 scripts/cwk_shadow_consistency.py \
  --mirror-root knowledge/工作协同镜像 \
  --top-k 8 --min-overlap 0.99 \
  --output runs/shadow-consistency.json
```

## 13. 测试与发布前检查

```bash
make test
make wiki-lint
make wiki-smoke
git diff --check
```

生产验收还应增加：

```bash
python3 scripts/cwk_source_coverage_audit.py \
  --start-date $(date +%F) \
  --end-date $(date +%F) \
  --strict
```

## 14. 常见故障处理

### 14.1 提示缺少 `CWORK_APP_KEY`

- 确认 `.env` 存在；
- 确认变量名正确；
- 或在当前 shell 执行 `export CWORK_APP_KEY=...`；
- 不要把真实 key 写入文档、命令日志或 Git。

### 14.2 源端数量和 raw 不一致

1. 运行 `cwk_source_coverage_audit.py --strict`；
2. 查看 `missing_raw_ids`；
3. 对相同日期执行 `cwk_backfill_range.py`；
4. 再审计一次。

### 14.3 raw 完整但 summary 缺失

1. 运行 `cwk_cloud_wiki_compile.py`；
2. 模型不可用时加 `--fallback-only` 先补覆盖；
3. 运行 `--reconcile`；
4. 重跑 lint。

### 14.4 模型调用超时

- 检查 `cwk-ai-reviewer` 是否为零工具 Agent；
- 确认 `sandbox.mode=off`，不应依赖 Docker；
- 确认模型仅为 `newapi/BD-MiniMax` 或 `newapi/BD-glm`；
- 降低并发或缩短单批量；
- 失败时保留 fallback，不删除已有 summary。

### 14.5 查询结果噪声较多

- 增加日期范围；
- 指定 `--writer`；
- 使用更具体的项目名或事件名；
- 提高 `--min-score`；
- 提高 `top-k` 后逐条依据 raw 引文筛选；
- 不把 topic/entity 页面正文当成最终证据。

### 14.6 DocDB 同步失败

- 查看本轮同步 manifest；
- 检查 retry queue；
- 先 dry-run 验证目标项目和根目录；
- 恢复外部服务后重跑相同 prefix/paths manifest。

### 14.7 日期进入 `raw/unknown/`

说明原文未提供可解析的接收、汇报或创建日期。不要按采集日强行归档；应检查原文元数据或建立人工修复记录。

## 15. 安全操作清单

可以做：

- 查询、采集、回填、审计；
- 本地编译和检索；
- 同步经过允许的派生 Wiki；
- 对页面创建新版本。

不能默认做：

- 标已读；
- 回复工作协同；
- 审批或驳回；
- 完成待办；
- 删除记录；
- 上传 raw 原文到共享知识库；
- 将真实 appKey、prompt、模型输出或个人镜像提交 Git。

## 16. 日常操作速查

```bash
# 查问题
python3 scripts/cwk_wiki_query.py "问题" --top-k 8

# 查完整性
python3 scripts/cwk_wiki_query.py --lint

# 查当天源端覆盖
python3 scripts/cwk_source_coverage_audit.py \
  --start-date $(date +%F) --end-date $(date +%F) --strict

# 补一个日期范围
python3 scripts/cwk_backfill_range.py \
  --start-date YYYY-MM-DD --end-date YYYY-MM-DD

# 完整 nightly
python3 scripts/cwk_nightly_pipeline.py \
  --config cwk-mirror.local.json \
  --run-name nightly-$(date +%Y%m%d-%H%M) \
  --date $(date +%F) --sync-wiki --sync-docdb

# 全套测试
make test && make wiki-lint && make wiki-smoke
```
