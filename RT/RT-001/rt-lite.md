# RT-Lite: RT-001 - AI 增强理解与质量复核 Pipeline

> profile: Spec-Lite | status: completed | branch: main

## 1. 目标

把 CWK 从“规则驱动的稳定镜像助手”升级为“AI 增强的管理日报助手”。

核心原则：

- 规则 pipeline 继续作为安全底座。
- AI 层只做理解、归并、审稿，不直接操作 CWork。
- 默认关闭 AI，生产 pilot 显式开启。
- 所有 AI 结论必须能追溯到 report_id 或原文证据。

## 2. 改造内容

### 2.1 新增脚本

- `scripts/cwk_ai_record_understanding.py`
- `scripts/cwk_ai_event_clustering.py`
- `scripts/cwk_ai_enhanced_digest.py`
- `scripts/cwk_ai_quality_review.py`

### 2.2 修改脚本

- `scripts/cwk_nightly_pipeline.py`
  - 新增 AI stage 编排。
  - 新增 degraded 状态。
  - manifest 记录 AI 输出路径和质量报告。

### 2.3 新增配置

```bash
CWK_AI_ENABLED=false
CWK_AI_RECORD_MODEL=
CWK_AI_CLUSTER_MODEL=
CWK_AI_QUALITY_MODEL=
CWK_AI_MAX_PARALLEL=4
CWK_AI_TIMEOUT_SECONDS=120
CWK_AI_DRY_RUN=false
CWK_AI_AGENT_ID=cwk-ai-reviewer
CWK_AI_THINKING=high
```

### 2.4 新增输出

- `ai-understanding/*.json`
- `ai-events.json`
- `ai-daily-priorities.json`
- `digest-ai-enhanced.md`
- `digest-ai-enhanced.html`
- `quality-review.json`
- `quality-review.md`

## 3. 不可破坏边界

- 不调用 CWork mutating command。
- 不提交真实 raw/run/AI 输出到 GitHub。
- 不在 manifest/log 中记录密钥、appKey、token。
- 真实模型调用必须通过无 Skills、仅允许 `read` 的专用 OpenClaw Agent。
- AI 失败不能导致规则版 nightly 失败。
- AI 增强版上线前必须并行 pilot，不直接替换规则版。

## 4. 验收标准

- [x] `CWK_AI_ENABLED=false make test` 通过。
- [x] `CWK_AI_ENABLED=true CWK_AI_DRY_RUN=true make test` 通过。
- [x] AI pilot run 生成 `digest-ai-enhanced.md/html`。
- [x] AI pilot run 生成 `quality-review.json/md`。
- [x] manifest 包含 AI stage 状态、模型名、耗时、输出路径。
- [x] manifest 不包含 appKey/token。
- [x] AI 增强版每条优先事项都能追溯 report_id。
- [x] 规则版 `digest-human-v4.md/html` 仍生成。
- [x] 生产 cron 未授权前仍默认运行规则版。

## 5. 实施顺序

1. 定义 AI JSON schema 与 dry-run fixture。
2. 实现单篇理解 stage。
3. 实现跨篇归并 stage。
4. 实现 AI 增强 digest。
5. 实现质量复核。
6. 接入 nightly pipeline。
7. 文档与 CI。
8. 连续 3 天 pilot 对比。

## 6. 风险

- 幻觉：必须要求 evidence_refs。
- 过度合并：保留置信度与人工复核列表。
- 成本高：用环境变量控制并发和模型。
- 模型不可用：AI stage 降级，不影响规则版。
- 隐私泄露：禁止输出密钥，真实产物只留本地和授权知识库。

## 7. 交付口径

本 RT 完成后发布 `v0.2.0-ai-pilot`，定位为内部试运行，不替代 `v0.1.0-internal` 稳定规则版。
