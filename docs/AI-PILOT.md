# CWK AI Pilot Runtime

## Security requirement

CWK real AI calls must target a dedicated OpenClaw Agent. The Agent policy must be:

```json
{
  "id": "cwk-ai-reviewer",
  "workspace": "/absolute/path/to/CWK/.cwk-ai-runtime",
  "sandbox": {
    "mode": "all",
    "scope": "agent",
    "workspaceAccess": "ro"
  },
  "tools": {
    "profile": "minimal",
    "alsoAllow": ["read"]
  },
  "skills": []
}
```

This exposes only local file `read`. A private `.cwk-ai-runtime` workspace containing only short-lived prompt files is mounted read-only inside an Agent-scoped sandbox. The runtime preflight requires the Agent workspace to match this fixed project-local directory; the path cannot be overridden by an environment variable, and neither it nor `prompts/` may be a symbolic link. The Agent cannot read CWK code, `.env`, run history, other projects, or host files. It does not expose runtime commands, filesystem mutation, CWork/DocDB plugins, messaging, cron, web, sessions, or Agent delegation.

Do not point `CWK_AI_AGENT_ID` at `chat-main-agent`, an operations Agent, or any Agent with `coding`, `messaging`, or `full` tools.

Agent creation and OpenClaw configuration are deployment operations and are intentionally not performed by `install.sh`. Add the dedicated Agent through your normal OpenClaw configuration process, validate the config, and have an external operator restart/reload the Gateway if the local installation requires it.

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
