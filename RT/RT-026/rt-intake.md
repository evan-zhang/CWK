# RT-026 rt-intake：试点准备、影子切换与回滚工具

- 状态：planned（未实现、未切换、未独立验收）
- Profile：Spec-Standard / Release-Critical
- 目标：在所有真实 tenant 默认关闭的前提下，交付可执行的影子读、legacy/new
  查询差异、统一 feature switch、go/no-go、doctor/install compatibility 与
  一键 legacy rollback；完成仅表示 `READY_FOR_G7_REVIEW`——即"**可被提交到 G6
  最终验收**"，**不表示**已获 G7 授权，也**不表示**可以跳过 G6 直接申请 G7。
- 硬依赖：RT-016、RT-018、RT-023、RT-024、RT-025，VG-A～VG-E 全部独立
  PASS，G1～G5 所需 receipt 可验证。**G6/G7 不是 RT-026 的输入**（它们是下游）。
  - VG receipt 的精确来源：`contracts/gates/gate_registry_v1.json` 的 `receipt_path`
    （`gate-receipts/VG-{A..E}/receipt.json`，schema `cwk.pr001.verification_gate_receipt.v1`）。
  - G1～G5 receipt 的精确来源：`contracts/gates/release_gate_registry_v1.json` 的
    `receipt_path`（`release-gate-receipts/G{1..5}/receipt.json`，
    schema `cwk.pr001.release_gate_receipt.v1`），**恰好五份**。两个根互不相交。
  - `release-gate-receipts/` **当前不存在**，故 G1～G5 现在全部 NOT_RUN；
    RT-026 只消费，**不得创建或补写**其中任何文件。
- 完成条件：合成双租户完整 E2E、legacy/new 数据与查询 diff、默认关闭、
  rollback、全库/Wiki/nightly fixture/secret scan 独立 PASS。不得自动进入
  M4/G7/真实试点。
- 门禁顺序（不可跳级，不可合并，不可倒置）：

  ```
  RT-026 独立验收 PASS
      -> 产出终态 READY_FOR_G7_REVIEW（仅"可提交"，非授权）
      -> G6 最终验收：由新鲜最终独立验收者签发，RT-026 不签发、不代签、不推断
      -> G7 release readiness：必须在 G6 之后，且必须有 Evan（用户）显式授权
  ```

  - `READY_FOR_G7_REVIEW` 是 RT-026 完成时**唯一**允许的终态标签，其语义严格为
    "本包已具备被提交进入 G6 最终验收的形态"。
  - RT-026 **不得跳过 G6**，**不得直接申请或自称获得 G7 授权**，**不得**把
    `READY_FOR_G7_REVIEW` 表述为 `G7_AUTHORIZED`、`RELEASE_APPROVED` 或任何等价物。
  - G6 与 G7 都**不是** RT-026 的输入（见"硬依赖"），也都**不由** RT-026 签发；
    RT-026 的 go/no-go 评估器只读消费 G1～G5，对 G6/G7 既不读也不写。

## 一、范围

1. 空 allowlist、独立 shadow schedule、默认关闭 release switch。
2. legacy/new 查询等价比较器：候选、命中、证据、拒答理由和允许差异分类。
3. go/no-go evaluator：消费 RT-024/025 实测及全部 gate receipts，不内置虚构
  数值；有任何硬 blocker 即 NO_GO。
4. install/doctor/兼容入口与一键恢复 legacy read path 的 runbook/tool。
5. `cwk_tenant_cmd_release.py` provider，以及显式的 RT-012 CLI slot 兼容修订。
6. 在 `cwk_release_switch.py` 内实现 neutral `PilotAdmissionProviderV1` 的唯一
  production adapter/factory；按 `collector_run/profile_workflow/query_broker` 构造绑定，
  只从当前 switch、仓库外验真 G7、target/flags/allowlist 派生短期快照。

## 二、明确不做

- 不启用真实用户、不注入真实 AppKey、不修改生产 cron、不部署/重启 Gateway、
  不写 Cloud/DocDB、不自动变更 tenant 到 pilot/active。
- 不执行 2～3 人 14 天或 5～10 人 30 天试点；这些属于 G7 后 release activity。
- 不把短 smoke、合成数据或开发完成表述为生产就绪/GA。
- 不删除新 evidence、audit、backup、route 或旧 legacy 数据。

## 三、CLI 兼容修订

当前 RT-012 dispatcher 的 `FROZEN_PROVIDER_SLOTS` 仅注释预留 release slot，
provider 文件单独落地无法被加载。RT-026 实施前必须二选一：

1. 由 RT-012 owner/兼容审查提交一项显式、可审计的 ABI 兼容修订，只将
   `cwk_tenant_cmd_release` 加入固定 slot 并追加 policy v1 ordinal 2 evolution receipt/migration note（中央 policy/pin/guard 不变）；或
2. 在 RT-026 中以独立 commit 标记 `RT-012 compatibility revision`，只做同一
   行 slot 激活，不改变 dispatcher、搜索路径、错误语义或 provider ABI。

不得偷改 dispatcher、动态扫描插件或绕开 provider registry。若兼容修订未获
独立审查，release CLI 保持不可用且 RT-026 不得 completed。

## 四、安全默认

- master flag=false、read path=legacy、所有新组件=false、allowlist=[]。
- shadow 只读新链并写 diff receipt，不影响用户响应、legacy writer 或 cron。
- pilot 变更要求外部 G7 authorization receipt；本 RT 只用合成 test receipt
  验证状态机，production mode 不接受 test signer。
- production PilotAdmission adapter 只在 `mode=pilot` 且 G7 target、component flag
  与 tenant allowlist 全部匹配时 admitted；off/shadow、过期/撤销/重放、
  revision 回退或坏配置都拒绝。adapter 不签发 G7，不读真实 key/token；
  G7 仍被 go/no-go 输入集排除，避免 DAG 自环。
- rollback 原子把 master 关、read path 设 legacy、停止新 schedule；不删数据。

## 五、拟议代码所有权

- 新增：`cwk_tenant_cmd_release.py`、`cwk_release_switch.py`、
  `cwk_shadow_query_diff.py`、`cwk_go_no_go.py`。
- `cwk_release_switch.py` 同时是 production PilotAdmission adapter/factory 的唯一 owner；
  不新增平行的 private allowlist API，不改 Wave-0 neutral Protocol/schema/test。
- RT-026 独占的有限适配：nightly/install/统一 flag/legacy compatibility；任何
  生产配置写入仍不在本 RT 授权范围。
- 新增 `contracts/rt026/schemas/` 与 `tests/test_rt026_*.py`。

## 六、回滚

执行原子 `off + legacy read`，停用独立 shadow/pilot schedule；保留所有新
evidence、audit、diff 和 backup receipt。现有 legacy nightly 行为保持原样。
