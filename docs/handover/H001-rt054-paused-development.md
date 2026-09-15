---
title: RT-054 大库词法融合检索性能治理暂停开发与接管交接
status: active
kind: session
created: 2026-09-08
last_verified: 2026-09-08
owners: [CWK]
rt: RT-054
related_items:
  - RT-051: 同族
  - RT-052: 同族
scope: >
  本文覆盖 2026-09-07 至 2026-09-08 的 RT-052 收口后性能问题识别、RT-054 方案与独立评审，以及 Evan 决定暂停当前开发并转交其他 Agent 的现场。接手者应先读 §6，再按 §7 复核，不得把已有方案稿误认成已获实现授权。
related:
  - .aodw-next/01-core/aodw-constitution.md
  - .aodw-next/01-core/ai-interaction-rules.md
  - .aodw-next/06-project/ai-overview.md
  - RT/_deferred-items.md
---

# RT-054 大库词法融合检索性能治理暂停开发与接管交接

> 交接对象：后续负责 CWK 检索性能治理的 Agent
> 落笔日期：2026-09-08 ｜ 工作发生于 2026-09-07 至 2026-09-08
> **落笔前已重新核实仓库、分支、PR、Agent 与检查状态，差异见 §6。**

## 0. 一句话

RT-052 已完成并上线；它暴露出的 cwork-3m 融合检索超时被独立为 RT-054。RT-054 目前只完成两版方案与一次首版独立评审，**没有产品代码、测试或 OPS 改动**。Evan 于 2026-09-08 决定停止在当前 OpenClaw/Discord 会话内继续开发，保留现有分支、worktree 和 PR #6，交给其他 Agent 接手。

## 1. 我们之前做了什么

### 1.1 已完成：RT-052 发现端点与 OPS 盘点面

- PR #4 已合并，merge commit `7fc0d7c`：新增 KB 发现端点与 OPS 状态面。
- PR #5 已合并，merge commit `0033b26`：修复 OPS 对 canonical `tokens` 与旧 `records` 登记表结构的兼容读取。
- 2026-09-07 的生产验收记录：三实例版本 1.3.0；cwork-3m、docdb-touqian、spbp-2027 均为 `ready + up_to_date`；登记表可用、挂载无差异。
- cwork-3m 词法代构建完成：482 docs、66,500 chunks；固定查询 `q=会议` 返回 HTTP 200、`total=121`，但冷路径耗时 425.685 秒，随后一笔在 300 秒客户端默认超时。
- 上述性能问题不影响 RT-052 的发现/盘点合同完成，但构成后续性能治理的起点。

### 1.2 已完成：RT-054 首版方案

- `03382b7`：新增 RT-054 方案、meta、RT 索引和模块索引，只改文档与治理登记，不改产品代码或测试。
- 分支：`feature/RT-054-lexical-snapshot-performance`。
- worktree：`/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK/.worktrees/openclaw-worktree-rt054-performance-spec`。
- PR #6：`https://github.com/evan-zhang/CWK/pull/6`。

### 1.3 已完成：首版独立 Codex 评审

首版裁决为 **GO-WITH-CHANGES**，明确当前不允许实施或合并。八项阻断意见为：

1. 先做单变量分段计时，不能预设 NAS 为首因；
2. 快照必须同时修正评分算法；
3. 补齐单库写锁、stale/epoch fence、S0/S1 双检、返回前指针与授权复核；
4. `query_corpus_digest` 覆盖全部查询可见字段；
5. 消除循环哈希，严格约束 immutable snapshot、locator、span 和回放；
6. 补齐 single-flight、旧代退休、每库/进程全局内存上限；
7. 把有界下载与流式 SHA 纳入存储读侧，确保解析前限额可证明；
8. 补齐代码/指针回滚与可执行性能、故障、突变验收。

评审同时发现关键静态热点：`scripts/kb_lexical.py` 的 `bm25_rank()` 对每个 chunk 调 `chunk_score()`，而 `chunk_score()` 重算遍历全库的 `avgdl` 并重复 tokenize query，最坏近似 O(chunks²)。因此 425.685 秒不能只归因于 NAS 下载，必须由 P0 分段实测确认占比。

### 1.4 已完成：按评审修订第二版方案

- `108b02a`：吸收全部八项阻断意见，新增 P0 诊断门、postings 驱动评分、单一 pointer/epoch、双 collect、不可变快照、受限流式读、缓存与回滚合同，以及可量化验收。
- 当前方案权威文件：worktree 内 `RT/RT-054/rt-lite.md`，关键词可搜 `P0：先测量，再决定`、`单一权威`、`epoch fence`、`可执行验收合同`。
- `RT/RT-054/meta.yaml` 当前状态为 `intaking`，明确写着方案门未通过。
- 2026-09-08 复核运行：`git diff --check` 通过；RT guard 13 PASS、0 error、1 个既有 pre-commit hook WARN；`make governance-audit` 通过，736 个受跟踪文件均有治理归属。

## 2. 我们是怎么做的、怎么查的

**方法论重点是把“观察到很慢”“静态可疑路径”和“最终根因”分开。**

1. 先固定生产事实：同一库、generation、query、page size、返回 total、冷/热状态和超时边界。
2. 再做静态调用链核验：区分 search 路径与 builder/read 路径，避免把候选正文 SHA 复核错误归因到 search。
3. 用单变量实验验证算法复杂度：计数型容器证明 1,000 chunks 触发 1,000 次全表 `avgdl` 扫描；2k/4k/8k 的纯内存样本呈接近四倍增长。
4. 把 P0 和 P1b 拆开：P0 只负责测量并允许推翻假设；P0 数据未通过实施门前，不锁定 P1b 文件格式和实现。
5. 用安全不变量反推架构：授权先于 I/O、GET 零持久写、only-current、generation/digest fail-closed、document_ref 与 span/read SHA 合同不得因性能优化被削弱。
6. 方案由独立只读 Codex 审查；写方案的 Agent 不自批。

接手者可先复算当前 Git/PR 状态：

```bash
ROOT=/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK
WT="$ROOT/.worktrees/openclaw-worktree-rt054-performance-spec"
git -C "$ROOT" status --short --branch
git -C "$WT" status --short --branch
git -C "$WT" log --oneline origin/main..HEAD
gh pr view 6 --repo evan-zhang/CWK --json state,mergeable,headRefOid,statusCheckRollup,url
```

方案文档门禁：

```bash
WT=/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK/.worktrees/openclaw-worktree-rt054-performance-spec
git -C "$WT" diff --check origin/main...HEAD
bash "$WT/.aodw-next/tools/rt-guard.sh" --root "$WT" --rt RT-054 --format json
LC_ALL=C make -C "$WT" governance-audit
```

注意：`make aodw-check` 在本机需带 `LC_ALL=C`；不要为消除既有 G001 warning 去安装共享 git common-dir 的 pre-commit hook，`RT/_deferred-items.md` 的 DI-003/EX-001 已解释原因。

## 3. 我们希望解决的问题是什么

目标不是简单让一次请求“不超时”，而是把大库词法融合检索变成可证明、可换代、可回滚且不会泄漏旧结果的受控查询路径：

- 找出 425.685 秒在下载、JSON 解析、结构恢复、metadata、BM25、span、RRF 中的真实分布；
- 消除当前近似 O(chunks²) 的重复评分，并避免每次完整下载/解析同一大索引；
- 使 source 更新、builder 发布、gateway 缓存和撤权形成统一的 epoch 状态机；
- 在损坏、截断、旧指针回放、源换代、撤权、双 builder、双冷请求和容量超限时 fail-closed；
- 保持 RT-051/052 的只读、授权、only-current、SHA 与无隐式降级合同。

明确不做：不靠增大 timeout 冒充优化；不把 P1a 正文分页成本混入；不引入向量/FTS5、多线程共享 FileStation session、网关持久写 cache；不触碰 RT-053 的业务改动。

## 4. 当前我们发现了什么问题

### 4.1 已知正在进行但没做完

- 第二版方案 `108b02a` 已写完，但**尚未进行第二次独立评审**；因此方案门仍未通过。
- PR #6 仍 OPEN，但当前 `mergeable=CONFLICTING`，没有针对最新状态的 CI check。
- RT-054 branch 的 worktree 干净，HEAD 与远端分支一致；没有未提交代码。
- RT-054 的 OpenClaw/Codex 会话均已终止或完成；没有活跃 Agent、shell 进程或 RT-054 自动化。

### 4.2 方案仍待数据证明的核心问题

- O(chunks²) 静态热点已证实存在，但它对生产 425.685 秒的实际占比未测。
- NAS 下载、登录/重试、JSON decode、结构恢复、metadata 和 span 各段耗时、请求数、字节数、RSS 峰值未测。
- source writer 是否都能接入同一 stale/epoch fence、StorageBackend 是否能提供解析前有界流式读，尚未实现和验证。
- 方案中的冷/热阈值是实施门目标，不是已达成结果。

### 4.3 风险与阻断

- **方案门阻断**：第二次独立评审未完成，禁止产品实现。
- **Git 冲突**：RT-054 从 `0033b26` 分叉；main 已前进到 `09c8aff`（PR #7 / RT-053）。两边共同修改 `RT/index.yaml`，这是当前可复算的冲突交集。解决时必须同时保留 RT-053 和 RT-054 条目。
- **生产状态未在 2026-09-08 重验**：最后一份可引用的生产验收是 2026-09-07。交接编写只做本地与 GitHub 只读核验，没有连接 OPS/NAS。
- **公开文档边界**：不得写入 token、Key、Cookie、密码、`.env` 内容、真实知识库原文或敏感日志；性能记录也不得含 query 原文、标题或路径。
- **并行资产边界**：不要清理、重置、stash、覆盖或合并其他 worktree。仓库存在多个历史/并行 worktree，其中部分为 prunable，但清理仍需 Evan 明确授权。

本次未新增 DI 台账项。RT-054 与 RT-051/052 是同族链路，不是把未完成目标转出后假称 RT-054 已完成。

## 5. 下一步我们计划做什么

| 优先级 | 具体动作 | 完成证据 |
|---|---|---|
| P0 | 在接手者获得继续开发授权后，将 `origin/main` 安全整合到 RT-054 分支，仅解决已知 `RT/index.yaml` 冲突并保留双方登记 | worktree clean；branch 基于最新 main；`git diff --check`、RT guard、governance audit 通过；PR #6 不再 conflicting |
| P0 | 对 `108b02a` 重新做一次独立只读架构评审 | 明确 GO / GO-WITH-CHANGES / NO-GO；GO 前不改产品代码 |
| P0 | 若评审 GO，只实施默认关闭、零持久写的 P0 分段计时与离线同结果对照 | 原始样本、阶段和误差、logical/physical NAS 次数、字节、重试、RSS；旧/新结果逐字段一致 |
| P1 | 依据 P0 数据确认或修改 P1b 设计；两大假设都不成立时回方案门 | 数据能支持最终实现；不得用 timeout 掩盖 |
| P1 | 实现 postings 驱动评分、单一 pointer/epoch、写锁/fence、immutable snapshot、single-flight、受限流式读与内存硬门 | 故障/突变测试和原有 RT-051/052/存储回归通过 |
| P2 | 跑完整 CI 与真实大库冷/热验收 | cwork 冷态 5/5 ≤10s；热态 30 次 P95 ≤2s、max ≤3s；请求、字节、内存与 fail-closed 门全部达标，或按数据回方案门 |
| P3 | 只有 Evan 另行批准后才合并、推送实施分支和部署 OPS | PR/CI/merge 回执、备份、文件 SHA、逐实例健康、真实冷/热验收 |

## 6. 你接手时的现状变化（最重要，先读这节）

落笔前 2026-09-08 已复核，较 2026-09-07 工作发生时有以下变化：

1. main 不再是 `0033b26`，现为 `09c8aff`，因为 PR #7（RT-053 单库授权文件 MVP）已合并。
2. RT-054 不再停在首版 `03382b7`；第二版 `108b02a` 已提交并推送，确实吸收独立评审的八项阻断意见。
3. PR #6 仍 OPEN，但从此前 `MERGEABLE + CI green` 变为 `CONFLICTING + 当前无 checks`。不要复述旧状态。
4. main 与 RT-054 branch 均为 clean；RT-054 worktree 仍存在。
5. 仓库根策略仍为 `manual`，RT-054 worktree 的集成策略为 `pr-required`。
6. 没有运行中的 coding agent 或 shell process。任务相关的 spec/review 会话均已终止或完成。
7. 存在一个与 CWK 运营相关的晨间汇报自动化，但它不是 RT-054 开发任务，也未因本次暂停而修改；其最近一次运行成功。
8. 2026-09-07 的 OPS 健康和性能数据未在交接日重验，接手者不能把它描述为 2026-09-08 当前生产状态。

## 7. 接手清单（照着做）

1. **先确认任务归属**：Evan 所说暂停的当前项目按本会话上下文识别为 CWK/RT-054。如实际另指一个名为“绘画”的项目，立即停止并要求提供那个项目路径；不要把本包套用过去。
2. 完整读取 `.aodw-next/01-core/aodw-constitution.md`、`.aodw-next/01-core/ai-interaction-rules.md`、`AGENTS.md`、`.aodw-next/06-project/ai-overview.md`。
3. 完整读取本交接包和 RT-054 worktree 内 `RT/RT-054/meta.yaml`、`RT/RT-054/rt-lite.md`。
4. 重跑 §2 的 Git/PR 命令；如果 main/PR/branch 与 §6 不同，以现实为准并先更新交接记录。
5. 在未获继续开发授权前，只做只读复核；不要改产品代码、测试、OPS、NAS、timeout 或 RT-053。
6. 获准后先把最新 main 整合进 RT-054 分支。只解决真实冲突，重点核对 `RT/index.yaml` 同时保留 RT-053/054；不要用 checkout/reset 覆盖整文件。
7. 复跑 `git diff --check`、RT guard、`LC_ALL=C make governance-audit`，更新 PR #6；推送与 PR 外部写遵守接手环境的授权边界。
8. 对第二版方案重新做独立只读评审。只有 GO 才进入 P0；GO-WITH-CHANGES 继续只修方案；NO-GO 则停下带证据报告。
9. P0 先测量、再决定，禁止直接照方案实现 P1b。性能样本必须脱敏，不记录 query、token、标题、路径、原文或凭据。
10. 实现后先完成本地/CI/突变验证；生产部署、合并、worktree 清理必须单独获得 Evan 授权。

## 8. 相关提交索引

| commit | 内容 |
|---|---|
| `7fc0d7c` | PR #4 合并：KB 发现端点与 OPS 状态面 |
| `0033b26` | PR #5 合并：OPS canonical token registry 兼容修复；RT-054 分叉基线 |
| `03382b7` | RT-054 首版性能治理方案 |
| `108b02a` | RT-054 第二版：吸收 GO-WITH-CHANGES 八项阻断意见 |
| `09c8aff` | PR #7 合并：RT-053 单库授权文件 MVP；当前 main |
