# RT-Lite: RT-064 - 主线 CI 假红收口

> profile: Spec-Lite | execution_mode: collaborative

## 方案（给人看）

- 做什么：让自动检查不再因为「云端没有第三方校验库」和「云端不是苹果电脑」而报红。
  相关旧实验用例在缺条件时跳过，而不是整组报错。
- 为什么：真问题会被假红淹没；RT-057 已修掉另一类，这类是当时故意留下的。
- 代价：这些旧实验台账用例在云端不会再跑（本机有条件时仍会跑）。
- 这次故意不做什么：不加第三方依赖；不改检索/问答服务；不改生产配置。
- 用户怎样算成功：之后每次推送，GitHub 自动检查变绿。
- 建议：定论（产品负责人已批准）。

## 假设与现状

- 失败清单来自 CI run 35219740174：约 9 条缺 jsonschema、3 条 Linux 无 sandbox-exec、
  1 条聚合退出码 3（缺库连锁）。
- 同仓库其它用例已有「缺 jsonschema 就跳过」「非 darwin 跳过 sandbox」写法，本 RT 对齐。

## 实现备注

- 模块顶层 `import jsonschema` 改成可缺失；缺了则整模块/整类跳过。
- 行内 `import jsonschema` 一律包 `skipTest`。
- `sandbox-exec` 实测用例加与 `test_rt055_privacy_recovery` 相同的 macOS 门闩。

## 验证

- 在没有 jsonschema 的解释器上跑原先失败的测试文件 → 应为 skip / OK，无 ERROR/FAIL。
- 有 sandbox-exec 的本机仍能跑沙箱用例（若本机具备）。
- `make aodw-check` / 针对改动文件的 unittest。

## 变更记录

- 用户能感到什么：推代码后检查不再无故发红。

## 遗留事项

- 无。
