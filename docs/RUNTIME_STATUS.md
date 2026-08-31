# CWK 运行状态

> 生效日期：2026-08-18
> 当前生产配置：Local-First

## 当前决定

- 完整本地镜像是唯一权威持久主库。
- 所有生产自然语言查询使用 `cwk_wiki_query.py --mode local`。
- Cloud-First 持久化暂停。
- `cloud` / `shadow` 查询暂停。
- 云端查询对象目录发布暂停。
- raw 原文不上云。

## 继续保留的 DocDB 用途

- 派生 Wiki 的版本化备份；
- 每日 Markdown / HTML 页面发布与预览；
- history/events/entities/_index 等派生索引和运行回执备份；
- nightly 同步回执与有界重试。

这些用途不代表 DocDB 是知识主库，也不允许在本地 raw 缺失时以云端副本替代生产查询。

历史 DocDB 中可能仍保留旧名 `cwk-wiki-manifest.json`；当前发布对象为
`cwk-wiki-manifest-v2.json`。旧对象不删除、不覆盖，也不得作为生产状态或查询入口。

## 运行时保护

- `cwk_wiki_query.py --mode cloud|shadow` 默认直接拒绝；受控实验还需显式传入 `--experimental-cloud`。
- `cwk_nightly_pipeline.py --cloud-first` 默认直接拒绝；受控实验还需显式传入 `--experimental-cloud-first`。
- 独立同步器即使收到 `--allow-raw` 也会拒绝；受控实验还需第二道 `--experimental-cloud-raw`。
- nightly 默认不再生成或发布仅供 cloud-mode 使用的 `cloud-objects.json`；即使显式请求发布，也必须同时传入 `--experimental-cloud-query-catalog`。
- 只有同时解锁的 Cloud-First 隔离实验才会为实验 raw 构建对象目录；这不属于生产 nightly。
- nightly manifest 必须记录 `production_mode=local`、云端功能暂停状态和 DocDB 的派生副本角色。

## 重新启动条件

重新评估 Cloud-First 需要 Evan 明确授权，并至少重新完成：

1. 云端 raw 与索引闭包 100%，missing 和 SHA mismatch 均为 0；
2. 云端增量编译与单写者提交协议完成；
3. 本地/云端双轨一致运行不少于 14 天；
4. 云端作为默认入口稳定运行不少于 30 天；
5. 旧快照、新机器空目录和独立备份恢复演练全部通过；
6. 自然语言检索评测达到约定的召回、排序、拒答和引文支持指标。
