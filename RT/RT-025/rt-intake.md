# RT-025 rt-intake：加密备份与 Clean-room 恢复

- 状态：planned（未实现、未备份、未恢复、未独立验收）
- Profile：Spec-Standard / Security-Critical
- 目标：从加密、密钥分离的备份在全新目录恢复权威状态，重建派生索引，
  并证明恢复不会复活撤权、跨 tenant 泄漏或自动开放 Broker/Scheduler。
- 依赖：RT-012～016、RT-018～021、RT-024；VG-C 与 RT-024 审计/基准
  receipt 必须可验证。
- 完成条件（**不含 VG-E**）：错误密钥/篡改/截断 fail closed；A-only restore 无 B
  产物；clean-room allow/deny/revoke/SHA/query 与恢复前等价；实测 RTO/RPO；
  RT-025 自身的独立验收明确 PASS。
- 门禁顺序（不可交换，禁止环）：RT-025 独立 PASS → 才执行 VG-E → VG-E receipt
  再作为 RT-026/G6 的输入。**VG-E 的 PASS 不是 RT-025 completed 的前置**。

## 一、范围

1. 加密 backup manifest、分离 key provider、备份/验证/恢复工具。
2. 权威/可重建/敏感数据矩阵，明确 staged event evidence、audit、scheduler
   state/retries、credential refs 的 include/exclude/restore 行为。
3. 全量与 tenant-selective clean-room restore；恢复顺序和安全暂停态固定。
4. SHA、profile/space、grant/tombstone、audit chain 和 Broker query 等价验证。
5. RTO/RPO 的真实测量与 receipt；不预填数字。

## 二、明确不做

- 不备份或恢复 AppKey/credential 明文、binding HMAC secret、签名私钥、
  backup KEK/DEK、完整 query、临时 URL 或 cache。
- 不在恢复后自动启用 Broker、Scheduler、collector、cron 或 production tenant。
- 不依赖原机器绝对路径，不原地覆盖源数据，不把派生索引当权威备份。
- 不执行生产部署或销毁任何现有目录。

## 三、拟议代码所有权

- 新增：`scripts/cwk_backup.py`、`scripts/cwk_restore.py`、
  `scripts/cwk_backup_crypto.py`。
- 新增：`contracts/rt025/schemas/`、`tests/test_rt025_*.py` 和精确 tracked 测试输入目录
  `tests/fixtures/rt025/`。该目录只存放合成、非生产、无真实凭据数据的 fixture；
  产生 RT-025 security receipt 时必须非空并存在于 `tested_subject_commit`。
- `tests/fixtures/rt025/` 不是 restore target；正式演练的运行时 clean-room target
  仍须由 `mktemp`（或等价安全 API）在仓库外创建独立临时目录，不得混同或复用。
- 只通过公开 reader/writer/validator 访问上游；不绕过 tombstone/ACL。

## 四、安全默认

- 备份数据与 key material 分离；manifest 不含密钥或 host absolute path。
- 未配置 production crypto provider、key unwrap 失败、manifest/segment 不一致、
  任何必需权威项缺失时均 fail closed。
- restore target 必须是新建且空的显式目录；Broker/Scheduler 状态写为 disabled/
  paused，credential refs 写为 `rebind_required` 直至外部 secret backend 复核。
- staged event evidence 恢复为 tenant-private quarantine，绝不进入 query/index。

## 五、回滚

销毁本次独立 clean-room 目录（验收临时数据），源备份与源 instance 不变；
删除工具不删除 backup/audit receipt。任何 material 删除需单独授权。
