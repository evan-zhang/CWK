# RT-021 rt-intake：多知识空间 Projector 与索引

- 状态：planned（仅契约；尚未实现、测试或独立验收）
- Profile：Spec-Standard
- 依赖：RT-014、RT-015、RT-020；RT-020 必须独立 PASS。
- 实现 Agent：`agent-rt021-impl`
- 独立验收 Agent：`agent-rt021-verify`

## 1. 目标

让一份共享 canonical evidence 支撑同一 tenant 的多个逻辑知识空间，不复制 raw。RT-021 实现 space registry/config/membership、summary/topic/entity/search projection、archive/review view、可重建索引，以及撤权、route 退出、Profile rollback、space disable 的清理消费者。

## 2. Space 首期冻结决策

- 创建主体：仅可信宿主机 `tenant_owner` 或 `platform_admin` actor，或绑定该 owner confirmation 的 Profile activation transaction；AI proposal 本身不能创建/激活 space。
- durable 状态仅 `active | disabled`；新 space 由已确认 activation 原子创建为 active，允许 `active → disabled → active`，不提供 delete。
- `space_id = "sp_" + base32(random 128 bit)`，其中 `base32` 固定为 RFC 4648 小写、去 padding、字符集 `[a-z2-7]`；ID 稳定且不因 rename 改变。Profile 流程中的候选 ID 由 RT-019 宿主机 `SpaceIdFactoryV1` 产生，RT-021 只在激活时校验未占用并注册；直接 owner 创建则复用同一 factory。AI、slug、标题均不能决定 ID。
- slug 必须是 1～32 位 lowercase ASCII，grammar `^(?=.{1,32}$)[a-z](?:[a-z0-9-]*[a-z0-9])?$`；输入不自动 lowercase/trim，非法直接拒绝。
- 保留 slug 固定为 `all/shared/raw/system/archive/review`，active/disabled/历史 tombstone 均不得占用。
- slug 在 tenant 内对 active/disabled/历史 tombstone 全局唯一；rename 后旧 slug 首期不可复用，避免陈旧 client 指向另一个 space。
- 每 tenant 首期最多 32 个 durable space（active + disabled）；超限 fail closed，不自动归并/删除。
- 首期禁止嵌套：schema 不包含 parent/children，deep-forbidden/semantic validator 拒绝任何 hierarchy 字段或 space-to-space membership。

## 3. 必须交付

- SpaceRegistry/SpaceRecord/SlugTombstone/SpaceChangeReceipt。
- SpaceMembership、ProjectionManifest、SearchIndexManifest 与 current pointer/历史。
- summary/topic/entity/search projection；archive/review tenant views。
- RT-010 entity catalog 与 Wiki search index 的 tenant/space 适配（RT-021 独占修改）。
- route request/projector provider、全量 deterministic rebuild、撤权与 route/profile/space cleanup。
- ProjectorJobProviderV1（遵守 RT-018 ABI），但生产注册留到 RT-026 composition root。
- 零漂移消费 neutral PilotAdmission ABI；Projector 构造绑定
  `purpose=profile_workflow`，只对 `pilot` 在 membership/projection/index pointer 提交前
  重验；`active` 保持既有语义且准入调用为 0。`profile_pending` 不是
  RT-021 membership/projector 的合法运行状态，不得借该 ABI 放宽。
- `ProfileSpaceSnapshotProviderV1` 生产 adapter（`cwk_space_snapshot_adapter.py`）：
  ABI 与 `cwk.profile_space_snapshot.v1` schema 由 **RT-022 唯一 owner** 冻结，
  RT-021 只按位零漂移消费同一 Protocol/schema 文件，不得在 `contracts/rt021/schemas/`
  重定义同名 schema。adapter 纯只读，一次返回 profile SHA/版本、default 与 queryable
  active opaque space IDs、membership/index versions；0 space 时仍带非空 `profile_sha256`。
- RT-007/008/010、Wiki smoke 与双租户/多空间/恶意测试。

## 4. 非目标

- 不复制 canonical raw，不创建独立事实库，不物理删除 raw/grant。
- 不实现 Query Broker、可信身份/transport 或沙箱入口（RT-022/023）。
- 不使用仓库级 `config/entity-family-registry.json` 作为多租户权威配置。
- 不修改 legacy nightly、生产 cron、installer 或 release flag。
- 不接受 slug 作为持久 membership/index/cache key。
- 不实现 PilotAdmission production adapter，不读 RT-026 私有 release/allowlist 配置。

## 5. 完成与 VG-C

RT-021 必须独立 PASS 后再执行 VG-C。VG-C 证明授权样本→Profile→Preview/Route→多空间投影→撤权/route退出/Profile回滚清理的完整合成链。RT-021 PASS 不自动等于 VG-C/G4；VG-C 也不代表真实沙箱查询或生产部署。
