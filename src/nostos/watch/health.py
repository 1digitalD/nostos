from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from statistics import median
from typing import Any


@dataclass(frozen=True, slots=True)
class HealthPolicy:
    history_window: int = 5
    minimum_baseline_samples: int = 3
    lower_ratio: float = 0.6
    upper_ratio: float = 1.6
    absolute_lower_bound: int = 1


@dataclass(frozen=True, slots=True)
class SourceHistory:
    counts: tuple[int, ...]
    watermark: str | None


@dataclass(frozen=True, slots=True)
class SourceHealthInput:
    name: str
    status: str
    count: int
    load_bearing: bool
    candidate_watermark: str | None


@dataclass(frozen=True, slots=True)
class BaselineBand:
    baseline: float | None
    lower: int | None
    upper: int | None
    sample_size: int
    within_band: bool


@dataclass(frozen=True, slots=True)
class WatermarkDecision:
    previous: str | None
    candidate: str | None
    effective: str | None
    advanced: bool


@dataclass(frozen=True, slots=True)
class SourceHealthDecision:
    name: str
    status: str
    count: int
    load_bearing: bool
    baseline: BaselineBand
    watermark: WatermarkDecision
    alerts: tuple[str, ...]


def load_source_histories(
    conn: sqlite3.Connection,
    *,
    source_names: Iterable[str],
    history_limit: int = 30,
) -> dict[str, SourceHistory]:
    names = tuple(dict.fromkeys(source_names))
    if not names:
        return {}

    counts_by_source: dict[str, list[int]] = {name: [] for name in names}
    watermark_by_source: dict[str, str | None] = {name: None for name in names}

    rows = conn.execute(
        """
        SELECT counts_json
        FROM run
        WHERE finished_at IS NOT NULL
        ORDER BY finished_at DESC
        LIMIT ?
        """,
        (history_limit,),
    ).fetchall()

    for row in rows:
        raw_counts = row["counts_json"]
        if not isinstance(raw_counts, str):
            continue
        payload = _decode_counts_json(raw_counts)
        if payload is None:
            continue
        for name in names:
            source_payload = _extract_source_payload(payload, source_name=name)
            if source_payload is None:
                continue
            count = _extract_count(source_payload)
            if count is not None:
                counts_by_source[name].append(count)
            if watermark_by_source[name] is None:
                watermark_by_source[name] = _extract_effective_watermark(source_payload)

    return {
        name: SourceHistory(
            counts=tuple(counts_by_source[name]),
            watermark=watermark_by_source[name],
        )
        for name in names
    }


def evaluate_sources(
    *,
    sources: Iterable[SourceHealthInput],
    history_by_source: Mapping[str, SourceHistory],
    policy: HealthPolicy | None = None,
) -> tuple[SourceHealthDecision, ...]:
    active_policy = policy or HealthPolicy()
    return tuple(
        evaluate_source(
            source=source,
            history=history_by_source.get(source.name, SourceHistory(counts=(), watermark=None)),
            policy=active_policy,
        )
        for source in sources
    )


def evaluate_source(
    *,
    source: SourceHealthInput,
    history: SourceHistory,
    policy: HealthPolicy,
) -> SourceHealthDecision:
    baseline = _baseline_band(current_count=source.count, history=history, policy=policy)
    can_advance = source.status != "failed" and baseline.within_band
    candidate = source.candidate_watermark
    previous = history.watermark
    advanced = bool(candidate) and can_advance
    effective = candidate if advanced else previous

    alerts: list[str] = []
    if source.load_bearing and source.count == 0:
        alerts.append(
            f"{source.name}: load-bearing source returned zero listings; review source health."
        )

    return SourceHealthDecision(
        name=source.name,
        status=source.status,
        count=source.count,
        load_bearing=source.load_bearing,
        baseline=baseline,
        watermark=WatermarkDecision(
            previous=previous,
            candidate=candidate,
            effective=effective,
            advanced=advanced,
        ),
        alerts=tuple(alerts),
    )


def _baseline_band(
    *,
    current_count: int,
    history: SourceHistory,
    policy: HealthPolicy,
) -> BaselineBand:
    samples = history.counts[: policy.history_window]
    if len(samples) < policy.minimum_baseline_samples:
        return BaselineBand(
            baseline=None,
            lower=None,
            upper=None,
            sample_size=len(samples),
            within_band=True,
        )

    baseline_value = float(median(samples))
    if baseline_value <= 0:
        return BaselineBand(
            baseline=baseline_value,
            lower=0,
            upper=0,
            sample_size=len(samples),
            within_band=current_count == 0,
        )

    lower = max(policy.absolute_lower_bound, int(math.floor(baseline_value * policy.lower_ratio)))
    upper = max(lower, int(math.ceil(baseline_value * policy.upper_ratio)))
    return BaselineBand(
        baseline=baseline_value,
        lower=lower,
        upper=upper,
        sample_size=len(samples),
        within_band=lower <= current_count <= upper,
    )


def _decode_counts_json(raw_counts: str) -> dict[str, Any] | None:
    try:
        decoded = json.loads(raw_counts)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def _extract_source_payload(
    counts_payload: Mapping[str, Any],
    *,
    source_name: str,
) -> Any | None:
    nested = counts_payload.get("sources")
    if isinstance(nested, Mapping) and source_name in nested:
        return nested[source_name]
    if source_name in counts_payload:
        return counts_payload[source_name]
    return None


def _extract_count(source_payload: Any) -> int | None:
    if isinstance(source_payload, bool):
        return None
    if isinstance(source_payload, int):
        return max(source_payload, 0)
    if isinstance(source_payload, float):
        return max(int(source_payload), 0)
    if isinstance(source_payload, Mapping):
        candidate = source_payload.get("count")
        if isinstance(candidate, bool):
            return None
        if isinstance(candidate, int):
            return max(candidate, 0)
        if isinstance(candidate, float):
            return max(int(candidate), 0)
    return None


def _extract_effective_watermark(source_payload: Any) -> str | None:
    if isinstance(source_payload, Mapping):
        watermark = source_payload.get("watermark")
        if isinstance(watermark, str):
            return watermark
        if isinstance(watermark, Mapping):
            effective = watermark.get("effective")
            if isinstance(effective, str):
                return effective
            current = watermark.get("current")
            if isinstance(current, str):
                return current
    return None
