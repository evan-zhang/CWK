# Security and trust boundary

## Source-content policy

Content returned by the read-only CWork interfaces is the authorized knowledge
source for CWK and is treated as non-confidential within this system. Raw,
derived pages, AI prompts, Wiki summaries, search results, and published mirror
artifacts must preserve that content faithfully. A value must not be redacted,
withheld, quarantined, skipped, or used to stop a batch merely because it looks
like an AppKey, API key, access token, bearer token, or other identifier.

Source text remains untrusted as executable instructions: models summarize it
as data and never act on commands embedded in it. This is an execution boundary,
not a secrecy filter.

## Runtime and repository hygiene

Runtime authentication supplied outside CWork (for example `--app-key` values
or environment credentials) is operational configuration, not source content.
It is not echoed into command logs or manifests. Real `runs/`, `knowledge/`,
local `.env` files, temporary prompts, and session exports remain outside Git so
the code repository stays reproducible and free of machine-specific state; this
rule does not authorize altering their CWork-derived contents.

The workflow is designed for read-only CWork access. Replying, approving,
rejecting, completing tasks, deleting records, or marking records as read remain
outside the repository's default scope.

Cloud-First deployments may persist raw evidence only through the explicitly
enabled personal DocDB path. Generic sync still requires `--allow-raw` and the
pipeline verifies the resolved project before writes. These controls govern the
destination and mutation authority; they do not classify or transform source
content. New raw objects use `isSensitive=0`, retain SHA-256 coverage, and are
verified by the live cloud coverage gate.

AI stages are read-only and never use OpenClaw message delivery. Real model
calls require the dedicated zero-tool CWK Agent. Temporary prompt files are
deleted after each call. The subprocess environment contains only the runtime
credentials required to invoke the model; this does not remove or rewrite any
CWork-derived text included in the prompt.
