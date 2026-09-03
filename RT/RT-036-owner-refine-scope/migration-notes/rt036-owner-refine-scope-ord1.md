# RT-036 ord1：cwk_cloud_wiki_compile.py 增加精编范围策略

- from_sha256（原 pin）：2e0c89e4b0c54259887300efb35f4053f075140be47710e1806a418aba8ab5af
- to_sha256（新 pin）：27932db3991003ccf7ec51e91965df1aecd7bbb3fe7ab5731220ffa123e0a624

## 行为差异

默认（--refine-scope all、--min-year 0）与旧版选择行为完全一致，属于纯增量能力。
宿主 cron 采用 `--refine-scope owner --refine-owner-name <主人> --refine-owner-emp-id
<工号> --min-year 2026` 后：AI 精编仅覆盖主人相关汇报；范围外 missing 记录物化为
fallback 页（覆盖契约不变，outcomes 记 scope_excluded=true）。

## 回滚

去掉四个新参数即回到旧行为；本 ord 不修改任何既有参数语义。

## 风险控制

不动 DEFAULT_MODEL/DEFAULT_REPAIR_MODEL 字面量（high_risk 关注点），不动模型允许
清单与零工具策略校验。
