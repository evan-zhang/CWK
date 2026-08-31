# RT-029 基线冻结记录

记录时间：2026-08-31T02:49:32Z。

## 已确认的 Git 事实

- GitHub：`https://github.com/evan-zhang/CWK.git`
- 远端默认分支：`main`
- `origin/main`：`445f6618798522be874539d41b95e9ef3551f306`
- 产品基线分支：`pr/PR-001-completion`
- 产品基线提交：`fe4add1e752d1e1a2438ad0f421d635a96321d02`
- 产品基线树：`3bee96282e9382bfe89b904f97651b4929f9e6c0`
- 与占号前本地 `main` 的共同祖先：`65196a765e1fb8ff851c4b92945a566cb36d4fda`
- 占号后本地 `main`：`a0d24c11833e379c05e0e8d11c4aa532551796dc`
- 分叉计数（占号前审计口径）：本地 AODW 主线独有 8 个提交，产品基线独有 59 个提交。

## 原工作树保护指纹

以下值只用于证明 RT-029 没有改动原工作树，不代表这些未提交内容已被审查或接受：

- HEAD：`fe4add1e752d1e1a2438ad0f421d635a96321d02`
- 状态条目：24 个已跟踪文件修改、3 个未跟踪目录，共 27 项。
- `git status --porcelain=v2` SHA-256：
  `57c287cb154d956ea0667e12ed1433d5281e03d49f165dd9379db0501c151d8d`
- 已跟踪二进制 diff SHA-256：
  `6d42d47989dfa8b6c5ba734cf2b12af12f246f32871e97c9885c5426d5628b81`
- 未跟踪路径清单 SHA-256：
  `e0fb23503b348f0f8ea55120fd16b9a5bc40b504f181ced2aed4ea406f3f0ca8`

RT-029 不在该工作树执行写操作，也不使用 stash、reset、checkout 或全量暂存。

### 复检命令（只读）

B0 当时没有把命令写下来，复检的人只能猜口径。补记如下，三条命令都只读，
不改工作树内容：

```bash
P=/Users/evan/.openclaw/gateways/life/state/workspace-life/projects/CWK
git -C "$P" rev-parse HEAD
git -C "$P" status --porcelain=v2                       | shasum -a 256   # 状态指纹
git -C "$P" diff --binary                               | shasum -a 256   # 已跟踪改动指纹
git -C "$P" ls-files --others --exclude-standard        | shasum -a 256   # 未跟踪路径指纹
git -C "$P" status --porcelain | wc -l                                     # 应为 27
```

注意第三条不带 `--directory`：带上会把未跟踪目录折叠成 3 条目录名，得到的是另一个
哈希（`23c429cc…`）。冻结时记的是**逐文件**清单。

复检结果（2026-08-31，B2/B3 完成时）：HEAD、三个 SHA-256 与 27 项计数全部与冻结值
逐字相同。

## 当前风险

- RT-028 基于旧产品主线，包含尚未合入的 Work Agent 设计与实验实现；本 RT 不并入它。
- `pr/PR-001-completion` 已提交代码的全量测试当前不是绿色基线，需要在独立 RT 分支
  按失败族收敛，不能把原工作树的在研修复当作捷径。
- GitHub 远端落后于两条本地线；未经用户收口授权，本 RT 不推送或合并。
