# RT-013 Stage-10 独立验收报告

> 结论：**PASS**
>
> Blocker：0；Major：0。
>
> 本报告只验收 Stage-10 对 `cwk_agent_binding.py` 并发 bind 竞态的补救，以及
> RT-013 已有语义在该补救后的兼容性；不证明真实 Gateway 身份、CWork authority、
> 沙箱 transport、多租户生产部署或生产就绪。

<!-- cwk-acceptance-v1
report_id: RT-013
verdict: PASS
open_blocker: 0
open_major: 0
subject_commit: 9477a3ed82cdb63c72a0bf3e06f1561fc72bc7ff
owner_scope_tree_sha256: dfb0754cc877c98702e37acf01ca8236b22c1a00e53442a2aa3db8522db9a5bf
implementer_ids: agent-rt013-stage10-implementation
reviewer_ids: agent-rt013-stage10-independent-review
-->

## 1. 验收对象与边界

- 候选提交：`9477a3ed82cdb63c72a0bf3e06f1561fc72bc7ff`。
- 验收环境：Darwin 25.5.0 arm64；Python 3.11.14 与 Python 3.14.5；所有测试均设置
  `TMPDIR=/private/tmp` 和 `PYTHONDONTWRITEBYTECODE=1`。
- 独立验收者未参与 Stage-10 实现；除本报告外未修改代码、契约、测试、receipt、
  index、状态文件或旧报告，未 commit、push、部署或读取真实凭据。
- 原 `RT/RT-013/reports/独立验收报告.md` 保持原始字节，SHA-256 为
  `ece89870ada955d296fe90c8be3a4b4989e97be6b75cf10bf2863736bac6b76b`；它只作为
  Stage-10 前历史溯源，不再充当当前候选的权威验收。

## 2. 演进闭包与内容绑定

独立逐字节重算结果：

- 中央 policy：
  `2089490e45bdd84ba3bac75fe40092f81f40765638b988e17facdc4040d14a6d`。
- `scripts/cwk_agent_binding.py`：
  `2d390d6fa1a5b84e1dcc137e64c642f3a1a9cb010e009fa5c7a6e00e076030c4`。
- migration note：
  `0ba8763702bba8922baa109184e9443d6076c1cad1fb1fd51a2f4a930b608a09`。
- Stage-10 receipt 原始字节：
  `3fc37db1b62ed1f48b83e72bdea6e2dbfb2996ddbf39bfed97fa160d5950511b`。

上述值与 Stage-10 receipt、中央 policy 和 migration note 的声明完全一致。
script-evolution replay 得到 2 份现有 receipt、17 个永久 immutable genesis entry，
Stage-10 tip 正是上述 script SHA；Stage-09 tip 与 tenant CLI slots 也保持预期。

## 3. 实现与攻击语义审阅

历史缺陷是两个 binder 都通过早期 absent 检查后，输家在晚 predecessor 读取中看见
赢家的 `active` record，却把它当成可重绑历史记录并以 epoch 2 合法 CAS。候选补救
在晚读取处只接受 `status == revoked`：

- 晚读取看见 `active` 或 `suspended` 时，在 journal、record、receipt 与 auth-epoch
  mutation 前抛 `BindingConflictError`。
- 两个晚读取都仍看见 absent 时，dirfd lock + CAS 只允许一个 epoch-1 record；另一方
  稳定 `RevisionConflict`，不会产生第二份 receipt 或第二次 auth-epoch bump。遗留
  journal 可由既有 `recover()` 幂等清扫，独立强制该交错并验证清扫完成。
- predecessor 确为 `revoked` 时继续继承旧 epoch/SHA，`rebind()` 仍严格执行
  revoke-out + bind-in 两步并生成两份 receipt；没有为修并发而破坏合法重绑。
- 顺序的同 tenant 与跨 tenant 重复 bind 均拒绝；最终 record 唯一且 epoch 单调。

另以独立夹具强制“早期 absent、晚读取 newly suspended”和“两方晚读取都 absent”两条
非概率路径；Python 3.11/3.14 均通过，并分别确认零额外写入和 CAS exactly-one-wins。

## 4. 验收执行证据

### 4.1 RT-013 与 Stage-10 canonical refs

- Python 3.11：RT-013 六个测试模块 `124/124 OK`；Stage-10 四个 canonical refs
  `4/4 OK`。
- Python 3.14：RT-013 六个测试模块 `124/124 OK`；Stage-10 四个 canonical refs
  `4/4 OK`。
- 高次数 fresh-fixture 并发 bind：Python 3.11 为 `500/500 OK`，Python 3.14 为
  `500/500 OK`，合计 `1000/1000`；每次均新建实例目录，失败、错误与 skip 均为 0。

四个 canonical refs 覆盖：普通并行 exactly-one-wins、确定性 late-active barrier、
顺序同/跨 tenant 冲突、revoked predecessor 两步 rebind。

### 4.2 演进守卫与相邻回归

- script-evolution guard + RT-016 bridge：Python 3.11 `263/263 OK`
  （1 个解释器能力相关预期 skip）；Python 3.14 `263/263 OK`（0 skip）。
- RT-012 + RT-014 + RT-015 相关回归：Python 3.11 `311/311 OK`；Python 3.14
  `311/311 OK`。
- `git diff --check`：PASS。
- Wiki lint：PASS；Wiki smoke：`14/14 PASS`，镜像摘要/主题/实体计数及 query contract
  全部满足门槛。

## 5. Git 派生 owner tree

严格按 frozen release registry 的
`candidate_tree_minus_closed_exclusions` 规则，对候选提交执行完整
`git ls-tree -r -z --full-tree`，排除四个 evidence receipt root 与
`RT/RT-011..026/reports/` 后：

- 纳入 406 个唯一 tracked records；排除 54 个 evidence records。
- domain：`cwk-owner-scope-tree-v1\0`。
- 结果：
  `dfb0754cc877c98702e37acf01ca8236b22c1a00e53442a2aa3db8522db9a5bf`。

该值由 frozen `GitSubject` helper、`ReleaseRepositoryFacts` 与一份独立的
`git ls-tree` 重算实现三路交叉验证，结果完全一致。报告目录属于闭合 evidence
exclusion，因此写入本报告不会改变其所绑定的候选 owner tree。

## 6. 最终判定

未发现 Blocker 或 Major。Stage-10 的并发覆盖缺陷已关闭，revoked-prior 合法重绑与
RT-012～015 相邻契约未回归。本报告允许 RT-013 作为候选提交
`9477a3ed82cdb63c72a0bf3e06f1561fc72bc7ff` 的当前独立验收输入；任何候选代码树、
policy、receipt、测试或 owner-tree 变化都必须重新验收。
