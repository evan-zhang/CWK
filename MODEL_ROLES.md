# CWK 模型角色矩阵 — 双模型约束

> 生效日期: 2026-08-01
> 约束等级: **硬约束** — 代码层强制 allowlist，不可静默漂移

## 允许的模型

| 角色简称 | OpenClaw 模型 ID | 厂商/型号 |
|---|---|---|
| MiniMax | `newapi/BD-MiniMax` | MiniMax M3 |
| GLM | `newapi/BD-glm` | 智谱 GLM-5.2 |

**禁止用于 CWK 流水线的模型**: `openai/gpt-*`, `claude-*`, `grok-*`, 以及其他任何非上述两个 ID 的模型。代码中的 `assert_cwk_model()` 会在每次 AI 调用前拒绝不在 allowlist 内的模型 ID。

## 角色分配

### Role 1 — 云Wiki摘要编译 (cloud-wiki-compile)
- **模型**: `evan-openai/glm-5.3-flash`
- **脚本**: `cwk_cloud_wiki_compile.py`
- **环境变量**: `CWK_CLOUD_WIKI_MODEL`（默认 `evan-openai/glm-5.3-flash`）
- **理由**: 大批量文档摘要生成；2026-08-30 实测精编质量达标（quote 忠实度 100%，22-29s/篇），价格 0.8/2.8/0.2 元每百万 tokens，替代 MiniMax（旧模型失败率高，138 篇重试未解决）
- **格式修复兜底**: `deepseek/deepseek-v4-flash` 官方渠道，仅在主力返回不可解析或不合约 JSON 时调用；环境变量 `CWK_CLOUD_WIKI_REPAIR_MODEL`。理由：内网 BD-glm 出现 billing 失败（2026-08-30），修复通道改走外部官方渠道隔离风险

### Role 2 — 记录理解 (record-understanding)
- **模型**: `newapi/BD-MiniMax`
- **脚本**: `cwk_ai_record_understanding.py`
- **环境变量**: `CWK_AI_RECORD_MODEL`（默认 `newapi/BD-MiniMax`）
- **流水线入口**: `cwk_nightly_pipeline.py --ai-record-model`
- **理由**: 批量结构化提取，MiniMax 足够且成本最低

### Role 3 — 主题/实体综合 (topics/entities synthesis)
- **模型**: `newapi/BD-glm`
- **脚本**: `cwk_ai_event_clustering.py`（含 topics/entities 阶段）
- **环境变量**: `CWK_AI_CLUSTER_MODEL`（默认 `newapi/BD-glm`）
- **流水线入口**: `cwk_nightly_pipeline.py --ai-cluster-model`
- **理由**: 综合分析需要更强的推理和聚合能力，用户指定 GLM-5.2

### Role 4 — 事件聚类 (event clustering)
- **模型**: `newapi/BD-glm`
- **脚本**: `cwk_ai_event_clustering.py`
- **环境变量**: `CWK_AI_CLUSTER_MODEL`（默认 `newapi/BD-glm`）
- **理由**: 跨文档事件关联与聚类，需要深度语义理解，用户指定 GLM-5.2

### Role 5 — 质量审核 (quality review)
- **模型**: `newapi/BD-glm`
- **脚本**: `cwk_ai_quality_review.py`
- **环境变量**: `CWK_AI_QUALITY_MODEL`（默认 `newapi/BD-glm`）
- **流水线入口**: `cwk_nightly_pipeline.py --ai-quality-model`
- **理由**: 质量审核需要批判性判断，GLM-5.2 推理更强

### Role 6 — 复杂问答 (complex Q&A)
- **模型**: `newapi/BD-glm`
- **理由**: 交互式问答场景需要最佳推理能力

## 代码实现

### Allowlist 守卫

`cwk_ai_common.py` 中定义:

```python
CWK_ALLOWED_MODELS = {"newapi/BD-MiniMax", "newapi/BD-glm"}

def assert_cwk_model(model: str) -> None:
    if not model:
        raise ValueError(...)
    if model not in CWK_ALLOWED_MODELS:
        raise ValueError(f"CWK pipeline rejects model {model!r}. ...")
```

调用路径:
- `invoke_openclaw_json()` → `assert_cwk_model(model)` — 覆盖所有 AI 阶段
- `cwk_cloud_wiki_compile.py:main()` → `assert_cwk_model(args.model)` — 编译入口独立校验

### 默认值

| 脚本 | 参数 | 默认值 |
|---|---|---|
| `cwk_cloud_wiki_compile.py` | `--model` | `newapi/BD-MiniMax` |
| `cwk_cloud_wiki_compile.py` | `--repair-model` | `newapi/BD-glm` |
| `cwk_nightly_pipeline.py` | `--ai-record-model` | `newapi/BD-MiniMax` |
| `cwk_nightly_pipeline.py` | `--ai-cluster-model` | `newapi/BD-glm` |
| `cwk_nightly_pipeline.py` | `--ai-quality-model` | `newapi/BD-glm` |
| `CONFIG.example.json` | `ai_record_model` | `newapi/BD-MiniMax` |
| `CONFIG.example.json` | `ai_cluster_model` | `newapi/BD-glm` |
| `CONFIG.example.json` | `ai_quality_model` | `newapi/BD-glm` |

## 变更历史

- **2026-08-01**: 初始版本。锁定为 MiniMax M3 + GLM-5.2 双模型。此前所有 openai/gpt/claude 默认值已清除。
