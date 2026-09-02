"""Re-score every stored listing against the active profile.

Shared by ``nostos rank`` and the web UI's profile editor so that changing a
weight or a hard filter is immediately reflected in the ranked list without a
fresh network fetch. Only the latest source record per listing is scored.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NamedTuple

from nostos.config.profile import ScaledWeight
from nostos.context import SearchContext
from nostos.enrich.base import Enricher
from nostos.enrich.text import TextRuleEnricher
from nostos.model import SourceRecord
from nostos.rank.engine import RankEngine, ScoreResult
from nostos.rank.profile_scoring import score_listing_for_profile
from nostos.sources.base import Source
from nostos.store.db import apply_migrations
from nostos.store.repo import ScoreRepo


class SourceRecordRow(NamedTuple):
    listing_id: str
    record: SourceRecord


@dataclass(frozen=True, slots=True)
class RescoreReport:
    """Outcome of one re-score pass."""

    profile_id: str
    # (listing_id, score) sorted by score desc.
    rows: tuple[tuple[str, float], ...]
    # Listings whose latest record exists but failed the hard filters (or whose
    # source is not instantiated) and therefore carry no score.
    skipped: int

    @property
    def scored_count(self) -> int:
        return len(self.rows)


def rescore_profile(
    conn: sqlite3.Connection,
    *,
    context: SearchContext,
    profile_id: str,
    sources: Mapping[str, Source],
    enrichers: Iterable[Enricher] | None = None,
) -> RescoreReport:
    """Drop and recompute every score row for ``profile_id``.

    Runs inside one transaction so a reader never observes a half-rescored
    table. Listings that fail the profile's hard filters lose their score row
    (and so disappear from the ranked list) — that is the point of editing a
    hard filter.
    """

    active_enrichers = tuple(enrichers) if enrichers is not None else (TextRuleEnricher(),)
    rank_engine = RankEngine(context.profile)
    apply_migrations(conn)
    rows = latest_source_records(conn)
    score_repo = ScoreRepo(conn)
    scored: list[tuple[str, float]] = []
    skipped = 0
    with conn:
        conn.execute("DELETE FROM score WHERE profile_id = ?", (profile_id,))
        for record_row in rows:
            source_obj = sources.get(record_row.record.source)
            if source_obj is None:
                skipped += 1
                continue
            listing = source_obj.to_listing(record_row.record, context)
            # ``to_listing`` derives a per-source identity; re-key to the
            # canonical id the record is stored under (post cross-source dedupe).
            if listing.identity.listing_id != record_row.listing_id:
                listing = listing.model_copy(
                    update={
                        "identity": listing.identity.model_copy(
                            update={"listing_id": record_row.listing_id}
                        )
                    }
                )
            scored_listing = score_listing_for_profile(
                listing,
                context=context,
                enrichers=active_enrichers,
                rank_engine=rank_engine,
            )
            if scored_listing is None:
                skipped += 1
                continue
            result = scored_listing.result
            listing_id = scored_listing.listing.identity.listing_id
            score_repo.upsert_score(
                listing_id=listing_id,
                profile_id=profile_id,
                score=result.score,
                breakdown_json=score_result_to_json(result),
                computed_at=record_row.record.fetched_at,
            )
            scored.append((listing_id, result.score))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return RescoreReport(profile_id=profile_id, rows=tuple(scored), skipped=skipped)


def latest_source_records(conn: sqlite3.Connection) -> tuple[SourceRecordRow, ...]:
    """Return the newest source record for every stored listing."""

    rows = conn.execute(
        """
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
            GROUP BY listing_id
        ) latest
        ON latest.latest_id = sr.id
        ORDER BY sr.listing_id ASC
        """,
    ).fetchall()

    records: list[SourceRecordRow] = []
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
        records.append(SourceRecordRow(listing_id=str(row["listing_id"]), record=source_record))
    return tuple(records)


def score_result_to_json(result: ScoreResult) -> dict[str, Any]:
    """Serialize a ``ScoreResult`` to the stored ``breakdown_json`` shape."""

    contributions: list[dict[str, Any]] = []
    for contribution in result.contributions:
        if isinstance(contribution.weight, ScaledWeight):
            weight_json: object = contribution.weight.model_dump(mode="json")
        else:
            weight_json = float(contribution.weight)
        signal_json: dict[str, Any] | None = None
        if contribution.signal is not None:
            signal_json = {
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
                "weight": weight_json,
                "signal": signal_json,
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


__all__ = [
    "RescoreReport",
    "SourceRecordRow",
    "latest_source_records",
    "rescore_profile",
    "score_result_to_json",
]
