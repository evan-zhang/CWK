# PR-001 Current Status

Updated: 2026-08-21
Accepted baseline branch: `pr/PR-001-completion`
Accepted baseline commit: `470daefd2534f6d5af3b0e1d450a6674333d92fc`

## Accepted scope

- RT-011: v1 contracts, security defaults, capability probes — `e3aa3e0`.
- RT-012: instance layout and tenant registry — `089fab0`.
- RT-013: agent binding and credential boundary — `afdc37e`.
- RT-014: immutable canonical evidence store — `e483f67`.
- RT-015: access ledger, tenant view, revocation core — `7ba906f`.
- VG-A: synthetic authority + host-chain integration gate — `88d3326`.
- RT-016: legacy raw shadow import and data reconciliation — `470daef`.

The authoritative RT-011 acceptance is `RT/RT-011/reports/独立验收报告-r2.md`;
the un-suffixed report is retained as historical failed evidence. RT-016 final
acceptance records 171/171 targeted tests, 1063/1063 full tests, and 27/27
independent attacks.

## 2026-08-20 baseline recheck

- Full Python suite: 1063/1063 `OK`.
- RT-011–015 frozen/exact-set guard: 9/9 `OK`.
- Wiki smoke: 14/14 `PASS`.
- Sanitized legacy nightly fixture: all expected artifacts emitted; its single
  tiny fixture reports `overall_pass=false` by documented design and performs
  no mirror publish.
- `git diff --check`: clean before reconciliation edits.
- Token-pattern scan: only intentional synthetic credential strings in test
  fixtures; no production credential was used.

## Gate receipt contract (frozen in Wave-0)

The single machine-readable input for RT-024 / RT-026 lives in
`contracts/gates/`: `verification_gate_receipt_v1.schema.json`
(`cwk.pr001.verification_gate_receipt.v1`), `gate_registry_v1.json` and its
schema. Key properties:

- The registry is **immutable configuration, not state**. It carries no
  `status`/`conclusion`/`verdict`/`last_run_at`; `forbiddenEntryKeys`
  structurally prevents them returning. Running a VG never edits it.
- **Status resolution is exact**: `receipt_path` missing ⇒ that gate is
  **NOT RUN**; present ⇒ the gate's status *is* the `status`/`conclusion`
  inside that receipt. Never inferred from Markdown or from this file.
- **Exact paths**: `gate-receipts/VG-{A..E}/receipt.json`, one per gate.
- **Per-gate `allowed_prerequisite_ids`** is a frozen exhaustive allowlist
  for `prerequisite_refs[].ref_id`, derived from three rank rules: no RT above
  that gate's `feeder_rt`; only strictly earlier VGs; only release gates
  already upstream (G3 consumes VG-A, G4 consumes VG-C, G5 consumes VG-D, so
  each is excluded from its own consumer). `ref_id` must also be unique within
  a receipt. This blocks forward references such as `VG-B → VG-C`.
- **RT-024 and RT-026 are consumers only** — they never create, backfill or
  guess a receipt, and never read gate status from Markdown.
- **Two consumer relations, neither derived from the other.** `consumers` lists
  the DIRECT RT consumers (exactly `RT-024`, `RT-026`);
  `release_gate_consumers` lists the release gates that rebind that VG
  (VG-A→`{G3,G6}`, VG-B→`{G6}`, VG-C→`{G4,G6}`, VG-D→`{G5,G6}`, VG-E→`{G6}`).
  Their value spaces are disjoint (`RT-*` vs `G*`) and each is the exact
  inverse of the other registry file's `consumes_verification_gates`.
- Verified by four stdlib-only modules, measured on 2026-08-21:
  `tests.test_pr001_gate_contracts` **211**,
  `tests.test_pr001_security_gate_contracts` **206**,
  `tests.test_pr001_release_gate_contracts` **106** (G1–G7 contract surface),
  `tests.test_pr001_release_gate_validation` **166** (G1–G7 executable
  positive/negative fixtures). Full discovery currently collects **2059 tests**;
  this Wave-0 candidate's complete full-suite run is still pending, so no full
  PASS/skip result is recorded here yet. None of these freezes which gates have run: VG-B–VG-E
  receipts, the ten SG receipts and every release-gate receipt may appear at
  any time and are picked up automatically.

`gate-receipts/VG-A/receipt.json` **exists now** and is fixed at
`status=pass`, `synthetic=true`, `conclusion=conservative_unknown`. That means
the synthetic gate passed; it does **not** mean real authority is available.
VG-B–VG-E have no receipt file yet and are therefore NOT RUN — a current
observation, not a frozen constraint.

### Synthetic closure: fail-closed today, still reachable later

VG-A is *permanently* synthetic. "Any synthetic hard gate ⇒ unconditional
NO_GO" with no declared exit would make `READY_FOR_G7_REVIEW` unreachable
forever, even after real capability lands. `synthetic_closure_map_v1.json`
makes the exit explicit without weakening fail-closed:

- **"Never upgraded" means three separate things.** (1) A signed receipt is
  never edited or re-signed in place. (2) No consumer — RT-024, RT-026, G6,
  G7 — may reinterpret `synthetic=true` / `conservative_unknown` as anything
  stronger, *even after the gap is closed*. (3) "Never re-run" applies only to
  gates with `rerun_allowed=false`, which is VG-A alone: it verified the
  RT-015 host chain against a `FakeSigningAuthority`, so its scope is a
  permanent fact and its receipt never rotates.
- **VG-A's gap closes only via two separately owned activation receipts**
  (`cwk.pr001.capability_activation_receipt.v1`, cross-listed in SG-00):
  `cwork-authority-source` owned by **RT-017**, and
  `gateway-identity-transport` owned by **RT-023**. They live under
  `capability-receipts/<capability_id>/receipt.json` — never under
  `gate-receipts/` — and use a different domain separator, so the two receipt
  families cannot be swapped.
- **Closure is a conjunction**: `status=pass` ∧ `synthetic=false` ∧
  `owner_rt_independent_pass=true` ∧ `conclusion=capability_activated` ∧
  recomputed `receipt_sha256` matches ∧ every `artifacts[].sha256` matches
  disk. Anything else ⇒ gap OPEN ⇒ **NO_GO**. Once closed, VG-A stays listed
  as scoped evidence with an explicit production caveat; it simply stops being
  a standalone permanent blocker.
- **VG-B–VG-E** may not borrow VG-A's mapping. A synthetic receipt there
  closes only via a non-synthetic re-run: archive the superseded receipt
  byte-identically at `gate-receipts/VG-{X}/archive/{its receipt_sha256}.json`
  (append-only), then publish a strictly newer current receipt at the registry
  path. Nothing signed is ever mutated in place.
- **The VG-B–VG-E chain is machine-walkable, not narrative.** Each of those
  receipts carries a monotonic integer `sequence` (first run = 1; archives plus
  current occupy exactly 1..N with the current at the tip) together with
  `supersedes_receipt_sha256` linking k to k-1. The validator walks one unique
  prefix chain and rejects gap, fork, orphan and cycle; every archive filename
  must equal its own recomputed receipt hash; the current receipt must link the
  latest archive entry. `created_at` monotonicity is an **auxiliary** check, never
  the only ordering proof. **VG-A is a pinned legacy exception**: it is fixed by
  static hash `7058a91a…e918657` in frozen config, carries no `sequence` and no
  supersedes link, its archive must stay absent or empty, and any alternate or
  newly-published VG-A current receipt is rejected outright. Empty / no-current /
  no-archive remains NOT_RUN, not a failure.
- **The map is config, not state.** It carries no `status`/`closed` and no
  point-in-time observation (`wave0_status`, `current_state_note`) that would
  go stale the day an activation receipt lands. "Is it closed?" is always
  recomputed from receipts on disk; the human snapshot lives here and in the
  contracts README.

- **Activation receipts are time-bounded and renewable, never write-once
  forever and never overwritten.** Each capability freezes a
  `max_validity_seconds` in the closure map — 90 days for
  `cwork-authority-source`, a deliberately shorter 30 days for
  `gateway-identity-transport`, since a stale trust-store claim is a silent
  authentication bypass rather than a stale permission list. The validator
  enforces `0 < expires_at - created_at <= max_validity_seconds` on every
  receipt in the chain, with an **inclusive** upper bound and without consulting
  the evaluator's clock, so a receipt exactly at the bound is valid and one
  second over is not. The bound lives only in frozen config, so a receipt can
  never widen its own TTL. Renewal reuses the same current + append-only archive
  shape as the VG chain (`sequence`, `previous_receipt_sha256`,
  `capability-receipts/<capability_id>/archive/<old sha256>.json`), and the owner
  may renew only after a **fresh real probe** while its RT acceptance remains
  valid. RT-026 reads `current` only.
- **Subject binding is ancestry plus ownership, never HEAD equality.** Every
  activation, security and VG-B–VG-E receipt binds an exact 40-hex
  `tested_subject_commit` plus an environment fingerprint. That commit must be an
  ancestor of the evidence-only commit that introduces the receipt, and must be
  the commit the receipt's own evidence and artifacts were produced against. It
  is explicitly **not** required to equal RT-026's final HEAD, because downstream
  merges legitimately advance HEAD after honest evidence was produced.

### Security Gates SG-00–SG-10

`contracts/security/` now holds the **machine authority** for SG-00–SG-10:
`security_gate_registry_v1.json` (frozen registry), its schema (which proves the
registry is config-not-state by forbidding every mutable status key), and one
frozen `security_gate_receipt_v1.schema.json`. The §5.1 Markdown matrix in the
remaining-work plan is derived documentation only. The registry declares ten
exact receipt paths `security-receipts/RT-0{17..26}/receipt.json`, 31 globally
unique frozen claim IDs, the producing task inside each RT's own task list, and
the six filesystem attack classes (`path_traversal`, `symlink_component`,
`symlink_leaf`, `hardlink`, `toctou`, `special_file` — symlink split because
`O_NOFOLLOW` only defends the leaf; `special_file` split because `O_NOFOLLOW`
does not reject a FIFO). SG-03's owner set is exactly all ten packages. Only
RT-022's `SGC-022-02` may be marked N/A, and only with a registry-permitted
`reason_code` plus static evidence.

RT-026's own SG-03/SG-10 receipt is the single `preflight-security` entry: an
independent preflight verifier writes it after the implementation candidate
commit is frozen and before the read-only go/no-go evaluator runs. The evaluator
consumes it and is not its declared author — `go_no_go_evaluation` is deliberately
absent from `write_phase_allowlist`, and the evaluator injects its own
`go_no_go_evaluator_identity` and recomputes exclusion — so RT-026 never depends
on its own output.

**Scope of that claim:** this is *interface-level authorship exclusion*, not
OS-level write denial. The allowlist is a declared-phase check and the identity
comparison is a string comparison; neither shows the kernel refused the process
write access. OS-level read-only execution — trusted launcher, launcher-signed
run attestation, pre/post exact manifests, and real write-denial evidence —
remains RT-026 acceptance evidence under AC-026-11 (tasks T026-15a~d) and is
**not** proven by Wave-0.

**Current observation (this file, not the machine contract):** none of the ten
security receipts exists, so every SG entry is **NOT_RUN**; and neither
activation receipt exists, so VG-A's capability gap is **OPEN**. Today's correct
evaluation is **NO_GO**. RT-026 aggregates only — it may not create, backfill or
infer a security or activation receipt, nor rotate any gate receipt.

### Release Gates G1–G7

`contracts/gates/` now also holds the **machine authority** for the release
gates: `release_gate_registry_v1.json`, its schema,
`release_gate_receipt_v1.schema.json` (`cwk.pr001.release_gate_receipt.v1`,
G1–G6) and `release_authorization_receipt_v1.schema.json`
(`cwk.pr001.release_authorization_receipt.v1`, G7 only). Key properties:

- **Independent receipt root.** Release receipts live at
  `release-gate-receipts/G{1..6}/receipt.json`; G7 is
  `release-gate-receipts/G7/authorization.json`. This root is **disjoint** from
  `gate-receipts/`, which existing VG validators already assert is an *exact*
  VG-A–VG-E closure — sharing one root would have made every release receipt an
  undeclared extra. Both roots enforce whole-subtree closure.
- **Verification ≠ authorization.** G1–G6 are verification receipts signed
  under domain separator `cwk-release-gate-receipt-v1\0` with self-hash field
  `receipt_sha256`. G7 is an authorization receipt signed by an **external**
  trust root under `cwk-release-authorization-receipt-v1\0` with field
  `authorization_sha256`. A cross-family replay fails on *both* the separator
  and the field name, and no project agent or test signer may produce a
  production G7.
- **Exact prerequisite sets, not subsets.** `G1={G0,RT-011}`,
  `G2={G1}`, `G3={G2,VG-A}`, `G4={G3,VG-C}`, `G5={G4,VG-D}`,
  `G6={G1,G2,G3,G4,G5,VG-A,VG-B,VG-C,VG-D,VG-E,RT-026}`, `G7={G6}`. G6
  **rebinds all five** of G1–G5 rather than trusting chain transitivity, which
  would hide a revoked middle gate; G6 freshness is **recomputed**, never
  trusted from the receipt. G7's authority is deliberately narrow.
- **G0 is a bootstrap gate outside the 7-entry registry** (`in_registry:false`).
  Its historical narrative `reviews/审核报告-r4.md` is explicitly recorded as
  *insufficient*; the frozen authority path is
  `reviews/审核报告-wave0-final.md`, which **does not exist yet**, so G1 is
  NOT_RUN by the same absent-file rule as every other gate.
- **AUTHORIZATION IS NOT EXECUTION.** A valid G7 permits a separately executed,
  separately evidenced pilot; it is not itself a deployment, and RT-026's
  terminal token `READY_FOR_G7_REVIEW` means only "may be submitted into G6
  final acceptance" — never G6-passed, G7-authorized or release-approved.

**Current observation (this file, not the machine contract):**
`release-gate-receipts/` **does not exist**, so **G1–G7 are all NOT_RUN**. No
release receipt, authorization receipt or final Wave-0 review report was
created by this remediation, and RT-026 may only consume G1–G5 — it never reads
or writes G6/G7.

## Wave-0 contract remediation ledger (2026-08-20)

Docs/contracts/tests only. **No PASS is claimed here**; every row below is
**pending independent re-review**. No runtime script, production config,
credential, receipt file or migration note was created; the only receipt tree on
disk remains the pre-existing `gate-receipts/VG-A/`.

Prior review round (four Majors), closed earlier in Wave-0:

| # | Finding | Closure surface |
| - | ------- | --------------- |
| P1 | Gate status inferred from Markdown | `contracts/gates/gate_registry_v1.json` + receipt schema; absent-file ⇒ NOT_RUN |
| P2 | Registry carried mutable state | `forbiddenEntryKeys` + `additionalProperties:false` in the registry schema |
| P3 | VG-A synthetic gap had no declared exit | `contracts/gates/synthetic_closure_map_v1.json` |
| P4 | SG-00–SG-10 existed only as a Markdown matrix | `contracts/security/` registry + schemas |

Final review round (six Majors + one stale-count finding), closed by this change:

| # | Finding | Closure surface |
| - | ------- | --------------- |
| A | "Read-only evaluator" overstated as OS-level denial | STATUS.md "Scope of that claim"; RT-026 `需求契约.md`; AC-026-11 / T026-15a~d retained as the real evidence |
| B | RT-023 first-run receipt fields underspecified (`sequence=1`) | `synthetic_closure_map_v1.json` chain rules; `tests.test_pr001_gate_contracts` |
| C | RT-023 DAG lacked real capability evidence | RT-023 activation-receipt ownership recorded in STATUS.md and the closure map |
| D | RT-026 could be read as skipping G6 or self-granting G7 | `RT/RT-026/rt-intake.md`, `specs/需求契约.md`, `tasks/开发任务.md`; `rt026_terminal_conclusion_rule` |
| E | Only one consumer relation existed for VGs | `consumers` vs `release_gate_consumers` in `gate_registry_v1.json`, exact inverses, disjoint value spaces |
| F | No machine-readable release-gate contract for G1–G7 | four files under `contracts/gates/` + `tests.test_pr001_release_gate_contracts` (106) and `tests.test_pr001_release_gate_validation` (166) |
| G | Stale test counts cited in active docs | corrected to measured 211 / 206 / 106 / 166; full discovery currently collects 2059 and remains pending until the complete run finishes |

**Deliberate deviation, flagged for the reviewer.** The independent review
preferred renaming RT-026's terminal token to `READY_FOR_G6_FINAL_ACCEPTANCE`.
It was **not** renamed: `READY_FOR_G7_REVIEW` is frozen in
`synthetic_closure_map_v1.json` and asserted by nine existing tests, so renaming
it would be an unversioned break of a frozen contract. Instead its normative
meaning — "may be *submitted into* G6 final acceptance", never an authorization —
was frozen by `rt026_terminal_conclusion_rule` and restated in every consumer
doc. G6's new `READY_FOR_G7_AUTHORIZATION` conclusion had no freeze conflict and
was adopted as specified. This trade-off is offered for re-review, not settled.

## Remaining execution

Wave-0 现已冻结两组**契约工件**，但这不表示任何下游 RT 已实现或验收：

- neutral `PilotAdmissionProviderV1` runtime/schema/test SHA-256 分别为
  `2bc26ff3b2b71bcfc1d3a5d7fba4a0cca99392a63837a62186086c9f6e817900`、
  `af003563cff163350e2b0ef458a9c5029b0d4330a559779ec0464055aebcc6a6`、
  `1416d8d273868b91eeb0f0f563a1e3103cba8ec27c8a15d5209dd6eb1395386e`；
  它是 central shared ABI，无 RT owner，production adapter 只属于 RT-026。
- RT-017 `CanonicalVersionProviderV1` runtime/schema/test SHA-256 分别为
  `4e6ec4ee88b5b23ebda160ccfb5dd60ee0c39092ca26e2247cf3caae67e1f518`、
  `dca2aceaad46fba973eb51d3ee7c4faa2350dc5191ac1af711cdb66f6bc6a3b3`、
  `aab334e40e36cdb2f48ec2178393921dc91dae585675d28a952065250434dc06`。

PilotAdmission 只约束 `pilot`；`profile_pending/active` 保持既有状态语义。provider
constructor 绑定 purpose，调用只有 `snapshot(*, agent_snapshot)`，九字段快照
TTL≤300s，Null fail closed。RT-022 的 `active` 调用 0 次，`pilot` 成功/cache hit 恰好
2 次；RT-017/018 和 profile workflow 在各自启动与持久化边界重验。当前所有相关 RT
报告仍为 planned/NOT RUN，这两组文件的存在不能被解读为 runtime 已接线。

Each `VG-x` runs only *after* its preceding RT has independently passed on its
own; a VG receipt is never a completion precondition of the RT that feeds it.

1. RT-017 → RT-018 (independent PASS) → VG-B.
2. RT-017 complete Stage-01 AccessLedger compatibility surface (authority injection,
   cleanup v2, `list_profile_eligible`, CanonicalVersionProvider and PilotAdmission)
   independent PASS → RT-019 → RT-020 → RT-021
   (independent PASS) → VG-C. RT-019 is **downstream of RT-017**, not a third
   independent branch: its sampler must read candidates while the profile is
   `profile_pending`, which the public `list_query_eligible` (`pilot|active`
   only) cannot serve, and scanning ledger-private layout is prohibited.
   `cwk_access_ledger.py` has one declared evolution stage, so a narrower eligibility-only
   receipt followed by a second edit is forbidden.
3. RT-022 runs independently in parallel throughout (it owns and freezes its
   own fake `ProfileSpaceSnapshotProviderV1` and fake `SpaceIndexProvider`);
   then RT-021 + RT-022 → RT-023 (independent PASS) → VG-D.
4. RT-024 → RT-025 (independent PASS) → VG-E → RT-026.
5. RT-026 consumes only prerequisite receipts (VG-A–VG-E, G1–G5) and its
   go/no-go conclusion is capped at `READY_FOR_G7_REVIEW`; it never consumes
   G6 or G7.
6. After RT-026 independently passes: full regression, adversarial audit,
   recovery rehearsal, then a *fresh* final acceptor signs G6. G7 release
   readiness is authorized separately, after G6.

Wave-1's real parallelism is therefore **2** (RT-017, RT-022), not 3.

## Boundaries and blockers

- VG-A is synthetic and does not prove a real Gateway identity/transport or a
  production CWork authority/revocation API.
- **Wave-0 completed *read-only feasibility receipts only*.** What was
  finished is a documentation-level review of what the platform *could*
  support (`references/外部能力可行性-2026-08-20.md`), with conclusion
  `conservative_unknown`. Wave-0 did **not** prove any capability, did not
  execute a real controlled transport, and did not obtain an authority
  snapshot. "Feasibility receipt completed" and "capability available" are
  different statements and must not be conflated.
- Gateway identity/transport and CWork authority capabilities therefore remain
  `conservative_unknown` / `blocked` until real controlled receipts exist.
  The outstanding deliverables are **capability activation receipts**, owned
  later: RT-017 owns the CWork authority activation receipt, RT-023 owns the
  Gateway identity/controlled-transport activation receipt. Neither is a
  Wave-0 output.
- Multi-tenant code is not deployed; no production tenant, AppKey, cron,
  Cloud, or DocDB mutation is enabled.
- RT-026 means ready for a separately authorized controlled pilot. G7 and the
  14/30-day real-user release gates are not part of automatic development.
- The local branch is ahead of its old remote baseline and has not been pushed
  or tagged; remote history must be reconciled before any publication.
