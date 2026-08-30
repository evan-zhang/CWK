# U1 任务包 — fixture：零命中判据与正文自冲突（G112 事故原型）

> 冻结件（fixture 数据，复刻 RT-125 unit-U5 事故形态，非真实任务包）。

## 1. 任务

把 `rules.md:66` 的「CSF 战略检查（plan 批准前必须执行）」改为
「CSF 战略检查（Spec-Full plan 批准前必须执行；Spec-Lite 可选但推荐）」。

## 2. 硬约束

1. 就地替换，行数不变。

## 3. 出口判据

1. `grep -n "plan 批准前必须执行" rules.md` → **0**（改前 1）
2. `grep -rn "aodw-skill guard" docs/` → **0**（改前 3）

> 溯源：rt-plan.md v1 U1 判据 2 条 → 本包 2 条（新增 0、合并 0、删除 0）
