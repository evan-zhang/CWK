# Git Discipline

授权边界见宪法。你执行 Git。收口时一次问清合并、推送和清理；不要拆成三次确认。
确认话术不超过三句，说清能不能撤销，以「要我现在执行吗？」结尾。不要把 diff 念给用户听。

---

## 在哪改

| 改什么 | 在哪 |
|---|---|
| 新 RT 的 `meta.yaml`（占号） | `main`，立刻提交 |
| `rt-lite.md`、方案讨论、实现 | worktree + `feature/RT-XXX-短名`，**立项后马上建** |
| 关 RT 的状态回填、复盘、索引 | `main`，收口时提交 |
| AODW 规则文件（`.aodw-next/`、`AGENTS.md` 协作条款） | `main`，直接改直接提交，不必开 RT |
| `RT/` 以外，五问全否 | `fix\|chore\|refactor\|docs\|perf\|test/<短名>`，做完走收口门合并 |

主仓库保持在 `main`。不要在主仓库 `git checkout -b` 建 feature 分支，用下面这条一步完成：

```bash
# 先确认 main 没有未提交改动；有改动就先查清归属，不要自动 stash
git status --short

# RT 车道
git worktree add -b feature/RT-XXX-short-name .claude/worktrees/RT-XXX-short-name main

# 轻量车道示例；目录名去掉斜杠
git worktree add -b fix/short-name .claude/worktrees/fix-short-name main

git worktree list
```

之后代码都在这个 worktree 里改。建之前看有没有别的活跃 worktree 在改同一批文件。没有重叠就继续。有重叠：带建议问一次（并行还是等），不要默默抢同一批文件。活着的 worktree 多，只在方案里说明并行风险，不要单纯因为数量停下来。

```bash
git worktree list
# 每个活跃 worktree 相对 main 改了哪些非 RT 文件
for w in $(git worktree list --porcelain | grep '^worktree' | cut -d' ' -f2); do
  b=$(git -C "$w" branch --show-current)
  echo "--- $b"
  git diff main..."$b" --name-only 2>/dev/null | grep -v '^RT/'
done
```

可直接提交 `main` 的只有：RT 占号与状态回填、看板快照重生成、AODW 规则文件改动。这些自己做。
**方案讨论不在此列**——它在 worktree 里做，理由见宪法「交付形态」。
工作树本来就有别人的改动时，只暂存本任务的明确文件；提交前用 `git diff --cached --name-only` 核对，不得用全量暂存把别人的改动带进去。

---

## 提交和标签

```text
<type>(<scope>): <subject>

Refs: RT-XXX
```

`type`：`feat` `fix` `docs` `style` `refactor` `perf` `test` `chore`。
写清 why。RT 车道必须带 `Refs: RT-XXX`；轻量车道没有 RT，不要编一个。

RT 合并后打 `done-RT-XXX`。合并用 `--no-ff`。琐碎提交可 squash，关键逻辑提交留着。

---

## 收口

目标达成、检查已跑之后，一次给出三个选择并带建议：

1. **合并、推送并清理（通常推荐）**：主线和远端立即生效，本地工作目录删除。
2. **只在本地合并**：主线本地生效，不推远端；适合还要离线复核。
3. **先保留分支**：不合并、不推送、不清理；适合目标还未最终确认。

说明本次推荐和可撤销性，以「要我现在执行哪一种？」结尾。用户选定后只执行对应动作，不把合并、推送和清理捆成不可拆的套餐。

### RT 车道

执行前先确认功能工作树已提交、主仓库在 `main` 且干净。发现未提交改动时先查归属并停下处理，不自动 stash，不覆盖别人工作。

```bash
git status --short
git pull --ff-only origin main
git merge --no-ff feature/RT-XXX-short-name
```

合并后，在 `main` 上写 `retrospective.md`，更新 `RT/index.yaml` 和变更记录。有遗留事项则写入 `RT/_deferred-items.md`，编号记进 `meta.yaml.deferred_items_raised`。把 `meta.yaml` 的 `status` 改为 `done` 并填写 `closed_at`。然后按顺序执行：

```bash
bash .aodw-next/tools/rt-guard.sh --root . --rt RT-XXX
git add RT/RT-XXX RT/index.yaml RT/_deferred-items.md
git diff --cached --name-only
git commit -m "docs(rtxxx): close RT-XXX"
git tag done-RT-XXX
```

仓库没有 `CHANGELOG.md`，或本次没有对应变更记录文件时，不要为了照抄命令新建；只暂存真实更新的文件。用户选择推送时再执行 `git push origin main` 和 `git push origin done-RT-XXX`。用户选择清理时再移除 worktree 和本地功能分支。

标签必须指向包含最终 `done` 状态和复盘记录的提交，不能提前打。行为或模块职责变了才更新模块文档和 `modules-index.yaml`。

### 轻量车道

轻量车道没有 RT 状态、复盘文件和完成标签。验证通过后按用户选择合并、推送或保留分支；需要清理时再移除对应 worktree 和短命分支。

创建新 worktree 前，或用户提到清理分支时：feature 分支超过 7 天没提交，或落后 `main` 超过 50 个提交，列出来让用户决定，不要自己删或 rebase。

---

## 容易踩坑的两处

**stash：** 只在同一条命令链里即存即取，用 `&&`。`stash pop/apply` 必须带 `stash@{N}`，先 `git stash list`。发现存量 stash：转成 `archive/stash-...` 分支后再 `git stash clear`。优先用 worktree 做对照，不要用长期 stash。

**生成快照：** 功能分支里不要顺手重生成看板快照一类入库产物。要刷新，在 `main` 上单独提交。冲突时以 `main` 为准。
