"""Revision-safe profile editing and apartment tracking shared by web, CLI and MCP."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nostos.config.profile import Profile, load_profile
from nostos.config.wizard import dump_profile_yaml
from nostos.context import SearchContext
from nostos.enrich.chain import run_enricher_chain
from nostos.enrich.text import TextRuleEnricher
from nostos.model import JSONValue, Origin, SourceRecord
from nostos.rank.criteria import classify_match_status
from nostos.rank.engine import RankEngine
from nostos.rank.profile_scoring import listing_title, passes_hard_filters
from nostos.rank.rescore import latest_source_records, rescore_profile
from nostos.sources.base import Source
from nostos.store.repo import ListingRepo, ObservationRepo


def revision(profile: Profile) -> str:
    return hashlib.sha256(profile.model_dump_json().encode()).hexdigest()


def merge_patch(current: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(current)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_patch(result[key], value)
        else:
            result[key] = value
    return result


def candidate(profile: Profile, patch: Mapping[str, Any]) -> Profile:
    proposed = Profile.model_validate(merge_patch(profile.model_dump(mode="json"), patch))
    if proposed.city != profile.city:
        raise ValueError("Use a separate profile and database to change city.")
    # Validate enabled keys even when there are no listings to score.
    from nostos.rank.rules import DEFAULT_REGISTRY
    for key, weight in proposed.weights.items():
        if weight and DEFAULT_REGISTRY.get(key) is None:
            raise ValueError(f"Unsupported ranking rule: {key}")
    return proposed


def preview_profile(conn: sqlite3.Connection, *, context: SearchContext,
                    proposed: Profile, sources: Mapping[str, Source]) -> dict[str, Any]:
    def evaluate(profile: Profile) -> dict[str, Any]:
        ctx = SearchContext(citypack=context.citypack, profile=profile)
        engine = RankEngine(profile)
        counts = {"match": 0, "unverified": 0, "miss": 0, "unreadable": 0}
        rows: list[dict[str, Any]] = []
        for row in records:
            source = sources.get(row.record.source)
            if source is None:
                counts["unreadable"] += 1
                continue
            listing = run_enricher_chain(source.to_listing(row.record, ctx),
                                        (TextRuleEnricher(),), ctx)
            result = classify_match_status(listing, profile)
            counts[result.status] += 1
            rows.append({"id": row.listing_id, "title": listing_title(listing),
                         "status": result.status, "reasons": result.reasons,
                         "eligible": passes_hard_filters(listing, profile),
                         "score": engine.score_listing(listing, context=ctx).score})
        rows.sort(key=lambda row: (-float(row["score"]), str(row["id"])))
        return {"counts": counts, "rows": rows}

    records = latest_source_records(conn)
    before, after = evaluate(context.profile), evaluate(proposed)
    before_ids = {r["id"] for r in before["rows"] if r["eligible"]}
    after_ids = {r["id"] for r in after["rows"] if r["eligible"]}
    return {"revision": revision(context.profile), "proposed_revision": revision(proposed),
            "before": before, "after": after,
            "entrants": sorted(after_ids - before_ids), "exits": sorted(before_ids - after_ids)}


@contextmanager
def profile_lock(path: Path) -> Iterator[None]:
    with path.with_suffix(path.suffix + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def atomic_write(path: Path, data: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def apply_profile(conn: sqlite3.Connection, *, path: Path, context: SearchContext,
                  patch: Mapping[str, Any], expected_revision: str,
                  sources: Mapping[str, Source], replace: bool = False) -> dict[str, Any]:
    with profile_lock(path):
        current = load_profile(path)
        if revision(current) != expected_revision:
            raise ValueError("Criteria changed since this draft. Reload and preview again.")
        proposed = Profile.model_validate(patch) if replace else candidate(current, patch)
        if proposed.city != current.city:
            raise ValueError("Use a separate profile and database to change city.")
        ctx = SearchContext(citypack=context.citypack, profile=current)
        preview = preview_profile(conn, context=ctx, proposed=proposed, sources=sources)
        original = path.read_bytes()
        try:
            # Scores commit before activation; restore old scores if the file write fails.
            report = rescore_profile(conn, context=SearchContext(
                citypack=context.citypack, profile=proposed), profile_id=path.stem, sources=sources)
            atomic_write(path, dump_profile_yaml(proposed.model_dump(
                mode="json", exclude_none=True)).encode())
            with conn:
                for item in (current, proposed):
                    conn.execute("INSERT INTO profile_revision(profile_path,revision,payload,"
                                 "created_at) VALUES (?,?,?,?)",
                                 (str(path.resolve()), revision(item), item.model_dump_json(),
                                  datetime.now(UTC).isoformat()))
        except Exception:
            atomic_write(path, original)
            rescore_profile(conn, context=ctx, profile_id=path.stem, sources=sources)
            raise
        return {"revision": revision(proposed), "scored": report.scored_count,
                "skipped": report.skipped, "preview": preview}


def profile_history(conn: sqlite3.Connection, path: Path) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT id,revision,created_at,payload FROM profile_revision "
                        "WHERE profile_path=? ORDER BY id DESC LIMIT 30",
                        (str(path.resolve()),)).fetchall()
    return [dict(row) for row in rows]


CorrectionField = Literal[
    "rent", "beds", "baths", "area", "floor", "parking_available",
    "in_suite_laundry", "available_date", "lease_months", "total_monthly",
    "research_address",
]


def correct_listing_fact(
    conn: sqlite3.Connection,
    *,
    listing_id: str,
    field: CorrectionField,
    value: str,
    currency: str,
    area_unit: str,
) -> None:
    """Record a user-owned fact that outranks parser and enrichment observations."""
    allowed = {
        "rent", "beds", "baths", "area", "floor", "parking_available",
        "in_suite_laundry", "available_date", "lease_months", "total_monthly",
        "research_address",
    }
    if field not in allowed:
        raise ValueError("Unknown correction field.")
    if not conn.execute("SELECT 1 FROM listing WHERE id=?", (listing_id,)).fetchone():
        raise ValueError("Listing not found.")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Enter the corrected value.")

    storage_field: str = field
    parsed: JSONValue
    if field in {"rent", "beds", "baths", "area", "floor", "lease_months", "total_monthly"}:
        try:
            number = float(cleaned)
        except ValueError:
            raise ValueError("The corrected value must be a number.") from None
        if number < 0 or number != number or number in {float("inf"), float("-inf")}:
            raise ValueError("The corrected value must be a non-negative finite number.")
        if field == "rent":
            parsed = {"amount": str(number), "currency": currency, "period": "month"}
        elif field == "area":
            parsed = {"value": number, "unit": area_unit}
        elif field == "floor":
            if not number.is_integer():
                raise ValueError("Floor must be a whole number.")
            parsed = int(number)
        elif field in {"lease_months", "total_monthly"}:
            storage_field = f"attributes.{field}"
            parsed = number
        else:
            parsed = number
    elif field == "research_address":
        if len(cleaned) > 240:
            raise ValueError("Address must be 240 characters or fewer.")
        storage_field = "attributes.research_address"
        parsed = cleaned
    elif field in {"parking_available", "in_suite_laundry"}:
        normalized = cleaned.lower()
        if normalized not in {"yes", "no", "true", "false"}:
            raise ValueError("Choose yes or no.")
        storage_field = f"attributes.{field}"
        parsed = normalized in {"yes", "true"}
    else:
        try:
            parsed = date.fromisoformat(cleaned).isoformat()
        except ValueError:
            raise ValueError("Availability must be a valid date.") from None
        storage_field = "attributes.available_date"

    with conn:
        ObservationRepo(conn).record_observation(
            listing_id=listing_id,
            field=storage_field,
            value_json=parsed,
            origin=Origin.USER,
            confidence=1.0,
            evidence="user correction",
            observed_at=datetime.now(UTC),
        )


def clear_listing_correction(conn: sqlite3.Connection, *, listing_id: str, field: str) -> None:
    """Remove user observations for one fact and reveal the best sourced value again."""
    allowed = {
        "rent", "beds", "baths", "area", "floor",
        "attributes.parking_available", "attributes.in_suite_laundry",
        "attributes.available_date", "attributes.lease_months", "attributes.total_monthly",
        "attributes.research_address",
    }
    if field not in allowed:
        raise ValueError("Unknown correction field.")
    with conn:
        conn.execute(
            "DELETE FROM observation WHERE listing_id=? AND field=? AND origin=?",
            (listing_id, field, Origin.USER.value),
        )
        repo = ListingRepo(conn)
        repo.replace_fields_projection(listing_id, ObservationRepo(conn).project_listing_fields(
            listing_id
        ))


def listing_corrections(conn: sqlite3.Connection, listing_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT field,value_json,observed_at FROM observation "
        "WHERE listing_id=? AND origin=? ORDER BY observed_at DESC,id DESC",
        (listing_id, Origin.USER.value),
    ).fetchall()
    return [{"field": str(row["field"]), "value": json.loads(str(row["value_json"])),
             "observed_at": str(row["observed_at"])} for row in rows]


def research_candidates(conn: sqlite3.Connection, listing_id: str) -> list[dict[str, Any]]:
    """Find stored ads with strong overlapping unit evidence for research."""
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT sr.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY sr.listing_id,sr.source,sr.source_id ORDER BY sr.id DESC
                   ) AS rn
            FROM source_record sr
        )
        SELECT id AS record_id,listing_id,source,source_id,url,payload
        FROM latest WHERE rn=1 ORDER BY listing_id,record_id DESC
        """
    ).fetchall()
    parsed: list[tuple[sqlite3.Row, dict[str, Any]]] = []
    target: dict[str, Any] | None = None
    target_record_id: int | None = None
    for row in rows:
        payload = json.loads(str(row["payload"]))
        if not isinstance(payload, dict):
            continue
        parsed.append((row, payload))
        record_id = int(row["record_id"])
        if str(row["listing_id"]) == listing_id and (
            target_record_id is None or record_id > target_record_id
        ):
            target = payload
            target_record_id = record_id
    if target is None:
        return []

    target_photos = _research_photos(target)
    target_point = target.get("point")
    target_facts = tuple(target.get(key) for key in ("price", "beds", "baths"))
    matches: list[dict[str, Any]] = []
    for row, payload in parsed:
        candidate_id = str(row["listing_id"])
        if candidate_id == listing_id and int(row["record_id"]) == target_record_id:
            continue
        evidence: list[str] = []
        if target_photos & _research_photos(payload):
            evidence.append("same listing photo")
        if isinstance(target_point, dict) and payload.get("point") == target_point:
            evidence.append("same map coordinates")
        facts = tuple(payload.get(key) for key in ("price", "beds", "baths"))
        if all(value is not None for value in target_facts) and facts == target_facts:
            evidence.append("same rent and bedroom/bathroom facts")
        if "same listing photo" not in evidence and len(evidence) < 2:
            continue
        matches.append({
            "listing_id": candidate_id,
            "source": str(row["source"]),
            "source_id": str(row["source_id"]),
            "url": str(row["url"]),
            "title": str(payload.get("title") or candidate_id),
            "evidence": evidence,
        })
    matches.sort(key=lambda item: (-len(item["evidence"]), str(item["listing_id"])))
    return matches


def _research_photos(payload: Mapping[str, Any]) -> set[str]:
    values: list[object] = []
    for key in ("photo", "photos", "images"):
        raw = payload.get(key)
        if isinstance(raw, str):
            values.append(raw)
        elif isinstance(raw, list):
            values.extend(raw)
    return {str(value).split("?", 1)[0] for value in values if isinstance(value, str) and value}


class ManualListing(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    url: str
    title: str = Field(min_length=1, max_length=300)
    address: str = ""
    description: str = Field(default="", max_length=10000)
    rent: float | None = Field(default=None, ge=0)
    beds: float | None = Field(default=None, ge=0)
    baths: float | None = Field(default=None, ge=0)
    area: float | None = Field(default=None, ge=0)
    floor: int | None = Field(default=None, ge=0)
    parking: str = ""
    furnishing: str = ""
    available_date: str = ""
    lease_months: float | None = Field(default=None, ge=0)
    total_monthly: float | None = Field(default=None, ge=0)

    @field_validator("url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("Enter an http or https listing URL.")
        return value.strip()

    @field_validator("available_date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        from datetime import date
        if value:
            date.fromisoformat(value)
        return value


def add_manual(conn: sqlite3.Connection, data: ManualListing) -> str:
    key = hashlib.sha256(data.url.encode()).hexdigest()[:24]
    record = SourceRecord(source="manual", source_id=key, url=data.url,
                          payload=data.model_dump(mode="json"),
                          content_hash=hashlib.sha256(data.model_dump_json().encode()).hexdigest(),
                          fetched_at=datetime.now(UTC))
    listing_id = f"manual:{key}"
    with conn:
        repo = ListingRepo(conn)
        repo.ensure_listing(listing_id)
        repo.add_source_record(listing_id=listing_id, record=record)
    return listing_id


Stage = Literal["spotted", "shortlisted", "contacted", "replied", "viewing_booked", "viewed",
                "applied", "rejected", "removed"]


def update_progress(conn: sqlite3.Connection, listing_id: str, stage: Stage,
                    viewing_at: str, timezone: str) -> None:
    if stage not in {"spotted", "shortlisted", "contacted", "replied", "viewing_booked",
                     "viewed", "applied", "rejected", "removed"}:
        raise ValueError("Choose a valid stage.")
    if not conn.execute("SELECT 1 FROM listing WHERE id=?", (listing_id,)).fetchone():
        raise ValueError("Listing not found.")
    when = None
    if viewing_at:
        dt = datetime.fromisoformat(viewing_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(timezone))
        when = dt.astimezone(UTC).isoformat()
    if stage == "viewing_booked" and when is None:
        raise ValueError("Enter a viewing date and time.")
    with conn:
        conn.execute("INSERT INTO hunt_progress VALUES (?,?,?,?) ON CONFLICT(listing_id) "
                     "DO UPDATE SET stage=excluded.stage,viewing_at=excluded.viewing_at,"
                     "updated_at=excluded.updated_at",
                     (listing_id, stage, when, datetime.now(UTC).isoformat()))


def progress(conn: sqlite3.Connection, listing_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM hunt_progress WHERE listing_id=?", (listing_id,)).fetchone()
    return dict(row) if row else {"stage": "spotted", "viewing_at": None}


def viewing_ics(conn: sqlite3.Connection, listing_id: str, title: str, address: str) -> str:
    item = progress(conn, listing_id)
    if not item["viewing_at"]:
        raise ValueError("Book a viewing first.")
    start = datetime.fromisoformat(item["viewing_at"])

    def escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("\r", "").replace("\n", "\\n").replace(
            ";", "\\;").replace(",", "\\,")

    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Nostos//Viewing//EN", "BEGIN:VEVENT",
        f"UID:{hashlib.sha256(listing_id.encode()).hexdigest()}@nostos",
        f"DTSTAMP:{datetime.now(UTC):%Y%m%dT%H%M%SZ}", f"DTSTART:{start:%Y%m%dT%H%M%SZ}",
        f"DTEND:{start + timedelta(hours=1):%Y%m%dT%H%M%SZ}",
        f"SUMMARY:{escape('Apartment viewing: ' + title)}", f"LOCATION:{escape(address)}",
        "END:VEVENT", "END:VCALENDAR", "",
    ])
