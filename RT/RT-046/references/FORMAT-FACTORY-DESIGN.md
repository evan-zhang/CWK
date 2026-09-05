# FORMAT-FACTORY-DESIGN — RT-046 设计依据

## 1. 蓝本：bd-eval-loop RT-108 document-ingest

源：`projects/bd-eval-loop/docs/modules/document-ingest.md`（last_audited 2026-08-17）
+ `src/bd_precheck/services/document_ingest.py`。

移植四条纪律：

1. **逐格式选转换器**（TEXT_CONVERTER_BY_SUFFIX）：无一转换器全能。
   实测证据：含 `=SUM(A2:A3)`（缓存值 82）的表，anydoc 丢 82、markitdown 保留。
   市场测算表的总额/CAGR/峰值恰是结论，静默丢失不可接受。
2. **版本 pin**：anydoc 0.1.9 / markitdown 0.1.7；换版过保真夹具（合成 docx/xlsx 可进 git）。
3. **回执三链**：converter{name,version} + source_sha256 + output_sha256。
   转换是纯函数且结果落盘；禁止交 LLM/Work Agent（非确定性毁溯源链）。
4. **图片旁路**：失败不阻断正文。图说文末附录带锚点（anydoc 不输出图片占位符，
   自渲染 block 树成本不成比例——已知妥协）。

蓝本自认缺口（吸收或规避）：
- 质量分诊未做（转成功但 0 字且静默）→ 本 RT 按「0 字=失败」处理
- PDF 内嵌图盲区 → v1.1 PDF 只出文本，图留 docling 候选
- .doc/.rtf/.epub/.odt 未测 → 不宣称支持

## 2. 本 RT 自补：无后缀嗅探

投前库 3 份关键流程文档无后缀（CMS投前阶段1流程诊断 v0.4 / 产品投前流程专题说明书 /
产品引进投前全流程框架汇报）。规则：
- 读前 16 字节：%PDF → pdf；\x89PNG → png 占位；\xff\xd8\xff → jpeg 占位；
  PK\x03\x04 → ZIP 容器，按 [Content_Types].xml / xl/ / word/ 判 docx|xlsx
- 判不出 → placeholder（unknown-format），不猜；嗅探结果进回执保证幂等

修正（返修后）：这 3 份不是「无后缀的二进制原件」，而是 DocDB 在线文档——
下载接口给的是查看器壳，嗅探对它无能为力（见 §2b、§5）。嗅探规则本身不变，
只是它们不再由嗅探定案。

## 2b. 壳检测：download 成功 ≠ 拿到文档

实测：`download-file.py` 对这 3 个 fileId 返回**同一份** 10,557B HTML
（sha `a8a004e5c0178d6a…`），是在线文档查看器的骨架，无正文；
`get-full-content.py` 对同样的 id 返回真身 15,920B / 28,110B / 513B
（`resultCode==1`，`data` 为全文 Markdown）。规则：

- 字节以 `<!DOCTYPE html` / `<html` 开头 **且** 文件名不以 `.html`/`.htm` 结尾
  → 判为查看器壳。两个条件必须同时成立：一份真 HTML 附件开头完全相同，
  只有名字分得开它们，改写它就是数据损坏。
- 判为壳 → 改调 `scripts/query/get-full-content.py --file-id <id>`，取 `data` 作为
  origin 字节；回执记 `fetch_mode=docdb-full-content`。取法即格式，不再嗅探：
  拿回的是 Markdown 全文，走 passthrough。
- 重取失败或 `data` 为空 → **保留壳字节**入原件区（原件仍是写一次、内容寻址的），
  产物落占位，两本账记 `docdb-shell-fallback`。不静默——装不上正文的那天，
  重跑名单要能从状态账查出来。

## 3. 依赖接入（红线合规）

纯标准库底线不动；anydoc/markitdown 按 openpyxl 先例做可选 extra：
- 无硬依赖；*_available() 探测，缺失 → placeholder（converter-missing 记账可审计）
- CI 基线保持绿；转换单测用合成夹具 + skipIf 探测缺失

## 4. 与现有库交互

- raw 只增不改：转换产出新版本 lineage，不覆盖已入库件
- 投前库 43 件 placeholder 重摄取走新表；cwork-3m 全 md 不受影响
- 网关/引文链路不改——converted 件自动可 citation

## 5. 验收样本（真数据）

### 5.1 三份无后缀文档 —— 走壳检测通路，不走嗅探

原表把它们的预期写成「嗅探→文本类→converted」，这是错的：嗅探拿到的是查看器壳，
只会判出 unknown-format。真实通路见 §2b。

| lineage | origin_name | download 给到 | 预期 |
|---|---|---|---|
| docdb:2087523108876566530 | CMS投前阶段1流程诊断与补丁处理备忘录（v0.4） | 10,557B 壳 | 壳检测→get-full-content 重取 15,920B→converted（`fetch_mode=docdb-full-content`） |
| docdb:2087523108977229825 | 产品投前流程专题说明书（讨论稿） | 同一份 10,557B 壳 | 同上，重取 28,110B |
| docdb:2087523109333745665 | 产品引进投前全流程框架及配套模板体系建设汇报 | 同一份 10,557B 壳 | 同上，重取 513B |

三份 download 字节的 sha 完全相同（`a8a004e5c0178d6a…`）——这一条本身就是判据：
若重跑后三份 origin_sha256 仍相同，说明壳检测没生效。

### 5.2 转换器验证样本 —— 用真有内容的原件

壳家族证明不了转换器。转换器的验收换成投前库里**真有内容**的原件，
按 sha 去重后共 18 个唯一原件族，覆盖 6 类：

| 类型 | 预期 | 判据来源 |
|---|---|---|
| png | placeholder（图片内容不可检索是 v1 口径，不是失败） | 表里 image 行 chain 为空 → `image-not-converted-in-v1` |
| html（真 HTML，非壳） | 只下载一次、不重取；走通用占位路径 | 壳检测第二个条件；HTML 正文转换未列入 v1.1 |
| json / yaml / py | 文本类原件，converted（passthrough） | 表里 markdown 行的直通面 + 反空转（0 字→占位） |
| zip | placeholder + 中央目录清单，不解压 | 表里 zip 行 note |

### 5.3 通过线

- 三份文档可经 /citation 拉正文且 matches_index=true；
  G2 追加题「S1/阶段一流程」可命中 CMS投前阶段1流程诊断 正文。
- 每一件的 provenance 行能同时回答：字节怎么拿到的（`fetch_mode`）、
  谁转的（`converter{name,version}`）、转的是哪份字节（`origin_sha256`）、
  转出了什么（`artifact_sha256`）。任一件答不全，即为不通过。
