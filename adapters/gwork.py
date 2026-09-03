"""RT-041 adapter #1: CWork (工作协同), wrapper-style.

This adapter WRAPS the existing pipeline instead of reimplementing it:

- ``discover`` → ``cwk_backfill_range.source_rows(source="dual")`` (inbox +
  outbox, the RT-040 ord1 default; report-1xx ids come back deduped)
- ``fetch``    → ``cwk_backfill_range.fetch_one`` (detail pull + raw
  markdown write, the very same code nightly uses today)
- ``dedupe_key`` → ``gwork-<native_id>``
- ``watch``    → reply dynamics via ``cwk_backfill_range.inbox_source_rows``
  + ``outbox_source_rows`` + ``reply_refresh.detect_changes`` (the RT-040
  ord2 baseline mechanism)

scripts/ is a closed namespace: this file changes nothing there.  Byte
equivalence with the legacy backfill lane is locked by
``tests/test_rt041_gwork_adapter.py`` (same window → same id set; fetch
output identical to fetch_one's file).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_PROJECT = Path(__file__).resolve().parents[1]
if str(_PROJECT / "adapters") not in sys.path:
    sys.path.insert(0, str(_PROJECT / "adapters"))

from base import NormalizedDoc, SourceItem, register  # noqa: E402

_SCRIPTS = _PROJECT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# Heavy/lazy imports live inside methods: importing scripts modules here at
# module top would also work (they are stdlib-only), but keeping the import
# graph shallow makes the contract module usable in isolation.


@register
class GWorkAdapter:
    source_type = "gwork"
    id_prefix = "gwork-"

    # ── discover ────────────────────────────────────────────────────────────
    def discover(self, app_key: str, start_date: str, end_date: str) -> list[SourceItem]:
        from cwk_backfill_range import source_rows  # noqa: PLC0415

        rows, _total = source_rows(app_key, start_date, end_date, source="dual")
        return [SourceItem(native_id=self._rid(row), row=row) for row in rows if self._rid(row)]

    # ── fetch ───────────────────────────────────────────────────────────────
    def fetch(self, item: SourceItem, app_key: str, *, raw_dir: Path | None = None) -> NormalizedDoc:
        from cwk_backfill_range import fetch_one, normalized_row  # noqa: PLC0415

        raw_dir = raw_dir or _PROJECT / "runs" / "adapter-gwork" / "collected-raw"
        raw_dir.mkdir(parents=True, exist_ok=True)  # fetch_one delegates to write_markdown which writes flat files
        record = fetch_one(item.row, app_key, raw_dir)
        if record.get("status") != "written":
            raise RuntimeError(f"gwork fetch failed for {item.native_id}: {record.get('error', record)}")
        path = Path(record["path"])
        text = path.read_text(encoding="utf-8")
        meta = _frontmatter(text)
        return NormalizedDoc(
            id=self.dedupe_key(item),
            native_id=item.native_id,
            title=meta.get("title", ""),
            author=meta.get("writer", ""),
            participants=_participants(text, meta.get("writer", "")),
            created=meta.get("create_time", ""),
            source_type=self.source_type,
            body_markdown=text,
        )

    # ── dedupe_key ──────────────────────────────────────────────────────────
    def dedupe_key(self, item: SourceItem) -> str:
        return f"{self.id_prefix}{item.native_id}"

    # ── watch ───────────────────────────────────────────────────────────────
    def watch(
        self, app_key: str, baseline: dict[str, Any], start_date: str, end_date: str
    ) -> tuple[list[SourceItem], dict[str, Any]]:
        from cwk_backfill_range import (  # noqa: PLC0415
            _inbox_client, inbox_source_rows, outbox_source_rows,
        )
        from reply_refresh import detect_changes  # noqa: PLC0415

        client = _inbox_client(app_key)
        inbox_rows, _ = inbox_source_rows(client, start_date, end_date)
        outbox_rows, _ = outbox_source_rows(client, start_date, end_date)
        for row in outbox_rows:
            row["_from_outbox"] = True
        changed, fresh = detect_changes(baseline, inbox_rows + outbox_rows)
        return [SourceItem(native_id=self._rid(r), row=r) for r in changed if self._rid(r)], fresh

    # ── helpers ─────────────────────────────────────────────────────────────
    @staticmethod
    def _rid(row: dict[str, Any]) -> str:
        return str(row.get("id") or row.get("reportId") or row.get("reportRecordId") or "").strip()


# ── frontmatter / participants (mirror compile-time semantics) ─────────────
# Same role-label vocabulary as cwk_cloud_wiki_compile.ROLE_FIELD_LINE; only
# role-labelled lines count — free-form body text never widens participants.
_ROLE_LINE = None  # compiled lazily to keep import side effects zero


def _frontmatter(text: str) -> dict[str, str]:
    import re  # noqa: PLC0415

    match = re.match(r"---\s*\n(.*?)\n---", text, re.S)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


def _role_lines(text: str) -> list[str]:
    import re  # noqa: PLC0415

    global _ROLE_LINE
    if _ROLE_LINE is None:
        _ROLE_LINE = re.compile(
            r"^[ \t]*(?:-[ \t]*\*\*)?(?:汇报人|发件人|收件人|建议人|审批人|决策人|申请人|参与人|部门负责人|抄送人?|知会人)(?:\*\*)?[ \t]*[:：\[][ \t]*(.*)$",
            re.MULTILINE,
        )
    head, _sep, _body = text.partition("\n---\n")
    values = _ROLE_LINE.findall(text)
    out: list[str] = []
    for value in values:
        for token in re.split(r"[、,，;；\s]+", value.strip()):
            token = token.strip("* ")
            if token and token not in out:
                out.append(token)
    return out


def _participants(text: str, writer: str) -> list[str]:
    seen: list[str] = []
    for name in [writer, *_role_lines(text)]:
        name = name.strip()
        if name and name not in seen:
            seen.append(name)
    return seen
