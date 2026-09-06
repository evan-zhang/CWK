# RT-051 开发前交接

**本轮只完成设计修订，仍停方案门/代码授权之前；没有后台开发。** 不新建RT、不改Issue、不合并推送部署。唯一方案为 [rt-lite.md](rt-lite.md)，状态/目录见 [meta.yaml](meta.yaml)。

## 最短复核入口

1. 读rt-lite开头目标与D01–D04：完整访问已确认，不再问要不要list/open/read；工程师负责推荐实现，用户只决定代码授权、生产拓扑/成本/权限边界。
2. 查C01–C06：当前权威映射签ref，不依赖BM25/generation；安全范围与分页、SHA/完整性、OPS prepare/builder、gateway零持久写、真实CLI/tool接线。
3. 查C07/P1–P3：**P1基础reader → P2实际Agent工具 → P3词法增强**，词法命中复用同reader，不另建citation路径。
4. 对照 [acceptance-matrix.md](acceptance-matrix.md) 的A01–A12；[evidence.md](evidence.md) 末尾本轮审核处理表；[validation.md](validation.md) 本轮文档检查。sources.json仅历史基线，未重核44文件。

## 基线与并发

- 修订起点 `effb1decec71d30a73b3af4abbce78825b212347`，分支 `feature/RT-051-body-lexical-retrieval`。
- 工作目录 `/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK/.claude/worktrees/RT-051-body-lexical-retrieval`。
- 初检main=`fe9f858`，已有RT-050 vanished补丁（ingest/测试/create Skill）及其文档未提交；后续main继续前进，最终值见validation。无本RT重叠改动，未合并/rebase。开发前必须重核main、各worktree、暂存及实际重叠，不按旧基线覆盖别人代码。
- 历史产品基线80b8b85、占号d63097d、sources.json均保留，不把main meta-only误认方案缺失；本分支index未补051，既有G109告警不改规范消红。

## 尚需工程实验，不是另向用户询问范围

- FileStation流式transport（含TLS pin/error envelope/取消/deadline）和OPS broker可用性；不假定NAS Range。
- 单宿主所有source写入口的锁/fence覆盖、prepare/status接线、原子快照与GC读锁/硬TTL续读。
- 真实ingest输出的title缺失/placeholder/partial可判定程度；未知完整性必须null，不自称完整转换。
- 宿主工具注册的真实落点和允许列表、无NAS token包装；reviewer零工具保持。
- 长文资源/元数据有界解析/中文token与BM25效果；建议预算未测，超成本才升级D02/D04。

源码未来写面为gateway/wizard/client/storage、OPS reader/builder/broker、ingest源屏障/refresh收尾、相应测试；Skills/治理登记须相应授权，本轮没有实际修改。读方不能import写模块；主raw可完整读不等于sheet/OCR/原附件转换已完整。

## 给开发Agent的接手提示

> 先读AODW宪章、交互规则、AGENTS、overview及本RT。只审方案直到用户明确允许写代码。本次用户已确认受控完整访问和Issue #2同RT，勿重新立项/裁剪成仅BM25；先C01–C06和P1–P2，再P3。先重核活跃worktrees及main RT050漂移，按owner协调源写屏障。以A01–A12真实脱敏工具链行为证明，不能只API200/0测试/查字符串。未授权不接触真实raw/API/凭据，不改Skills/规范，不给reviewer扩权，不关闭Issue。全部通过后仍停用户收口门，不自动合并推送部署。

## 父会话独立核验（最短只读命令）

在上述worktree执行，检查本轮commit相对effb1de只改六份RT文档：

```bash
git branch --show-current
git status --short
git log -1 --format='%H %s'
git diff --name-status effb1de..HEAD
git diff --check effb1de..HEAD
git diff --exit-code effb1de HEAD -- RT/RT-051/sources.json scripts tests skills skill config .aodw-next AGENTS.md
bash .aodw-next/tools/rt-guard.sh --root . --rt RT-051 --format json
```

最终commit以原会话交付回执为准。文档绿灯只说明文档/Git边界，不是产品已修。建议用户下一道门仅为“审阅本修订并明确是否授权代码开发”，不要求重答已确定的基础访问范围。
