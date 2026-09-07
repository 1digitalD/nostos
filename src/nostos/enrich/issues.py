"""Narrow saved-evidence warnings; never overwrite a fact or infer a remedy."""

from __future__ import annotations

import re
from dataclasses import dataclass

from nostos.enrich.text import _BEDS_RE, _ROOM_ONLY_RE, has_ambiguous_bedroom_range
from nostos.model import Listing, Observed, Origin

_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4}
_BED_CLAIM = re.compile(
    _BEDS_RE.pattern.replace(r"\d+(?:\.5)?", r"\d+(?:\.5)?|one|two|three|four").replace(
        r"\s*", r"[\s-]*"
    ),
    re.IGNORECASE,
)
_PRIVATE_ROOM = re.compile(
    r"\bprivate\s+rooms?\s+(?:for\s+rent|available)\b|"
    r"\bshared\s+kitchen\s*(?:and|&|/)\s*bathroom\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EvidenceIssue:
    field: str
    reason: str
    evidence: str


def saved_evidence_issues(listing: Listing) -> tuple[EvidenceIssue, ...]:
    """Flag only supported scope/count concerns, respecting explicit corrections.

    This is a conservative guard, not comprehensive semantic interpretation.
    It runs even on populated facts so a fill-only parser cannot hide conflicts.
    """
    text = "\n".join(
        item.value
        for key, item in listing.attributes.items()
        if key in {"title", "description", "source_attributes"}
        and isinstance(item, Observed)
        and isinstance(item.value, str)
    )
    issues: list[EvidenceIssue] = []
    full_unit = listing.attributes.get("full_unit")
    if isinstance(full_unit, Observed) and full_unit.value is False:
        issues.append(
            EvidenceIssue(
                "full_unit",
                "This is recorded as a room or shared rental. "
                "Confirm that unit-wide price, bedrooms, and area apply to what you would rent.",
                full_unit.evidence or "Recorded entire-unit value: no",
            )
        )
    elif not (isinstance(full_unit, Observed) and full_unit.origin == Origin.USER):
        room = _ROOM_ONLY_RE.search(text) or _PRIVATE_ROOM.search(text)
        if room is not None:
            issues.append(
                EvidenceIssue(
                    "full_unit",
                    "Confirm whether this price and these facts describe an entire unit "
                    "or a room with shared facilities.",
                    room.group(0),
                )
            )
    beds = listing.beds
    if not (isinstance(beds, Observed) and beds.origin == Origin.USER):
        claims = list(_BED_CLAIM.finditer(text))
        counts = {
            float(_NUMBERS[raw]) if raw in _NUMBERS else float(raw)
            for match in claims
            for raw in [match.group(1).lower()]
        }
        mismatch = isinstance(beds, Observed) and any(n != beds.value for n in counts)
        if len(counts) > 1 or mismatch or has_ambiguous_bedroom_range(text):
            issues.append(
                EvidenceIssue(
                    "beds",
                    "Bedroom claims differ or describe multiple layouts. "
                    "Confirm bedrooms for this unit, excluding offices and dens.",
                    "; ".join(dict.fromkeys(match.group(0) for match in claims)),
                )
            )
    return tuple(issues)
