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

Cloud-First deployments may persist raw evidence only in the authorized user's
**personal/private DocDB** after explicit approval. Generic sync denies `raw/`;
the migration/nightly path must pass `--allow-raw`, use physical-file versions,
verify that the resolved project is the authenticated user's personal project,
set `isSensitive=1` for newly created raw objects, record SHA-256 in
`cloud-objects.json`, and pass the live cloud coverage gate. Raw
must never be uploaded to a shared/team folder or made public by default.
Large raw Markdown is published as content-addressed gzip parts because DocDB's
Markdown materializer can return zero bytes for multi-megabyte files; every
part and the reconstructed logical raw are independently hash-checked.
The live coverage gate repeats the personal-project assertion, records the
resolved project/root identifiers, and fails while any persistent retry item
remains. This turns the privacy boundary into a continuously auditable
condition instead of a one-time upload precondition.

AI stages are read-only and never use OpenClaw message delivery. Real model calls require a dedicated zero-tool OpenClaw Agent with `tools.profile=minimal`, empty allow lists, `deny=["*"]`, no skills, and `sandbox.mode=off`; general-purpose Agents are rejected at runtime. The OpenClaw CLI reads the private `.cwk-ai-runtime` message file before the model turn, so the reviewer needs no filesystem tool and Docker is not a runtime dependency. The model subprocess environment removes CWork and provider secrets, retaining only Gateway authentication when needed. Model prompts are deleted after each call. Manifests record model names and stage status only; they must not contain prompt text, app keys, tokens, or provider credentials.
