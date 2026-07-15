# RT-001 Architecture Spec — AI 增强理解与质量复核 Pipeline

## 1. 架构总览

```text
Layer 0: CWork 只读采集
  cwk_collect_live.py
  -> raw Markdown evidence

Layer 1: 规则基线 pipeline
  cwk_sample_pilot.py
  cwk_human_digest.py
  cwk_incremental_link_preview.py
  -> digest-human-v4.md/html

Layer 2: AI 单篇理解
  cwk_ai_record_understanding.py
  -> ai-understanding/{report_id}.json

Layer 3: AI 跨篇归并
  cwk_ai_event_clustering.py
  -> ai-events.json
  -> ai-daily-priorities.json

Layer 4: AI 管理版简报
  cwk_ai_enhanced_digest.py
  -> digest-ai-enhanced.md/html

Layer 5: AI 质量复核
  cwk_ai_quality_review.py
  -> quality-review.json/md
```

现有规则 pipeline 保持为必跑 baseline；AI 层作为增强层，可开关、可降级、可对比。

## 2. 数据流

### 2.1 输入

- `runs/<run_name>/raw/*.md` 或 `collected-raw/*.md`
- `runs/<run_name>/extracted/*.json`
- `runs/<run_name>/digest-human-v4.md`
- 可选历史基线：`CWK_HISTORY_RUN_NAME`

### 2.2 输出

```text
runs/<run_name>/
  ai-understanding/
    <report_id>.json
  ai-events.json
  ai-daily-priorities.json
  digest-ai-enhanced.md
  digest-ai-enhanced.html
  quality-review.json
  quality-review.md
  nightly-pipeline-manifest.json
```

## 3. AI 单篇理解 Schema

每篇汇报输出一个 JSON：

```json
{
  "schema_version": "cwk.ai_record_understanding.v1",
  "report_id": "string",
  "title": "string",
  "writer": "string",
  "created_at_shanghai": "YYYY-MM-DD HH:mm:ss",
  "source_lane": "todo_backed|reply_chain|persistent_stream|inbox_awareness|unknown",
  "document_type": "meeting_minutes|request|daily_report|weekly_report|contract_legal|technical_plan|other",
  "event_anchor": "string",
  "event_anchor_confidence": 0.0,
  "summary": "string",
  "background": "string",
  "decisions": [
    {
      "text": "string",
      "evidence": "short quote or section marker"
    }
  ],
  "action_items": [
    {
      "task": "string",
      "owner": "string|null",
      "due_date": "YYYY-MM-DD|null",
      "status": "not_started|in_progress|blocked|done|unknown",
      "evidence": "short quote or section marker"
    }
  ],
  "risks": [
    {
      "risk": "string",
      "severity": "low|medium|high|unknown",
      "evidence": "short quote or section marker"
    }
  ],
  "entities": {
    "people": [],
    "teams": [],
    "systems": [],
    "products": [],
    "projects": []
  },
  "priority_hint": "must_read|review|FYI|archive",
  "noise_flags": [
    "json_artifact",
    "filename_only",
    "low_information",
    "duplicate_candidate"
  ],
  "evidence_refs": [
    {
      "report_id": "string",
      "quote": "short quote"
    }
  ]
}
```

## 4. AI 跨篇归并 Schema

`ai-events.json`：

```json
{
  "schema_version": "cwk.ai_events.v1",
  "run_name": "string",
  "events": [
    {
      "event_id": "stable slug",
      "event_title": "string",
      "event_type": "string",
      "status": "new|continuing|updated|blocked|closed|unknown",
      "priority": "P0|P1|P2|FYI",
      "record_ids": ["string"],
      "history_match": {
        "matched": true,
        "history_event": "string",
        "confidence": 0.0,
        "reason": "string"
      },
      "merged_summary": "string",
      "decisions": [],
      "action_items": [],
      "risks": [],
      "why_it_matters": "string"
    }
  ]
}
```

## 5. 模型配置

环境变量：

```bash
CWK_AI_ENABLED=false
CWK_AI_RECORD_MODEL=
CWK_AI_CLUSTER_MODEL=
CWK_AI_QUALITY_MODEL=
CWK_AI_MAX_PARALLEL=4
CWK_AI_TIMEOUT_SECONDS=120
CWK_AI_AGENT_ID=cwk-ai-reviewer
CWK_AI_THINKING=high
```

推荐默认策略：

- 单篇理解：中高质量模型，可并发。
- 跨篇归并：强模型。
- 质量复核：强模型。

真实模型调用必须使用专用 OpenClaw Agent。该 Agent 必须显式设置 `skills: []`、`tools.profile: minimal`，且唯一附加工具为 `read`；通用 Agent 会被 runtime preflight 拒绝。

不在代码中硬编码供应商密钥；模型 API 凭证走运行环境或 OpenClaw 已有模型配置。

## 6. 降级策略

- 单篇 AI 失败：保留规则抽取结果，给该记录标记 `ai_status=failed`。
- 跨篇归并失败：继续生成规则版 digest，跳过 AI 增强版。
- 质量复核失败：保留 `digest-ai-enhanced.md`，manifest 标记 `quality_review=failed`。
- AI 总失败率超过阈值：pipeline 返回成功但 `degraded=true`，不阻塞 nightly。

## 7. 安全边界

- AI 层只读本地 raw / extracted / digest 文件。
- AI 层不得调用 CWork 写接口。
- AI 层不得输出 appKey、token、环境变量值。
- manifest 只记录模型名、stage 状态、耗时、文件路径，不记录 prompt 全文和密钥。
- GitHub 仓库不提交真实 raw、真实 run、真实 AI 输出。

## 8. 验收命令

```bash
# 关闭 AI 时，现有测试保持通过
CWK_AI_ENABLED=false make test

# fixture 模式验证 AI stage 编排，不调用真实模型
CWK_AI_ENABLED=true CWK_AI_DRY_RUN=true make test

# 真实调用前检查专用只读 Agent，见 docs/AI-PILOT.md

# 生产试运行，生成规则版 + AI 增强版
CWK_AI_ENABLED=true \
CWK_AI_RECORD_MODEL="$CWK_AI_RECORD_MODEL" \
CWK_AI_CLUSTER_MODEL="$CWK_AI_CLUSTER_MODEL" \
CWK_AI_QUALITY_MODEL="$CWK_AI_QUALITY_MODEL" \
python3 scripts/cwk_nightly_pipeline.py \
  --config cwk-mirror.local.json \
  --run-name ai-pilot-$(date +%Y%m%d-%H%M) \
  --date $(date +%F) \
  --sync-docdb
```

## 9. 对比评估

每个 pilot run 输出一份对比报告：

```text
rules_digest: digest-human-v4.md
ai_digest: digest-ai-enhanced.md
quality_score: 0-100
improvements:
  - duplicate_reduction
  - cleaner_summaries
  - better_event_anchors
  - clearer_action_items
regressions:
  - hallucination_risk
  - missed_evidence
  - over_merge
```

切换条件：连续 3 个 pilot run 中，AI 增强版质量评分高于规则版，且无安全边界违规。
