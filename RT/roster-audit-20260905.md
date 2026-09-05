# RT 花名册大对账（2026-09-05 18:22 Evan 指令）

方法：index.yaml 状态 × 工件证据（receipts/reports）× git 最后活动三方交叉。

## 翻正为 completed（证据齐全）
- RT-002/003/007/008/010：8 月中下旬批次，报告含测试全绿结论（93/93、130/130 等），git 最后活动即收口提交
- RT-029/030/031/032：8 月末~9 月初 CI 治理批次，meta/提交含「最终全量 CI 回执/收口决策」；RT-029 meta 本就自报 completed
- RT-036：receipts 有 owner-refine 回执

## 维持现状（如实）
- RT-017：报告结论「当前不可交付」（gated on RT-017 core PASS）——不能翻 completed，保持 planned
- RT-018~026：PR-001 安全族后续波次，从未启动（相关能力已由 RT-013/014/015 落地），保持 planned 待产品化排期
- RT-028：created 空壳，保持

## 在飞（今日）
- RT-045（验收收尾）/ RT-046（格式工厂返修）/ RT-047（三机生产部署 P1 已过）

## 卫生备注
- index.yaml 存在 in_progress 与 in-progress 两种拼写（G109 按 id 行认条目不受影响）；本次未统一，留待下次治理批次
