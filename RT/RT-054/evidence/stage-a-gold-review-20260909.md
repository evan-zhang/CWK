# RT-054 阶段 A gold 独立复核

- 日期：2026-09-09
- 方式：独立只读逐题复核；复核者未修改目标文件
- 裁决：**PASS，可签收**

## 复核对象

- `stage-a-gold-20260909.json`：72 题
- `stage-a-gold-fixture-20260909.json`：22 个新增合成文档
- `tests/test_rt054_stage_a_contracts.py`：gold、fixture、权限与 API 合同判据

签收时 SHA-256：

- gold：`527b4d8f85f8e3ae3e21e3418a318d0d4aa86b29a6575d86b403f57b2f4bd14f`
- fixture：`a33a2d6cbefeba2f3df9e060209ca07f36c1478954155c0b79d528c0e5f8d649`
- contract tests：`96dc8918127ceeedcc7d0ace01825c1446d04dd4d30ebceabf764181af2ef8ed`

## 初审 FAIL 与修复闭环

初审没有放行。发现新增题没有实际语料、近邻干扰只写在理由中、表格查询与证据不闭合、权限题丢失状态码/洁净度断言、继承题行为字段不完整、质量门不能机器计算。随后重建，再审逐项确认：

1. 24 个新增 company/person、date、table 题均绑定仓库内 fixture、唯一 evidence unit 与 locator。
2. 公司名、人名和日期题有同库近邻文档；表格有相似编号、相同数值和错配行。
3. 每个表格证据由表头加唯一目标行构成，所有 `must_match` 原子位于同一证据单元，locator 精确到 Sheet/行。
4. G31–G36 有可执行 action 和结构化断言，覆盖 403 洁净响应、跨库零命中、授权命中、撤权 401、过期 410/重新解析 200、篡改 400；G33 期望件为 `docdb:701`。
5. 48 个 RT-051 继承题保留题号、类别、查询、期望文档、理由与权限/故障行为断言。
6. gold 定义 document Recall@10、exact identifier Recall@10、permission leaks 和 table evidence 计分合同。

## 判据结果

`python3 -m unittest -v tests.test_rt054_stage_a_contracts`：6 项通过。

本签收只证明 gold 本身完整、可执行、可证伪，不证明新检索实现已经达到 Recall 或性能门。阶段 B 必须让实际执行器消费该 gold，并报告逐题结果；不得以本文件代替质量实测。
