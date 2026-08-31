# CWK 遗留事项台账

> AODW 接入于 2026-08-31。这里记录“当前 RT 的成功标准已达成，但不应扩大进本次范围”
> 的后续事项；它不能用来拆走未完成的本 RT 目标。
>
> 条目格式由 `.aodw-next/02-workflow/rt-manager.md`「遗留事项」规定，门禁 G114/G115
> 按 `### DI-0NN` 标题行和下面的「发现于 / 状态」两行认账。改格式前先看那两条判据。

## 待认领

### DI-001 — RT-022 与 RT-026 的脚本演化预留槽位已被占用

| 字段 | 内容 |
|---|---|
| **发现于** | RT-029（AODW 产品基线收敛），2026-08-31 |
| **状态** | 未认领 |
| **建议认领时机** | RT-022 或 RT-026 立项时一并评估，不要等到实现中途才发现槽位不可用 |

产品基线 `fe4add1` 里有两处 2026-08-30 的日常维护改动，动了受
`pr001-script-evolution-v1` 契约管辖的脚本，但当时没有留下演化回执：

- `c26c7ad`「按作者筛选汇报列表」改了 `scripts/cwk_wiki_query.py`（策略把该文件
  的唯一演化槽位 stage-06 预留给 RT-022）；
- `dc96c28`「云 Wiki 主力模型切到 glm-5.3-flash」改了
  `scripts/cwk_nightly_pipeline.py`（stage-08，预留给 RT-026）。

RT-029 的处理：策略已冻结，且回执 schema 把 `owner_rt` 限定为策略声明值，
没有任何方式把这两处改动登记到 RT-022/RT-026 之外的名下。为避免重签已签名证据，
RT-029 用预留槽位如实登记了真实的 `from`/`to` 哈希转移，并在两份 migration note
的「Provenance」小节里写明这不是该 RT 的工作。

遗留问题：`max_ordinal` 均为 1，槽位现已用尽。**当 RT-022 真正实现 Query Broker、
或 RT-026 真正实现试点影子切换时，若需再改这两个文件，必须先修订
`policy_v1.json`**，而修订会牵动已签发的 stage-09/stage-10 回执、两份独立验收报告、
安全登记表常量和 guard helper 里的人工审查基准值。

### DI-002 — 两个 legacy frozen 脚本的基线指纹已重新锚定

| 字段 | 内容 |
|---|---|
| **发现于** | RT-029（AODW 产品基线收敛），2026-08-31 |
| **状态** | 未认领 |
| **建议认领时机** | 下一个要改 `legacy_frozen_files` 名下脚本的 RT |

同一次模型切换 `dc96c28` 还改了 `scripts/cwk_ai_common.py`（模型允许清单从
2 个扩到 4 个）和 `scripts/cwk_cloud_wiki_compile.py`（两个默认模型字面量）。这两个
文件属于安全登记表的 `legacy_frozen_files`，**没有**演化回执机制可用。

RT-029 的处理：只更新了安全登记表里这两条指纹，使其描述已获批准的基线。核对过：
这两个文件不在 RT-016 genesis 表、不在 `companion_immutable_paths`、不在 VG-A 已签
收执的 artifacts 列表里，登记表本身的哈希也没有被任何地方固定，因此该操作没有重写
任何已签名证据。门禁机制未改动，对后续漂移仍然 fail closed。

遗留问题：`legacy_frozen_files` 缺少「有主的演化路径」。今后再有人改这 53 个文件，
仍然只能靠改指纹来放行，缺少 migration note 与验收测试的约束。建议后续 RT 评估是否
把高风险项（尤其是承载模型允许清单的 `cwk_ai_common.py`）迁入有回执机制的管辖。

### DI-003 — `pre-commit` hook 在本仓库无法安全安装

| 字段 | 内容 |
|---|---|
| **发现于** | RT-029（AODW 产品基线收敛），2026-08-31 |
| **状态** | 未认领 |
| **建议认领时机** | 用户决定 `main` 落点、原 PR-001 工作树处置完毕之后 |

AODW 的 G001 判据检查 `git rev-parse --git-common-dir` 下有没有 `pre-commit`
hook，未装则每个 RT 报一条告警。CWK 的实际情况是：`main` 检出、RT-028/RT-029 的
worktree 与原 PR-001 工作树共用同一个 git common dir
（`/Users/evan/.openclaw/.../CWK/.git`）。hook 是 common dir 级别的，装一次对**所有**
worktree 生效——包括用户明确要求保持原样、不得写入的 PR-001 工作树。

RT-029 的处理：不装。G001 是告警级，不阻断；`make aodw-check` 会照实报出来。
用「为了消一条告警而去动受保护工作树的提交路径」交换是明显的坏买卖。

遗留问题：等 `main` 的落点定了、原 PR-001 工作树处置完毕，再决定是否启用 hook。
在那之前防绕底线层没有生效，RT 门禁只在有人主动跑 `make aodw-check` 或 CI 时执行。

## 已认领 / 已结清

（当前无。）
