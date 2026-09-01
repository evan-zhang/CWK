# CWK Activation Dialogue

Installing the program and authorising it to read someone's work every night
are two different acts. This reference covers the second one.

The conversation is yours to drive. **The judgement is not.** Every decision
that changes whether CWK may run automatically is made by
`scripts/cwk_activation_wizard.py`: the state machine, the two human gates,
the daily contract and its drift, the pilot verdict, and the scheduler handoff.
You read its JSON and explain it. You never assert an outcome it did not emit.

## Hard rules

Never, in any state:

- **Never collect a credential in chat.** Not the key, not a prefix, not "just
  to check". Credentials go into the user's own `.env` or shell, by their hand.
  If the user pastes one anyway, tell them to rotate it and do not repeat it
  back.
- **Never treat your current tool access as authorization.** Being able to read
  CWork right now proves capability, not consent. Only a `confirm-discovery`
  receipt authorises discovery; only a `confirm-activation` receipt authorises
  scheduling.
- **Never show raw evidence.** Discovery reports counts and shapes. If the user
  asks "what did you find", answer with the report's numbers, not with message
  bodies, titles, or names pulled out of `raw/`.
- **Never create, modify, or delete a scheduled task**, and never claim the
  repository did. It emits a handoff describing a task; the host creates it.
- **Never invent an OpenClaw scheduling API.** There is no local documentation
  for one. Hand the user the handoff and let them use their host's own
  mechanism.
- **Never advance past a refusal.** Exit codes 3, 4, 5 and 8 mean stop and
  explain, not retry with different arguments.

## Driving from state

Always start by asking the wizard where things stand, and let `next_step` pick
the conversation. Do not ask the user which step they are on.

```bash
cd "${CWK_PROJECT_DIR:-/workspace/CWK}"
python3 scripts/cwk_activation_wizard.py status
```

`state` is `UNINITIALIZED` before `init`. `next_step` is one of a fixed set of
tokens. Map it to the section below, act, then re-read `status`.

| `next_step` | what the user has to decide |
| --- | --- |
| `init` | nothing yet — explain the boundary first |
| `confirm_discovery_scope` | gate 1: may CWK read, read-only, this scope? |
| `run_discovery` | nothing — you run discovery from existing receipts |
| `propose_profile` | nothing — you draft the profile |
| `confirm_profile` | is this description of my work correct? |
| `run_pilot` | nothing — you run and score one read-only pass |
| `confirm_activation` | gate 2: given this pilot, may it run nightly? |
| `emit_scheduler_handoff` | nothing — you produce the handoff |
| `record_external_schedule` | the user creates the task in the host |
| `reconfirm_contract` | something changed; the old consent is void |
| `rerun_pilot` | the pilot failed; nothing is scheduled |
| `resume_or_reconfirm` | paused by the user |
| `none` | active; report and stop |

If `status` reports `healthy: false`, stop. The private state exists but cannot
be validated. Do not delete it, do not re-`init` over it, do not "start fresh".
Tell the user what the integrity reason was and that any already-scheduled run
must be treated as untrusted until it is resolved.

## 0. Explain the boundary, before anything

Say plainly, in the user's language:

- what CWK will read (their own CWork lanes, read-only);
- what it will never do: mark as read, reply, approve, reject, complete,
  delete, or send anything;
- where the output goes (local Markdown/HTML, optionally derived Wiki pages);
- that raw evidence stays local and is never mixed between people;
- that they will be asked twice: once for reading, once much later for
  scheduling, and that the second question comes only after they have seen a
  real pilot result.

Then `init`. It creates only the private state record — no reads, no tasks.

## 1. Gate one — read-only discovery consent

Write a scope file with the user, describing what they are authorising. It is a
small JSON document: mirror kind, subject reference, the CWork lanes in scope,
and `read_only: true`. Show it to them in full and get an explicit yes.

```bash
python3 scripts/cwk_activation_wizard.py confirm-discovery --scope-file scope.json
```

The confirmation is bound to the hash of that exact scope. Widen the scope
later and the consent voids itself — that is the intended behaviour, not a bug
to work around.

## 2. Discovery — from existing receipts only

Discovery reads run artifacts the user already has. It does not call CWork.

```bash
python3 scripts/cwk_activation_wizard.py record-discovery \
  --scope-file scope.json \
  --collect-manifest runs/<run>/collect-manifest.json \
  --nightly-manifest runs/<run>/nightly-pipeline-manifest.json \
  --acceptance runs/<run>/acceptance.json
```

When reporting the result, keep the report's own distinction intact: entity
*names and aliases seen*, *candidate families*, and *entities the user has
confirmed* are three different numbers. Do not merge them into one impressive
total, and do not describe candidates as if the user had confirmed them.

## 3. Profile — a draft bounded by evidence

Draft a profile from the discovery report: recurring topics, frequent
collaborators, reporting rhythm, primary lanes. Every claim must be traceable
to something in the report. If the evidence is thin, say so and propose less.

```bash
python3 scripts/cwk_activation_wizard.py propose-profile --profile-file profile.json
```

Present it as a proposal and invite correction. Then, and only then:

```bash
python3 scripts/cwk_activation_wizard.py confirm-profile
```

## 4. The daily execution contract — read it out loud

Before asking anyone to accept nightly automation, show them exactly what the
nightly run will do. Render it and walk through it:

```bash
python3 scripts/cwk_activation_wizard.py render-contract --config cwk-mirror.local.json
```

The payload carries both the machine contract and a Markdown rendering. Cover
the schedule, the lanes, the caps, the outputs, the forbidden actions, and
whether publishing is on. The contract is computed from the *actual* config —
if the user disagrees with something in it, change the config, not the
explanation.

## 5. Pilot — one read-only pass, scored by the gate

Run one real read-only nightly pass by the project's normal command, then hand
its receipts to the gate:

```bash
python3 scripts/cwk_activation_wizard.py record-pilot \
  --config cwk-mirror.local.json \
  --nightly-manifest runs/<run>/nightly-pipeline-manifest.json \
  --acceptance runs/<run>/ACCEPTANCE-RESULT.json \
  --collect-manifest runs/<run>/collect-manifest.json
```

All three receipts are required. A missing collection receipt is a FAIL, not a
gap to be argued around: without it there is no evidence the day's sources were
complete. Exit code 4 means the state is now `DEGRADED`; report the failed
predicates by name and stop. Nothing gets scheduled.

## 6. Gate two — scheduling consent

Only after a PASS. Show the pilot receipt — processed count, business date,
which predicates passed — and ask a separate, explicit question: *given this
result, may CWK run this every night at this time?*

Do not roll this into the profile confirmation. Do not treat "the pilot looks
good" as a yes.

```bash
python3 scripts/cwk_activation_wizard.py confirm-activation
```

## 7. Handoff — the host creates the task, not the repository

```bash
python3 scripts/cwk_activation_wizard.py schedule-handoff --config cwk-mirror.local.json
```

The handoff describes the task: cadence, local run time, timezone, the exact
argv, the environment variable *names* it needs, and the preconditions. It
carries no credential values and no absolute paths — the config is named by a
project-relative locator plus a contract telling the host how to resolve the
project root itself (`CWK_PROJECT_DIR`, verified by `scripts/cwk_doctor.py`).

If the config sits outside the project directory the handoff is refused rather
than filled in with a host path. Move the config into the project and retry.

Give the handoff to the user. They create the task with their own host
mechanism. When they report back the identifier the host assigned:

```bash
python3 scripts/cwk_activation_wizard.py record-schedule \
  --external-system openclaw --external-task-id <id-from-the-host>
```

That records what the user did. It does not verify that a task exists, and you
must not claim it does.

## 8. Living with it

```bash
python3 scripts/cwk_activation_wizard.py check-drift --config cwk-mirror.local.json
```

Config or contract changed ⇒ exit code 5, confirmations void, state
`NEEDS_RECONFIRMATION`. Re-walk from the step `next_step` names. An unknown
scheduled task is reported, never deleted — tell the user and let them decide.

`pause` stops automation on the user's word; `resume` restores it. Neither
touches the external task, so a paused mirror still needs the host-side task
disabled if the user wants the run to actually stop.

## Reading the output

Every command emits exactly one JSON object. `ok` is the verdict.
`invalidated_gates` names the confirmations *that command* voided — if it is
non-empty, say so before anything else, because the user is about to be asked
to confirm something again and deserves to know why. Errors are redacted and
truncated by design: no paths, no file contents, no credentials. If an error
seems unhelpfully terse, that is the redaction working; re-run the failing
command yourself rather than asking the user to paste more.
