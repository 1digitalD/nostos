"""Shared query helpers for the local web UI.

These helpers wrap the existing score / source_record / observation /
listing paths that the CLI commands use. No parallel SQL — both the CLI's
`nostos list` and the web list view build `Listing` objects via the same
`source_obj.to_listing(...)` entry point, and the web app uses the same
`ScoreRepo.get_score(...)` path as `nostos explain`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import nostos.rank.rules as _rules
from nostos.config.profile import Profile
from nostos.context import SearchContext
from nostos.model import Area, Listing, Money, Observed, Photo, SourceRecord
from nostos.model.source_record import JSONValue
from nostos.rank.profile_scoring import (
    _is_basement_listing,
    _is_furnished,
    listing_area_key,
    rent_display,
)
from nostos.sources.base import Source

# Human labels for rule categories. Prefer the registry's mapping when the
# rank module provides one; otherwise fall back to this small table.
_FALLBACK_CATEGORY_LABELS: dict[str, str] = {
    "amenities": "Amenities",
    "space": "Space & layout",
    "cost": "Cost",
    "proximity": "Location & proximity",
}
CATEGORY_LABELS: Mapping[str, str] = getattr(
    _rules, "CATEGORY_LABELS", _FALLBACK_CATEGORY_LABELS
)


def category_label(category: str) -> str:
    """Return the display label for a rule category (falls back to the key)."""

    return str(CATEGORY_LABELS.get(category, category))


# Sort keys accepted by the list view, with their display labels. The first
# entry is the default. Legacy keys from older bookmarks map via _SORT_ALIASES.
SORT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("score", "Match score ↓"),
    ("rent_asc", "Rent ↑"),
    ("rent_desc", "Rent ↓"),
    ("area_desc", "Size ↓"),
    ("posted_desc", "Newest"),
    ("posted_asc", "Oldest"),
    ("address", "Address A→Z"),
)
_SORT_ALIASES: dict[str, str] = {"rent": "rent_asc", "posted": "posted_desc"}
DEFAULT_SORT = SORT_OPTIONS[0][0]
_SORT_LABELS: dict[str, str] = dict(SORT_OPTIONS)


def normalize_sort(sort: str | None) -> str:
    """Map a user-supplied sort key onto a canonical one (unknown → default)."""

    if sort is None:
        return DEFAULT_SORT
    canonical = _SORT_ALIASES.get(sort, sort)
    return canonical if canonical in _SORT_LABELS else DEFAULT_SORT


def sort_label(sort: str) -> str:
    return _SORT_LABELS.get(normalize_sort(sort), _SORT_LABELS[DEFAULT_SORT])


MatchStatusKind = Literal["match", "unverified", "miss"]
STATUS_FILTER_VALUES: frozenset[str] = frozenset({"match", "unverified", "miss"})


@dataclass(frozen=True, slots=True)
class MatchStatus:
    """Outcome of comparing a listing against the profile's hard filters."""

    status: MatchStatusKind
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ListFilter:
    rent_min: float | None = None
    rent_max: float | None = None
    beds: float | None = None
    baths_min: float | None = None
    area_min: float | None = None
    score_min: float | None = None
    source: str | None = None
    area_name: str | None = None
    sort: str = DEFAULT_SORT
    # Quick toggles (URL params `starred=1`, `hide_dismissed=1`,
    # `show_excluded=1`) and the match-status filter (`status=match|...`).
    starred: bool = False
    hide_dismissed: bool = False
    show_excluded: bool = False
    status: str | None = None

    @property
    def has_numeric(self) -> bool:
        """True when any of the collapsible "More filters" inputs is active."""

        return any(
            value is not None
            for value in (
                self.rent_min,
                self.rent_max,
                self.beds,
                self.baths_min,
                self.area_min,
                self.score_min,
                self.source,
            )
        )


@dataclass(frozen=True, slots=True)
class ListRow:
    listing_id: str
    title: str
    source: str
    url: str
    rent_text: str
    rent_value: float | None
    beds: float | None
    baths: float | None
    area_value: float | None
    area_unit: str | None
    address: str | None
    area_key: str | None
    area_label: str | None
    score: float
    posted_at: datetime | None
    first_seen: datetime | None
    photos: tuple[Photo, ...]
    listing: Listing
    matched_filtered: bool = field(default=True)
    # Extra decision factors surfaced as a facts row in the UI. Parsers fill
    # these when the listing states them; otherwise None means "unstated".
    floor_text: str | None = None
    furnished: str | None = None
    parking: str | None = None
    available: str | None = None
    # Computed from profile + listing. One of:
    # "match"      — listing meets hard criteria
    # "unverified" — listing is missing data needed to evaluate a criterion
    # "miss"       — listing fails at least one criterion
    # "excluded"   — user has explicitly excluded this listing
    match_status: str = "match"
    # Short human explanations behind match_status ("rent $3,800 > max $3,600").
    match_reasons: tuple[str, ...] = ()
    # User action flags (star / dismiss) so the query layer can filter on them.
    starred: bool = False
    dismissed: bool = False
    # Per-category aggregated contribution (sum of contributions per category),
    # ready for rendering as horizontal bars in the card.
    category_scores: tuple[dict[str, float | str], ...] = field(default_factory=tuple)

    @property
    def primary_photo(self) -> Photo | None:
        return self.photos[0] if self.photos else None

    @property
    def is_excluded(self) -> bool:
        return self.match_status == "excluded"

    @property
    def match_reasons_text(self) -> str:
        return "; ".join(self.match_reasons)

    @property
    def facts_complete(self) -> dict[str, bool]:
        """Per-field presence flags so the template can show 'unstated' pills."""

        return {
            "floor": self.floor_text is not None,
            "furnished": self.furnished is not None,
            "parking": self.parking is not None,
            "available": self.available is not None,
        }

    @property
    def facts_summary(self) -> dict[str, object]:
        """Per-field label + value + presence for the facts row in the UI."""

        return {
            "floor": {"value": self.floor_text, "present": self.floor_text is not None},
            "furnished": {"value": self.furnished, "present": self.furnished is not None},
            "parking": {"value": self.parking, "present": self.parking is not None},
            "available": {"value": self.available, "present": self.available is not None},
        }


def query_list(
    conn: sqlite3.Connection,
    *,
    context: SearchContext,
    profile_id: str,
    sources: Mapping[str, Source],
    filters: ListFilter,
    limit: int | None = None,
) -> list[ListRow]:
    """Return scored listings joined with the latest source record, after filter.

    Listings that fail the profile's hard filters are *kept* and flagged
    ``miss`` (with reasons) rather than dropped, so a profile edited after
    the last rescore still shows what would fall out and why. Excluded
    listings are dropped unless ``filters.show_excluded`` is set.
    """

    rows = conn.execute(
        """
        SELECT listing_id, score
        FROM score
        WHERE profile_id = ?
        ORDER BY score DESC, listing_id ASC
        """,
        (profile_id,),
    ).fetchall()

    listing_ids = tuple(str(item["listing_id"]) for item in rows)
    score_by_id = {str(item["listing_id"]): float(item["score"]) for item in rows}
    latest_records = _latest_source_records_by_listing_ids(conn, listing_ids=listing_ids)
    excluded_ids = _action_listing_ids(conn, kind="excluded", listing_ids=listing_ids)
    starred_ids = _action_listing_ids(conn, kind="star", listing_ids=listing_ids)
    dismissed_ids = _action_listing_ids(conn, kind="dismiss", listing_ids=listing_ids)
    breakdowns_by_id = _breakdowns_by_listing(conn, listing_ids=listing_ids, profile_id=profile_id)
    area_labels = dict(known_areas(context))

    prepared: list[ListRow] = []
    for listing_id in listing_ids:
        if listing_id in excluded_ids and not filters.show_excluded:
            continue
        record_row = latest_records.get(listing_id)
        if record_row is None:
            continue
        source_obj = sources.get(record_row.source)
        if source_obj is None:
            continue
        listing = _listing_from_record(source_obj, record_row.record, listing_id, context)
        row = _build_list_row(
            listing_id=listing_id,
            listing=listing,
            score=score_by_id[listing_id],
            record=record_row.record,
            context=context,
            excluded=listing_id in excluded_ids,
            starred=listing_id in starred_ids,
            dismissed=listing_id in dismissed_ids,
            breakdown=breakdowns_by_id.get(listing_id),
            area_labels=area_labels,
        )
        if _passes_filter(row, filters):
            prepared.append(row)

    return _apply_sort(prepared, filters.sort, limit=limit)


def _listing_from_record(
    source_obj: Source,
    record: SourceRecord,
    listing_id: str,
    context: SearchContext,
) -> Listing:
    listing = source_obj.to_listing(record, context)
    # Re-key to the canonical listing_id (post cross-source dedupe).
    if listing.identity.listing_id != listing_id:
        listing = listing.model_copy(
            update={"identity": listing.identity.model_copy(update={"listing_id": listing_id})}
        )
    return listing


def _action_listing_ids(
    conn: sqlite3.Connection, *, kind: str, listing_ids: tuple[str, ...]
) -> set[str]:
    """Return the subset of listing_ids carrying a flag action of ``kind``."""

    if not listing_ids:
        return set()
    placeholders = ",".join("?" for _ in listing_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT listing_id FROM listing_action
        WHERE kind = ? AND listing_id IN ({placeholders})
        """,
        (kind, *listing_ids),
    ).fetchall()
    return {str(row["listing_id"]) for row in rows}


def _breakdowns_by_listing(
    conn: sqlite3.Connection,
    *,
    listing_ids: tuple[str, ...],
    profile_id: str,
) -> dict[str, Mapping[str, object]]:
    """Pre-fetch the score breakdown JSON for each listing in one pass."""

    if not listing_ids:
        return {}
    placeholders = ",".join("?" for _ in listing_ids)
    rows = conn.execute(
        f"""
        SELECT listing_id, breakdown_json
        FROM score
        WHERE profile_id = ? AND listing_id IN ({placeholders})
        """,
        (profile_id, *listing_ids),
    ).fetchall()
    out: dict[str, Mapping[str, object]] = {}
    for row in rows:
        parsed = _parse_breakdown(row["breakdown_json"])
        if parsed is not None:
            out[str(row["listing_id"])] = parsed
    return out


def _parse_breakdown(raw: object) -> Mapping[str, object] | None:
    if isinstance(raw, Mapping):
        return raw
    try:
        loaded = json.loads(str(raw))
    except (ValueError, TypeError):
        return None
    return loaded if isinstance(loaded, Mapping) else None


@dataclass(frozen=True, slots=True)
class _SourceRecordRow:
    listing_id: str
    source: str
    record: SourceRecord


def _latest_source_records_by_listing_ids(
    conn: sqlite3.Connection,
    *,
    listing_ids: tuple[str, ...],
) -> dict[str, _SourceRecordRow]:
    if not listing_ids:
        return {}
    placeholders = ",".join("?" for _ in listing_ids)
    rows = conn.execute(
        f"""
        SELECT
            sr.listing_id AS listing_id,
            sr.source AS source,
            sr.source_id AS source_id,
            sr.url AS url,
            sr.payload AS payload,
            sr.content_hash AS content_hash,
            sr.fetched_at AS fetched_at
        FROM source_record sr
        INNER JOIN (
            SELECT listing_id, MAX(id) AS latest_id
            FROM source_record
            WHERE listing_id IN ({placeholders})
            GROUP BY listing_id
        ) latest
        ON latest.latest_id = sr.id
        ORDER BY sr.listing_id ASC
        """,
        listing_ids,
    ).fetchall()

    records: dict[str, _SourceRecordRow] = {}
    for row in rows:
        payload = json.loads(str(row["payload"]))
        if not isinstance(payload, Mapping):
            continue
        fetched_at = datetime.fromisoformat(str(row["fetched_at"]))
        source_record = SourceRecord(
            source=str(row["source"]),
            source_id=str(row["source_id"]),
            url=str(row["url"]),
            payload=dict(payload),
            content_hash=str(row["content_hash"]),
            fetched_at=fetched_at,
        )
        listing_id = str(row["listing_id"])
        records[listing_id] = _SourceRecordRow(
            listing_id=listing_id,
            source=str(row["source"]),
            record=source_record,
        )
    return records


def _build_list_row(
    *,
    listing_id: str,
    listing: Listing,
    score: float,
    record: SourceRecord,
    context: SearchContext | None = None,
    excluded: bool = False,
    starred: bool = False,
    dismissed: bool = False,
    breakdown: Mapping[str, object] | None = None,
    area_labels: Mapping[str, str] | None = None,
) -> ListRow:
    title = _listing_title(listing)
    rent_text = rent_display(listing)
    rent_value = _rent_amount(listing)
    beds = _observed_scalar(listing.beds)
    baths = _observed_scalar(listing.baths)
    area_value, area_unit = _area_values(listing)
    address = listing.place.raw_address
    area_key = listing_area_key(listing)
    area_label = (area_labels or {}).get(area_key or "", area_key)
    photos = tuple(listing.photos)
    floor_text = _field_text(listing.floor)
    furnished = _field_text(listing.furnishing)
    parking = _field_text(listing.parking)
    available = _attribute_text(listing, "available") or _attribute_text(listing, "avail")
    category_scores = _category_scores_from_breakdown(breakdown)
    if context is not None:
        classified = classify_match_status(listing, context.profile)
    else:
        classified = MatchStatus(status="unverified", reasons=("no profile loaded",))
    match_status = "excluded" if excluded else classified.status
    return ListRow(
        listing_id=listing_id,
        title=title,
        source=str(listing.identity.source),
        url=str(listing.identity.url),
        rent_text=rent_text,
        rent_value=rent_value,
        beds=beds,
        baths=baths,
        area_value=area_value,
        area_unit=area_unit,
        address=address,
        area_key=area_key,
        area_label=area_label,
        score=score,
        posted_at=record.fetched_at,
        first_seen=None,
        photos=photos,
        listing=listing,
        floor_text=floor_text,
        furnished=furnished,
        parking=parking,
        available=available,
        match_status=match_status,
        match_reasons=classified.reasons,
        starred=starred,
        dismissed=dismissed,
        category_scores=category_scores,
    )


def _attribute_text(listing: Listing, key: str) -> str | None:
    """Read a text attribute from listing.attributes if present and well-formed."""

    attr = listing.attributes.get(key)
    if not isinstance(attr, Observed):
        return None
    value = attr.value
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return str(value).strip() or None


def _field_text(field: object) -> str | None:
    """Read a typed Listing field and return its value as display text."""

    if not isinstance(field, Observed):
        return None
    value = field.value
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int | float):
        return f"{value:g}"
    return None


def _field_int(field: object) -> int | None:
    """Read a typed Listing field (Observed[int] | Absence) and return its int value."""

    if (
        isinstance(field, Observed)
        and isinstance(field.value, int)
        and not isinstance(field.value, bool)
    ):
        return field.value
    return None


# ---------------------------------------------------------------------------
# Hard-filter classification
# ---------------------------------------------------------------------------


def _fmt_money(value: float) -> str:
    return f"${value:,.0f}"


def _fmt_num(value: float) -> str:
    return f"{value:g}"


def _check_numeric(
    *,
    name: str,
    value: float | None,
    eq: float | None,
    minimum: float | None,
    maximum: float | None,
    unstated_is_miss: bool,
    misses: list[str],
    unknowns: list[str],
) -> None:
    """Append a reason to ``misses`` or ``unknowns`` for one numeric hard filter."""

    if value is None:
        (misses if unstated_is_miss else unknowns).append(f"{name} unstated")
        return
    if eq is not None and value != eq:
        misses.append(f"{name} {_fmt_num(value)} ≠ {_fmt_num(eq)}")
        return
    if minimum is not None and value < minimum:
        misses.append(f"{name} {_fmt_num(value)} < min {_fmt_num(minimum)}")
    if maximum is not None and value > maximum:
        misses.append(f"{name} {_fmt_num(value)} > max {_fmt_num(maximum)}")


def classify_match_status(listing: Listing, profile: Profile) -> MatchStatus:
    """Compare the listing against every hard filter and explain the verdict.

    - ``miss``       — at least one stated criterion fails (reasons list each)
    - ``unverified`` — nothing fails, but a criterion-relevant field is
                       unstated (reasons list what is missing)
    - ``match``      — every criterion passes with the data available

    ``miss`` wins over ``unverified``; the reasons tuple carries the misses
    first, then the unknowns, so the tooltip leads with the decisive facts.
    """

    hard = profile.hard
    misses: list[str] = []
    unknowns: list[str] = []

    if hard.rent is not None:
        rent_value = _rent_amount(listing)
        if rent_value is None:
            unknowns.append("rent unstated")
        else:
            if rent_value > hard.rent.max:
                misses.append(f"rent {_fmt_money(rent_value)} > max {_fmt_money(hard.rent.max)}")
            if hard.rent.min is not None and rent_value < hard.rent.min:
                misses.append(f"rent {_fmt_money(rent_value)} < min {_fmt_money(hard.rent.min)}")

    if hard.beds is not None:
        _check_numeric(
            name="beds",
            value=_observed_scalar(listing.beds),
            eq=hard.beds.eq,
            minimum=hard.beds.min,
            maximum=hard.beds.max,
            unstated_is_miss=False,
            misses=misses,
            unknowns=unknowns,
        )

    if hard.baths is not None:
        _check_numeric(
            name="baths",
            value=_observed_scalar(listing.baths),
            eq=hard.baths.eq,
            minimum=hard.baths.min,
            maximum=hard.baths.max,
            unstated_is_miss=False,
            misses=misses,
            unknowns=unknowns,
        )

    if hard.area is not None:
        area_value, area_unit = _area_values(listing)
        if area_value is None:
            unknowns.append("area unstated")
        elif area_unit is not None and area_unit.lower() != hard.area.unit.lower():
            unknowns.append(f"area in {area_unit}, profile uses {hard.area.unit}")
        elif area_value < hard.area.min:
            misses.append(
                f"area {area_value:,.0f} < min {hard.area.min:,.0f} {hard.area.unit}"
            )

    if hard.floor is not None:
        _check_numeric(
            name="floor",
            value=_observed_scalar(listing.floor),
            eq=hard.floor.eq,
            minimum=hard.floor.min,
            maximum=hard.floor.max,
            unstated_is_miss=False,
            misses=misses,
            unknowns=unknowns,
        )

    if hard.areas:
        area_key = listing_area_key(listing)
        if area_key is None:
            unknowns.append("area unknown")
        elif area_key not in set(hard.areas):
            misses.append("area not in allowed list")

    excludes = {token.strip().lower() for token in hard.exclude}
    if "basement" in excludes and _is_basement_listing(listing):
        misses.append("basement unit")
    if "furnished_only" in excludes and _is_furnished(listing):
        misses.append("furnished only")

    if misses:
        return MatchStatus(status="miss", reasons=tuple(misses + unknowns))
    if unknowns:
        return MatchStatus(status="unverified", reasons=tuple(unknowns))
    return MatchStatus(status="match", reasons=())


def _classify_match_status(listing: Listing, context: SearchContext) -> str:
    """Backwards-compatible shim: status string only."""

    return classify_match_status(listing, context.profile).status


# ---------------------------------------------------------------------------
# Score breakdown helpers
# ---------------------------------------------------------------------------


def _category_scores_from_breakdown(
    breakdown: Mapping[str, object] | None,
) -> tuple[dict[str, float | str], ...]:
    """Aggregate a stored breakdown into one row per category for bar charts.

    Each row: ``{"category": str, "label": str, "score": float, "max": float,
    "pct": float}``. Sorted by absolute score desc so the most-impactful
    categories lead.
    """

    if not isinstance(breakdown, Mapping):
        return ()
    contributions = breakdown.get("contributions")
    if not isinstance(contributions, list):
        return ()

    by_category: dict[str, dict[str, float]] = {}
    for entry in contributions:
        if not isinstance(entry, Mapping):
            continue
        category = entry.get("category")
        score = entry.get("contribution")
        max_possible = entry.get("max_possible")
        if not isinstance(category, str) or not isinstance(score, int | float):
            continue
        bucket = by_category.setdefault(category, {"score": 0.0, "max": 0.0})
        bucket["score"] += float(score)
        if isinstance(max_possible, int | float):
            bucket["max"] += float(max_possible)

    rows: list[dict[str, float | str]] = []
    for category, totals in by_category.items():
        score = float(totals["score"])
        max_possible = float(totals["max"])
        pct = (score / max_possible * 100.0) if max_possible > 0 else 0.0
        rows.append(
            {
                "category": category,
                "label": category_label(category),
                "score": score,
                "max": max_possible,
                "pct": max(0.0, min(100.0, pct)),
            }
        )
    rows.sort(key=lambda r: abs(float(r["score"])), reverse=True)
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class RuleRow:
    """One rule's line in the detail-page breakdown table."""

    rule_key: str
    label: str
    category: str
    category_label: str
    evidence: str | None
    contribution: float
    weight_text: str
    fired: bool

    @property
    def contribution_text(self) -> str:
        return f"{self.contribution:+.1f}"


def _weight_text(weight: object) -> str:
    if isinstance(weight, bool):
        return str(weight)
    if isinstance(weight, int | float):
        return f"{float(weight):+g}"
    if isinstance(weight, Mapping):
        cap = weight.get("cap")
        rate = weight.get("per_100_sqft")
        rate_label = "per 100 sqft"
        if rate is None:
            rate = weight.get("per_100")
            rate_label = "per 100"
        bits: list[str] = []
        if isinstance(rate, int | float):
            bits.append(f"{float(rate):+g} {rate_label}")
        if isinstance(cap, int | float):
            bits.append(f"cap {float(cap):g}")
        return ", ".join(bits) or "scaled"
    return "—"


def rule_rows_from_breakdown(breakdown: Mapping[str, object] | None) -> tuple[RuleRow, ...]:
    """Flatten a stored breakdown into per-rule rows sorted by |contribution|."""

    if not isinstance(breakdown, Mapping):
        return ()
    contributions = breakdown.get("contributions")
    if not isinstance(contributions, list):
        return ()
    rows: list[RuleRow] = []
    for entry in contributions:
        if not isinstance(entry, Mapping):
            continue
        rule_key = str(entry.get("rule_key") or "")
        label = str(entry.get("label") or rule_key or "rule")
        category = str(entry.get("category") or "")
        contribution_raw = entry.get("contribution")
        contribution = (
            float(contribution_raw) if isinstance(contribution_raw, int | float) else 0.0
        )
        signal = entry.get("signal")
        evidence: str | None = None
        fired = False
        if isinstance(signal, Mapping):
            raw_evidence = signal.get("evidence")
            if isinstance(raw_evidence, str) and raw_evidence.strip():
                evidence = raw_evidence.strip()
            fired = bool(signal.get("fired", False))
        rows.append(
            RuleRow(
                rule_key=rule_key,
                label=label,
                category=category,
                category_label=category_label(category),
                evidence=evidence,
                contribution=contribution,
                weight_text=_weight_text(entry.get("weight")),
                fired=fired or contribution != 0.0,
            )
        )
    rows.sort(key=lambda r: abs(r.contribution), reverse=True)
    return tuple(rows)


def _listing_title(listing: Listing) -> str:
    attr = listing.attributes.get("title")
    if isinstance(attr, Observed) and isinstance(attr.value, str):
        text = attr.value.strip()
        if text:
            return text
    return listing.identity.listing_id


def _rent_amount(listing: Listing) -> float | None:
    field = listing.rent
    if isinstance(field, Observed) and isinstance(field.value, Money):
        return float(field.value.amount)
    return None


def _observed_scalar(field: object) -> float | None:
    if isinstance(field, Observed):
        value = field.value
        if isinstance(value, bool | int | float):
            return float(value)
    return None


def _category_scores_from_listing(
    listing: Listing,
) -> tuple[dict[str, float | str], ...]:
    """Aggregate directly from a Listing's per-rule observations (no DB needed).

    Used by the profile page to show how each rule category is contributing,
    without needing the score table populated yet. Falls back to the same
    shape as :func:`_category_scores_from_breakdown` so the template can
    render either.
    """

    rows: list[dict[str, float | str]] = []
    # listing.attributes carries per-rule Observed values from the parser.
    for key, attr in listing.attributes.items():
        category = key.split(".", 1)[0] if "." in key else key
        if not isinstance(attr, Observed):
            continue
        confidence = float(getattr(attr, "confidence", 0.0) or 0.0)
        rows.append(
            {
                "category": category,
                "label": category_label(category),
                "score": confidence,
                "max": 1.0,
                "pct": confidence * 100.0,
            }
        )
    rows.sort(key=lambda r: abs(float(r["score"])), reverse=True)
    return tuple(rows)


def _area_values(listing: Listing) -> tuple[float | None, str | None]:
    field = listing.area
    if isinstance(field, Observed) and isinstance(field.value, Area):
        return float(field.value.value), str(field.value.unit)
    return None, None


def _passes_filter(row: ListRow, filters: ListFilter) -> bool:
    if filters.rent_min is not None and row.rent_value is not None:
        if row.rent_value < filters.rent_min:
            return False
    if filters.rent_max is not None and row.rent_value is not None:
        if row.rent_value > filters.rent_max:
            return False
    if filters.beds is not None and row.beds is not None:
        if row.beds < filters.beds:
            return False
    if filters.baths_min is not None and row.baths is not None:
        if row.baths < filters.baths_min:
            return False
    if filters.area_min is not None and row.area_value is not None:
        if row.area_value < filters.area_min:
            return False
    if filters.score_min is not None:
        if row.score < filters.score_min:
            return False
    if filters.source is not None and row.source != filters.source:
        return False
    if filters.area_name is not None and row.area_key != filters.area_name:
        return False
    if filters.starred and not row.starred:
        return False
    if filters.hide_dismissed and row.dismissed:
        return False
    if filters.status is not None and row.match_status != filters.status:
        return False
    return True


def _posted_ts(row: ListRow) -> float | None:
    return row.posted_at.timestamp() if row.posted_at is not None else None


_SORT_KEYS = {
    "score": lambda row: (-row.score, row.listing_id),
    "rent_asc": lambda row: (
        row.rent_value if row.rent_value is not None else float("inf"),
        row.listing_id,
    ),
    "rent_desc": lambda row: (
        -row.rent_value if row.rent_value is not None else float("inf"),
        row.listing_id,
    ),
    "area_desc": lambda row: (
        -row.area_value if row.area_value is not None else float("inf"),
        row.listing_id,
    ),
    "posted_desc": lambda row: (
        -(_posted_ts(row) or 0.0) if _posted_ts(row) is not None else float("inf"),
        row.listing_id,
    ),
    "posted_asc": lambda row: (
        _posted_ts(row) if _posted_ts(row) is not None else float("inf"),
        row.listing_id,
    ),
    "address": lambda row: ((row.address or "").lower(), row.listing_id),
}


def _apply_sort(rows: list[ListRow], sort: str, *, limit: int | None) -> list[ListRow]:
    key = _SORT_KEYS[normalize_sort(sort)]
    ordered = sorted(rows, key=key)
    if limit is not None:
        return ordered[:limit]
    return ordered


def load_detail(
    conn: sqlite3.Connection,
    *,
    listing_id: str,
    context: SearchContext,
    profile_id: str,
    sources: Mapping[str, Source],
) -> ListRow | None:
    """Load a single listing row for the detail view."""

    latest_records = _latest_source_records_by_listing_ids(conn, listing_ids=(listing_id,))
    record_row = latest_records.get(listing_id)
    if record_row is None:
        return None
    source_obj = sources.get(record_row.source)
    if source_obj is None:
        return None
    listing = _listing_from_record(source_obj, record_row.record, listing_id, context)

    score_row = conn.execute(
        """
        SELECT score, breakdown_json FROM score WHERE listing_id = ? AND profile_id = ?
        """,
        (listing_id, profile_id),
    ).fetchone()
    score_value = float(score_row["score"]) if score_row is not None else 0.0
    breakdown = _parse_breakdown(score_row["breakdown_json"]) if score_row is not None else None

    ids = (listing_id,)
    return _build_list_row(
        listing_id=listing_id,
        listing=listing,
        score=score_value,
        record=record_row.record,
        context=context,
        excluded=listing_id in _action_listing_ids(conn, kind="excluded", listing_ids=ids),
        starred=listing_id in _action_listing_ids(conn, kind="star", listing_ids=ids),
        dismissed=listing_id in _action_listing_ids(conn, kind="dismiss", listing_ids=ids),
        breakdown=breakdown,
        area_labels=dict(known_areas(context)),
    )


def known_areas(context: SearchContext) -> tuple[tuple[str, str], ...]:
    """Return (area_key, area_label) pairs for the filter chips."""

    pairs: list[tuple[str, str]] = []
    for area in context.citypack.areas:
        pairs.append((str(area.key), str(area.label)))
    return tuple(pairs)


def known_sources(sources: Iterable[Source]) -> tuple[str, ...]:
    return tuple(sorted(source.name for source in sources))


def to_jsonable(row: ListRow) -> dict[str, JSONValue]:
    """Render a ListRow to a JSON-serializable dict for the static export."""

    posted_iso = row.posted_at.astimezone(UTC).isoformat() if row.posted_at else None
    return {
        "listing_id": row.listing_id,
        "title": row.title,
        "source": row.source,
        "url": row.url,
        "rent_text": row.rent_text,
        "rent_value": float(row.rent_value) if row.rent_value is not None else None,
        "beds": float(row.beds) if row.beds is not None else None,
        "baths": float(row.baths) if row.baths is not None else None,
        "area_value": float(row.area_value) if row.area_value is not None else None,
        "area_unit": row.area_unit,
        "address": row.address,
        "area_key": row.area_key,
        "area_label": row.area_label,
        "score": float(row.score),
        "posted_at": posted_iso,
        "primary_photo": row.primary_photo.url if row.primary_photo else None,
        "match_status": row.match_status,
        "match_reasons": list(row.match_reasons),
    }


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
