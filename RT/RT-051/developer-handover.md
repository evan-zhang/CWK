# RT-051 开发前交接

## 接手边界

**先审方案，后拿代码开发授权。没有后台开发任务。** 本轮用户只授权登记RT、建立独立工作目录、完善设计与资料。不得把这份交接当“同意做完”。不要另起重复RT，也不要自动合并、推送、部署或清理。

生命周期、分支、工作目录的唯一权威是 [meta.yaml](meta.yaml)。它保持intaking，notes明确方案门待批准；不得改done/parked-design-only冒充实现完成。

### 文件导航

1. [rt-lite.md](rt-lite.md)：唯一方案；先看D01–D07，再读C01–C10与P0–P5。
2. [evidence.md](evidence.md)：代码现况/相关RT与第三方观察，帮助判断范围重叠。
3. [sources.json](sources.json)：commit、源码路径、已读行区间和SHA，供独立核验。
4. [acceptance-matrix.md](acceptance-matrix.md)：待开发行为与故障映射，不是测试通过回执。
5. [validation.md](validation.md)：本轮文档级检查、告警及检查边界。

没有第二份设计、没有handoff目录、没有产品实现文件或测试实现。

## 关键Git依据

- 占号前产品主线：`80b8b850dd25c2527c18822923ccacae104205e8`。
- main占号提交：`d63097d7095ac02c13207d3fac24667593d0d523`，只有 `RT/RT-051/meta.yaml`。
- 文档提交可用当前RT分支的HEAD读取；最终完整commit在原会话交付回执中。禁止把main上的meta-only误判为方案丢失。
- 仓库入口：`/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK`。
- 本会话未修改RT/index.yaml；另一会话已在main提交bb414a63c78371e8aca57d38ed96a18dc32582a3，仅更新RT-050文档与index（含051登记）。本worktree未同步，因此G109会告警；不要擅自为了全仓绿色去合并、改共享索引或改规则。主线与本分支的生命周期显示差异以本RT worktree/meta为设计交接依据。

## 给开发Agent的接手提示（可原样转交）

> 接手CWK的RT-051，只做方案门评审直到用户明确授权代码开发。先读仓库AODW宪章、交互规则、AGENTS、overview及RT/Spec-Lite/Git/测试规则，再按meta.yaml进入本RT已有独立worktree。主线产品代码取证基线80b8b85，先重查当前main、index、所有worktrees与kb_ingest/kb_gateway重叠，不凭本次回执假定仍无并发变化。rt-lite.md是唯一方案；逐项回应D01–D07，特别是库快照授权与实时源撤权差距、单写宿主屏障、主件而非sheet/意见JSON的范围、Python vs FTS5待实验及预算未测。源证据见sources.json，Yuxi只参考不复制、不调用模型/API。未获明确代码开发授权不写业务/测试/配置/规范；获准后按P1–P4逐阶段实现与验收，新增治理登记仍需权限与owner确认。不能接管或修改PR-001冻结ABI/legacy专属脚本，不自动合并、推送、部署或清理。进入代码开发前通知Evan。

## 父会话/独立核验者的最小只读检查

在meta指定的工作目录执行，不运行服务，不访问真实业务库：

```bash
git branch --show-current
git status --porcelain=v1
git log -3 --format='%H %s'
git show --format=fuller --stat d63097d7095ac02c13207d3fac24667593d0d523
git diff --name-status 80b8b850dd25c2527c18822923ccacae104205e8..HEAD
git diff --check 80b8b850dd25c2527c18822923ccacae104205e8..HEAD
bash .aodw-next/tools/rt-guard.sh --root . --rt RT-051 --format json
```

预期变更只在RT/RT-051；工作区应干净。源码sha可与sources.json逐一比较，并用 `git show <commit>:<path>` 确认对应字节。本轮未跑产品测试或FTS5实验，不应期望看到实现验收PASS。

### 建议给Evan的决策句

“请先审RT-051方案，逐项确认D01–D07；方案批准后另行明确由哪位开发Agent接手代码开发。”

本轮停在等待方案/代码授权，不创建新的开发会话，不使用‘下一步自动开工’承诺。
