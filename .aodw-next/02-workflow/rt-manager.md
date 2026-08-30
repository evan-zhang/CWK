# RT-Manager

和宪法打架时，听宪法。五问和授权也以宪法为准。
你判断这件事值不值得开 RT、该续做还是新建，并给出建议。不要把每件事都变成工单。

---

## 先判断在哪改

| 改什么 | 在哪 |
|---|---|
| 新 RT 的 `meta.yaml`（占号） | `main`，立刻提交 |
| `rt-lite.md`、方案讨论、实现 | worktree + `feature/RT-XXX-短名`，**立项后马上建** |
| 关 RT 的状态回填、复盘、索引 | `main`，收口时提交 |
| AODW 规则文件 | `main`，直接改直接提交，不必开 RT |
| 五问全否 | 轻量车道，当天合并 |

---

## 立项前先查

搜关键词和未走完的 RT，也扫 `RT/_deferred-items.md` 的未认领段。池子里已有同类问题：建议并进本 RT，或说明为什么仍要分开。

```bash
rg -il "<关键词>" RT/*/meta.yaml | head
rg -l '^status: (created|intaking|decided|in-progress|reviewing|paused)$' RT/*/meta.yaml
```

命中了：续做、认领台账，或说明为什么仍要新建。相关不等于重复。
同一验收目标没达成：重开原 RT，不要另开同目标工单。
多个小修复若共享一个验收目标，合成一个 RT。

写方案前真正打开会改到的入口，现状断言要带引用。

---

## 编号

取 `RT/` 目录和 `RT/index.yaml` 全部条目的并集，最大序号 + 1，补零到至少三位。
任一来源已占用就继续加。只扫目录会撞号。

---

## 创建

**分两步：`main` 上占号，worktree 里讨论。**

第一步——在 `main` 上只建 `meta.yaml` 并立刻提交，让编号可见：

```
RT/RT-XXX/
  meta.yaml          ← 只有这个进 main
```

第二步——马上建 worktree，`rt-lite.md` 和后续一切都写在里面：

```bash
git worktree add -b feature/RT-XXX-short-name .claude/worktrees/RT-XXX-short-name main
```

```
RT/RT-XXX/
  rt-lite.md         ← 在分支上写，收口时随分支合回
```

理由见宪法「交付形态」：`main` 上并发会话会互相卷提交，而号不落 `main` 会撞号。
两个坑都有实证，缺一不可。

- `profile: Spec-Lite`
- `execution_mode: collaborative`（不必问）
- `type` 只能是：`Feature` | `Bugfix` | `Refactor` | `Infrastructure` | `Design` | `Experiment`
- 状态、分支和 worktree 只写在 `meta.yaml`；`rt-lite.md` 不复制这三个字段。

状态：`created → intaking → decided → in-progress → reviewing → done`。
终态还有 `abandoned`、`parked-design-only`。暂停用 `paused`。

要动 `RT/` 以外的文件才建 worktree。

---

## 关闭

收口门前，对照 `rt-lite.md` 里用户能感知的成功标准，跑完匹配的检查并说明结果；此时不要提前把状态写成 `done`。

用户批准所选的合并／推送方案后，按 `git-discipline.md` 的统一顺序处理：先合并功能分支，再在 `main` 上写短 `retrospective.md`、更新 `RT/index.yaml`、补变更记录和 `closed_at`，最后把 `status` 改为 `done`。随后运行 `tools/rt-guard.sh`，提交收口记录，再打标签、推送和清理。

不要创建 `handoff/`。旧 RT 中已有的 `handoff/` 是历史材料，不要求回删。

认领了台账：`deferred_items_claimed` 必须和台账状态对上。没认领就不要填。

---

## 遗留事项（交接）

本 RT 的成功标准已经能验证，但做的过程中发现了不该扩大进本次的事，记为遗留事项。
**不是**把没做完的本目标拆出去关 RT。本目标没达成，就继续做或重开本 RT。

两处落点，缺一不可：

| 落点 | 作用 |
|---|---|
| 本 RT 的 `rt-lite.md`「遗留事项」 | 这次交接了什么，给人看 |
| `RT/_deferred-items.md` | 全仓库需求池 / 改进池。新建 RT 和定期选题都查这里 |

关闭前用人话告诉用户：本目标怎样算完成；还有几条不进本次、已准备放进需求池。用户点头后，再写入台账。

**写入台账时：**

1. 取 `RT/_deferred-items.md` 里 `### DI-` 的最大序号 + 1，不要复用已有编号。
2. 追加到「待认领」，标题行必须是 `### DI-0NN — 短标题`（门禁按这行认条目）。
3. 填全：发现于哪个 RT、问题是什么、为何不在本 RT 做、建议以后怎么处置、状态=未认领。
4. 本 RT 的 `meta.yaml.deferred_items_raised` 列出这些编号。没转出就不要写这个字段。

**认领时（新建 RT 或用户说从台账挑任务）：**

- 列出未认领条目：编号、标题、发现于哪次、当时为什么没做、建议处置。按会不会反复咬人排序，不要只丢编号。
- 用户选定后，台账状态改成 `已认领（RT-XXX，日期）`，本 RT 的 `meta.yaml.deferred_items_claimed` 写上编号。
- 关闭时：做完的改成 `已结清（RT-XXX）`；只做了一半，写明做了什么，剩下的留在台账里可继续认领。
