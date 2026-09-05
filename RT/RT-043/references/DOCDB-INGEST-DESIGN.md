# 摄取统一模型（DOCDB-INGEST-DESIGN v1.3）

- 日期：2026-09-04（v1.3：双引擎双向评审后的裁决修订）
- 定调链：16:43 格式转换/重组/动态分类/拆分 → 16:47 AI 理解前置 → 16:50 路由与源无关
  → 16:52 raw 索引寻址 → 17:01 Evan 指定 Grok+Claude 双向评审 → 本版吸收裁决
- 评审档案：reviews/2026-09-04-v12-{grok,claude}-bidirectional.md

## 一、统一摄取管道（不变）

```
拉取 → originals（原样字节/write-once/SHA-256）→ 格式工厂（确定性）
     → 【AI 理解回合】一次理解双产出：落位决策（分类+置信度）+ 精编产物
     → raw/<用户分类树>（lineage 寻址）→ wiki/
```

## 二、raw 寻址与身份（v1.3 重设计：采纳 Claude 硬阻塞项）

### lineage_id 方案（取代 v1.2 的 raw_id）

```
lineage_id = "<source>:<源内稳定ID>"     ← 永不含 rev/seq（快照号不是身份）
  cwork:  "cwork:2095046023776104449"
  docdb:  "docdb:2087519593823322113"     ← 拆分件 "docdb:<fileId>#<内容锚点slug>"
```

- **docdb 版本模型（活文档）**：同 fileId 新 rev → 格式工厂派生 → 内容实质变化才
  更新该 lineage 件 + manifest 记版本链（version/rev/sha256/supersedes）；
  引文固定 (lineage_id, version)。cwork 不变（raw 永不改+snapshot 追加）。
- **v1 裁定**：引文钉版本；查询可看最新但显示版本注记。

### 索引（_system/raw-index.json）与三账职责

```
manifest（内容账）: lineage_id → 当前 sha256 + versions[] 版本链
index（定位账）:    lineage_id → path + size + model_version + rule_version
                     + artifact_kind(document|placeholder) + placeholder_reason
                     + status(ok|unrouted|placeholder|failed:<reason>)
provenance（来源账）: lineage_id → originals 原件 + 派生关系
```

- **原子落盘**：tmp+fsync+rename+前代备份；NAS rename 原子性实测（RT-042 冒烟项）
- **对账主键 = lineage_id + sha256，路径只是缓存**；禁止「路径存在→更新哈希」
- **互换检测**：doctor 校验 (id,sha) 绑定，两件互换必红
- **单写锁**：index 写者（ingest / doctor --apply / reclassify）互斥，锁文件机制
- 查询引用一律 `{lineage_id, version, quote}`——**无 path 字段**（locate 即时解析）

## 三、路由默认（v1.3 裁决：智能缺省，非平台写死）

- **机制统一**：`--route classify|timeline` 两源都可配 ✅（两引擎一致批）
- **默认值**：向导第 4 步 AI 按源内容**提议** + 用户确认（配置摘要卡显式展示
  route.mode）——cwork 高频短件 AI 会提示 timeline 更合适；docdb 项目文档提
  classify。不写死平台级默认 ✅（采纳 Claude「默认值统一无技术必然性」）
- **成本护栏（摄取期，采纳 Claude）**：并发度上限、限流退避、单篇超时、
  AI 失败件进 `raw/_unrouted/` 隔离区（status=unrouted，不丢不卡）、
  长文档分类截断策略（开头+目录+采样段，精编单独全量）
- **中置信不当成功**（采纳 Grok）：置信度分层，中段进待审队列
- **占位件不当分类成功**：placeholder 不进精编引用集、不计入 classify 成功率
- index 记 `model_version`（换模型可定向重跑）✅

## 四、doctor --layer index（v1.3 规格化：采纳双引擎全部必改）

```
默认只读：kb doctor --layer index → 差异清单
  [move|copy|missing|extra|hash-mismatch|swap|out-of-root|ambiguous]
显式写：  --apply 才更新 index；apply 日志可审计
```

- **ignore 规则表**（规格的一部分）：`@eaDir/ #recycle/ .DS_Store ._* .Spotlight-V100 Thumbs.db *.partial`
- **sha 二义拒绝**：同哈希多候选 → ambiguous，转人工，禁止猜
- **删除语义**：缺失条目默认报警不摘除；`--gc` 显式才清（记录死链影响）
- **红线**：raw 用户可移动、可重命名，**不可编辑内容**——sha 不符报
  「raw 被手工修改」专用错误（不报「文件丢失」）
- **剪枝**：(size, mtime) 预筛后才算 sha
- 与 ingest 互斥（单写锁）；NFD/NFC 路径规范化

## 五、格式工厂（v1.3 范围调整）

| 形态 | v1 处理 |
|---|---|
| md/txt/html/json/yaml/py | 直转（html 剥标签） |
| docx | 转 md 做满 + **质量门**：转换警告/过短/空正文 → 待审队列（不许哈希绿灯） |
| xlsx / pptx | **降级**：xlsx→每 sheet 一个 CSV + 清单（不硬转 md）；pptx→占位+结构清单 |
| 图片 | 占位（**口径：按文件名/路径可检索，内容不可检索**；daily 报告未索引数） |
| zip | 占位+中央目录清单（不解压）；清单读取失败必红 |
| rar/7z/未知 | 通用占位路径（无扩展名探测） |
| 超限 | 单文件大小上限 + parser 超时（防 nightly 挂死） |

## 六、处理状态账（v1.3 硬阻塞新增：防静默丢件）

- `_system/ingest-state.json`（B#29）：每个 originals 条目一条
  `{lineage_id, status: ok|placeholder|failed:<reason>, ts}`
- **doctor 新增 originals↔index 覆盖率对账**：三账全绿但 originals 有、index 无
  的静默丢件必须红；daily 报告失败清单

## 七、B 表/CLI 同步

- B#2 raw 落位=用户分类树；B#2c originals；B#28 raw-index；B#29 ingest-state
- CLI-SPEC v1.1 同步：citations 改 `{lineage_id, version, quote}`（删 raw_path）；
  doctor 层加 `index|provenance|coverage`；source set 加 `--route`；
  加 `kb locate <lineage_id>`；`changed-paths` 重定义为 originals 层变更检测

## 八、实现顺序（采纳 Claude：先地基与确定性，后 AI，最后自动改账权）

0. 文档对齐（CLI-SPEC v1.1）→ 1. index+lineage+原子写 → 2. 格式工厂+状态账
（拿真实数据：类型直方图/转换质量/失败率）→ 3. classify 单源灰度（docdb 先行，
cwork 对照，跑完拿真实调用量/耗时再定 cwork 策略）→ 4. doctor 收编（最后上，
首发即带 --dry-run 默认）

## 九、待 Evan 确认（裁决后终版四点）

1. **lineage 寻址 + 活文档版本链 + 原子写**（取代 v1.2 raw_id 方案）
2. **路由智能缺省**：AI 提议+用户确认（cwork 倾向 timeline、docdb 倾向 classify，
   都可换）——不再写死「双源默认 classify」
3. **doctor 收编**：默认只读差异清单，--apply 显式写；配套 ignore 表/二义拒绝/
   删除语义/手工修改检测
4. **格式工厂 v1 范围**：docx 满做+质量门；xlsx→CSV、pptx/图片/zip 占位；
   rar/7z 不承诺；处理状态账+覆盖率对账
