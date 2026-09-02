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

from nostos.context import SearchContext
from nostos.model import Area, Listing, Money, Observed, Photo, SourceRecord
from nostos.model.source_record import JSONValue
from nostos.rank.profile_scoring import prepare_listing_for_profile, rent_display
from nostos.sources.base import Source


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
    sort: str = "score"


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
    # Computed in app.py from profile + listing. One of:
    # "match"      — listing meets hard criteria
    # "unverified" — listing is missing data needed to score
    # "excluded"   — user has explicitly excluded this listing
    # "dead"       — listing is no longer available (reserved for liveness tracking)
    match_status: str = "match"
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
    """Return scored listings joined with the latest source record, after filter."""

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
    excluded_ids = _excluded_listing_ids(conn, listing_ids=listing_ids)
    breakdowns_by_id = _breakdowns_by_listing(conn, listing_ids=listing_ids, profile_id=profile_id)

    prepared: list[ListRow] = []
    for listing_id in listing_ids:
        record_row = latest_records.get(listing_id)
        if record_row is None:
            continue
        source_obj = sources.get(record_row.source)
        if source_obj is None:
            continue
        listing = source_obj.to_listing(record_row.record, context)
        # Re-key to the canonical listing_id (post cross-source dedupe).
        if listing.identity.listing_id != listing_id:
            listing = listing.model_copy(
                update={
                    "identity": listing.identity.model_copy(
                        update={"listing_id": listing_id}
                    )
                }
            )

        enriched = prepare_listing_for_profile(
            listing,
            context=context,
            enrichers=(),
        )
        if enriched is None:
            continue
        row = _build_list_row(
            listing_id=listing_id,
            listing=enriched,
            score=score_by_id[listing_id],
            record=record_row.record,
            context=context,
            excluded=listing_id in excluded_ids,
            breakdown=breakdowns_by_id.get(listing_id),
        )
        if _passes_filter(row, filters):
            prepared.append(row)

    return _apply_sort(prepared, filters.sort, limit=limit)


def _excluded_listing_ids(
    conn: sqlite3.Connection, *, listing_ids: tuple[str, ...]
) -> set[str]:
    """Return the subset of listing_ids that the user has marked excluded."""

    if not listing_ids:
        return set()
    placeholders = ",".join("?" for _ in listing_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT listing_id FROM listing_action
        WHERE kind = 'excluded' AND listing_id IN ({placeholders})
        """,
        listing_ids,
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
    import json as _json
    for row in rows:
        raw = row["breakdown_json"]
        try:
            out[str(row["listing_id"])] = _json.loads(str(raw))
        except (ValueError, TypeError):
            continue
    return out


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
    breakdown: Mapping[str, object] | None = None,
) -> ListRow:
    title = _listing_title(listing)
    rent_text = rent_display(listing)
    rent_value = _rent_amount(listing)
    beds = _observed_scalar(listing.beds)
    baths = _observed_scalar(listing.baths)
    area_value, area_unit = _area_values(listing)
    address = listing.place.raw_address
    area_key = listing.place.area_key
    photos = tuple(listing.photos)
    floor_text = _field_text(listing.floor)
    furnished = _field_text(listing.furnishing)
    parking = _field_text(listing.parking)
    available = _attribute_text(listing, "available") or _attribute_text(listing, "avail")
    category_scores = _category_scores_from_breakdown(breakdown)
    # Compute Match vs Unverified against the profile's hard criteria.
    # match_status is one of:
    #   "excluded"   — user marked excluded via the web UI
    #   "match"      — listing meets all stated profile criteria
    #   "unverified" — at least one criterion-relevant field is unstated
    #   "miss"       — listing fails at least one criterion (we still
    #                  show it, just flagged, so the user can see why the
    #                  score is low)
    match_status = (
        "excluded"
        if excluded
        else (
            _classify_match_status(listing, context)
            if context is not None
            else "unverified"
        )
    )
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
        area_label=area_key,
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
    """Read a typed Listing field (Observed[str] | Absence) and return its string value."""

    if isinstance(field, Observed) and isinstance(field.value, str):
        text = field.value.strip()
        return text or None
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


def _classify_match_status(listing: Listing, context: SearchContext) -> str:
    """Compare the listing against the profile's hard filters and label the result.

    Returns:
      "match"      — every stated criterion passes, and no criterion-relevant
                     field is unstated (we had enough info to evaluate them all)
      "unverified" — at least one criterion-relevant field is unstated
      "miss"       — at least one stated criterion is failed

    The status is informational only; the index query already filters out
    listings that fail any active URL filter via `_passes_filter`.
    """

    profile = context.profile
    hard = profile.hard

    # rent.max
    if hard.rent is not None and hard.rent.max is not None:
        rent_field = listing.rent
        if not isinstance(rent_field, Observed) or not isinstance(rent_field.value, Money):
            return "unverified"
        if float(rent_field.value.amount) > float(hard.rent.max):
            return "miss"

    # beds.min / beds.eq
    if hard.beds is not None:
        target = hard.beds.eq if hard.beds.eq is not None else hard.beds.min
    if target is not None:
        beds_field = listing.beds
        if not isinstance(beds_field, Observed) or not isinstance(
            beds_field.value, (int, float)
        ):
            return "unverified"
            if hard.beds.eq is not None and float(beds_field.value) != float(target):
                return "miss"
            if hard.beds.min is not None and float(beds_field.value) < float(target):
                return "miss"

    # baths.min
    if hard.baths is not None and hard.baths.min is not None:
        baths_field = listing.baths
        if not isinstance(baths_field, Observed) or not isinstance(baths_field.value, (int, float)):
            return "unverified"
        if float(baths_field.value) < float(hard.baths.min):
            return "miss"

    # area.min
    if hard.area is not None and hard.area.min is not None:
        area_field = listing.area
        if not isinstance(area_field, Observed) or not isinstance(area_field.value, Area):
            return "unverified"
        if float(area_field.value.value) < float(hard.area.min):
            return "miss"

    return "match"


def _category_scores_from_breakdown(
    breakdown: Mapping[str, object] | None,
) -> tuple[dict[str, float | str], ...]:
    """Aggregate a stored breakdown into one row per category for bar charts.

    Each row: ``{"category": str, "score": float, "max": float, "pct": float}``
    Sorted by absolute score desc so the most-impactful categories lead.
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
        if not isinstance(category, str) or not isinstance(score, (int, float)):
            continue
        bucket = by_category.setdefault(
            category, {"score": 0.0, "max": 0.0}
        )
        bucket["score"] += float(score)
        if isinstance(max_possible, (int, float)):
            bucket["max"] += float(max_possible)

    rows: list[dict[str, float | str]] = []
    for category, totals in by_category.items():
        score = float(totals["score"])
        max_possible = float(totals["max"])
        pct = (score / max_possible * 100.0) if max_possible > 0 else 0.0
        rows.append(
            {
                "category": category,
                "score": score,
                "max": max_possible,
                "pct": pct,
            }
        )
    rows.sort(key=lambda r: abs(float(r["score"])), reverse=True)
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
    return True


_SORT_KEYS = {
    "score": lambda row: (-row.score, row.listing_id),
    "rent": lambda row: (
        row.rent_value if row.rent_value is not None else float("inf"),
        row.listing_id,
    ),
    "posted": lambda row: (
        -(row.posted_at.timestamp()) if row.posted_at is not None else float("inf"),
        row.listing_id,
    ),
    "address": lambda row: ((row.address or "").lower(), row.listing_id),
}


def _apply_sort(
    rows: list[ListRow], sort: str, *, limit: int | None
) -> list[ListRow]:
    key = _SORT_KEYS.get(sort, _SORT_KEYS["score"])
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

    latest_records = _latest_source_records_by_listing_ids(
        conn, listing_ids=(listing_id,)
    )
    record_row = latest_records.get(listing_id)
    if record_row is None:
        return None
    source_obj = sources.get(record_row.source)
    if source_obj is None:
        return None
    listing = source_obj.to_listing(record_row.record, context)
    if listing.identity.listing_id != listing_id:
        listing = listing.model_copy(
            update={
                "identity": listing.identity.model_copy(
                    update={"listing_id": listing_id}
                )
            }
        )

    enriched = prepare_listing_for_profile(
        listing,
        context=context,
        enrichers=(),
    )
    if enriched is None:
        return None

    score_row = conn.execute(
        """
        SELECT score, breakdown_json FROM score WHERE listing_id = ? AND profile_id = ?
        """,
        (listing_id, profile_id),
    ).fetchone()
    score_value = float(score_row["score"]) if score_row is not None else 0.0
    breakdown: Mapping[str, object] | None = None
    if score_row is not None:
        try:
            import json as _json
            breakdown = _json.loads(str(score_row["breakdown_json"]))
        except (ValueError, TypeError):
            breakdown = None

    excluded_ids = _excluded_listing_ids(conn, listing_ids=(listing_id,))
    return _build_list_row(
        listing_id=listing_id,
        listing=enriched,
        score=score_value,
        record=record_row.record,
        context=context,
        excluded=listing_id in excluded_ids,
        breakdown=breakdown,
    )


def known_areas(context: SearchContext) -> tuple[tuple[str, str], ...]:
    """Return (area_key, area_label) pairs for the filter dropdown."""

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
        "score": float(row.score),
        "posted_at": posted_iso,
        "primary_photo": row.primary_photo.url if row.primary_photo else None,
    }


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
