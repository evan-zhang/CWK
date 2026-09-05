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

## 3. 依赖接入（红线合规）

纯标准库底线不动；anydoc/markitdown 按 openpyxl 先例做可选 extra：
- 无硬依赖；*_available() 探测，缺失 → placeholder（converter-missing 记账可审计）
- CI 基线保持绿；转换单测用合成夹具 + skipIf 探测缺失

## 4. 与现有库交互

- raw 只增不改：转换产出新版本 lineage，不覆盖已入库件
- 投前库 43 件 placeholder 重摄取走新表；cwork-3m 全 md 不受影响
- 网关/引文链路不改——converted 件自动可 citation

## 5. 验收样本（真数据）

| lineage | origin_name | 预期 |
|---|---|---|
| docdb:2087523108876566530 | CMS投前阶段1流程诊断与补丁处理备忘录（v0.4） | 嗅探→文本类→converted |
| docdb:2087523108977229825 | 产品投前流程专题说明书（讨论稿） | 同上 |
| docdb:2087523109333745665 | 产品引进投前全流程框架及配套模板体系建设汇报 | 同上 |
| 其余 40 件 | png/html/zip 等 | 图→placeholder 不变；zip→不解包仍 placeholder |

通过线：三份无后缀文档可经 /citation 拉正文且 matches_index=true；
G2 追加题「S1/阶段一流程」可命中 CMS投前阶段1流程诊断 正文。
