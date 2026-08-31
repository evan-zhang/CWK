"""Real evidence binding for PR-001 receipts: git ancestry, owner scope, clock.

Three families of receipt (verification gate, capability activation, security
gate) all claim to bind `tested_subject_commit` -- "the exact candidate commit
this evidence was produced against".  Originally every validator only checked
that the value matched `^[0-9a-f]{40}$`, so `"f" * 40` was accepted and the
whole staleness defence was decorative.  An independent review demonstrated
exactly that.  This module makes the claim mechanical.

The frozen two-commit evidence pattern this preserves
-----------------------------------------------------
    commit S  (subject)   the implementation candidate, frozen first
    commit E  (evidence)  introduces the receipt file, which binds S

`S` must be a *strict* ancestor of `E`.  Nothing in `S` can reference `E`,
because `E` does not exist yet, and `E`'s receipt binds `S` by hash-free commit
id.  Reports reference the receipt later still.  No two artifacts ever hash
each other, so the graph stays acyclic.  A receipt that binds `E` itself, or a
commit on an unrelated branch, or a commit that does not exist, is rejected.

Design corrections applied after an independent read-only audit
---------------------------------------------------------------
The audit reproduced four concrete defects against the first version of this
module.  Each is now closed:

1.  **`commit_introducing()` returned the FIRST commit that added a path, using
    `--follow`.**  A renewed receipt at sequence 2 lives at the same path as
    sequence 1, so the "evidence commit" resolved to the sequence-1 commit and
    a sequence-2 receipt could bind a subject that was only an ancestor of the
    *old* evidence.  `--follow` made it worse by chasing renames heuristically.
    Evidence commits are now derived as the UNIQUE LATEST blob-changing commit
    reachable from an explicit `evaluation_commit`, with no `--follow`.

2.  **The evidence commit was never checked against the bytes on disk.**  The
    receipt actually read by the evaluator and the receipt recorded in git
    could differ.  `resolve_evidence_commit()` now requires the git blob at `E`
    to equal the safe-read bytes byte-for-byte, requires `E` to have exactly
    one parent (a merge can smuggle content from an unreviewed side), and
    requires the blob to be unchanged between `E` and `evaluation_commit`.

3.  **Owner scope used `str.startswith`.**  `RT/RT-023-evil/x` therefore
    satisfied the `RT/RT-023` prefix.  Matching is now on exact path
    boundaries.

4.  **Owner scope conflated code with evidence output.**  The prefixes included
    the capability's own receipt directory, so a commit that touched nothing
    but the receipt satisfied "the subject commit modified the owning package".
    Owner CODE prefixes and owner EVIDENCE-OUTPUT prefixes are now separate,
    and only a change under a CODE prefix counts as subject scope.

Additionally the subject's owner-scope tree digest is bound, so two different
commits that both touch the package cannot be substituted for one another.

Clock
-----
`EvaluationClock` replaces the hard-coded `_now = 2026-10-15` that used to sit
in the gate suite.  A baked future date silently mis-classifies receipts (a
receipt valid on the real evaluation date was reported EXPIRED, and a receipt
dated 2099 was reported VALID).  Production callers use `EvaluationClock.now()`
which derives real UTC; fixtures inject a deterministic instant.  `not_before`
enforcement rejects a receipt created after the evaluation instant plus a
frozen, bounded skew allowance.

Renewal probes
--------------
A renewal at sequence N must be backed by a *fresh real probe*, not by a copy
of the sequence-1 evidence with a different byte.  `verify_probe_manifest()`
freezes the manifest shape (capability, sequence, challenge, observed_at,
subject, environment, API version, result, verifier, signature), recomputes the
signature, and enforces monotonic `observed_at` plus a bounded probe age.  That
age is measured backwards from the receipt the probe certifies, not from the
evaluation instant: anchoring it to "now" would have made every renewal older
than a day invalid and the contract's own 90-day TTL unreachable.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

import pr001_safe_read as _sr

SHA1_RE = re.compile(r"\A[0-9a-f]{40}\Z")
SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")

# Frozen, bounded allowance for honest clock disagreement between the machine
# that signed a receipt and the machine evaluating it.  Deliberately small: it
# exists to tolerate NTP drift, not to admit future-dated evidence.
DEFAULT_MAX_CLOCK_SKEW_SECONDS = 300

# A renewal probe certifies a live external capability.  Evidence older than
# this was not observed "for this renewal" in any meaningful sense.
DEFAULT_MAX_PROBE_AGE_SECONDS = 86_400

_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    # An object-replacement ref could make cat-file return content that is not
    # what the commit actually records.  Evidence must be the real object.
    "GIT_NO_REPLACE_OBJECTS": "1",
    "LC_ALL": "C",
}

_OWNER_TREE_DOMAIN = b"cwk-owner-scope-tree-v1\0"
_SECURITY_OWNER_TREE_DOMAIN = b"cwk-owner-scope-tree-v2\0"
_PROBE_DOMAIN = b"cwk-capability-renewal-probe-v1\0"

_SECURITY_FORBIDDEN_OWNER_COMPONENTS = frozenset(
    {
        "reports",
        "receipts",
        "security-receipts",
        "capability-receipts",
        "gate-receipts",
        "release-gate-receipts",
    }
)
_SECURITY_TEST_SUFFIX_RE = re.compile(r"\A[a-z0-9_]+\.py\Z")


class EvidenceBindingError(Exception):
    """Raised when a subject commit cannot be bound. Never means 'maybe'."""


# ---------------------------------------------------------------------------
# path boundaries
# ---------------------------------------------------------------------------


def path_within(path: object, prefix: object) -> bool:
    """Exact path-boundary containment.

    `startswith` is wrong here and the audit proved it: with a prefix of
    `RT/RT-023`, the path `RT/RT-023-evil/receipt.json` matched, so an attacker
    could create a sibling package whose name merely begins with the owner's
    and have it count as owner scope.  Containment is a *directory* relation,
    so it must end on a separator.
    """

    if not isinstance(path, str) or not isinstance(prefix, str):
        return False
    if not path or not prefix:
        return False
    clean = prefix.rstrip("/")
    if not clean:
        return False
    return path == clean or path.startswith(clean + "/")


def path_within_any(path: object, prefixes: object) -> bool:
    if not isinstance(prefixes, (list, tuple)):
        return False
    return any(path_within(path, p) for p in prefixes)


# ---------------------------------------------------------------------------
# clock
# ---------------------------------------------------------------------------


class EvaluationClock:
    """An explicit evaluation instant plus a frozen skew allowance.

    Nothing in the evaluators reads the wall clock directly any more: an
    evaluation instant is always passed in, so a test is deterministic and a
    production run is honest about *when* it decided something.
    """

    def __init__(
        self,
        instant: _dt.datetime,
        *,
        max_skew_seconds: int = DEFAULT_MAX_CLOCK_SKEW_SECONDS,
        max_probe_age_seconds: int = DEFAULT_MAX_PROBE_AGE_SECONDS,
    ) -> None:
        if instant.tzinfo is None:
            raise ValueError("evaluation instant must be timezone-aware UTC")
        if not isinstance(max_skew_seconds, int) or isinstance(max_skew_seconds, bool):
            raise ValueError("max_skew_seconds must be an int")
        if not 0 <= max_skew_seconds <= 3600:
            raise ValueError("max_skew_seconds must be within [0, 3600]")
        if not isinstance(max_probe_age_seconds, int) or isinstance(max_probe_age_seconds, bool):
            raise ValueError("max_probe_age_seconds must be an int")
        if not 0 < max_probe_age_seconds <= 30 * 86_400:
            raise ValueError("max_probe_age_seconds must be within (0, 30 days]")
        self.instant = instant.astimezone(_dt.timezone.utc)
        self.max_skew_seconds = max_skew_seconds
        self.max_probe_age_seconds = max_probe_age_seconds

    @classmethod
    def now(
        cls,
        *,
        max_skew_seconds: int = DEFAULT_MAX_CLOCK_SKEW_SECONDS,
        max_probe_age_seconds: int = DEFAULT_MAX_PROBE_AGE_SECONDS,
    ) -> "EvaluationClock":
        """The production spelling: real UTC now, never a baked constant."""

        return cls(
            _dt.datetime.now(_dt.timezone.utc),
            max_skew_seconds=max_skew_seconds,
            max_probe_age_seconds=max_probe_age_seconds,
        )

    @property
    def not_after(self) -> _dt.datetime:
        """The latest `created_at` an honest receipt may carry."""

        return self.instant + _dt.timedelta(seconds=self.max_skew_seconds)

    @property
    def probe_not_before(self) -> _dt.datetime:
        """The earliest `observed_at` a renewal probe may carry."""

        return self.instant - _dt.timedelta(seconds=self.max_probe_age_seconds)

    def is_future_dated(self, created_at: _dt.datetime) -> bool:
        return created_at > self.not_after

    def is_expired(self, expires_at: _dt.datetime) -> bool:
        # Inclusive: a receipt is dead at the instant it expires.
        return expires_at <= self.instant

    def is_stale_probe(
        self, observed_at: _dt.datetime, certified_at: _dt.datetime | None = None
    ) -> bool:
        """Was this probe too old to have certified `certified_at`?

        Probe age is measured backwards from the RECEIPT the probe justifies,
        not from the evaluation instant.  Anchoring it to "now" was incoherent
        with the contract's own 90-day activation TTL: any renewal older than
        `max_probe_age_seconds` would have gone invalid a day after it was
        signed, which would make the TTL unreachable and re-create exactly the
        dead end expiry exists to remove.  A probe certifies a renewal at the
        moment of that renewal, and the receipt's own expiry is what bounds how
        long that certification remains good.
        """

        anchor = self.instant if certified_at is None else certified_at
        return observed_at < anchor - _dt.timedelta(seconds=self.max_probe_age_seconds)


def parse_instant(value: object) -> _dt.datetime | None:
    """Parse a strict UTC ISO-8601 instant, or `None`. Naive values are refused."""

    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(_dt.timezone.utc)


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


class GitSubject:
    """Ancestry queries against a real git repository.

    Every method fails closed: an unavailable repository, an unknown object or
    a non-zero git exit is `False`/`None`, never an optimistic default.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # --- plumbing ---------------------------------------------------------

    def _run(self, *args: str) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                env=_GIT_ENV,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        """Text-mode helper, used only by fixture setup and simple hex output."""

        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            env=_GIT_ENV,
            timeout=30,
        )

    def _out_bytes(self, *args: str) -> bytes | None:
        proc = self._run(*args)
        if proc is None or proc.returncode != 0:
            return None
        return proc.stdout

    def _out_text(self, *args: str) -> str | None:
        raw = self._out_bytes(*args)
        if raw is None:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    @classmethod
    def for_repo(cls, root: Path) -> "GitSubject | None":
        """A usable `GitSubject`, or `None` when `root` is not a git work tree."""

        subject = cls(root)
        out = subject._out_text("rev-parse", "--is-inside-work-tree")
        if out is None or out.strip() != "true":
            return None
        return subject

    # --- commit queries ---------------------------------------------------

    def commit_exists(self, sha: object) -> bool:
        if not isinstance(sha, str) or not SHA1_RE.match(sha):
            return False
        proc = self._run("cat-file", "-e", f"{sha}^{{commit}}")
        return proc is not None and proc.returncode == 0

    def is_ancestor(self, ancestor: object, descendant: object) -> bool:
        """True only for a genuine ancestor relation, `merge-base --is-ancestor`."""

        if not self.commit_exists(ancestor) or not self.commit_exists(descendant):
            return False
        proc = self._run("merge-base", "--is-ancestor", str(ancestor), str(descendant))
        return proc is not None and proc.returncode == 0

    def is_strict_ancestor(self, ancestor: object, descendant: object) -> bool:
        """Ancestry AND non-identity: a receipt may not bind its own commit."""

        if ancestor == descendant:
            return False
        return self.is_ancestor(ancestor, descendant)

    def resolve(self, rev: str) -> str | None:
        out = self._out_text("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
        if out is None:
            return None
        text = out.strip()
        return text if SHA1_RE.match(text) else None

    def head(self) -> str | None:
        return self.resolve("HEAD")

    def parents(self, sha: str) -> tuple[str, ...] | None:
        """Parent commit ids, or `None` when the commit is unknown."""

        if not self.commit_exists(sha):
            return None
        out = self._out_text("rev-list", "--parents", "--max-count=1", sha)
        if out is None:
            return None
        fields = out.split()
        if not fields or fields[0] != sha:
            return None
        parents = tuple(f for f in fields[1:] if SHA1_RE.match(f))
        if len(parents) != len(fields) - 1:
            return None
        return parents

    def paths_touched(self, sha: str) -> frozenset[str]:
        """Repo-relative paths a commit changed. Empty set when unknown.

        `-z` matters: a path containing a newline (or any byte git would
        otherwise quote) must not be able to forge an extra record.
        """

        if not self.commit_exists(sha):
            return frozenset()
        raw = self._out_bytes(
            "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "--no-renames", "-z", sha
        )
        if raw is None:
            return frozenset()
        names: set[str] = set()
        for chunk in raw.split(b"\0"):
            if not chunk:
                continue
            try:
                names.add(chunk.decode("utf-8"))
            except UnicodeDecodeError:
                # A path we cannot even decode is never a declared artifact.
                continue
        return frozenset(names)

    # --- blob / tree queries ---------------------------------------------

    def blob_bytes(self, commit: str, rel: str) -> bytes | None:
        """The exact bytes `rel` had at `commit`, or `None`."""

        cache = getattr(self, "_blob_bytes_cache", None)
        if cache is None:
            cache = self._blob_bytes_cache = {}
        key = (commit, rel)
        if key in cache:
            return cache[key]
        if not self.commit_exists(commit):
            cache[key] = None
            return None
        spec = f"{commit}:{rel}"
        sha = self._out_text("rev-parse", "--verify", "--quiet", spec)
        if sha is None:
            cache[key] = None
            return None
        sha = sha.strip()
        if not SHA256_RE.match(sha) and not SHA1_RE.match(sha):
            cache[key] = None
            return None
        kind = self._out_text("cat-file", "-t", sha)
        if kind is None or kind.strip() != "blob":
            cache[key] = None
            return None
        cache[key] = self._out_bytes("cat-file", "blob", sha)
        return cache[key]

    def latest_commit_touching(self, rel: str, *, until: str) -> str | None:
        """The newest commit reachable from `until` that CHANGED `rel`.

        Deliberately not `--follow` (rename heuristics are not evidence) and
        deliberately the LATEST rather than the first: a renewed receipt at
        sequence 2 occupies the same path as sequence 1, and binding the
        sequence-1 commit would let a stale subject pass.
        """

        if not self.commit_exists(until):
            return None
        out = self._out_text(
            "rev-list", "--max-count=1", "--no-renames", until, "--", rel
        )
        if out is None:
            return None
        text = out.strip()
        return text if SHA1_RE.match(text) else None

    def owner_scope_tree_sha256(self, commit: str, prefixes: object) -> str | None:
        """A domain-separated digest of the owner's code tree at `commit`.

        Ancestry says *when*, owner scope says *which package*; this says
        *exactly which content*.  Two different commits that both touch the
        package produce different digests, so a receipt cannot be re-pointed at
        a neighbouring commit that happens to satisfy the other two rules.
        """

        if not self.commit_exists(commit):
            return None
        if not isinstance(prefixes, (list, tuple)) or not prefixes:
            return None
        records: set[bytes] = set()
        for prefix in sorted(str(p) for p in prefixes):
            raw = self._out_bytes("ls-tree", "-r", "-z", "--full-tree", commit, "--", prefix)
            if raw is None:
                return None
            for chunk in raw.split(b"\0"):
                if not chunk:
                    continue
                head, _, path = chunk.partition(b"\t")
                if not path:
                    return None
                fields = head.split()
                if len(fields) != 3:
                    return None
                mode, kind, obj = fields
                records.add(b" ".join((mode, kind, obj)) + b"\t" + path)
        digest = hashlib.sha256(_OWNER_TREE_DOMAIN)
        for record in sorted(records):
            digest.update(record)
            digest.update(b"\0")
        return digest.hexdigest()

    def candidate_tree_sha256(
        self,
        commit: str,
        *,
        excluded_prefixes: object = (),
        excluded_patterns: object = (),
    ) -> str | None:
        """Digest the whole tracked tree minus a closed evidence exclusion."""
        if not self.commit_exists(commit):
            return None
        prefixes = tuple(str(p) for p in excluded_prefixes or ())
        try:
            patterns = tuple(re.compile(str(p)) for p in excluded_patterns or ())
        except re.error:
            return None
        raw = self._out_bytes("ls-tree", "-r", "-z", "--full-tree", commit)
        if raw is None:
            return None
        records: set[bytes] = set()
        for chunk in raw.split(b"\0"):
            if not chunk:
                continue
            head, _, path = chunk.partition(b"\t")
            if not path:
                return None
            fields = head.split()
            if len(fields) != 3:
                return None
            mode, kind, obj = fields
            try:
                rel = path.decode("utf-8")
            except UnicodeDecodeError:
                return None
            if path_within_any(rel, prefixes) or any(
                pattern.search(rel) for pattern in patterns
            ):
                continue
            records.add(b" ".join((mode, kind, obj)) + b"\t" + path)
        digest = hashlib.sha256(_OWNER_TREE_DOMAIN)
        for record in sorted(records):
            digest.update(record)
            digest.update(b"\0")
        return digest.hexdigest()


class ReleaseRepositoryFacts:
    """Authoritative release facts derived from one explicit git snapshot.

    ``evaluation_commit`` is never inferred from moving ``HEAD``.  Every fact
    used by the canonical release entrypoint comes from this object: the
    evidence commit that introduced the bytes being read, strict subject
    ancestry, the whole non-evidence candidate tree, per-gate owner touch,
    tracked artifact blobs, and external receipt subject/tree/environment
    bindings.  Construction fails closed for a non-repository or an unknown
    evaluation commit.
    """

    REGISTRY_REL = (
        "PR/PR-001-multitenant-knowledge-spaces/contracts/gates/"
        "release_gate_registry_v1.json"
    )

    # Every policy or schema that can change a canonical release verdict is a
    # first-class input to the explicit evaluation snapshot.  Reading one of
    # these from moving HEAD (or accepting a caller-supplied replacement)
    # would let the same receipt mean two different things for the same
    # ``evaluation_commit``.
    BOUND_JSON_RELS = {
        "release_registry": REGISTRY_REL,
        "release_registry_schema": (
            "PR/PR-001-multitenant-knowledge-spaces/contracts/gates/"
            "release_gate_registry_v1.schema.json"
        ),
        "release_receipt_schema": (
            "PR/PR-001-multitenant-knowledge-spaces/contracts/gates/"
            "release_gate_receipt_v1.schema.json"
        ),
        "release_authorization_schema": (
            "PR/PR-001-multitenant-knowledge-spaces/contracts/gates/"
            "release_authorization_receipt_v1.schema.json"
        ),
        "go_no_go_schema": (
            "PR/PR-001-multitenant-knowledge-spaces/contracts/rt026/schemas/"
            "go_no_go_report_v1.schema.json"
        ),
        "verification_registry": (
            "PR/PR-001-multitenant-knowledge-spaces/contracts/gates/"
            "gate_registry_v1.json"
        ),
        "verification_registry_schema": (
            "PR/PR-001-multitenant-knowledge-spaces/contracts/gates/"
            "gate_registry_v1.schema.json"
        ),
        "verification_receipt_schema": (
            "PR/PR-001-multitenant-knowledge-spaces/contracts/gates/"
            "verification_gate_receipt_v1.schema.json"
        ),
        "capability_map": (
            "PR/PR-001-multitenant-knowledge-spaces/contracts/gates/"
            "synthetic_closure_map_v1.json"
        ),
        "capability_map_schema": (
            "PR/PR-001-multitenant-knowledge-spaces/contracts/gates/"
            "synthetic_closure_map_v1.schema.json"
        ),
        "capability_receipt_schema": (
            "PR/PR-001-multitenant-knowledge-spaces/contracts/gates/"
            "capability_activation_receipt_v1.schema.json"
        ),
        "security_registry": (
            "PR/PR-001-multitenant-knowledge-spaces/contracts/security/"
            "security_gate_registry_v1.json"
        ),
        "security_registry_schema": (
            "PR/PR-001-multitenant-knowledge-spaces/contracts/security/"
            "security_gate_registry_v1.schema.json"
        ),
        "security_receipt_schema": (
            "PR/PR-001-multitenant-knowledge-spaces/contracts/security/"
            "security_gate_receipt_v1.schema.json"
        ),
    }

    def __init__(self, root: Path, evaluation_commit: object) -> None:
        self.root = Path(root)
        self.git = GitSubject.for_repo(self.root)
        if self.git is None:
            raise EvidenceBindingError("release evaluation root is not a git repository")
        if not isinstance(evaluation_commit, str) or not self.git.commit_exists(
            evaluation_commit
        ):
            raise EvidenceBindingError("release evaluation commit is missing or unknown")
        resolved = self.git.resolve(evaluation_commit)
        if resolved != evaluation_commit:
            raise EvidenceBindingError("release evaluation commit must be explicit full hex")
        self.evaluation_commit = evaluation_commit
        def _unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON key: {key}")
                result[key] = value
            return result

        self.bound_json_documents: dict[str, dict] = {}
        for name, rel_path in self.BOUND_JSON_RELS.items():
            raw = _sr.try_read_bytes(self.root, rel_path)
            blob = self.git.blob_bytes(evaluation_commit, rel_path)
            if raw is None or blob is None or raw != blob:
                raise EvidenceBindingError(
                    f"{name} is untracked or differs from evaluation commit"
                )
            try:
                document = json.loads(
                    raw.decode("utf-8"), object_pairs_hook=_unique_object
                )
            except (UnicodeDecodeError, ValueError) as exc:
                raise EvidenceBindingError(f"{name} is not valid JSON") from exc
            if not isinstance(document, dict):
                raise EvidenceBindingError(f"{name} is not a JSON object")
            self.bound_json_documents[name] = document

        self.registry = self.bound_json_documents["release_registry"]
        self.release_registry_schema = self.bound_json_documents[
            "release_registry_schema"
        ]
        self.release_receipt_schema = self.bound_json_documents[
            "release_receipt_schema"
        ]
        self.release_authorization_schema = self.bound_json_documents[
            "release_authorization_schema"
        ]
        self.go_no_go_schema = self.bound_json_documents["go_no_go_schema"]
        self.verification_registry = self.bound_json_documents[
            "verification_registry"
        ]
        self.verification_registry_schema = self.bound_json_documents[
            "verification_registry_schema"
        ]
        self.verification_receipt_schema = self.bound_json_documents[
            "verification_receipt_schema"
        ]
        self.capability_map = self.bound_json_documents["capability_map"]
        self.capability_map_schema = self.bound_json_documents[
            "capability_map_schema"
        ]
        self.capability_receipt_schema = self.bound_json_documents[
            "capability_receipt_schema"
        ]
        self.security_registry = self.bound_json_documents["security_registry"]
        self.security_registry_schema = self.bound_json_documents[
            "security_registry_schema"
        ]
        self.security_receipt_schema = self.bound_json_documents[
            "security_receipt_schema"
        ]
        self.owner_model = self.registry.get("owner_scope_model") or {}
        self.observed_environment = self.derive_observed_environment()
        self._evidence_commit_cache: dict[str, str | None] = {}
        self._evidence_blob_cache: dict[str, bytes | None] = {}
        self._candidate_tree_cache: dict[str, str | None] = {}
        self._rt_touched_cache: dict[tuple[str, str], bool] = {}
        self._rt_scope_tree_cache: dict[tuple[str, str], str | None] = {}
        self._tracked_git_blob_cache: dict[str, bytes | None] = {}
        self._legacy_validation_cache: dict[tuple[str, object, object], tuple[str, ...]] = {}
        self._strict_ancestor_cache: dict[tuple[str, str], bool] = {}

    @staticmethod
    def derive_observed_environment() -> dict:
        system = sys.platform.lower()
        if system == "darwin":
            system = "darwin"
        machine = platform.machine().lower() or "unknown"
        return {
            "python_version": platform.python_version(),
            "platform": f"{system}-{machine}",
            "toolchain_build": "cwk-toolchain-2026.08",
        }

    def evidence_commit(self, rel_path: str) -> str | None:
        if rel_path not in self._evidence_commit_cache:
            self._evidence_commit_cache[rel_path] = resolve_evidence_commit(
                self.git,
                self.root,
                rel_path,
                evaluation_commit=self.evaluation_commit,
            )
            self._evidence_blob_cache[rel_path] = self.git.blob_bytes(
                self.evaluation_commit, rel_path
            )
        evidence = self._evidence_commit_cache[rel_path]
        if evidence is None:
            return None
        # Cache immutable Git facts, never mutable filesystem truth. A path
        # rewritten between two recursive checks must fail the later check.
        on_disk = _sr.try_read_bytes(self.root, rel_path)
        expected = self._evidence_blob_cache.get(rel_path)
        return evidence if on_disk is not None and on_disk == expected else None

    def _is_strict_ancestor(self, ancestor: object, descendant: object) -> bool:
        if not isinstance(ancestor, str) or not isinstance(descendant, str):
            return False
        key = (ancestor, descendant)
        if key not in self._strict_ancestor_cache:
            self._strict_ancestor_cache[key] = self.git.is_strict_ancestor(
                ancestor, descendant
            )
        return self._strict_ancestor_cache[key]

    def candidate_tree(self, subject_commit: str) -> str | None:
        if not isinstance(subject_commit, str):
            return None
        if subject_commit not in self._candidate_tree_cache:
            self._candidate_tree_cache[subject_commit] = self.git.candidate_tree_sha256(
                subject_commit,
                excluded_prefixes=self.owner_model.get(
                    "candidate_tree_excluded_prefixes", ()
                ),
                excluded_patterns=self.owner_model.get(
                    "candidate_tree_excluded_patterns", ()
                ),
            )
        return self._candidate_tree_cache[subject_commit]

    def _rt_touched(self, subject_commit: str, rt_id: str) -> bool:
        if not isinstance(subject_commit, str):
            return False
        key = (subject_commit, rt_id)
        if key in self._rt_touched_cache:
            return self._rt_touched_cache[key]
        touched = self.git.paths_touched(subject_commit)
        prefixes = (self.owner_model.get("rt_owner_code_prefixes") or {}).get(
            rt_id, ()
        )
        if any(path_within_any(path, prefixes) for path in touched):
            self._rt_touched_cache[key] = True
            return True
        digits = rt_id.split("-")[-1]
        templates = (
            (self.owner_model.get("rt_owner_code_patterns") or {}).get("templates")
            or ()
        )
        try:
            patterns = [re.compile(template.format(rt_digits=digits)) for template in templates]
        except (re.error, KeyError, ValueError):
            self._rt_touched_cache[key] = False
            return False
        result = any(any(pattern.search(path) for pattern in patterns) for path in touched)
        self._rt_touched_cache[key] = result
        return result

    def gate_subject_touched(self, gate_id: str, subject_commit: str) -> bool:
        rt_ids = (self.owner_model.get("gate_owner_scope_rt_ids") or {}).get(gate_id)
        if not isinstance(rt_ids, list) or not rt_ids:
            return False
        if any(self._rt_touched(subject_commit, rt_id) for rt_id in rt_ids):
            return True
        # The six grandfathered acceptance commits predate the RT-keyed path
        # convention and legitimately changed shared PR-001 scripts/contracts.
        # Their exact accepted_subject_commit is itself a frozen, git-resolved
        # touch witness; this exception is closed to the registry's legacy set.
        acceptances = (
            (self.registry.get("prerequisite_resolution") or {}).get(
                "rt_acceptance_reports"
            )
            or {}
        )
        return any(
            (acceptances.get(rt_id) or {}).get("marker_style")
            == "legacy_frozen_hash"
            and (acceptances.get(rt_id) or {}).get("accepted_subject_commit")
            == subject_commit
            for rt_id in rt_ids
        )

    def _rt_scope_tree(self, commit: str, rt_id: str) -> str | None:
        if not isinstance(commit, str):
            return None
        cache_key = (commit, rt_id)
        if cache_key in self._rt_scope_tree_cache:
            return self._rt_scope_tree_cache[cache_key]
        if not self.git.commit_exists(commit):
            return None
        prefixes = (self.owner_model.get("rt_owner_code_prefixes") or {}).get(
            rt_id, ()
        )
        digits = rt_id.split("-")[-1]
        templates = (
            (self.owner_model.get("rt_owner_code_patterns") or {}).get("templates")
            or ()
        )
        try:
            patterns = [re.compile(template.format(rt_digits=digits)) for template in templates]
        except (re.error, KeyError, ValueError):
            return None
        raw = self.git._out_bytes("ls-tree", "-r", "-z", "--full-tree", commit)
        if raw is None:
            return None
        records: list[bytes] = []
        for record in raw.split(b"\0"):
            if not record:
                continue
            _head, separator, path_raw = record.partition(b"\t")
            if not separator:
                return None
            try:
                rel = path_raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
            if path_within_any(rel, prefixes) or any(
                pattern.search(rel) for pattern in patterns
            ):
                records.append(record)
        digest = hashlib.sha256(_OWNER_TREE_DOMAIN)
        for record in sorted(records):
            digest.update(record)
            digest.update(b"\0")
        result = digest.hexdigest()
        self._rt_scope_tree_cache[cache_key] = result
        return result

    def validate_legacy_acceptance(
        self, rt_id: str, accepted_subject: object, candidate_subject: object
    ) -> list[str]:
        if not isinstance(accepted_subject, str) or not self.git.commit_exists(
            accepted_subject
        ):
            return [f"legacy_accepted_subject_unknown:{rt_id}"]
        if not isinstance(candidate_subject, str) or not self.git.commit_exists(
            candidate_subject
        ):
            return [f"legacy_candidate_subject_unknown:{rt_id}"]
        cache_key = (rt_id, accepted_subject, candidate_subject)
        cached = self._legacy_validation_cache.get(cache_key)
        if cached is not None:
            return list(cached)
        violations: list[str] = []
        if accepted_subject != candidate_subject and not self._is_strict_ancestor(
            accepted_subject, candidate_subject
        ):
            violations.append(f"legacy_subject_not_ancestor:{rt_id}")
        accepted_tree = self._rt_scope_tree(accepted_subject, rt_id)
        candidate_tree = self._rt_scope_tree(candidate_subject, rt_id)
        if accepted_tree is None or candidate_tree is None:
            violations.append(f"legacy_owner_scope_unavailable:{rt_id}")
        elif accepted_tree != candidate_tree:
            violations.append(f"legacy_drift_unresolved:{rt_id}")
        self._legacy_validation_cache[cache_key] = tuple(violations)
        return violations

    def validate_release_subject(
        self, gate_id: str, receipt_path: str, receipt: dict
    ) -> tuple[list[str], str | None, str | None]:
        violations: list[str] = []
        evidence = self.evidence_commit(receipt_path)
        if evidence is None:
            violations.append("repository_evidence_commit_unverifiable")
            return violations, None, None
        subject = receipt.get("tested_subject_commit")
        if subject == evidence:
            violations.append("subject_commit_is_its_own_commit")
        elif not self._is_strict_ancestor(subject, evidence):
            violations.append("subject_commit_not_strict_ancestor")
        if not self.gate_subject_touched(gate_id, subject):
            violations.append("subject_commit_did_not_touch_owner_code")
        tree = self.candidate_tree(subject)
        if tree is None:
            violations.append("owner_scope_tree_unavailable")
        elif receipt.get("owner_scope_tree_sha256") != tree:
            violations.append("owner_scope_tree_hash_mismatch")
        if not verify_environment_fingerprint_exact(
            receipt.get("environment_fingerprint"), self.observed_environment
        ):
            violations.append("environment_fingerprint_mismatch")
        return violations, evidence, tree

    def tracked_blob_matches(self, rel_path: object) -> bool:
        if not isinstance(rel_path, str):
            return False
        try:
            _sr.safe_relpath(rel_path, label=rel_path)
        except _sr.SafeReadError:
            return False
        raw = _sr.try_read_bytes(self.root, rel_path)
        if rel_path not in self._tracked_git_blob_cache:
            self._tracked_git_blob_cache[rel_path] = self.git.blob_bytes(
                self.evaluation_commit, rel_path
            )
        blob = self._tracked_git_blob_cache[rel_path]
        return raw is not None and blob is not None and raw == blob

    def validate_external_binding(
        self, ref_id: str, rel_path: str, body: dict
    ) -> list[str]:
        violations: list[str] = []
        evidence = self.evidence_commit(rel_path)
        subject = body.get("tested_subject_commit")
        if evidence is None:
            violations.append(f"prereq_body_evidence_commit_unverifiable:{ref_id}")
            return violations
        if subject == evidence or not self._is_strict_ancestor(subject, evidence):
            violations.append(f"prereq_body_subject_mismatch:{ref_id}")
        tree = self.candidate_tree(subject)
        if tree is None or body.get("owner_scope_tree_sha256") != tree:
            violations.append(f"prereq_body_owner_tree_mismatch:{ref_id}")
        if not verify_environment_fingerprint_exact(
            body.get("environment_fingerprint"), self.observed_environment
        ):
            violations.append(f"prereq_body_environment_mismatch:{ref_id}")
        return violations

# ---------------------------------------------------------------------------
# binding checks
# ---------------------------------------------------------------------------


def subject_touches_owner_code(
    git: GitSubject,
    subject_commit: str,
    *,
    code_prefixes: object,
    evidence_prefixes: object = (),
) -> bool:
    """The subject commit must have touched the owner's CODE, not its output.

    Ancestry proves *when*; this proves *what*.  The separation matters: the
    capability's own `capability-receipts/<id>/` directory used to be listed as
    owner scope, so a commit that changed nothing but the receipt satisfied
    "the subject modified the owning package".  A path under an evidence-output
    prefix is explicitly disqualified from counting as code, so evidence can
    never bootstrap its own subject binding.
    """

    if not isinstance(code_prefixes, (list, tuple)) or not code_prefixes:
        return False
    touched = git.paths_touched(subject_commit)
    if not touched:
        return False
    for path in touched:
        if path_within_any(path, evidence_prefixes):
            continue  # evidence output is not owner code
        if path_within_any(path, code_prefixes):
            return True
    return False


def resolve_evidence_commit(
    git: GitSubject | None,
    root: Path,
    receipt_rel: str,
    *,
    evaluation_commit: object,
) -> str | None:
    """The commit `E` that produced the receipt bytes we actually read.

    Returns `None` unless every one of these holds:

    * `evaluation_commit` is a real commit (it is injected, never `HEAD` by
      accident, so the evaluator states which candidate it is judging);
    * `receipt_rel` has a latest blob-changing commit `E` reachable from it;
    * `E` has exactly ONE parent -- a merge could carry content that was never
      part of the reviewed line;
    * the blob at `E` equals the blob at `evaluation_commit`, i.e. nothing
      rewrote the receipt after the evidence commit;
    * the blob equals the bytes a fail-closed read returns from disk, i.e. the
      file the evaluator read and the file git recorded are the same file.
    """

    if git is None:
        return None
    if not isinstance(evaluation_commit, str) or not git.commit_exists(evaluation_commit):
        return None
    try:
        _sr.safe_relpath(receipt_rel, label=receipt_rel)
    except _sr.SafeReadError:
        return None
    evidence = git.latest_commit_touching(receipt_rel, until=evaluation_commit)
    if evidence is None:
        return None
    parents = git.parents(evidence)
    if parents is None or len(parents) != 1:
        return None
    at_evidence = git.blob_bytes(evidence, receipt_rel)
    if at_evidence is None:
        return None
    at_evaluation = git.blob_bytes(evaluation_commit, receipt_rel)
    if at_evaluation is None or at_evaluation != at_evidence:
        return None
    on_disk = _sr.try_read_bytes(root, receipt_rel)
    if on_disk is None or on_disk != at_evidence:
        return None
    return evidence


def verify_subject_commit(
    git: GitSubject | None,
    subject_commit: object,
    evidence_commit: object,
    *,
    code_prefixes: object,
    evidence_prefixes: object = (),
    declared_tree_sha256: object = None,
) -> bool:
    """Full binding: exists, strict ancestor of `E`, in owner code scope, no drift.

    Fails closed when git is unavailable: if we cannot prove the binding we do
    not get to assume it.
    """

    if git is None:
        return False
    if not isinstance(subject_commit, str) or not SHA1_RE.match(subject_commit):
        return False
    if not isinstance(evidence_commit, str) or not SHA1_RE.match(evidence_commit):
        return False
    if not git.commit_exists(subject_commit):
        return False
    if not git.is_strict_ancestor(subject_commit, evidence_commit):
        return False
    if not subject_touches_owner_code(
        git, subject_commit, code_prefixes=code_prefixes, evidence_prefixes=evidence_prefixes
    ):
        return False
    if declared_tree_sha256 is not None:
        if not isinstance(declared_tree_sha256, str) or not SHA256_RE.match(declared_tree_sha256):
            return False
        actual = git.owner_scope_tree_sha256(subject_commit, code_prefixes)
        if actual is None or actual != declared_tree_sha256:
            return False
    return True


# ---------------------------------------------------------------------------
# security owner scope v2
# ---------------------------------------------------------------------------


def _canonical_json_bytes(value: object) -> bytes:
    """The repository's JSON subset in deterministic NFC/UTF-8 form."""

    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return unicodedata.normalize("NFC", text).encode("utf-8")


def _security_selector_is_safe(
    selector: object,
    *,
    directory: bool | None = None,
    forbid_outputs: bool = True,
) -> bool:
    if not isinstance(selector, str) or not selector or selector != unicodedata.normalize("NFC", selector):
        return False
    if selector.startswith("/") or "//" in selector or "\\" in selector:
        return False
    components = selector.rstrip("/").split("/")
    if any(part in ("", ".", "..") for part in components):
        return False
    if forbid_outputs and any(part in _SECURITY_FORBIDDEN_OWNER_COMPONENTS for part in components):
        return False
    if directory is True and not selector.endswith("/"):
        return False
    if directory is False and selector.endswith("/"):
        return False
    return True


def _security_selectors_overlap(left: str, right: str) -> bool:
    """Static path overlap: exact files and recursive dirs have distinct meaning."""

    left_dir = left.endswith("/")
    right_dir = right.endswith("/")
    left_clean = left.rstrip("/")
    right_clean = right.rstrip("/")
    if not left_dir and not right_dir:
        return left == right
    if left_dir and right_dir:
        return path_within(left_clean, right_clean) or path_within(right_clean, left_clean)
    directory = left_clean if left_dir else right_clean
    exact = right_clean if left_dir else left_clean
    return path_within(exact, directory)


def _security_managed_inventory_parts(
    registry: object,
) -> tuple[dict[str, dict], set[str], set[str], set[str]] | None:
    """Return the four closed inventory families, or fail closed.

    The inventory is policy, not a best-effort scan.  Every managed namespace
    path is assigned to exactly one family: an RT owner (with the sole
    tenant-cli two-owner exception), a central immutable ABI, or immutable
    legacy.  ``shared_evolution_paths`` describes how an owner path may move;
    it is deliberately not a fourth owner family.
    """

    if not isinstance(registry, dict):
        return None
    inventory = registry.get("managed_script_inventory")
    if not isinstance(inventory, dict) or set(inventory) != {
        "schema",
        "namespace_pattern",
        "explicit_managed_paths",
        "owner_map_source",
        "central_shared_abi_source",
        "shared_evolution_source",
        "legacy_frozen_files",
    }:
        return None
    if (
        inventory.get("schema") != "cwk.pr001.managed_script_inventory.v1"
        or inventory.get("namespace_pattern") != r"^scripts/cwk_[a-z0-9_]+\.py$"
        or inventory.get("explicit_managed_paths") != ["install.sh"]
        or inventory.get("owner_map_source") != "entries[].owner_code_path_prefixes"
        or inventory.get("central_shared_abi_source") != "shared_abi_dependencies"
        or inventory.get("shared_evolution_source") != "shared_evolution_paths"
    ):
        return None

    namespace = re.compile(inventory["namespace_pattern"])
    explicit = set(inventory["explicit_managed_paths"])
    entries = registry.get("entries")
    dependencies = registry.get("shared_abi_dependencies")
    shared = registry.get("shared_evolution_paths")
    legacy_rows = inventory.get("legacy_frozen_files")
    if not isinstance(entries, list) or not isinstance(dependencies, list) or not isinstance(shared, list):
        return None
    if not isinstance(legacy_rows, list) or len(legacy_rows) != 53:
        return None

    owner_sets: dict[str, set[str]] = {}
    owner_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("producer_rt"), str):
            return None
        for selector in entry.get("owner_code_path_prefixes", ()):
            if isinstance(selector, str) and (namespace.fullmatch(selector) or selector in explicit):
                owner_sets.setdefault(selector, set()).add(entry["producer_rt"])
                owner_paths.add(selector)

    shared_paths: set[str] = set()
    for row in shared:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            return None
        path = row["path"]
        if not namespace.fullmatch(path) or path in shared_paths:
            return None
        shared_paths.add(path)
        owners = row.get("owner_stage_indices")
        if not isinstance(owners, dict) or owner_sets.get(path) != set(owners):
            return None
    if shared_paths != {"scripts/cwk_tenant_cli.py"}:
        return None
    if any(len(owners) != (2 if path in shared_paths else 1) for path, owners in owner_sets.items()):
        return None

    central_paths: set[str] = set()
    for dep in dependencies:
        if not isinstance(dep, dict):
            return None
        for binding in dep.get("exact_paths", ()):
            if not isinstance(binding, dict):
                return None
            path = binding.get("path")
            if isinstance(path, str) and (namespace.fullmatch(path) or path in explicit):
                if path in central_paths:
                    return None
                central_paths.add(path)

    legacy: dict[str, dict] = {}
    for row in legacy_rows:
        if not isinstance(row, dict) or set(row) != {"path", "mode", "sha256"}:
            return None
        path = row.get("path")
        mode = row.get("mode")
        sha = row.get("sha256")
        if (
            not isinstance(path, str)
            or not namespace.fullmatch(path)
            or path in legacy
            or mode not in {"100644", "100755"}
            or not isinstance(sha, str)
            or not SHA256_RE.match(sha)
        ):
            return None
        legacy[path] = row
    if list(legacy) != sorted(legacy):
        return None
    if owner_paths & central_paths or owner_paths & set(legacy) or central_paths & set(legacy):
        return None
    return legacy, owner_paths, central_paths, shared_paths


def security_registry_owner_semantics_ok(registry: object) -> bool:
    """Cross-entry invariants JSON Schema cannot express.

    Planned paths need not exist yet, so this function validates declaration
    semantics only.  Existence and regular-blob closure are checked against a
    concrete subject commit by :func:`security_owner_scope_tree_sha256`.
    """

    if not isinstance(registry, dict):
        return False
    if registry.get("owner_scope_hash_model") != "cwk-owner-scope-tree-v2":
        return False
    policy_ref = registry.get("script_evolution_policy_ref")
    policy_sha = registry.get("script_evolution_policy_sha256")
    if not _security_selector_is_safe(policy_ref, directory=False) or not SHA256_RE.match(
        str(policy_sha)
    ):
        return False
    if _security_managed_inventory_parts(registry) is None:
        return False
    entries = registry.get("entries")
    if not isinstance(entries, list) or len(entries) != 10:
        return False
    shared_items = registry.get("shared_evolution_paths")
    if not isinstance(shared_items, list) or not shared_items:
        return False
    shared_paths: set[str] = set()
    shared_owners: dict[str, set[str]] = {}
    for item in shared_items:
        if not isinstance(item, dict) or set(item) != {"path", "owner_stage_indices"}:
            return False
        path = item.get("path")
        owners = item.get("owner_stage_indices")
        if not _security_selector_is_safe(path, directory=False) or not isinstance(owners, dict):
            return False
        if path in shared_paths or len(owners) < 2:
            return False
        shared_paths.add(path)
        shared_owners[path] = set(owners)
        for rt, indices in owners.items():
            if not re.fullmatch(r"RT-0(?:1[7-9]|2[0-6])", str(rt)):
                return False
            if not isinstance(indices, list) or not indices or len(set(indices)) != len(indices):
                return False
            if any(not isinstance(i, int) or isinstance(i, bool) or not 1 <= i <= 8 for i in indices):
                return False

    dependencies = registry.get("shared_abi_dependencies")
    if not isinstance(dependencies, list) or not dependencies:
        return False
    dependencies_by_id: dict[str, dict] = {}
    for dep in dependencies:
        if not isinstance(dep, dict) or set(dep) != {"dependency_id", "exact_paths", "consumer_rts"}:
            return False
        dep_id = dep.get("dependency_id")
        paths = dep.get("exact_paths")
        consumers = dep.get("consumer_rts")
        if not isinstance(dep_id, str) or dep_id in dependencies_by_id:
            return False
        if not isinstance(paths, list) or not paths:
            return False
        seen_dependency_paths: set[str] = set()
        for binding in paths:
            if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
                return False
            path = binding.get("path")
            sha = binding.get("sha256")
            if (
                not _security_selector_is_safe(path, directory=False)
                or not isinstance(sha, str)
                or not SHA256_RE.match(sha)
                or path in seen_dependency_paths
            ):
                return False
            seen_dependency_paths.add(path)
        if not isinstance(consumers, list) or not consumers or len(consumers) != len(set(consumers)):
            return False
        dependencies_by_id[dep_id] = dep

    declared: list[tuple[str, str]] = []
    test_prefixes: set[str] = set()
    declared_test_prefixes: list[tuple[str, str]] = []
    by_rt: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            return False
        rt = entry.get("producer_rt")
        if not isinstance(rt, str) or rt in by_rt:
            return False
        by_rt[rt] = entry
        selectors = entry.get("owner_code_path_prefixes")
        evidence = entry.get("owner_evidence_path_prefixes")
        prefixes = entry.get("owner_test_file_prefixes")
        required = entry.get("required_security_test_files")
        stages = entry.get("owner_evolution_stage_indices")
        dep_ids = entry.get("required_shared_abi_ids")
        unresolved = entry.get("unresolved_owner_surface_requirements")
        if not isinstance(selectors, list) or not selectors or len(selectors) != len(set(selectors)):
            return False
        if not isinstance(evidence, list) or not evidence or len(evidence) != len(set(evidence)):
            return False
        if not isinstance(prefixes, list) or len(prefixes) != 1:
            return False
        prefix = prefixes[0]
        digits = rt.replace("RT-", "").lstrip("0")
        canonical_prefix = f"tests/test_rt{int(digits):03d}_" if digits.isdigit() else ""
        if prefix != canonical_prefix or prefix in test_prefixes:
            return False
        test_prefixes.add(prefix)
        declared_test_prefixes.append((rt, prefix))
        if not isinstance(required, list) or len(required) < 2 or len(required) != len(set(required)):
            return False
        if not all(
            isinstance(path, str)
            and path.startswith(prefix)
            and "/" not in path[len("tests/") :]
            and _SECURITY_TEST_SUFFIX_RE.match(path[len(prefix) :])
            for path in required
        ):
            return False
        if not isinstance(stages, list) or len(stages) != len(set(stages)):
            return False
        if any(not isinstance(i, int) or isinstance(i, bool) or not 1 <= i <= 8 for i in stages):
            return False
        if not isinstance(dep_ids, list) or len(dep_ids) != len(set(dep_ids)):
            return False
        if any(dep_id not in dependencies_by_id for dep_id in dep_ids):
            return False
        if not isinstance(unresolved, list):
            return False
        for selector_index, selector in enumerate(selectors):
            if not _security_selector_is_safe(selector):
                return False
            if any(
                _security_selectors_overlap(selector, evidence_prefix)
                for evidence_prefix in evidence
            ):
                return False
            if any(
                _security_selectors_overlap(selector, other)
                for other in selectors[selector_index + 1 :]
            ):
                return False
            if selector.startswith(prefix) and not selector.endswith("/"):
                suffix = selector[len(prefix) :]
                if _SECURITY_TEST_SUFFIX_RE.match(suffix):
                    return False
            declared.append((rt, selector))
        if not all(
            _security_selector_is_safe(prefix, directory=True, forbid_outputs=False)
            for prefix in evidence
        ):
            return False
        for path in required:
            if any(path_within(path, prefix.rstrip("/")) for prefix in evidence):
                return False

    for dep_id, dep in dependencies_by_id.items():
        consumers = set(dep["consumer_rts"])
        if set(by_rt) & consumers != consumers:
            return False
        actual = {rt for rt, entry in by_rt.items() if dep_id in entry["required_shared_abi_ids"]}
        if actual != consumers:
            return False
        for binding in dep["exact_paths"]:
            path = binding["path"]
            if any(_security_selectors_overlap(path, selector) for _, selector in declared):
                return False  # central ABI is a dependency, never RT-owned code
            if any(
                path.startswith(prefix)
                and _SECURITY_TEST_SUFFIX_RE.match(path[len(prefix) :])
                for _, prefix in declared_test_prefixes
            ):
                return False

    for index, (left_rt, left) in enumerate(declared):
        for right_rt, right in declared[index + 1 :]:
            if left_rt == right_rt or not _security_selectors_overlap(left, right):
                continue
            if left == right and left in shared_paths:
                if {left_rt, right_rt} <= shared_owners[left]:
                    continue
            return False
    for owner_rt, selector in declared:
        if selector.endswith("/"):
            continue
        for test_rt, prefix in declared_test_prefixes:
            if selector.startswith(prefix) and _SECURITY_TEST_SUFFIX_RE.match(
                selector[len(prefix) :]
            ):
                return False
    for path, owners in shared_owners.items():
        actual = {rt for rt, selector in declared if selector == path}
        if actual != owners:
            return False
        for rt in owners:
            declared_stages = set(by_rt[rt]["owner_evolution_stage_indices"])
            if not set(next(item["owner_stage_indices"][rt] for item in shared_items if item["path"] == path)) <= declared_stages:
                return False
    return True


def _git_tree_records(
    git: GitSubject, commit: str
) -> dict[str, tuple[str, str, str, bytes]] | None:
    """Return every recursive tree entry, retaining its mode and object kind.

    Filtering to regular blobs before selector matching made a selected
    symlink (120000) or gitlink (160000/commit) disappear.  A missing entry and
    a hostile entry then looked identical.  The full tree is retained here;
    each selector decides whether the entry is admissible.
    """

    cache = getattr(git, "_security_tree_records_cache", None)
    if cache is None:
        cache = git._security_tree_records_cache = {}
    if commit in cache:
        return cache[commit]
    if not git.commit_exists(commit):
        cache[commit] = None
        return None
    raw = git._out_bytes("ls-tree", "-r", "-z", "--full-tree", commit)
    if raw is None:
        cache[commit] = None
        return None
    records: dict[str, tuple[str, str, str, bytes]] = {}
    for chunk in raw.split(b"\0"):
        if not chunk:
            continue
        head, separator, path_raw = chunk.partition(b"\t")
        fields = head.split()
        if not separator or len(fields) != 3:
            cache[commit] = None
            return None
        mode_raw, kind_raw, object_raw = fields
        try:
            path = path_raw.decode("utf-8")
            mode = mode_raw.decode("ascii")
            kind = kind_raw.decode("ascii")
            object_id = object_raw.decode("ascii")
        except UnicodeDecodeError:
            cache[commit] = None
            return None
        if path != unicodedata.normalize("NFC", path) or path in records:
            cache[commit] = None
            return None
        records[path] = (mode, kind, object_id, chunk)
    cache[commit] = records
    return records


def _security_regular_record(
    record: tuple[str, str, str, bytes] | None,
) -> bool:
    return record is not None and record[0] in {"100644", "100755"} and record[1] == "blob"


def _security_policy_stage_rows(
    git: GitSubject, commit: str, entry: dict, registry: dict
) -> tuple[dict, list[dict]] | None:
    policy_ref = registry["script_evolution_policy_ref"]
    raw = git.blob_bytes(commit, policy_ref)
    if raw is None or hashlib.sha256(raw).hexdigest() != registry["script_evolution_policy_sha256"]:
        return None
    try:
        policy = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(policy, dict) or not isinstance(policy.get("stages"), list):
        return None
    by_index: dict[int, dict] = {}
    for stage in policy["stages"]:
        if not isinstance(stage, dict) or not isinstance(stage.get("stage_index"), int):
            return None
        if stage["stage_index"] in by_index:
            return None
        by_index[stage["stage_index"]] = stage
    declared = entry["owner_evolution_stage_indices"]
    rows: list[dict] = []
    for stage_index in declared:
        stage = by_index.get(stage_index)
        if stage is None or stage.get("owner_rt") != entry["producer_rt"]:
            return None
        target = stage.get("target_path")
        if target not in entry["owner_code_path_prefixes"]:
            return None
        required = {"stage_index", "owner_rt", "target_path", "receipt_path", "ordinal"}
        if not required <= set(stage):
            return None
        rows.append({key: stage[key] for key in sorted(required)})
    actual = sorted(
        stage["stage_index"]
        for stage in policy["stages"]
        if stage.get("owner_rt") == entry["producer_rt"]
    )
    if sorted(declared) != actual:
        return None
    return policy, sorted(rows, key=lambda row: row["stage_index"])


def _security_scope_snapshot(
    git: GitSubject, commit: str, entry: object, registry: object
) -> tuple[
    dict,
    dict[str, tuple[str, str, str, bytes]],
    frozenset[str],
    frozenset[str],
] | None:
    if not security_registry_owner_semantics_ok(registry) or not isinstance(entry, dict):
        return None
    if entry.get("unresolved_owner_surface_requirements"):
        return None
    scope_cache = getattr(git, "_security_scope_snapshot_cache", None)
    if scope_cache is None:
        scope_cache = git._security_scope_snapshot_cache = {}
    try:
        scope_key = (
            commit,
            hashlib.sha256(_canonical_json_bytes(registry)).hexdigest(),
            hashlib.sha256(_canonical_json_bytes(entry)).hexdigest(),
        )
    except (TypeError, ValueError):
        return None
    if scope_key in scope_cache:
        return scope_cache[scope_key]
    all_records = _git_tree_records(git, commit)
    if all_records is None:
        return None
    inventory_parts = _security_managed_inventory_parts(registry)
    if inventory_parts is None:
        return None
    legacy, declared_owner_paths, central_managed_paths, declared_shared_paths = inventory_parts
    inventory = registry["managed_script_inventory"]
    namespace = re.compile(inventory["namespace_pattern"])
    explicit = set(inventory["explicit_managed_paths"])
    declared_managed = declared_owner_paths | central_managed_paths | set(legacy)
    actual_managed = {
        path for path in all_records if namespace.fullmatch(path) or path in explicit
    }
    if not actual_managed <= declared_managed:
        return None  # a new managed script has no frozen owner/category

    selected: dict[str, tuple[str, str, str, bytes]] = {}
    owner_paths: set[str] = set()
    for path, binding in legacy.items():
        record = all_records.get(path)
        blob = git.blob_bytes(commit, path)
        if (
            not _security_regular_record(record)
            or record[0] != binding["mode"]
            or blob is None
            or hashlib.sha256(blob).hexdigest() != binding["sha256"]
        ):
            return None
        selected[path] = record
    central_bindings = {
        binding["path"]: binding["sha256"]
        for dependency in registry["shared_abi_dependencies"]
        for binding in dependency["exact_paths"]
        if binding["path"] in central_managed_paths
    }
    for path in central_managed_paths:
        record = all_records.get(path)
        blob = git.blob_bytes(commit, path)
        if (
            not _security_regular_record(record)
            or blob is None
            or hashlib.sha256(blob).hexdigest() != central_bindings.get(path)
        ):
            return None
        selected[path] = record

    selectors = entry["owner_code_path_prefixes"]
    for selector in selectors:
        if selector.endswith("/"):
            matched = {path: record for path, record in all_records.items() if path.startswith(selector)}
            if not matched or any(not _security_regular_record(record) for record in matched.values()):
                return None
            selected.update(matched)
            owner_paths.update(matched)
        else:
            record = all_records.get(selector)
            if not _security_regular_record(record):
                return None
            selected[selector] = record
            owner_paths.add(selector)

    # Authority docs/tasks are closed sets, not directory prefixes.  A hidden
    # sibling or nested lookalike must not escape the exact selectors.
    for parent_suffix in ("/specs/", "/tasks/"):
        expected = {
            selector
            for selector in selectors
            if parent_suffix in selector and not selector.endswith("/")
        }
        for parent in {path.split(parent_suffix, 1)[0] + parent_suffix for path in expected}:
            actual = {path for path in all_records if path.startswith(parent)}
            if actual != {path for path in expected if path.startswith(parent)}:
                return None

    for prefix in entry["owner_test_file_prefixes"]:
        basename_prefix = prefix.removeprefix("tests/")
        matched: dict[str, tuple[str, str, str, bytes]] = {}
        for path, record in all_records.items():
            basename = path.rsplit("/", 1)[-1]
            if not (path.startswith(prefix) or basename.startswith(basename_prefix)):
                continue
            if (
                "/" in path[len("tests/") :]
                or not path.startswith(prefix)
                or not _SECURITY_TEST_SUFFIX_RE.fullmatch(path[len(prefix) :])
                or not _security_regular_record(record)
            ):
                return None
            matched[path] = record
        if not matched:
            return None
        selected.update(matched)
        owner_paths.update(matched)
    if not set(entry["required_security_test_files"]) <= set(selected):
        return None

    dependency_paths: set[str] = set()
    dependencies = {item["dependency_id"]: item for item in registry["shared_abi_dependencies"]}
    for dep_id in entry["required_shared_abi_ids"]:
        for binding in dependencies[dep_id]["exact_paths"]:
            path = binding["path"]
            record = all_records.get(path)
            if not _security_regular_record(record):
                return None
            blob = git.blob_bytes(commit, path)
            if blob is None or hashlib.sha256(blob).hexdigest() != binding["sha256"]:
                return None
            selected[path] = record
            dependency_paths.add(path)

    policy_result = _security_policy_stage_rows(git, commit, entry, registry)
    if policy_result is None:
        return None
    _policy, stage_rows = policy_result
    policy_ref = registry["script_evolution_policy_ref"]
    policy_record = all_records.get(policy_ref)
    if not _security_regular_record(policy_record):
        return None
    selected[policy_ref] = policy_record

    evidence = entry["owner_evidence_path_prefixes"]
    if any(path_within_any(path, evidence) for path in selected):
        return None
    manifest = {
        "model": registry["owner_scope_hash_model"],
        "producer_rt": entry["producer_rt"],
        "owner_code_selectors": sorted(selectors),
        "owner_test_file_prefixes": sorted(entry["owner_test_file_prefixes"]),
        "required_security_test_files": sorted(entry["required_security_test_files"]),
        "owner_evolution_stage_indices": sorted(entry["owner_evolution_stage_indices"]),
        "owner_evolution_stages": stage_rows,
        "script_evolution_policy_ref": policy_ref,
        "script_evolution_policy_sha256": registry["script_evolution_policy_sha256"],
        "managed_script_inventory": inventory,
        "required_shared_abi_ids": sorted(entry["required_shared_abi_ids"]),
        "shared_abi_dependency_paths": sorted(dependency_paths),
        "owner_evidence_path_prefixes": sorted(evidence),
    }
    shared_paths = frozenset(
        item["path"]
        for item in registry["shared_evolution_paths"]
        if entry["producer_rt"] in item["owner_stage_indices"]
    )
    if set(shared_paths) - declared_shared_paths:
        return None
    result = manifest, selected, frozenset(owner_paths), shared_paths
    scope_cache[scope_key] = result
    return result


def security_owner_scope_tree_sha256(
    git: GitSubject | None, commit: object, entry: object, registry: object
) -> str | None:
    """Security-only v2 digest; release/capability callers keep v1 unchanged."""

    if git is None or not isinstance(commit, str):
        return None
    snapshot = _security_scope_snapshot(git, commit, entry, registry)
    if snapshot is None:
        return None
    manifest, records, _owner_paths, _shared_paths = snapshot
    digest = hashlib.sha256(_SECURITY_OWNER_TREE_DOMAIN)
    digest.update(_canonical_json_bytes(manifest))
    digest.update(b"\0")
    for path in sorted(records):
        digest.update(records[path][3])
        digest.update(b"\0")
    return digest.hexdigest()


def _security_disk_walk(root: Path, rel_dir: str) -> dict[str, str] | None:
    """Snapshot one worktree directory without following links.

    Values are ``file`` or ``dir``.  Symlinks, sockets, FIFOs and devices are
    represented as ``unsafe`` so a selected closure can reject them rather
    than silently filtering them out.
    """

    base = root / rel_dir.rstrip("/")
    try:
        root_stat = base.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        return None
    result: dict[str, str] = {}
    stack = [(base, rel_dir.rstrip("/"))]
    while stack:
        current, current_rel = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            return None
        for item in entries:
            rel = f"{current_rel}/{item.name}"
            if rel != unicodedata.normalize("NFC", rel) or rel in result:
                return None
            try:
                st = item.stat(follow_symlinks=False)
            except OSError:
                return None
            if stat.S_ISDIR(st.st_mode):
                result[rel] = "dir"
                stack.append((Path(item.path), rel))
            elif stat.S_ISREG(st.st_mode):
                result[rel] = "file"
            else:
                result[rel] = "unsafe"
    return result


def _security_worktree_matches_evaluation(
    git: GitSubject,
    evaluation_commit: str,
    entry: dict,
    registry: dict,
    snapshot: tuple[
        dict,
        dict[str, tuple[str, str, str, bytes]],
        frozenset[str],
        frozenset[str],
    ],
) -> bool:
    """Bind mutable disk/index state to the explicit evaluation commit."""

    try:
        head = git.head()
        # Release evaluation deliberately freezes an explicit candidate and
        # may later run from a clean descendant whose additional commits are
        # outside this RT's closed owner surface.  Never replace the explicit
        # candidate with moving HEAD: require ancestry, then compare every
        # selected disk byte and exact directory/test closure to that frozen
        # candidate below.  A sibling branch, dirty tree or selected drift is
        # still rejected.
        if head is None or not git.is_ancestor(evaluation_commit, head) or worktree_is_dirty(git):
            return False
    except EvidenceBindingError:
        return False
    _manifest, selected, _owner_paths, _shared_paths = snapshot
    for path, record in selected.items():
        disk = _sr.try_read_bytes(git.root, path)
        blob = git.blob_bytes(evaluation_commit, path)
        if disk is None or blob is None or disk != blob or not _security_regular_record(record):
            return False

    all_records = _git_tree_records(git, evaluation_commit)
    if all_records is None:
        return False
    for selector in entry["owner_code_path_prefixes"]:
        if not selector.endswith("/"):
            continue
        disk = _security_disk_walk(git.root, selector)
        if disk is None or any(kind == "unsafe" for kind in disk.values()):
            return False
        expected_files = {path for path in all_records if path.startswith(selector)}
        actual_files = {path for path, kind in disk.items() if kind == "file"}
        if actual_files != expected_files:
            return False

    tests_disk = _security_disk_walk(git.root, "tests/")
    if tests_disk is None:
        return False
    for prefix in entry["owner_test_file_prefixes"]:
        basename_prefix = prefix.removeprefix("tests/")
        relevant = {
            path: kind
            for path, kind in tests_disk.items()
            if path.startswith(prefix) or path.rsplit("/", 1)[-1].startswith(basename_prefix)
        }
        expected = {
            path
            for path in all_records
            if path.startswith(prefix)
            and "/" not in path[len("tests/") :]
            and _SECURITY_TEST_SUFFIX_RE.fullmatch(path[len(prefix) :])
        }
        if set(relevant) != expected or any(kind != "file" for kind in relevant.values()):
            return False

    inventory = registry["managed_script_inventory"]
    namespace = re.compile(inventory["namespace_pattern"])
    scripts_disk = _security_disk_walk(git.root, "scripts/")
    if scripts_disk is None:
        return False
    disk_managed = {
        path: kind for path, kind in scripts_disk.items() if namespace.fullmatch(path)
    }
    inventory_parts = _security_managed_inventory_parts(registry)
    if inventory_parts is None:
        return False
    legacy, owners, central, _shared = inventory_parts
    declared = set(legacy) | owners | central
    if not set(disk_managed) <= declared or any(kind != "file" for kind in disk_managed.values()):
        return False
    return True


def _materialize_git_blobs(git: GitSubject, commit: str, root: Path, paths: set[str]) -> bool:
    for rel in sorted(paths):
        blob = git.blob_bytes(commit, rel)
        if blob is None:
            return False
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    return True


def _security_evolution_state(
    git: GitSubject, commit: str, registry: dict
) -> tuple[frozenset[int], dict[str, str]] | None:
    """Replay the pinned append-only chain from immutable Git blobs at commit."""

    cache = getattr(git, "_security_evolution_cache", None)
    if cache is None:
        cache = git._security_evolution_cache = {}
    cache_key = (commit, registry.get("script_evolution_policy_sha256"))
    if cache_key in cache:
        return cache[cache_key]
    policy_ref = registry["script_evolution_policy_ref"]
    policy_raw = git.blob_bytes(commit, policy_ref)
    if policy_raw is None or hashlib.sha256(policy_raw).hexdigest() != registry[
        "script_evolution_policy_sha256"
    ]:
        cache[cache_key] = None
        return None
    try:
        policy_doc = json.loads(policy_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        cache[cache_key] = None
        return None
    central = {
        policy_ref,
        "PR/PR-001-multitenant-knowledge-spaces/contracts/script-evolution/policy_v1.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/script-evolution/receipt_v1.schema.json",
    }
    paths = set(central)
    companions = policy_doc.get("companion_immutable_paths")
    if not isinstance(companions, list):
        cache[cache_key] = None
        return None
    for companion in companions:
        if not isinstance(companion, dict) or not isinstance(companion.get("target_path"), str):
            cache[cache_key] = None
            return None
        paths.add(companion["target_path"])
    present: set[int] = set()
    for stage in policy_doc.get("stages", []):
        if not isinstance(stage, dict):
            cache[cache_key] = None
            return None
        target = stage.get("target_path")
        receipt_path = stage.get("receipt_path")
        if not isinstance(target, str) or not isinstance(receipt_path, str):
            cache[cache_key] = None
            return None
        paths.add(target)
        raw = git.blob_bytes(commit, receipt_path)
        if raw is None:
            continue
        present.add(stage["stage_index"])
        paths.add(receipt_path)
        try:
            receipt = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            cache[cache_key] = None
            return None
        migration = receipt.get("migration_note_path")
        refs = receipt.get("acceptance_test_refs")
        if not isinstance(migration, str) or not isinstance(refs, list):
            cache[cache_key] = None
            return None
        paths.add(migration)
        for ref in refs:
            if not isinstance(ref, str):
                cache[cache_key] = None
                return None
            paths.add(ref.partition("::")[0])
    try:
        import pr001_script_evolution_guard as _eg

        with tempfile.TemporaryDirectory() as tmp:
            snapshot_root = Path(tmp)
            if not _materialize_git_blobs(git, commit, snapshot_root, paths):
                cache[cache_key] = None
                return None
            policy = _eg.load_policy(
                snapshot_root,
                expected_policy_sha256=registry["script_evolution_policy_sha256"],
            )
            genesis = {
                item["target_path"]: item["genesis_sha256"]
                for item in policy.raw["evolvable_paths"]
            }
            chain = _eg.replay_chain(snapshot_root, policy, genesis=genesis)
            for target, tip in chain.tips.items():
                blob = git.blob_bytes(commit, target)
                if blob is None or hashlib.sha256(blob).hexdigest() != tip:
                    cache[cache_key] = None
                    return None
            for companion in policy.raw["companion_immutable_paths"]:
                rel = companion["target_path"]
                blob = git.blob_bytes(commit, rel)
                if blob is None or hashlib.sha256(blob).hexdigest() != companion["sha256"]:
                    cache[cache_key] = None
                    return None
            _eg.verify_tenant_cli(snapshot_root, policy, chain.tenant_cli_slots)
    except (OSError, ValueError, KeyError, TypeError, _eg.ScriptEvolutionError):
        cache[cache_key] = None
        return None
    result = (frozenset(present), dict(chain.tips))
    cache[cache_key] = result
    return result


def verify_security_subject_commit(
    git: GitSubject | None,
    subject_commit: object,
    evidence_commit: object,
    *,
    evaluation_commit: object,
    entry: object,
    registry: object,
    declared_tree_sha256: object,
) -> bool:
    """Full security v2 binding including candidate drift and evolution closure."""

    if git is None or not isinstance(entry, dict) or not isinstance(registry, dict):
        return False
    if not security_registry_owner_semantics_ok(registry):
        return False
    if entry.get("unresolved_owner_surface_requirements"):
        return False
    if not isinstance(subject_commit, str) or not SHA1_RE.match(subject_commit):
        return False
    if not isinstance(evidence_commit, str) or not SHA1_RE.match(evidence_commit):
        return False
    if not isinstance(evaluation_commit, str) or not SHA1_RE.match(evaluation_commit):
        return False
    if not git.commit_exists(subject_commit) or not git.commit_exists(evaluation_commit):
        return False
    if not git.is_strict_ancestor(subject_commit, evidence_commit):
        return False
    if evidence_commit != evaluation_commit and not git.is_ancestor(evidence_commit, evaluation_commit):
        return False
    subject = _security_scope_snapshot(git, subject_commit, entry, registry)
    candidate = _security_scope_snapshot(git, evaluation_commit, entry, registry)
    if subject is None or candidate is None:
        return False
    if not _security_worktree_matches_evaluation(
        git, evaluation_commit, entry, registry, candidate
    ):
        return False
    subject_manifest, subject_records, subject_owner_paths, shared_paths = subject
    candidate_manifest, candidate_records, _candidate_owner_paths, candidate_shared = candidate
    if subject_manifest != candidate_manifest or shared_paths != candidate_shared:
        return False
    if not set(git.paths_touched(subject_commit)) & set(subject_owner_paths):
        return False  # shared ABI and evidence outputs never satisfy owner touch
    actual = security_owner_scope_tree_sha256(git, subject_commit, entry, registry)
    if not isinstance(declared_tree_sha256, str) or actual != declared_tree_sha256:
        return False
    immutable_subject = {p: r for p, r in subject_records.items() if p not in shared_paths}
    immutable_candidate = {p: r for p, r in candidate_records.items() if p not in shared_paths}
    if immutable_subject != immutable_candidate:
        return False
    # Replay the complete pinned policy for every SG receipt, not only RTs
    # that own a future stage.  Stage 9 is a completed RT-012 compatibility
    # evolution that predates all RT-017..026 security receipts; no SG entry
    # owns it, but every candidate must still prove its receipt chain and final
    # ``cwk_instance.py`` tip.  Per-owner stage checks below remain additional
    # constraints for stages 1..8.
    required_stages = set(entry["owner_evolution_stage_indices"])
    subject_evolution = _security_evolution_state(git, subject_commit, registry)
    candidate_evolution = _security_evolution_state(git, evaluation_commit, registry)
    if subject_evolution is None or candidate_evolution is None:
        return False
    subject_stages, _subject_tips = subject_evolution
    candidate_stages, _candidate_tips = candidate_evolution
    if not required_stages <= set(subject_stages) or not set(subject_stages) <= set(candidate_stages):
        return False
    if required_stages:
        shared_cfg = {
            item["path"]: item for item in registry["shared_evolution_paths"]
        }
        for path in shared_paths:
            owned = shared_cfg[path]["owner_stage_indices"][entry["producer_rt"]]
            policy_bytes = git.blob_bytes(subject_commit, registry["script_evolution_policy_ref"])
            if policy_bytes is None:
                return False
            try:
                policy_doc = json.loads(policy_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return False
            path_stages = [
                stage["stage_index"]
                for stage in policy_doc.get("stages", [])
                if stage.get("target_path") == path and stage["stage_index"] in subject_stages
            ]
            if not path_stages or max(path_stages) != max(owned):
                return False
    elif shared_paths:
        return False
    return True


def verify_environment_fingerprint(fingerprint: object, expected: object) -> bool:
    """Recompute, don't format-check.

    `expected` is the environment actually being certified (built by the
    caller from the live interpreter/platform).  Every key the receipt declares
    must equal the observed value; a fabricated `platform` or a `python_version`
    that is not the one running is rejected instead of merely regex-matched.
    """

    if not isinstance(fingerprint, dict) or not isinstance(expected, dict):
        return False
    if not expected:
        return False
    for key, value in expected.items():
        if key not in fingerprint:
            return False
        if fingerprint[key] != value:
            return False
    return True


def verify_environment_fingerprint_exact(fingerprint: object, expected: object) -> bool:
    """As above, but EXACT equality in both directions.

    :func:`verify_environment_fingerprint` checks that every observed key is
    declared with the right value, which leaves the receipt free to declare
    EXTRA keys the evaluator never looked at. For the release gates that is a
    hole: a receipt could declare ``{python_version, platform, toolchain_build,
    feature_flags: "all-on"}`` and be certified against an environment where
    only the first three were observed, so the fourth is asserted, unchecked,
    and carried forward as if verified. The release layer therefore requires
    the declared key set and the observed key set to be EQUAL - every declared
    key observed with the same value AND no undeclared key present.

    Kept as a separate function rather than a stricter version of its sibling
    so the capability/probe callers that legitimately observe a subset keep
    their existing semantics unchanged.
    """

    if not isinstance(fingerprint, dict) or not isinstance(expected, dict):
        return False
    if not expected:
        return False
    if set(fingerprint) != set(expected):
        return False
    return all(fingerprint[key] == value for key, value in expected.items())


# ---------------------------------------------------------------------------
# renewal probe manifests
# ---------------------------------------------------------------------------

PROBE_FIELDS = (
    "capability_id",
    "sequence",
    "challenge",
    "observed_at",
    "tested_subject_commit",
    "environment_fingerprint",
    "api_version",
    "result",
    "verifier",
    "signature",
)

_PROBE_SIGNED_FIELDS = tuple(f for f in PROBE_FIELDS if f != "signature")


def probe_signing_payload(manifest: dict) -> bytes:
    """Canonical, domain-separated bytes a probe signature covers."""

    body = {key: manifest.get(key) for key in _PROBE_SIGNED_FIELDS}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _PROBE_DOMAIN + encoded.encode("utf-8")


def probe_signature(manifest: dict, signing_key: bytes) -> str:
    return hmac.new(signing_key, probe_signing_payload(manifest), hashlib.sha256).hexdigest()


def verify_probe_manifest(
    manifest: object,
    *,
    capability_id: str,
    sequence: int,
    subject_commit: str,
    expected_environment: dict,
    clock: EvaluationClock,
    signing_key: bytes,
    certified_at: _dt.datetime,
    previous_observed_at: _dt.datetime | None = None,
    expected_api_version: object = None,
) -> str | None:
    """Validate a renewal probe manifest. Returns `None` when valid, else why not.

    A renewal is only a renewal if something was actually re-observed.  The
    first version of this check merely required the probe bytes to differ from
    the previous evidence, which any byte flip satisfies.  A manifest instead
    has to say *what was observed, when, against which subject, by whom*, and
    carry a signature over exactly that -- and the `sequence` and `challenge`
    inside it are what stop a prior probe from being replayed under a new name.
    """

    if not isinstance(manifest, dict):
        return "probe manifest is not a JSON object"
    missing = [f for f in PROBE_FIELDS if f not in manifest]
    if missing:
        return f"probe manifest is missing {missing!r}"
    extra = sorted(set(manifest) - set(PROBE_FIELDS))
    if extra:
        return f"probe manifest carries undeclared fields {extra!r}"

    if manifest["capability_id"] != capability_id:
        return (
            f"probe manifest certifies {manifest['capability_id']!r}, "
            f"not {capability_id!r}"
        )
    seq = manifest["sequence"]
    if not isinstance(seq, int) or isinstance(seq, bool) or seq != sequence:
        return f"probe manifest sequence {seq!r} does not match receipt sequence {sequence}"
    challenge = manifest["challenge"]
    if not isinstance(challenge, str) or not SHA256_RE.match(challenge):
        return "probe challenge must be 64 lowercase hex characters"
    if manifest["tested_subject_commit"] != subject_commit:
        return "probe manifest binds a different subject commit than the receipt"
    if not verify_environment_fingerprint(manifest["environment_fingerprint"], expected_environment):
        return "probe environment fingerprint does not match the observed environment"
    api_version = manifest["api_version"]
    if not isinstance(api_version, str) or not api_version.strip():
        return "probe manifest must record the external API version it observed"
    if expected_api_version is not None and api_version != expected_api_version:
        return f"probe observed API version {api_version!r}, expected {expected_api_version!r}"
    if manifest["result"] != "pass":
        return f"probe result is {manifest['result']!r}; a renewal requires a passing probe"
    verifier = manifest["verifier"]
    if not isinstance(verifier, str) or not verifier.strip():
        return "probe manifest must name the verifier that ran it"

    observed_at = parse_instant(manifest["observed_at"])
    if observed_at is None:
        return "probe observed_at is not a timezone-aware ISO-8601 instant"
    if clock.is_future_dated(observed_at):
        return "probe observed_at is in the future beyond the allowed clock skew"
    if certified_at.tzinfo is None:
        return "certified_at anchor must be timezone-aware"
    # A probe justifies a renewal, so it must have been observed BEFORE that
    # renewal was signed -- a "probe" dated after the receipt it certifies is
    # backfill, not observation.
    if observed_at > certified_at + _dt.timedelta(seconds=clock.max_skew_seconds):
        return "probe observed_at is later than the receipt it claims to certify"
    if clock.is_stale_probe(observed_at, certified_at):
        return (
            f"probe observed_at is more than {clock.max_probe_age_seconds}s older "
            "than the receipt it certifies; it did not certify this renewal"
        )
    if previous_observed_at is not None and observed_at <= previous_observed_at:
        return "probe observed_at must be strictly later than the previous probe"

    signature = manifest["signature"]
    if not isinstance(signature, str) or not SHA256_RE.match(signature):
        return "probe signature must be 64 lowercase hex characters"
    expected_sig = probe_signature(manifest, signing_key)
    if not hmac.compare_digest(signature, expected_sig):
        return "probe signature does not recompute; the manifest was altered or forged"
    return None


# ---------------------------------------------------------------------------
# fixture support: a real two-commit evidence repository
# ---------------------------------------------------------------------------


def init_fixture_repo(root: Path) -> GitSubject:
    """Create a real git repo in `root`. Tests need real ancestry, not stubs."""

    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(root)],
        check=True,
        capture_output=True,
        env=_GIT_ENV,
        timeout=30,
    )
    git = GitSubject(root)
    git._git("config", "user.email", "fixture@example.invalid")
    git._git("config", "user.name", "pr001-fixture")
    git._git("config", "commit.gpgsign", "false")
    return git


def index_has_hidden_entries(git: GitSubject) -> bool:
    """True when Git's index is configured to conceal worktree changes.

    ``git status`` intentionally honours ``assume-unchanged`` and
    ``skip-worktree``.  That behaviour is useful for local development but is
    unsafe for a release decision: deleting an acceptance report can otherwise
    look clean.  ``ls-files -v`` exposes those bits as ``h`` (assume unchanged),
    ``S`` (skip worktree), or ``s`` (both/lower-cased skip-worktree).
    """

    raw = git._out_bytes("ls-files", "-v", "-z")
    if raw is None:
        raise EvidenceBindingError("fixture index inspection failed")
    for record in raw.split(b"\0"):
        if not record:
            continue
        if len(record) < 3 or record[1:2] != b" ":
            raise EvidenceBindingError("fixture index inspection was malformed")
        if record[:1] in {b"h", b"s", b"S"}:
            return True
    return False


def worktree_is_dirty(git: GitSubject) -> bool:
    """True when the fixture worktree holds content no commit records yet.

    A fixture writes receipts and only then asks for a verdict, so the
    evaluation commit has to be made lazily.  Committing unconditionally would
    manufacture an empty commit on every recomputation and move the evidence
    commit away from the commit that actually introduced the receipt.
    """

    if index_has_hidden_entries(git):
        return True
    proc = git._git("status", "--porcelain", "--untracked-files=all")
    if proc.returncode != 0:
        raise EvidenceBindingError(f"fixture status failed: {proc.stderr.strip()}")
    return bool(proc.stdout.strip())


def commit_all(git: GitSubject, message: str) -> str:
    """Stage everything and commit; returns the new commit sha."""

    git._git("add", "-A")
    proc = git._git("commit", "-q", "--no-verify", "--allow-empty", "-m", message)
    if proc.returncode != 0:
        raise EvidenceBindingError(f"fixture commit failed: {proc.stderr.strip()}")
    sha = git.head()
    if sha is None:
        raise EvidenceBindingError("fixture commit produced no HEAD")
    return sha
