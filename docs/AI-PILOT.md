# CWK AI Pilot Runtime

## Security requirement

CWK real AI calls must target a dedicated OpenClaw Agent. The Agent policy must be:

```json
{
  "id": "cwk-ai-reviewer",
  "workspace": "/absolute/path/to/CWK/.cwk-ai-runtime",
  "sandbox": {
    "mode": "off"
  },
  "tools": {
    "profile": "minimal",
    "allow": [],
    "alsoAllow": [],
    "deny": ["*"]
  },
  "skills": []
}
```

This is a zero-tool message transformer. The OpenClaw CLI reads the short-lived
`--message-file` and sends its content as the user message; the model worker does
not need filesystem `read` or any other tool. Sandbox mode is deliberately off,
so Docker is not a runtime dependency. Safety comes from the stricter tool policy:
`deny: ["*"]`, empty allow lists, no skills, no delivery, and a code-level preflight
that rejects policy drift. The fixed `.cwk-ai-runtime` workspace remains a local
staging directory for temporary prompts and cannot be overridden or symlinked.

Do not point `CWK_AI_AGENT_ID` at `chat-main-agent`, an operations Agent, or any Agent with `coding`, `messaging`, or `full` tools.

Agent creation and OpenClaw configuration are deployment operations and are intentionally not performed by `install.sh`. Add the dedicated Agent through your normal OpenClaw configuration process and validate the config. Current OpenClaw releases hot-reload this agent policy; if a particular installation requires a Gateway restart, run it externally rather than from a Life-triggered turn.

## Configuration

```bash
CWK_AI_ENABLED=true
CWK_AI_DRY_RUN=false
CWK_AI_AGENT_ID=cwk-ai-reviewer
CWK_AI_RECORD_MODEL=provider/model
CWK_AI_CLUSTER_MODEL=provider/model
CWK_AI_QUALITY_MODEL=provider/model
CWK_AI_MAX_PARALLEL=4
CWK_AI_TIMEOUT_SECONDS=120
CWK_AI_THINKING=high
```

## Pre-production sequence

1. Run `CWK_AI_ENABLED=true CWK_AI_DRY_RUN=true make smoke-ai`.
2. Verify the dedicated Agent policy in `openclaw config get agents.list --json`.
3. Run one sanitized real-model smoke with `tests/smoke/raw` and `--no-publish-mirror`.
4. Run three side-by-side live pilots without changing the production cron.
5. Compare `quality-review.json`, rules digest, AI digest, evidence coverage, priority inflation, missed actions, missed risks, and over-merge.

Only after three reviewed pilots may an operator propose changing the scheduled job. That change is outside RT-001 implementation and requires explicit authorization.

## Sensitive-source quarantine

Before creating a prompt, the runner scans the complete source record for
secret-shaped values. Matching records are marked `skipped_sensitive`, withheld
from the model and AI clustering, and retained only in the deterministic rules
path. A runtime lock prevents concurrent pilots and clears prompt remnants from
an interrupted prior run. Any real credential found in a source must be rotated
at its issuing system; local redaction does not revoke a credential.

Historical records retain `collection_mode: historical-backfill` and
`change_type: historical_backfill`. The human digest separates them from
current new, updated, and continuation priorities.
