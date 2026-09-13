"""Deterministic exact-term extraction used by both indexing and querying."""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass
from typing import Any


DASHES = str.maketrans({char: "-" for char in "\u2010\u2011\u2012\u2013\u2014\u2212\ufe63"})
_DATE = re.compile(r"(?<!\d)(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?(?!\d)")
_IDENTIFIER = re.compile(
    r"(?<![a-z0-9_])(?=[a-z0-9_-]*[a-z])(?=[a-z0-9_-]*\d)"
    r"[a-z0-9]+(?:[-_][a-z0-9]+)+(?![a-z0-9_])"
)
_FILENAME = re.compile(r"[^\s/\\<>\"'‘’“”「」《》()\[\]{}（）【】,，;；:：!?？。]+\.[a-z][a-z0-9]{0,7}\b")
MAX_EXACT_TERMS = 32


def normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).translate(DASHES).casefold()


def _valid_dates(text: str) -> list[str]:
    dates: set[str] = set()
    for match in _DATE.finditer(text):
        try:
            dates.add(dt.date(*(int(value) for value in match.groups())).isoformat())
        except ValueError:
            continue
    return sorted(dates)


def extract_exact_fields(text: str, filename: str = "") -> dict[str, list[str]]:
    """Extract normalized identifiers, valid ISO dates, and filenames."""
    if not isinstance(text, str):
        raise TypeError("exact query must be text")
    normalized = normalize(text)
    filenames = set(_FILENAME.findall(normalized))
    if filename:
        if not isinstance(filename, str):
            raise TypeError("filename must be text")
        filenames.add(normalize(filename.replace("\\", "/").rsplit("/", 1)[-1]))
    return {
        "identifiers": sorted(set(_IDENTIFIER.findall(normalized))),
        "date_values": _valid_dates(normalized),
        "filenames": sorted(filenames),
    }


@dataclass(frozen=True)
class ExactResolution:
    identifiers: tuple[str, ...] = ()
    date_values: tuple[str, ...] = ()
    filenames: tuple[str, ...] = ()

    @property
    def has_terms(self) -> bool:
        return bool(self.identifiers or self.date_values or self.filenames)

    @property
    def term_count(self) -> int:
        return len(self.identifiers) + len(self.date_values) + len(self.filenames)

    def as_filters(self) -> list[dict[str, Any]]:
        fields = (
            ("identifiers", self.identifiers),
            ("date_values", self.date_values),
            ("filenames", self.filenames),
        )
        return [{"term": {field: value}} for field, values in fields for value in values]


def resolve_exact(query: str) -> ExactResolution:
    fields = extract_exact_fields(query)
    result = ExactResolution(
        tuple(fields["identifiers"]), tuple(fields["date_values"]), tuple(fields["filenames"])
    )
    if result.term_count > MAX_EXACT_TERMS:
        raise ValueError("exact query contains too many terms")
    return result
