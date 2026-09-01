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

    @property
    def primary_photo(self) -> Photo | None:
        return self.photos[0] if self.photos else None


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
        )
        if _passes_filter(row, filters):
            prepared.append(row)

    return _apply_sort(prepared, filters.sort, limit=limit)


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
    )


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
        SELECT score FROM score WHERE listing_id = ? AND profile_id = ?
        """,
        (listing_id, profile_id),
    ).fetchone()
    score_value = float(score_row["score"]) if score_row is not None else 0.0

    return _build_list_row(
        listing_id=listing_id,
        listing=enriched,
        score=score_value,
        record=record_row.record,
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
