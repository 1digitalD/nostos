"""Preview and atomically publish extraction revisions from saved source content."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, cast

from nostos.config.profile import ScaledWeight
from nostos.context import SearchContext
from nostos.corrections import apply_geo_observations, apply_user_corrections
from nostos.enrich.base import Enricher
from nostos.enrich.chain import run_enricher_chain
from nostos.enrich.text import TextRuleEnricher
from nostos.model import Absence, Listing, Observed, Origin, SourceRecord
from nostos.model.source_record import JSONValue
from nostos.rank.engine import RankEngine, ScoreResult
from nostos.rank.profile_scoring import passes_hard_filters
from nostos.sources.base import Source
from nostos.store.db import apply_migrations
from nostos.store.repo import ListingRepo, ObservationRepo, ScoreRepo

EXTRACTION_REVISION = "saved-content-v1"
_CORE_FACT_FIELDS = ("rent", "beds", "baths", "area", "floor", "parking", "furnishing")


class ExtractionReviewError(RuntimeError):
    """Base error for saved-content extraction review."""


class ExtractionPreviewNotFound(ExtractionReviewError):
    """The preview token or listing does not exist."""


class StaleExtractionPreview(ExtractionReviewError):
    """The source, corrections, supporting evidence, or profile changed."""


@dataclass(frozen=True, slots=True)
class SavedSourceSnapshot:
    record_id: int
    source: str
    source_id: str
    url: str
    content_hash: str
    fetched_at: datetime
    payload: JSONValue
    limitations: tuple[str, ...]

    def to_record(self) -> SourceRecord:
        return SourceRecord(
            source=self.source,
            source_id=self.source_id,
            url=self.url,
            content_hash=self.content_hash,
            fetched_at=self.fetched_at,
            payload=self.payload,
        )


@dataclass(frozen=True, slots=True)
class FactSnapshot:
    field: str
    value: JSONValue
    origin: str | None
    confidence: float | None
    evidence: str | None


@dataclass(frozen=True, slots=True)
class FactChange:
    field: str
    before: FactSnapshot | None
    after: FactSnapshot | None


@dataclass(frozen=True, slots=True)
class ExtractionPreview:
    preview_token: str
    listing_id: str
    extractor_revision: str
    source: SavedSourceSnapshot
    correction_revision: int
    profile_id: str
    profile_fingerprint: str
    machine_listing: Listing
    resolved_listing: Listing
    changes: tuple[FactChange, ...]
    previous_eligible: bool
    eligible: bool
    score: float | None
    score_breakdown: dict[str, JSONValue] | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AppliedExtractionRevision:
    preview: ExtractionPreview
    applied_at: datetime
    repeated: bool = False

    @property
    def preview_token(self) -> str:
        return self.preview.preview_token

    @property
    def listing_id(self) -> str:
        return self.preview.listing_id

    @property
    def machine_listing(self) -> Listing:
        return self.preview.machine_listing

    @property
    def resolved_listing(self) -> Listing:
        return self.preview.resolved_listing


def _preview_extraction_revision_locked(
    conn: sqlite3.Connection,
    *,
    listing_id: str,
    context: SearchContext,
    profile_id: str,
    sources: Mapping[str, Source],
    enrichers: Iterable[Enricher] | None = None,
) -> ExtractionPreview:
    """Resolve current parsers against the latest saved record and save a dry-run preview."""

    source_snapshot = _latest_source_snapshot(conn, listing_id)
    source = sources.get(source_snapshot.source)
    if source is None:
        raise ExtractionReviewError(
            f"Source {source_snapshot.source!r} is not available for saved-content review."
        )

    base_listing = _canonical_listing(
        source.to_listing(source_snapshot.to_record(), context), listing_id=listing_id
    )
    active_enrichers = tuple(enrichers) if enrichers is not None else (TextRuleEnricher(),)
    machine_listing = run_enricher_chain(base_listing, active_enrichers, context)
    resolved_listing = _apply_saved_overrides(conn, listing_id, machine_listing)

    previous_listing = _current_resolved_listing(conn, listing_id, base_listing)
    previous_eligible = passes_hard_filters(previous_listing, context.profile)
    eligible = passes_hard_filters(resolved_listing, context.profile)
    result = (
        RankEngine(context.profile).score_listing(resolved_listing, context=context)
        if eligible
        else None
    )
    breakdown = _score_result_to_json(result) if result is not None else None

    correction_revision = _correction_revision(conn, listing_id)
    supporting_fingerprint = _supporting_fingerprint(conn, listing_id)
    profile_fingerprint = _profile_fingerprint(context)
    existing = conn.execute(
        """
        SELECT * FROM extraction_review
        WHERE listing_id=? AND source_record_id=? AND extractor_revision=?
          AND correction_revision=? AND supporting_fingerprint=?
          AND profile_id=? AND profile_fingerprint=?
          AND applied_at IS NULL
        ORDER BY created_at DESC LIMIT 1
        """,
        (
            listing_id,
            source_snapshot.record_id,
            EXTRACTION_REVISION,
            correction_revision,
            supporting_fingerprint,
            profile_id,
            profile_fingerprint,
        ),
    ).fetchone()
    if existing is not None:
        return _preview_from_row(existing)
    created_at = datetime.now(tz=UTC)
    preview_token = _preview_token(
        listing_id=listing_id,
        source=source_snapshot,
        correction_revision=correction_revision,
        supporting_fingerprint=supporting_fingerprint,
        profile_id=profile_id,
        profile_fingerprint=profile_fingerprint,
    )
    changes = _fact_changes(previous_listing, resolved_listing)
    preview = ExtractionPreview(
        preview_token=preview_token,
        listing_id=listing_id,
        extractor_revision=EXTRACTION_REVISION,
        source=source_snapshot,
        correction_revision=correction_revision,
        profile_id=profile_id,
        profile_fingerprint=profile_fingerprint,
        machine_listing=machine_listing,
        resolved_listing=resolved_listing,
        changes=changes,
        previous_eligible=previous_eligible,
        eligible=eligible,
        score=result.score if result is not None else None,
        score_breakdown=breakdown,
        created_at=created_at,
    )
    with _transaction(conn):
        conn.execute(
            """
            INSERT INTO extraction_review(
                preview_token,listing_id,source_record_id,source,source_id,source_url,
                source_content_hash,source_snapshot_json,extractor_revision,
                correction_revision,supporting_fingerprint,profile_id,profile_fingerprint,
                machine_listing_json,resolved_listing_json,changes_json,previous_eligible,
                eligible,score_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                preview.preview_token,
                preview.listing_id,
                preview.source.record_id,
                preview.source.source,
                preview.source.source_id,
                preview.source.url,
                preview.source.content_hash,
                _source_snapshot_json(preview.source),
                preview.extractor_revision,
                preview.correction_revision,
                supporting_fingerprint,
                preview.profile_id,
                preview.profile_fingerprint,
                preview.machine_listing.model_dump_json(warnings=False),
                preview.resolved_listing.model_dump_json(warnings=False),
                _changes_json(preview.changes),
                int(preview.previous_eligible),
                int(preview.eligible),
                _json_dumps(cast(JSONValue, preview.score_breakdown))
                if preview.score_breakdown is not None
                else None,
                preview.created_at.isoformat(),
            ),
        )
        conn.execute(
            """
            DELETE FROM extraction_review
            WHERE preview_token IN (
                SELECT preview_token FROM extraction_review
                WHERE listing_id=? AND applied_at IS NULL AND preview_token<>?
                ORDER BY created_at DESC LIMIT -1 OFFSET 9
            )
            """,
            (listing_id, preview.preview_token),
        )
    return preview


def preview_extraction_revision(
    conn: sqlite3.Connection,
    *,
    listing_id: str,
    context: SearchContext,
    profile_id: str,
    sources: Mapping[str, Source],
    enrichers: Iterable[Enricher] | None = None,
) -> ExtractionPreview:
    """Resolve and save one transactionally consistent extraction preview."""

    if not conn.in_transaction:
        apply_migrations(conn)
    with _transaction(conn, immediate=True):
        return _preview_extraction_revision_locked(
            conn,
            listing_id=listing_id,
            context=context,
            profile_id=profile_id,
            sources=sources,
            enrichers=enrichers,
        )


def _apply_extraction_revision_locked(
    conn: sqlite3.Connection,
    *,
    listing_id: str,
    preview_token: str,
    context: SearchContext,
) -> AppliedExtractionRevision:
    """Publish an unchanged preview's facts and profile score in one transaction."""

    row = conn.execute(
        "SELECT * FROM extraction_review WHERE preview_token=? AND listing_id=?",
        (preview_token, listing_id),
    ).fetchone()
    if row is None:
        raise ExtractionPreviewNotFound("Extraction preview was not found for this listing.")
    preview = _preview_from_row(row)
    _assert_preview_current(conn, preview, row=row, context=context)

    if row["applied_at"] is not None:
        if row["superseded_at"] is not None:
            raise StaleExtractionPreview("This extraction revision has been superseded.")
        return AppliedExtractionRevision(
            preview=preview,
            applied_at=datetime.fromisoformat(str(row["applied_at"])),
            repeated=True,
        )

    applied_at = datetime.now(tz=UTC)
    listing_state = conn.execute(
        "SELECT last_seen,status FROM listing WHERE id=?", (listing_id,)
    ).fetchone()
    if listing_state is None:
        raise ExtractionPreviewNotFound("Listing was removed after this preview.")
    with _transaction(conn):
        conn.execute(
            """
            UPDATE extraction_review
            SET superseded_at=?
            WHERE listing_id=? AND applied_at IS NOT NULL AND superseded_at IS NULL
            """,
            (applied_at.isoformat(), listing_id),
        )
        updated = conn.execute(
            """
            UPDATE extraction_review
            SET applied_at=?
            WHERE preview_token=? AND listing_id=? AND applied_at IS NULL
            """,
            (applied_at.isoformat(), preview_token, listing_id),
        )
        if updated.rowcount != 1:
            raise StaleExtractionPreview("This extraction preview can no longer be applied.")

        for field, observed in _observed_fields(preview.machine_listing).items():
            value_json = _json_dumps(_observed_json_value(observed))
            observed_at = observed.observed_at.astimezone(UTC).isoformat()
            reusable = conn.execute(
                """
                SELECT id FROM observation
                WHERE listing_id=? AND field=? AND value_json=? AND origin=?
                  AND confidence=? AND COALESCE(evidence,'')=COALESCE(?,'')
                  AND observed_at=? AND extraction_review_id IS NULL
                ORDER BY id DESC LIMIT 1
                """,
                (
                    listing_id,
                    field,
                    value_json,
                    observed.origin.value,
                    observed.confidence,
                    observed.evidence,
                    observed_at,
                ),
            ).fetchone()
            if reusable is not None:
                conn.execute(
                    "UPDATE observation SET extraction_review_id=? WHERE id=?",
                    (preview_token, int(reusable["id"])),
                )
                continue
            conn.execute(
                """
                INSERT INTO observation(
                    listing_id,field,value_json,origin,confidence,evidence,observed_at,
                    extraction_review_id
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    listing_id,
                    field,
                    value_json,
                    observed.origin.value,
                    observed.confidence,
                    observed.evidence,
                    observed_at,
                    preview_token,
                ),
            )

        projection = ObservationRepo(conn).project_listing_fields(listing_id)
        if preview.eligible:
            if preview.score is None or preview.score_breakdown is None:
                raise ExtractionReviewError("Eligible preview is missing its score result.")
            ScoreRepo(conn).upsert_score(
                listing_id=listing_id,
                profile_id=preview.profile_id,
                score=preview.score,
                breakdown_json=preview.score_breakdown,
                computed_at=applied_at,
            )
        else:
            conn.execute(
                "DELETE FROM score WHERE listing_id=? AND profile_id=?",
                (listing_id, preview.profile_id),
            )
        conn.execute(
            """
            UPDATE listing
            SET fields_json=?,schema_version=?,last_seen=?,status=?
            WHERE id=?
            """,
            (
                _json_dumps(cast(JSONValue, projection)),
                preview.machine_listing.schema_version,
                str(listing_state["last_seen"]),
                str(listing_state["status"]),
                listing_id,
            ),
        )

    return AppliedExtractionRevision(preview=preview, applied_at=applied_at)


def apply_extraction_revision(
    conn: sqlite3.Connection,
    *,
    listing_id: str,
    preview_token: str,
    context: SearchContext,
) -> AppliedExtractionRevision:
    """Validate and publish a preview while holding a SQLite write lock."""

    if not conn.in_transaction:
        apply_migrations(conn)
    with _transaction(conn, immediate=True):
        return _apply_extraction_revision_locked(
            conn,
            listing_id=listing_id,
            preview_token=preview_token,
            context=context,
        )


def load_current_extraction_revision(
    conn: sqlite3.Connection, *, listing_id: str
) -> AppliedExtractionRevision | None:
    """Load the current applied snapshot, unless a newer source record makes it stale."""

    row = conn.execute(
        """
        SELECT er.*
        FROM extraction_review er
        JOIN source_record sr ON sr.id = er.source_record_id
        WHERE er.listing_id=?
          AND er.applied_at IS NOT NULL
          AND er.superseded_at IS NULL
          AND er.extractor_revision=?
          AND sr.content_hash=er.source_content_hash
          AND sr.source=er.source
          AND sr.source_id=er.source_id
          AND sr.url=er.source_url
          AND sr.id=(SELECT MAX(id) FROM source_record WHERE listing_id=er.listing_id)
        """,
        (listing_id, EXTRACTION_REVISION),
    ).fetchone()
    if row is None:
        return None
    return AppliedExtractionRevision(
        preview=_preview_from_row(row),
        applied_at=datetime.fromisoformat(str(row["applied_at"])),
    )


def _latest_source_snapshot(conn: sqlite3.Connection, listing_id: str) -> SavedSourceSnapshot:
    row = conn.execute(
        """
        SELECT id,source,source_id,url,payload,content_hash,fetched_at
        FROM source_record WHERE listing_id=? ORDER BY id DESC LIMIT 1
        """,
        (listing_id,),
    ).fetchone()
    if row is None:
        raise ExtractionPreviewNotFound("Listing has no saved source content to review.")
    payload = cast(JSONValue, json.loads(str(row["payload"])))
    return SavedSourceSnapshot(
        record_id=int(row["id"]),
        source=str(row["source"]),
        source_id=str(row["source_id"]),
        url=str(row["url"]),
        content_hash=str(row["content_hash"]),
        fetched_at=datetime.fromisoformat(str(row["fetched_at"])),
        payload=payload,
        limitations=_source_limitations(payload),
    )


def _source_limitations(payload: JSONValue) -> tuple[str, ...]:
    if not isinstance(payload, Mapping):
        return ("Saved source content is not a structured listing snapshot.",)
    limitations: list[str] = []
    if payload.get("detail_error"):
        limitations.append(
            "The saved detail retrieval failed; only discovery content is available."
        )
    description = next(
        (
            value
            for key in ("description", "listingText", "listing_text")
            if isinstance((value := payload.get(key)), str) and value.strip()
        ),
        None,
    )
    if description is None:
        limitations.append("The saved snapshot has no description text.")
    elif payload.get("description_truncated"):
        limitations.append("The saved description reached its storage limit and is truncated.")
    elif len(description) == 2000:
        limitations.append("The legacy saved description may be truncated at 2,000 characters.")
    photo_values = (
        payload.get("photos")
        or payload.get("photo")
        or payload.get("images")
        or payload.get("image")
    )
    if not photo_values:
        limitations.append("The saved snapshot has no photo URLs; review cannot recover them.")
    elif payload.get("photos_truncated"):
        limitations.append("The saved gallery reached its 50-photo storage limit.")
    return tuple(limitations)


def _canonical_listing(listing: Listing, *, listing_id: str) -> Listing:
    if listing.identity.listing_id == listing_id:
        return listing
    return listing.model_copy(
        update={"identity": listing.identity.model_copy(update={"listing_id": listing_id})}
    )


def _apply_saved_overrides(
    conn: sqlite3.Connection, listing_id: str, listing: Listing
) -> Listing:
    listing = apply_geo_observations(conn, listing_id=listing_id, listing=listing)
    return apply_user_corrections(conn, listing_id=listing_id, listing=listing)


def _current_resolved_listing(
    conn: sqlite3.Connection, listing_id: str, fallback: Listing
) -> Listing:
    # A newly saved source record intentionally makes the public "current"
    # revision stale.  The last applied machine snapshot is still the correct
    # before-state for a revision diff, and avoids reparsing legacy projections.
    current = _load_latest_applied_extraction_revision(conn, listing_id=listing_id)
    if current is not None:
        return _apply_saved_overrides(conn, listing_id, current.machine_listing)
    projection = ListingRepo(conn).get_fields_projection(listing_id)
    projected = _overlay_projection(fallback, projection)
    return _apply_saved_overrides(conn, listing_id, projected)


def _load_latest_applied_extraction_revision(
    conn: sqlite3.Connection, *, listing_id: str
) -> AppliedExtractionRevision | None:
    row = conn.execute(
        """
        SELECT * FROM extraction_review
        WHERE listing_id=?
          AND applied_at IS NOT NULL
          AND superseded_at IS NULL
          AND extractor_revision=?
        ORDER BY applied_at DESC,created_at DESC LIMIT 1
        """,
        (listing_id, EXTRACTION_REVISION),
    ).fetchone()
    if row is None:
        return None
    return AppliedExtractionRevision(
        preview=_preview_from_row(row),
        applied_at=datetime.fromisoformat(str(row["applied_at"])),
    )


def _overlay_projection(listing: Listing, projection: Mapping[str, JSONValue]) -> Listing:
    payload = listing.model_dump(mode="json")
    attributes = cast(dict[str, Any], payload.setdefault("attributes", {}))
    for field, value in projection.items():
        if field.startswith("attributes."):
            attributes[field.removeprefix("attributes.")] = value
        elif field in _CORE_FACT_FIELDS:
            payload[field] = value
    return Listing.model_validate(payload)


def _observed_fields(listing: Listing) -> dict[str, Observed[Any]]:
    fields: dict[str, Observed[Any]] = {}
    for field in _CORE_FACT_FIELDS:
        value = getattr(listing, field)
        if isinstance(value, Observed):
            fields[field] = value
    for key, value in listing.attributes.items():
        if isinstance(value, Observed):
            fields[f"attributes.{key}"] = value
    return fields


def _fact_snapshots(listing: Listing) -> dict[str, FactSnapshot]:
    snapshots: dict[str, FactSnapshot] = {}
    for field in _CORE_FACT_FIELDS:
        snapshots[field] = _fact_snapshot(field, getattr(listing, field))
    for key, value in listing.attributes.items():
        field = f"attributes.{key}"
        snapshots[field] = _fact_snapshot(field, value)
    return snapshots


def _fact_snapshot(field: str, value: object) -> FactSnapshot:
    if isinstance(value, Observed):
        return FactSnapshot(
            field=field,
            value=_observed_json_value(value),
            origin=value.origin.value,
            confidence=value.confidence,
            evidence=value.evidence,
        )
    absence = value.value if isinstance(value, Absence) else str(value)
    return FactSnapshot(
        field=field,
        value=cast(JSONValue, absence),
        origin=None,
        confidence=None,
        evidence=None,
    )


def _fact_changes(before: Listing, after: Listing) -> tuple[FactChange, ...]:
    before_fields = _fact_snapshots(before)
    after_fields = _fact_snapshots(after)
    changes: list[FactChange] = []
    for field in sorted(before_fields.keys() | after_fields.keys()):
        old = before_fields.get(field)
        new = after_fields.get(field)
        if old != new:
            changes.append(FactChange(field=field, before=old, after=new))
    return tuple(changes)


def _correction_revision(conn: sqlite3.Connection, listing_id: str) -> int:
    row = conn.execute(
        "SELECT revision FROM listing_correction_revision WHERE listing_id=?", (listing_id,)
    ).fetchone()
    return int(row["revision"]) if row is not None else 0


def _supporting_fingerprint(conn: sqlite3.Connection, listing_id: str) -> str:
    rows = conn.execute(
        """
        SELECT id,field,value_json,confidence,evidence,observed_at
        FROM observation WHERE listing_id=? AND origin=? ORDER BY id
        """,
        (listing_id, Origin.GEO_PROVIDER.value),
    ).fetchall()
    return _fingerprint([dict(row) for row in rows])


def _profile_fingerprint(context: SearchContext) -> str:
    return _fingerprint(
        {
            "profile": context.profile.model_dump(mode="json"),
            "citypack": context.citypack.model_dump(mode="json"),
        }
    )


def _preview_token(
    *,
    listing_id: str,
    source: SavedSourceSnapshot,
    correction_revision: int,
    supporting_fingerprint: str,
    profile_id: str,
    profile_fingerprint: str,
) -> str:
    state = {
        "listing_id": listing_id,
        "source_record_id": source.record_id,
        "source": source.source,
        "source_id": source.source_id,
        "content_hash": source.content_hash,
        "correction_revision": correction_revision,
        "supporting_fingerprint": supporting_fingerprint,
        "profile_id": profile_id,
        "profile_fingerprint": profile_fingerprint,
        "extractor_revision": EXTRACTION_REVISION,
        "nonce": uuid.uuid4().hex,
    }
    return _fingerprint(state)


def _assert_preview_current(
    conn: sqlite3.Connection,
    preview: ExtractionPreview,
    *,
    row: sqlite3.Row,
    context: SearchContext,
) -> None:
    latest = _latest_source_snapshot(conn, preview.listing_id)
    if (
        latest.record_id != preview.source.record_id
        or latest.source != preview.source.source
        or latest.source_id != preview.source.source_id
        or latest.url != preview.source.url
        or latest.content_hash != preview.source.content_hash
    ):
        raise StaleExtractionPreview("Saved source content changed after this preview.")
    if _correction_revision(conn, preview.listing_id) != preview.correction_revision:
        raise StaleExtractionPreview("A user correction changed after this preview.")
    if _supporting_fingerprint(conn, preview.listing_id) != str(row["supporting_fingerprint"]):
        raise StaleExtractionPreview("Saved supporting evidence changed after this preview.")
    if _profile_fingerprint(context) != preview.profile_fingerprint:
        raise StaleExtractionPreview("The active profile changed after this preview.")


def _preview_from_row(row: sqlite3.Row) -> ExtractionPreview:
    source_data = cast(dict[str, Any], json.loads(str(row["source_snapshot_json"])))
    source = SavedSourceSnapshot(
        record_id=int(source_data["record_id"]),
        source=str(source_data["source"]),
        source_id=str(source_data["source_id"]),
        url=str(source_data["url"]),
        content_hash=str(source_data["content_hash"]),
        fetched_at=datetime.fromisoformat(str(source_data["fetched_at"])),
        payload=cast(JSONValue, source_data["payload"]),
        limitations=tuple(str(item) for item in source_data["limitations"]),
    )
    changes_data = cast(list[dict[str, Any]], json.loads(str(row["changes_json"])))
    changes = tuple(
        FactChange(
            field=str(item["field"]),
            before=_fact_from_json(item.get("before")),
            after=_fact_from_json(item.get("after")),
        )
        for item in changes_data
    )
    score_json = row["score_json"]
    breakdown = (
        cast(dict[str, JSONValue], json.loads(str(score_json))) if score_json is not None else None
    )
    return ExtractionPreview(
        preview_token=str(row["preview_token"]),
        listing_id=str(row["listing_id"]),
        extractor_revision=str(row["extractor_revision"]),
        source=source,
        correction_revision=int(row["correction_revision"]),
        profile_id=str(row["profile_id"]),
        profile_fingerprint=str(row["profile_fingerprint"]),
        machine_listing=Listing.model_validate_json(str(row["machine_listing_json"])),
        resolved_listing=Listing.model_validate_json(str(row["resolved_listing_json"])),
        changes=changes,
        previous_eligible=bool(row["previous_eligible"]),
        eligible=bool(row["eligible"]),
        score=_score_from_breakdown(breakdown),
        score_breakdown=breakdown,
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )


def _fact_from_json(value: object) -> FactSnapshot | None:
    if not isinstance(value, Mapping):
        return None
    return FactSnapshot(
        field=str(value["field"]),
        value=cast(JSONValue, value["value"]),
        origin=str(value["origin"]) if value.get("origin") is not None else None,
        confidence=float(value["confidence"]) if value.get("confidence") is not None else None,
        evidence=str(value["evidence"]) if value.get("evidence") is not None else None,
    )


def _score_from_breakdown(breakdown: dict[str, JSONValue] | None) -> float | None:
    if breakdown is None:
        return None
    score = breakdown.get("score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise ExtractionReviewError("Stored extraction preview has an invalid score.")
    return float(score)


def _source_snapshot_json(source: SavedSourceSnapshot) -> str:
    return _json_dumps(
        cast(
            JSONValue,
            {
                "record_id": source.record_id,
                "source": source.source,
                "source_id": source.source_id,
                "url": source.url,
                "content_hash": source.content_hash,
                "fetched_at": source.fetched_at.isoformat(),
                "payload": source.payload,
                "limitations": list(source.limitations),
            },
        )
    )


def _changes_json(changes: tuple[FactChange, ...]) -> str:
    return _json_dumps(cast(JSONValue, [asdict(change) for change in changes]))


def _score_result_to_json(result: ScoreResult) -> dict[str, JSONValue]:
    contributions: list[dict[str, JSONValue]] = []
    for contribution in result.contributions:
        weight: JSONValue
        if isinstance(contribution.weight, ScaledWeight):
            weight = cast(JSONValue, contribution.weight.model_dump(mode="json"))
        else:
            weight = float(contribution.weight)
        signal: dict[str, JSONValue] | None = None
        if contribution.signal is not None:
            signal = {
                "fired": contribution.signal.fired,
                "magnitude": contribution.signal.magnitude,
                "confidence": contribution.signal.confidence,
                "evidence": contribution.signal.evidence,
            }
        contributions.append(
            {
                "rule_key": contribution.rule_key,
                "category": contribution.category,
                "label": contribution.label,
                "weight": weight,
                "signal": signal,
                "shaped_magnitude": contribution.shaped_magnitude,
                "confidence_factor": contribution.confidence_factor,
                "min_possible": contribution.min_possible,
                "max_possible": contribution.max_possible,
                "contribution": contribution.contribution,
            }
        )
    return {
        "score": result.score,
        "total_contribution": result.total_contribution,
        "normalization": {
            "min_possible": result.normalization.min_possible,
            "max_possible": result.normalization.max_possible,
        },
        "contributions": contributions,
    }


def _observed_json_value(observed: Observed[Any]) -> JSONValue:
    return cast(JSONValue, observed.model_dump(mode="json")["value"])


def _json_dumps(value: JSONValue) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


@contextmanager
def _transaction(conn: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """Commit independently, or nest safely when a caller owns the transaction."""

    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
        return
    savepoint = f"extraction_review_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {savepoint}")
    if immediate:
        conn.execute(
            "UPDATE listing SET last_seen=last_seen "
            "WHERE id=(SELECT id FROM listing ORDER BY id LIMIT 1)"
        )
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")


__all__ = [
    "AppliedExtractionRevision",
    "EXTRACTION_REVISION",
    "ExtractionPreview",
    "ExtractionPreviewNotFound",
    "ExtractionReviewError",
    "FactChange",
    "FactSnapshot",
    "SavedSourceSnapshot",
    "StaleExtractionPreview",
    "apply_extraction_revision",
    "load_current_extraction_revision",
    "preview_extraction_revision",
]
