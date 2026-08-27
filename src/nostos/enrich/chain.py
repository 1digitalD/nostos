from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from nostos.enrich.base import Enricher
from nostos.enrich.budget import BudgetState, estimate_confirm_cap
from nostos.model import Absence, Field, Listing, Observed, Origin, merge_field


def topological_enrichers(enrichers: Iterable[Enricher]) -> list[Enricher]:
    indexed = list(enrichers)
    field_providers: dict[str, list[int]] = defaultdict(list)
    for idx, enricher in enumerate(indexed):
        for provided in enricher.provides:
            field_providers[provided].append(idx)

    outgoing_edges: dict[int, set[int]] = {idx: set() for idx in range(len(indexed))}
    incoming_degree = [0] * len(indexed)
    for consumer_idx, enricher in enumerate(indexed):
        dependencies: set[int] = set()
        for required in enricher.requires:
            dependencies.update(field_providers.get(required, ()))
        dependencies.discard(consumer_idx)
        for dependency_idx in dependencies:
            if consumer_idx not in outgoing_edges[dependency_idx]:
                outgoing_edges[dependency_idx].add(consumer_idx)
                incoming_degree[consumer_idx] += 1

    remaining = set(range(len(indexed)))
    ordered: list[Enricher] = []
    while remaining:
        ready = sorted(idx for idx in remaining if incoming_degree[idx] == 0)
        if not ready:
            raise ValueError("Enricher requires/provides graph contains a cycle")
        current_idx = ready[0]
        remaining.remove(current_idx)
        ordered.append(indexed[current_idx])
        for downstream_idx in outgoing_edges[current_idx]:
            incoming_degree[downstream_idx] -= 1
    return ordered


def run_enricher_chain(
    listing: Listing,
    enrichers: Iterable[Enricher],
    context: object,
    *,
    budget: BudgetState | None = None,
) -> Listing:
    current = listing
    for enricher in topological_enrichers(enrichers):
        if not _requirements_met(current, enricher.requires):
            continue
        if _provides_already_known(
            current,
            enricher.provides,
            enricher.origin,
            enricher.confidence,
        ):
            continue
        estimate_confirm_cap(enricher, current, context, budget)
        updates = enricher.enrich(current, context)
        for field_name in sorted(updates):
            current = _apply_observed(current, field_name, updates[field_name])
    return current


def _requirements_met(listing: Listing, required_fields: frozenset[str]) -> bool:
    for required in required_fields:
        if not isinstance(_read_field(listing, required), Observed):
            return False
    return True


def _provides_already_known(
    listing: Listing,
    provided_fields: frozenset[str],
    incoming_origin: Origin,
    incoming_confidence: float,
) -> bool:
    if not provided_fields:
        return False
    for field_name in provided_fields:
        existing = _read_field(listing, field_name)
        if not isinstance(existing, Observed):
            return False
        higher_precedence = existing.origin.precedence > incoming_origin.precedence
        equal_precedence = existing.origin.precedence == incoming_origin.precedence
        equal_or_better_confidence = existing.confidence >= incoming_confidence
        if not (higher_precedence or (equal_precedence and equal_or_better_confidence)):
            return False
    return True


def _read_field(listing: Listing, field_name: str) -> Field[Any] | None:
    if field_name.startswith("attributes."):
        key = field_name.removeprefix("attributes.")
        return listing.attributes.get(key)
    if "." in field_name:
        raise ValueError(f"Unsupported enricher field path: {field_name!r}")
    if not hasattr(listing, field_name):
        raise ValueError(f"Unknown listing field: {field_name!r}")

    value = getattr(listing, field_name)
    if not isinstance(value, (Observed, Absence)):
        raise ValueError(f"Field {field_name!r} is not an enrichable Field")
    return value


def _apply_observed(listing: Listing, field_name: str, observed: Observed[Any]) -> Listing:
    if field_name.startswith("attributes."):
        key = field_name.removeprefix("attributes.")
        attributes = dict(listing.attributes)
        existing = attributes.get(key)
        attributes[key] = merge_field(existing, observed) if existing is not None else observed
        return listing.model_copy(update={"attributes": attributes})

    existing = _read_field(listing, field_name)
    if existing is None:
        raise ValueError(f"Cannot write missing field {field_name!r}")
    merged = merge_field(existing, observed)
    return listing.model_copy(update={field_name: merged})
