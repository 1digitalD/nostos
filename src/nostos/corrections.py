"""Apply user-confirmed listing facts on top of replayed source records."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from nostos.model import Area, Listing, Money, Observed, Origin


def apply_user_corrections(
    conn: sqlite3.Connection,
    *,
    listing_id: str,
    listing: Listing,
    rows: Iterable[Mapping[str, object]] | None = None,
) -> Listing:
    if rows is None:
        rows = conn.execute(
            """
            WITH ranked AS (
                SELECT field,value_json,observed_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY field ORDER BY observed_at DESC,id DESC
                       ) AS rn
                FROM observation
                WHERE listing_id=? AND origin=?
            )
            SELECT field,value_json,observed_at FROM ranked WHERE rn=1
            """,
            (listing_id, Origin.USER.value),
        ).fetchall()
    rows = tuple(rows)
    if not rows:
        return listing

    updates: dict[str, Any] = {}
    attributes = dict(listing.attributes)
    for row in rows:
        field = str(row["field"])
        raw = json.loads(str(row["value_json"]))
        observed_at = datetime.fromisoformat(str(row["observed_at"]))
        if field == "rent":
            value: Any = Money.model_validate(raw)
        elif field == "area":
            value = Area.model_validate(raw)
        elif field in {"beds", "baths"}:
            value = float(raw)
        elif field == "floor":
            value = int(raw)
        else:
            key = field.removeprefix("attributes.")
            attributes[key] = Observed(
                value=raw,
                origin=Origin.USER,
                confidence=1.0,
                evidence="user correction",
                observed_at=observed_at,
            )
            continue
        updates[field] = Observed(
            value=value,
            origin=Origin.USER,
            confidence=1.0,
            evidence="user correction",
            observed_at=observed_at,
        )
    updates["attributes"] = attributes
    return listing.model_copy(update=updates)


def load_user_corrections(
    conn: sqlite3.Connection, *, listing_ids: tuple[str, ...]
) -> dict[str, tuple[Mapping[str, object], ...]]:
    """Load the latest user correction for every field in one query."""

    if not listing_ids:
        return {}
    placeholders = ",".join("?" for _ in listing_ids)
    rows = conn.execute(
        f"""
        WITH ranked AS (
            SELECT listing_id,field,value_json,observed_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY listing_id,field ORDER BY observed_at DESC,id DESC
                   ) AS rn
            FROM observation
            WHERE origin=? AND listing_id IN ({placeholders})
        )
        SELECT listing_id,field,value_json,observed_at FROM ranked WHERE rn=1
        """,
        (Origin.USER.value, *listing_ids),
    ).fetchall()
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        grouped.setdefault(str(row["listing_id"]), []).append(row)
    return {listing_id: tuple(values) for listing_id, values in grouped.items()}


def apply_geo_observations(
    conn: sqlite3.Connection, *, listing_id: str, listing: Listing
) -> Listing:
    """Apply the latest provider-derived attributes used by ranking rules."""

    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT field,value_json,confidence,evidence,observed_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY field ORDER BY observed_at DESC,id DESC
                   ) AS rn
            FROM observation
            WHERE listing_id=? AND origin=?
        )
        SELECT field,value_json,confidence,evidence,observed_at FROM ranked WHERE rn=1
        """,
        (listing_id, Origin.GEO_PROVIDER.value),
    ).fetchall()
    if not rows:
        return listing
    attributes = dict(listing.attributes)
    for row in rows:
        field = str(row["field"])
        if not field.startswith("attributes."):
            continue
        attributes[field.removeprefix("attributes.")] = Observed(
            value=json.loads(str(row["value_json"])),
            origin=Origin.GEO_PROVIDER,
            confidence=float(row["confidence"]),
            evidence=str(row["evidence"] or "map provider"),
            observed_at=datetime.fromisoformat(str(row["observed_at"])),
        )
    return listing.model_copy(update={"attributes": attributes})


__all__ = ["apply_geo_observations", "apply_user_corrections", "load_user_corrections"]
