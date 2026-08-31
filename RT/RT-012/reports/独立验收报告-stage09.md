# RT-012 Stage-09 独立验收报告

- 验收日期：2026-08-21
- 独立验收人：`agent-rt012-stage09-independent-review`
- 冻结候选：`9477a3ed82cdb63c72a0bf3e06f1561fc72bc7ff`
- 结论：**PASS**
- 未关闭 Blocker：`0`
- 未关闭 Major：`0`

## 1. 验收范围与边界

本轮仅复核 RT-012 Stage-09 对 `scripts/cwk_instance.py` 的实例根锚定修订，以及与之直接相关的权威契约、迁移说明、script-evolution receipt、攻击测试和回归面。除新增本报告外，未修改代码、测试、契约、状态、索引、receipt、原验收报告或 Git 历史；未读取真实凭据，未连接生产 CWork/Cloud/DocDB，未修改 cron、scheduler 或部署配置。

原 `RT/RT-012/reports/独立验收报告.md` 保持候选提交中的原始字节，SHA-256 为 `bce39f7dfaf1ac92b2f9765bb82622e4e0a5b16be2c48f7ed6cb5c0b822791e2`，Git blob 为 `0c460e39edbac0a0188b5c62663728036594be5b`。它只作为 Stage-09 之前的历史验收来源，本报告不改写或重新解释其结论。

## 2. 候选与 owner tree 绑定

验收开始和测试结束时，`HEAD` 均精确为 `9477a3ed82cdb63c72a0bf3e06f1561fc72bc7ff`；工作区除既存 `.serena/`、`.spec-workflow/` 外无变更。

按照冻结的 `candidate_tree_minus_closed_exclusions` 算法，直接从该 commit 的 `git ls-tree -r -z --full-tree` 记录重算：

- tracked records：`460`
- 闭合排除 records：`54`（四类 evidence receipt root 与 `RT/RT-011..026/reports/`）
- 纳入 digest records：`406`
- domain：`cwk-owner-scope-tree-v1\0`
- `owner_scope_tree_sha256`：`dfb0754cc877c98702e37acf01ca8236b22c1a00e53442a2aa3db8522db9a5bf`

独立实现与仓库权威 `GitSubject.candidate_tree_sha256()` 的结果逐字一致；未使用 caller-supplied map、moving worktree 文件或报告目录内容参与 digest。

## 3. Stage-09 演进链复核

以下值均从候选文件原始 bytes 独立重算：

- policy SHA-256：`2089490e45bdd84ba3bac75fe40092f81f40765638b988e17facdc4040d14a6d`
- genesis `from_sha256`：`418bbdaabb8842b0a20443b42eca661c7be4c87c59e1e49c5d1a973a56bd5ae7`
- domain/path-bound genesis link：`5d553900e8271414192a5bba771a6bf51b97cba77035e93dc9fcdd063e3cab14`
- Stage-09 target SHA-256：`827a3dacafd746ab760360c7a872362fb9c9327a2622c13dccb95c1bdec59d4f`
- migration note SHA-256：`d4fac4202b0154c9092358bfc020ead89d67cd252acb8b212578c9e480d78dfa`
- Stage-09 receipt raw SHA-256：`bb360525405145f7889b3fc5977ca2b8b7bfed0be14cdafacae0a5e23f90059d`

receipt 的 owner、stage index、ordinal、target、from/to、migration hash、previous link 和 6 个 canonical acceptance refs 均与 policy/真实文件一致。全仓演进 replay 验证当前只有 Stage-09 与 Stage-10 两份 receipt，Stage-09 链 tip 精确等于上述 target SHA。

## 4. 实现与攻击面审查

实现符合 Stage-09 冻结语义：

- 绝对路径从 `/` 开始逐组件执行 no-follow `stat`、`openat(O_DIRECTORY|O_NOFOLLOW)` 与 `fstat` identity 比对；祖先 symlink、非目录、打开过程中的 inode 替换均稳定拒绝。
- `InstanceLayout.open()` 保留 root anchor FD 和完整 `(st_dev, st_ino)` 链；每次 `root_fd()` 重走文本路径并同时比对完整链和 anchor identity。打开后的 root/ancestor rename-and-replace 只能得到原 inode 或 `InstanceRootError`，不能静默切换到 replacement tree。
- lifecycle lock 只保护 anchor duplicate/close；yield 期间不持锁。已 yield 的 duplicate FD 在并发 close 后保持原 identity，新访问稳定拒绝。
- `close()` 幂等；context manager 退出关闭；copy/deepcopy 共享同一 handle；pickle 被拒绝，未产生双重 FD ownership。
- tenant child、registry、lock/CAS、symlink/hardlink、rollback 与 secret-scan 旧边界在完整 RT-012 回归中保持通过。

## 5. 独立执行证据

所有 Python 测试均设置 `TMPDIR=/private/tmp` 与 `PYTHONDONTWRITEBYTECODE=1`：

- Python 3.11.14：RT-012 全套 `125/125 OK`。
- Python 3.14.5：RT-012 全套 `125/125 OK`。
- 两版本各自执行 Stage-09 receipt 的 6 个 canonical refs：`6/6 OK`。
- Python 3.11.14：script-evolution guard + RT-016 bridge `263/263 OK`，其中 1 个 skip 是该解释器没有 PEP-695 AST 字段的预期能力差异。
- Python 3.14.5：script-evolution guard + RT-016 bridge `263/263 OK`，`0 skip`。
- Python 3.11.14：RT-012～RT-014 相关回归 `311/311 OK`。
- Python 3.14.5：RT-012～RT-014 相关回归 `311/311 OK`。
- Wiki lint：`PASS`，`8373` summaries，duplicate/missing/invalid/dangling 均为 `0`。
- Wiki smoke：两版本各 `14/14 PASS`；summaries/topics/entities 为 `8373/315/944`。
- `git diff --check`：无输出。

## 6. 结论与保留边界

未发现普通软件缺陷、Blocker 或 Major。Stage-09 已关闭本轮定义的祖先 symlink、已打开 handle 的 root/ancestor replacement、root lifecycle 与 dir-fd 并发问题；RT-012 的 schema、Tenant Registry、CLI ABI、退出码和磁盘布局未被扩展。

本 PASS 只证明冻结候选上的 RT-012 Stage-09 及上述回归面，不证明真实 authority、Gateway identity/transport、多租户生产部署或 G6/G7 readiness；这些能力仍由后续 RT 与门禁独立关闭。

<!-- cwk-acceptance-v1
report_id: RT-012
verdict: PASS
open_blocker: 0
open_major: 0
subject_commit: 9477a3ed82cdb63c72a0bf3e06f1561fc72bc7ff
owner_scope_tree_sha256: dfb0754cc877c98702e37acf01ca8236b22c1a00e53442a2aa3db8522db9a5bf
implementer_ids: agent-rt012-stage09-implementation
reviewer_ids: agent-rt012-stage09-independent-review
-->
