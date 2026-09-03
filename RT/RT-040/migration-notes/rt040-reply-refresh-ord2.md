# RT-040 ord2：cwk_cloud_wiki_compile.py 的 vN 版本择新（高风险成员点名说明）

- from_sha256（原 pin）：27932db3991003ccf7ec51e91965df1aecd7bbb3fe7ab5731220ffa123e0a624
- to_sha256（新 pin）：472e9fc4269b5060265298a42ce6f2f6fbec2633bde4d246ea923c8880e5e842

## 为什么必须动这个高风险文件（overlay 要求 RT 点名说明）

本文件在 script-evolution-v2 的 high_risk_members 里（两个默认模型字面量，
dc96c28 前科）。本次改动**一个字节都不碰**模型字面量、允许清单或任何 AI
调用语义；改的是 candidate→by_id 的文件选择逻辑。但它正好住在同一文件里，
而 RT-040 ord2 的核心正确性依赖这一处：回复刷新写入 `<id>-v2-<标题>.md`
新快照后触发重编译，编译器必须选**版本号最大的**快照作为编译源。旧实现
`by_id = {meta["report_id"]: raw for ...}` 是纯 last-wins，而 Python 排序里
`-v2-`（0x2d）排在 CJK 标题前，导致 plain 原文后写入、覆盖 v2——重编译
永远编译旧快照，回复内容到不了 Wiki。已在线上实测复现（2026-09-03：
summary 的 source 指针落在原文而非 v2）。

## 行为差异

仅当同一 report_id 同时存在 `<id>-标题.md` 与 `<id>-vN-标题.md`（N≥2）时，
by_id 现在选版本号最大者；只有 plain 文件时行为与旧实现逐字节等价
（单文件无 tie-break 可发生）。模型清单、默认模型字面量、选择优先级
（missing → fallback → failure-queue）、manifest 结构、CLI 全部不变。

## 回滚

revert 本提交即可；v2 快照文件不受影响（它们只是普通 raw 文件）。

## 风险控制

不碰模型相关任何内容（high_risk 的关注点）；新增 `_version_of` 为纯函数、
只读文件名；配套断言见 tests/test_rt040_reply_refresh.py 的
CompilerTieBreakTests（含 plain-only 等价性金样例）。
