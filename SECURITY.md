# Security

Do not commit:

- CWork app keys or access tokens
- DocDB credentials
- real `runs/`
- real `knowledge/`
- raw work-collaboration evidence
- personal mirror exports
- colleague private data
- real AI prompts, model outputs, or OpenClaw session exports

The workflow is designed for read-only CWork access. Mutating operations such as replying, approving, rejecting, completing tasks, deleting records, or marking records as read are outside this repository's default scope.

If a deployment needs team sharing, publish summaries and confirmed event pages first. Raw evidence should remain personal/private unless explicitly approved.

AI stages are read-only and never use OpenClaw message delivery. Real model calls require a dedicated zero-tool OpenClaw Agent with `tools.profile=minimal`, empty allow lists, `deny=["*"]`, no skills, and `sandbox.mode=off`; general-purpose Agents are rejected at runtime. The OpenClaw CLI reads the private `.cwk-ai-runtime` message file before the model turn, so the reviewer needs no filesystem tool and Docker is not a runtime dependency. The model subprocess environment removes CWork and provider secrets, retaining only Gateway authentication when needed. Model prompts are deleted after each call. Manifests record model names and stage status only; they must not contain prompt text, app keys, tokens, or provider credentials.
