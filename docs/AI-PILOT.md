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

## Single-agent sandbox: `CWK_AI_TRANSPORT=exec`

Deployments that run exactly one Agent (the sandbox assistant itself) do not
need to provision the dedicated reviewer. Set:

```bash
CWK_AI_TRANSPORT=exec
```

Real AI calls then run as one-shot headless turns (`openclaw agent exec --json`)
using the host's configured provider credentials — no extra Agent, no gateway
restart. Everything else stays enforced: the CWK model allowlist, the JSON-only
transform prompt, the secret-scrubbed subprocess environment, timeout with
process-group termination, and retries. Isolation in this mode is
invocation-level (one-shot turn, no delivery, no persistent session, workspace
pinned to `.cwk-ai-runtime`); the config-level zero-tool policy applies only
to the default `agent` transport. Hosts that already provisioned the dedicated
reviewer keep the default and are unaffected.

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
2. Verify the dedicated Agent policy in `openclaw config get agents --json` (look under `entries` by id; OpenClaw 2026.8+).
3. Run one sanitized real-model smoke with `tests/smoke/raw` and `--no-publish-mirror`.
4. Run three side-by-side live pilots without changing the production cron.
5. Compare `quality-review.json`, rules digest, AI digest, evidence coverage, priority inflation, missed actions, missed risks, and over-merge.

Only after three reviewed pilots may an operator propose changing the scheduled job. That change is outside RT-001 implementation and requires explicit authorization.

## CWork source-content policy

Every record returned by the read-only CWork source is authorized knowledge
material. The AI stages receive that content unchanged, including technical
identifiers or strings that resemble keys or tokens. Such text must not trigger
redaction, quarantine, model skipping, or pipeline failure. A runtime lock still
prevents concurrent pilots and clears prompt remnants from an interrupted run.

Historical records retain `collection_mode: historical-backfill` and
`change_type: historical_backfill`. The human digest separates them from
current new, updated, and continuation priorities.
