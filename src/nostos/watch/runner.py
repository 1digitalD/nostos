from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, cast

from pydantic import BaseModel

from nostos.context import SearchContext
from nostos.enrich.chain import run_enricher_chain
from nostos.enrich.text import TextRuleEnricher
from nostos.model import JSONValue, Listing, Observed, SourceRecord
from nostos.rank.engine import RankEngine, RuleContribution, ScoreResult
from nostos.sources.base import Liveness, Source
from nostos.store.repo import ListingRepo, ObservationRepo, RunRepo, ScoreRepo
from nostos.watch.health import (
    HealthPolicy,
    SourceHealthDecision,
    SourceHealthInput,
    evaluate_sources,
    load_source_histories,
)
from nostos.watch.notify import Notifier, notifier_from_urls


@dataclass(frozen=True, slots=True)
class SourceRunReport:
    name: str
    status: str
    count: int
    load_bearing: bool
    within_baseline_band: bool
    watermark_advanced: bool
    effective_watermark: str | None
    candidate_watermark: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class WatchRunReport:
    run_id: str
    started_at: datetime
    finished_at: datetime
    source_reports: dict[str, SourceRunReport]
    alerts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PersistableListing:
    source_name: str
    record: SourceRecord
    listing: Listing


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    name: str
    status: str
    records_seen: int
    listings: tuple[_PersistableListing, ...]
    candidate_watermark: str | None
    error: str | None

    @property
    def count(self) -> int:
        return len(self.listings)


def run_watch(
    *,
    conn: sqlite3.Connection,
    context: SearchContext,
    sources: Iterable[Source],
    profile_id: str,
    enrichers: Iterable[Any] | None = None,
    rank_engine: RankEngine | None = None,
    notifier: Notifier | None = None,
    run_id: str | None = None,
    run_id_factory: Callable[[], str] | None = None,
    now: Callable[[], datetime] | None = None,
    max_workers: int | None = None,
    health_policy: HealthPolicy | None = None,
) -> WatchRunReport:
    now_fn = now or _utc_now
    run_identifier = run_id or _next_run_id(run_id_factory)
    started_at = now_fn()

    source_list = tuple(sources)
    source_metadata = _source_metadata(context=context, sources=source_list)

    run_repo = RunRepo(conn)
    with conn:
        run_repo.create_run(
            run_id=run_identifier,
            started_at=started_at,
            sources_json=cast(dict[str, JSONValue], source_metadata),
            counts_json=cast(dict[str, JSONValue], {"sources": {}}),
        )

    snapshots = _collect_sources(
        context=context,
        sources=source_list,
        source_metadata=source_metadata,
        max_workers=max_workers,
    )

    source_names = tuple(snapshot.name for snapshot in snapshots)
    history = load_source_histories(conn, source_names=source_names)
    health_inputs = tuple(
        SourceHealthInput(
            name=snapshot.name,
            status=snapshot.status,
            count=snapshot.count,
            load_bearing=bool(
                source_metadata[snapshot.name].get("load_bearing", False)
                if isinstance(source_metadata[snapshot.name], Mapping)
                else False
            ),
            candidate_watermark=snapshot.candidate_watermark,
        )
        for snapshot in snapshots
    )
    health_decisions = evaluate_sources(
        sources=health_inputs,
        history_by_source=history,
        policy=health_policy,
    )
    health_by_name = {decision.name: decision for decision in health_decisions}

    persistable = tuple(
        listing
        for snapshot in snapshots
        for listing in snapshot.listings
    )
    new_listing_ids = _persist_stage(conn=conn, listings=persistable)

    active_enrichers = tuple(enrichers or (TextRuleEnricher(),))
    enriched = _enrich_stage(
        conn=conn,
        context=context,
        listings=persistable,
        enrichers=active_enrichers,
    )
    engine = rank_engine or RankEngine(context.profile)
    scored = _rank_stage(
        conn=conn,
        context=context,
        listings=enriched,
        rank_engine=engine,
        profile_id=profile_id,
    )

    active_notifier = notifier or notifier_from_urls(context.profile.notify)
    alerts = tuple(alert for decision in health_decisions for alert in decision.alerts)
    _notify(
        notifier=active_notifier,
        alerts=alerts,
        scored=scored,
        new_listing_ids=new_listing_ids,
    )

    source_reports = _build_source_reports(
        snapshots=snapshots,
        health_by_name=health_by_name,
    )
    counts_json = _build_counts_json(
        snapshots=snapshots,
        health_by_name=health_by_name,
        total_new=len(new_listing_ids),
        total_ranked=len(scored),
        alerts=alerts,
    )

    finished_at = now_fn()
    with conn:
        run_repo.finish_run(
            run_id=run_identifier,
            finished_at=finished_at,
            counts_json=counts_json,
        )

    return WatchRunReport(
        run_id=run_identifier,
        started_at=started_at,
        finished_at=finished_at,
        source_reports=source_reports,
        alerts=alerts,
    )


def _collect_sources(
    *,
    context: SearchContext,
    sources: tuple[Source, ...],
    source_metadata: Mapping[str, dict[str, Any]],
    max_workers: int | None,
) -> tuple[_SourceSnapshot, ...]:
    del source_metadata
    if not sources:
        return ()

    worker_count = max_workers or min(8, len(sources))
    snapshots: list[_SourceSnapshot] = []
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                _collect_source_snapshot,
                source=source,
                context=context,
            ): source
            for source in sources
        }
        for future in as_completed(futures):
            snapshots.append(future.result())
    snapshots.sort(key=lambda item: item.name)
    return tuple(snapshots)


def _collect_source_snapshot(*, source: Source, context: SearchContext) -> _SourceSnapshot:
    try:
        discovered = list(source.discover(context))
    except Exception as exc:  # noqa: BLE001 - isolate source-level failures
        return _SourceSnapshot(
            name=source.name,
            status=Liveness.FAILED.value,
            records_seen=0,
            listings=(),
            candidate_watermark=None,
            error=str(exc),
        )

    status = Liveness.OK.value
    persistable: list[_PersistableListing] = []
    max_fetched_at: datetime | None = None
    source_error: str | None = None

    for record in discovered:
        if max_fetched_at is None or record.fetched_at > max_fetched_at:
            max_fetched_at = record.fetched_at
        try:
            detailed = source.fetch_detail(record)
        except Exception as exc:  # noqa: BLE001 - isolate per-listing failures
            status = _merge_status(status, Liveness.DEGRADED.value)
            source_error = str(exc)
            continue

        liveness = source.check_liveness(detailed).value
        status = _merge_status(status, liveness)
        try:
            listing = source.to_listing(detailed, context)
        except Exception as exc:  # noqa: BLE001 - isolate per-listing failures
            status = _merge_status(status, Liveness.DEGRADED.value)
            source_error = str(exc)
            continue
        persistable.append(
            _PersistableListing(
                source_name=source.name,
                record=detailed,
                listing=listing,
            )
        )

    return _SourceSnapshot(
        name=source.name,
        status=status,
        records_seen=len(discovered),
        listings=tuple(persistable),
        candidate_watermark=max_fetched_at.isoformat() if max_fetched_at else None,
        error=source_error,
    )


def _persist_stage(
    *,
    conn: sqlite3.Connection,
    listings: tuple[_PersistableListing, ...],
) -> set[str]:
    listing_repo = ListingRepo(conn)
    observation_repo = ObservationRepo(conn, listing_repo=listing_repo)

    listing_ids = tuple(item.listing.identity.listing_id for item in listings)
    existing_ids = _existing_listing_ids(conn=conn, listing_ids=listing_ids)
    seen_new_ids: set[str] = set()

    with conn:
        for item in listings:
            listing_id = item.listing.identity.listing_id
            if listing_id not in existing_ids:
                seen_new_ids.add(listing_id)
            listing_repo.upsert_listing_source(
                listing_id=listing_id,
                source=item.listing.identity.source,
                source_id=item.listing.identity.source_id,
                signature=item.listing.identity.signature,
                seen_at=item.record.fetched_at,
            )
            listing_repo.add_source_record(listing_id=listing_id, record=item.record)
            for field_name, observed in _observed_fields(item.listing).items():
                observation_repo.record_observation(
                    listing_id=listing_id,
                    field=field_name,
                    value_json=_json_ready(observed.value),
                    origin=observed.origin,
                    confidence=observed.confidence,
                    evidence=observed.evidence,
                    observed_at=observed.observed_at,
                    schema_version=item.listing.schema_version,
                )

    return seen_new_ids


def _enrich_stage(
    *,
    conn: sqlite3.Connection,
    context: SearchContext,
    listings: tuple[_PersistableListing, ...],
    enrichers: tuple[Any, ...],
) -> tuple[_PersistableListing, ...]:
    if not listings or not enrichers:
        return listings

    listing_repo = ListingRepo(conn)
    observation_repo = ObservationRepo(conn, listing_repo=listing_repo)
    enriched: list[_PersistableListing] = []

    with conn:
        for item in listings:
            updated_listing = run_enricher_chain(item.listing, enrichers, context)
            before = _observed_fields(item.listing)
            after = _observed_fields(updated_listing)
            for field_name, observed in after.items():
                if before.get(field_name) == observed:
                    continue
                observation_repo.record_observation(
                    listing_id=updated_listing.identity.listing_id,
                    field=field_name,
                    value_json=_json_ready(observed.value),
                    origin=observed.origin,
                    confidence=observed.confidence,
                    evidence=observed.evidence,
                    observed_at=observed.observed_at,
                    schema_version=updated_listing.schema_version,
                )
            enriched.append(
                _PersistableListing(
                    source_name=item.source_name,
                    record=item.record,
                    listing=updated_listing,
                )
            )
    return tuple(enriched)


def _rank_stage(
    *,
    conn: sqlite3.Connection,
    context: SearchContext,
    listings: tuple[_PersistableListing, ...],
    rank_engine: RankEngine,
    profile_id: str,
) -> tuple[tuple[_PersistableListing, ScoreResult], ...]:
    score_repo = ScoreRepo(conn)
    scored: list[tuple[_PersistableListing, ScoreResult]] = []

    with conn:
        for item in listings:
            result = rank_engine.score_listing(item.listing, context=context)
            score_repo.upsert_score(
                listing_id=item.listing.identity.listing_id,
                profile_id=profile_id,
                score=result.score,
                breakdown_json=_score_breakdown(result),
                computed_at=item.record.fetched_at,
            )
            scored.append((item, result))
    scored.sort(key=lambda pair: pair[1].score, reverse=True)
    return tuple(scored)


def _notify(
    *,
    notifier: Notifier,
    alerts: tuple[str, ...],
    scored: tuple[tuple[_PersistableListing, ScoreResult], ...],
    new_listing_ids: set[str],
) -> None:
    if alerts:
        notifier.send(title="Nostos watch alert", body="\n".join(alerts))

    new_scored = [item for item in scored if item[0].listing.identity.listing_id in new_listing_ids]
    if not new_scored:
        return

    lines = ["New listings detected:"]
    for persistable, result in new_scored[:10]:
        lines.append(
            f"- {persistable.listing.identity.listing_id}: {result.score:.1f}/100 "
            f"({persistable.listing.identity.url})"
        )
    notifier.send(title="Nostos new listings", body="\n".join(lines))


def _build_source_reports(
    *,
    snapshots: tuple[_SourceSnapshot, ...],
    health_by_name: Mapping[str, SourceHealthDecision],
) -> dict[str, SourceRunReport]:
    reports: dict[str, SourceRunReport] = {}
    for snapshot in snapshots:
        decision = health_by_name[snapshot.name]
        reports[snapshot.name] = SourceRunReport(
            name=snapshot.name,
            status=snapshot.status,
            count=snapshot.count,
            load_bearing=decision.load_bearing,
            within_baseline_band=decision.baseline.within_band,
            watermark_advanced=decision.watermark.advanced,
            effective_watermark=decision.watermark.effective,
            candidate_watermark=decision.watermark.candidate,
            error=snapshot.error,
        )
    return reports


def _build_counts_json(
    *,
    snapshots: tuple[_SourceSnapshot, ...],
    health_by_name: Mapping[str, SourceHealthDecision],
    total_new: int,
    total_ranked: int,
    alerts: tuple[str, ...],
) -> dict[str, JSONValue]:
    by_source: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        decision = health_by_name[snapshot.name]
        payload: dict[str, Any] = {
            "status": snapshot.status,
            "count": snapshot.count,
            "records_seen": snapshot.records_seen,
            "load_bearing": decision.load_bearing,
            "baseline": {
                "value": decision.baseline.baseline,
                "lower": decision.baseline.lower,
                "upper": decision.baseline.upper,
                "sample_size": decision.baseline.sample_size,
                "within_band": decision.baseline.within_band,
            },
            "watermark": {
                "previous": decision.watermark.previous,
                "candidate": decision.watermark.candidate,
                "effective": decision.watermark.effective,
                "advanced": decision.watermark.advanced,
            },
        }
        if snapshot.error:
            payload["error"] = snapshot.error
        by_source[snapshot.name] = payload

    return cast(
        dict[str, JSONValue],
        {
        "sources": by_source,
        "totals": {"ranked": total_ranked, "new_listings": total_new},
        "alerts": list(alerts),
        },
    )


def _source_metadata(
    *,
    context: SearchContext,
    sources: tuple[Source, ...],
) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    for source in sources:
        citypack_cfg = context.citypack.sources.get(source.name)
        metadata[source.name] = {
            "enabled": bool(context.profile.sources.get(source.name, False)),
            "load_bearing": bool(citypack_cfg.load_bearing) if citypack_cfg else False,
        }
    return metadata


def _existing_listing_ids(*, conn: sqlite3.Connection, listing_ids: tuple[str, ...]) -> set[str]:
    if not listing_ids:
        return set()
    placeholders = ",".join("?" for _ in listing_ids)
    query = f"SELECT id FROM listing WHERE id IN ({placeholders})"
    rows = conn.execute(query, listing_ids).fetchall()
    return {str(row[0]) for row in rows}


def _observed_fields(listing: Listing) -> dict[str, Observed[Any]]:
    fields: dict[str, Observed[Any]] = {}
    for field_name in ("rent", "beds", "baths", "area", "floor", "parking", "furnishing"):
        value = getattr(listing, field_name)
        if isinstance(value, Observed):
            fields[field_name] = value
    for key, field in listing.attributes.items():
        if isinstance(field, Observed):
            fields[f"attributes.{key}"] = field
    return fields


def _score_breakdown(result: ScoreResult) -> dict[str, Any]:
    return {
        "score": result.score,
        "total_contribution": result.total_contribution,
        "normalization": {
            "min_possible": result.normalization.min_possible,
            "max_possible": result.normalization.max_possible,
        },
        "contributions": [_contribution_to_json(item) for item in result.contributions],
    }


def _contribution_to_json(item: RuleContribution) -> dict[str, Any]:
    weight_value = (
        item.weight.model_dump(mode="json")
        if isinstance(item.weight, BaseModel)
        else item.weight
    )
    signal_payload: dict[str, Any] | None = None
    if item.signal is not None:
        signal_payload = {
            "fired": item.signal.fired,
            "magnitude": item.signal.magnitude,
            "confidence": item.signal.confidence,
            "evidence": item.signal.evidence,
        }
    return {
        "rule_key": item.rule_key,
        "category": item.category,
        "label": item.label,
        "weight": weight_value,
        "signal": signal_payload,
        "shaped_magnitude": item.shaped_magnitude,
        "confidence_factor": item.confidence_factor,
        "min_possible": item.min_possible,
        "max_possible": item.max_possible,
        "contribution": item.contribution,
    }


def _json_ready(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(item) for item in value]
    return str(value)


def _next_run_id(run_id_factory: Callable[[], str] | None) -> str:
    if run_id_factory is not None:
        return run_id_factory()
    return f"run-{uuid.uuid4().hex}"


def _merge_status(current: str, incoming: str) -> str:
    order = {
        Liveness.OK.value: 0,
        Liveness.DEGRADED.value: 1,
        Liveness.FAILED.value: 2,
    }
    return incoming if order.get(incoming, 0) > order.get(current, 0) else current


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)
