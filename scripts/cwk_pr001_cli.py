#!/usr/bin/env python3
"""RT-011 public CLI: ``cwk-pr001 <subcommand>``.

Redesigned in the r1 remediation to:

- redact all incoming file paths (never echo absolute host paths);
- reject unknown/dangerous keys in ``security_defaults.json`` on
  ``conformance``;
- refuse to accept free-form ``notes`` or ``evidence_refs`` (probe schema
  no longer carries them);
- keep exit codes stable: 0 ok, 2 contract error, 3 probe aggregate
  ``all_verified=false``, 4 usage error, 5 IO problem.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

_PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT / "scripts"))

import cwk_pr001_contracts as C  # noqa: E402
import cwk_pr001_probes as P  # noqa: E402
import cwk_pr001_view_compare as V  # noqa: E402


EXIT_OK = 0
EXIT_CONTRACT = 2
EXIT_UNKNOWN = 3
EXIT_USAGE = 4
EXIT_IO = 5


def _safe_path(display: str) -> str:
    """Never echo absolute host paths; show the trailing components only."""

    p = Path(display)
    parts = p.parts
    # Drop any absolute-root indicators before we join.
    filtered = [part for part in parts if part not in ("/", "\\")]
    if not filtered:
        return "<input>"
    return "/".join(filtered[-2:]) if len(filtered) >= 2 else filtered[-1]


def _read_json(path: str) -> Any:
    try:
        return C.strict_json_load_path(Path(path))
    except FileNotFoundError:
        print(f"error: input not found: {_safe_path(path)}", file=sys.stderr)
        raise SystemExit(EXIT_IO)
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON in {_safe_path(path)}: {exc.msg}", file=sys.stderr)
        raise SystemExit(EXIT_CONTRACT)
    except C.ContractError as exc:
        # Duplicate JSON keys raise ContractError from strict_json_loads.
        print(f"contract-error: {_safe_path(path)}: {exc.args[0]}", file=sys.stderr)
        raise SystemExit(EXIT_CONTRACT)
    except OSError as exc:
        print(f"error: cannot read {_safe_path(path)}: {exc.strerror or 'io error'}", file=sys.stderr)
        raise SystemExit(EXIT_IO)


def _emit(payload: Any) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _handle_contract(exc: C.ContractError) -> int:
    prefix = f"{exc.path}: " if exc.path else ""
    print(f"contract-error: {prefix}{exc.args[0]}", file=sys.stderr)
    return EXIT_CONTRACT


# ---------------------------------------------------------------------------
# Redaction helpers
# ---------------------------------------------------------------------------


def _redact_probe(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a redacted copy suitable for CLI stdout.

    The probe schema does not carry ``notes``/``evidence_refs``, but the
    ``receipt.signature`` value is a shared secret-derived digest and must
    not be echoed verbatim.  We keep ``envelope_sha256`` (already a hash)
    and replace ``signature`` with ``"<redacted>"``; downstream aggregators
    that need to verify signatures should read the source file directly.
    """

    receipt = payload.get("receipt")
    if receipt is None:
        return payload
    redacted_receipt = dict(receipt)
    if "signature" in redacted_receipt:
        redacted_receipt["signature"] = "<redacted>"
    out = dict(payload)
    out["receipt"] = redacted_receipt
    return out


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    payload = _read_json(args.file)
    try:
        if args.schema:
            validator = C.VALIDATORS.get(args.schema)
            if validator is None:
                print(f"error: unknown schema {args.schema!r}", file=sys.stderr)
                return EXIT_USAGE
            validator(payload)
        else:
            C.validate(payload)
    except C.ContractError as exc:
        return _handle_contract(exc)
    print("ok", file=sys.stderr)
    return EXIT_OK


def cmd_canonicalize(args: argparse.Namespace) -> int:
    payload = _read_json(args.file)
    try:
        data = C.canonical_json_bytes(payload)
    except C.ContractError as exc:
        return _handle_contract(exc)
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.write(b"\n")
    return EXIT_OK


def cmd_sha256(args: argparse.Namespace) -> int:
    payload = _read_json(args.file)
    try:
        digest = C.canonical_sha256(payload)
    except C.ContractError as exc:
        return _handle_contract(exc)
    print(digest)
    return EXIT_OK


def cmd_profile_hash(args: argparse.Namespace) -> int:
    proposal = _read_json(args.proposal)
    manifest = _read_json(args.sample_manifest)
    try:
        C.validate_sample_manifest(manifest)
        sample_sha = C.canonical_sha256(manifest)
        digest = C.compute_profile_sha256(
            nfc_normalized_proposal=proposal,
            sample_manifest_sha256=sample_sha,
            prompt_template_sha256=args.prompt_template_sha256,
            model_id=args.model_id,
        )
    except C.ContractError as exc:
        return _handle_contract(exc)
    _emit(
        {
            "schema": "cwk.pr001.profile_hash_receipt.v1",
            "profile_sha256": digest,
            "sample_manifest_sha256": sample_sha,
            "prompt_template_sha256": args.prompt_template_sha256,
            "model_id": args.model_id,
            "formula": (
                "sha256(b'cwk-profile-v1' + 0x00 + jcs_utf8(nfc_normalized_proposal) + 0x00 "
                "+ sample_manifest_sha256_ascii + 0x00 + prompt_template_sha256_ascii + 0x00 + model_id_utf8)"
            ),
        }
    )
    return EXIT_OK


def cmd_compare(args: argparse.Namespace) -> int:
    try:
        set_a = V.load_observation_set(args.tenant_a)
        set_b = V.load_observation_set(args.tenant_b)
        report = V.compare(set_a, set_b, upgrade_threshold=args.threshold)
    except C.ContractError as exc:
        return _handle_contract(exc)
    _emit(report)
    return EXIT_OK


def cmd_probe_run(args: argparse.Namespace) -> int:
    """Run every frozen probe; without a signed receipt, all are conservative.

    The CLI does not accept a ``--receipt`` flag in RT-011 because the
    trusted signer allowlist is empty.  Test suites that need to exercise
    the ``verified`` code path import the library and use
    :func:`cwk_pr001_probes.build_receipt` directly.
    """

    probes: list[dict[str, Any]] = []
    for probe_id in P.ALL_PROBE_IDS:
        try:
            probes.append(P.run_probe(probe_id))
        except C.ContractError as exc:
            return _handle_contract(exc)
    _emit([_redact_probe(p) for p in probes])
    return EXIT_OK


def cmd_probe_aggregate(args: argparse.Namespace) -> int:
    payloads: list[dict[str, Any]] = []
    for path in args.files:
        payload = _read_json(path)
        if isinstance(payload, list):
            payloads.extend(payload)
        elif isinstance(payload, dict):
            payloads.append(payload)
        else:
            print(f"error: {_safe_path(path)} is neither object nor array", file=sys.stderr)
            return EXIT_CONTRACT
    try:
        summary = P.aggregate(payloads)
    except C.ContractError as exc:
        return _handle_contract(exc)
    _emit(summary)
    return EXIT_OK if summary["all_verified"] else EXIT_UNKNOWN


def cmd_conformance(args: argparse.Namespace) -> int:
    try:
        defaults = C.load_security_defaults()
    except C.ContractError as exc:
        return _handle_contract(exc)

    candidate = _read_json(args.file)
    # First: schema-strict validation catches added dangerous keys because
    # every object is additionalProperties:false and the security handler
    # rejects any deep dangerous keys.
    try:
        C.validate_security_defaults(candidate)
    except C.ContractError as exc:
        return _handle_contract(exc)

    diffs = _diff_conformance(defaults, candidate)
    if diffs:
        print("conformance-fail:", file=sys.stderr)
        for d in diffs:
            print(f"  - {d}", file=sys.stderr)
        return EXIT_CONTRACT
    _emit({"schema": "cwk.pr001.conformance_receipt.v1", "conforms": True})
    return EXIT_OK


def _diff_conformance(expected: Any, actual: Any, path: str = "") -> list[str]:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path or '<root>'}: expected object, got {type(actual).__name__}"]
        diffs: list[str] = []
        for key, value in expected.items():
            child = f"{path}.{key}" if path else key
            if key not in actual:
                diffs.append(f"{child}: missing")
                continue
            diffs.extend(_diff_conformance(value, actual[key], child))
        for key in actual.keys():
            if key not in expected:
                child = f"{path}.{key}" if path else key
                diffs.append(f"{child}: additional key not in frozen defaults")
        return diffs
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path}: expected array, got {type(actual).__name__}"]
        if expected != actual:
            return [f"{path}: array differs"]
        return []
    if expected != actual:
        return [f"{path or '<root>'}: value differs"]
    return []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cwk-pr001",
        description="RT-011 external contract probes and schema validators (read-only).",
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="command")

    p_validate = sub.add_parser("validate", help="Validate a JSON payload against v1 schemas.")
    p_validate.add_argument("--file", required=True)
    p_validate.add_argument("--schema", required=False, help="Force a schema id (else dispatch on payload).")

    p_canon = sub.add_parser("canonicalize", help="Emit NFC + RFC 8785 JCS canonical JSON.")
    p_canon.add_argument("--file", required=True)

    p_sha = sub.add_parser("sha256", help="Print canonical SHA-256.")
    p_sha.add_argument("--file", required=True)

    p_ph = sub.add_parser("profile-hash", help="Compute profile_sha256 per DESIGN §7.2.")
    p_ph.add_argument("--proposal", required=True)
    p_ph.add_argument("--sample-manifest", required=True)
    p_ph.add_argument("--prompt-template-sha256", required=True)
    p_ph.add_argument("--model-id", required=True)

    p_cmp = sub.add_parser("compare-user-views", help="Dual-user field comparator.")
    p_cmp.add_argument("--tenant-a", required=True)
    p_cmp.add_argument("--tenant-b", required=True)
    p_cmp.add_argument("--threshold", type=float, default=V.DEFAULT_SHARE_UPGRADE_THRESHOLD)

    p_probe = sub.add_parser("probe", help="Capability probes.")
    probe_sub = p_probe.add_subparsers(dest="probe_command")
    probe_sub.add_parser("run", help="Run frozen probe matrix (always conservative in RT-011).")
    p_agg = probe_sub.add_parser("aggregate", help="Aggregate probe payloads.")
    p_agg.add_argument("files", nargs="+")

    p_conf = sub.add_parser("conformance", help="Check config against security_defaults.json.")
    p_conf.add_argument("--file", required=True)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    if args.command == "probe" and getattr(args, "probe_command", None) is None:
        parser.parse_args([args.command, "--help"])
        return EXIT_USAGE

    try:
        if args.command == "validate":
            return cmd_validate(args)
        if args.command == "canonicalize":
            return cmd_canonicalize(args)
        if args.command == "sha256":
            return cmd_sha256(args)
        if args.command == "profile-hash":
            return cmd_profile_hash(args)
        if args.command == "compare-user-views":
            return cmd_compare(args)
        if args.command == "probe":
            if args.probe_command == "run":
                return cmd_probe_run(args)
            if args.probe_command == "aggregate":
                return cmd_probe_aggregate(args)
        if args.command == "conformance":
            return cmd_conformance(args)
        print(f"error: unknown command {args.command!r}", file=sys.stderr)
        return EXIT_USAGE
    except SystemExit:
        raise
    except C.ContractError as exc:
        return _handle_contract(exc)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"error: internal failure ({exc.__class__.__name__})", file=sys.stderr)
        return EXIT_IO


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
