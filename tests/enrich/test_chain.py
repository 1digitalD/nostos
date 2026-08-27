from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from nostos.enrich.base import CostModel
from nostos.enrich.budget import BudgetState, UncappedSpendError
from nostos.enrich.chain import run_enricher_chain
from nostos.model.identity import Identity
from nostos.model.listing import Absence, Field, Listing, Observed, Origin
from nostos.model.source_record import SourceRecordRef
from nostos.model.value import Place

OBSERVED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@dataclass
class StubEnricher:
    name: str
    provides: frozenset[str]
    requires: frozenset[str]
    cost: CostModel
    origin: Origin
    confidence: float
    produced: dict[str, Observed[Any]]
    estimate: Decimal
    call_log: list[str] = field(default_factory=list)

    def estimate_cost(self, listing: Listing, context: object) -> Decimal:
        del listing, context
        return self.estimate

    def enrich(self, listing: Listing, context: object) -> dict[str, Observed[Any]]:
        del listing, context
        self.call_log.append(self.name)
        return self.produced


def make_listing(*, beds: Field[float]) -> Listing:
    return Listing(
        identity=Identity(
            listing_id="listing-1",
            source="stub",
            source_id="stub-1",
            url="https://example.test/listing-1",
            signature="sig-1",
        ),
        place=Place(),
        rent=Absence.NOT_STATED,
        beds=beds,
        baths=Absence.NOT_STATED,
        area=Absence.NOT_STATED,
        floor=Absence.NOT_STATED,
        parking=Absence.NOT_STATED,
        furnishing=Absence.NOT_STATED,
        photos=[],
        raw_ref=SourceRecordRef(
            source="stub",
            source_id="stub-1",
            url="https://example.test/listing-1",
            content_hash="hash-1",
            fetched_at=OBSERVED_AT,
        ),
        schema_version=1,
    )


def observed_float(value: float, *, origin: Origin, confidence: float) -> Observed[float]:
    return Observed[float](
        value=value,
        origin=origin,
        confidence=confidence,
        evidence=None,
        observed_at=OBSERVED_AT,
    )


def test_chain_orders_enrichers_by_requires_before_running() -> None:
    listing = make_listing(beds=Absence.NOT_STATED)
    call_log: list[str] = []
    enrich_beds = StubEnricher(
        name="beds",
        provides=frozenset({"beds"}),
        requires=frozenset(),
        cost=CostModel.FREE,
        origin=Origin.TEXT_RULE,
        confidence=0.6,
        produced={"beds": observed_float(1.0, origin=Origin.TEXT_RULE, confidence=0.6)},
        estimate=Decimal("0"),
        call_log=call_log,
    )
    enrich_floor = StubEnricher(
        name="floor",
        provides=frozenset({"floor"}),
        requires=frozenset({"beds"}),
        cost=CostModel.FREE,
        origin=Origin.DETAIL_PAGE,
        confidence=0.8,
        produced={"floor": observed_float(3.0, origin=Origin.DETAIL_PAGE, confidence=0.8)},
        estimate=Decimal("0"),
        call_log=call_log,
    )

    updated = run_enricher_chain(listing, [enrich_floor, enrich_beds], context={})

    assert call_log == ["beds", "floor"]
    assert isinstance(updated.beds, Observed)
    assert isinstance(updated.floor, Observed)


def test_chain_skips_when_field_is_known_at_higher_precedence() -> None:
    listing = make_listing(
        beds=observed_float(2.0, origin=Origin.SOURCE_FIELD, confidence=0.95),
    )
    call_log: list[str] = []
    text_enricher = StubEnricher(
        name="text-beds",
        provides=frozenset({"beds"}),
        requires=frozenset(),
        cost=CostModel.FREE,
        origin=Origin.TEXT_RULE,
        confidence=0.7,
        produced={"beds": observed_float(3.0, origin=Origin.TEXT_RULE, confidence=0.7)},
        estimate=Decimal("0"),
        call_log=call_log,
    )

    updated = run_enricher_chain(listing, [text_enricher], context={})

    assert call_log == []
    assert updated == listing


def test_chain_skips_when_equal_precedence_has_equal_or_better_confidence() -> None:
    listing = make_listing(
        beds=observed_float(2.0, origin=Origin.TEXT_RULE, confidence=0.9),
    )
    call_log: list[str] = []
    text_enricher = StubEnricher(
        name="text-beds",
        provides=frozenset({"beds"}),
        requires=frozenset(),
        cost=CostModel.FREE,
        origin=Origin.TEXT_RULE,
        confidence=0.8,
        produced={"beds": observed_float(3.0, origin=Origin.TEXT_RULE, confidence=0.8)},
        estimate=Decimal("0"),
        call_log=call_log,
    )

    run_enricher_chain(listing, [text_enricher], context={})

    assert call_log == []


def test_chain_runs_when_equal_precedence_has_worse_confidence() -> None:
    listing = make_listing(
        beds=observed_float(2.0, origin=Origin.TEXT_RULE, confidence=0.5),
    )
    call_log: list[str] = []
    text_enricher = StubEnricher(
        name="text-beds",
        provides=frozenset({"beds"}),
        requires=frozenset(),
        cost=CostModel.FREE,
        origin=Origin.TEXT_RULE,
        confidence=0.8,
        produced={"beds": observed_float(3.0, origin=Origin.TEXT_RULE, confidence=0.8)},
        estimate=Decimal("0"),
        call_log=call_log,
    )

    updated = run_enricher_chain(listing, [text_enricher], context={})

    assert call_log == ["text-beds"]
    assert isinstance(updated.beds, Observed)
    assert updated.beds.value == 3.0


def test_non_free_enricher_refuses_to_run_without_budget_cap() -> None:
    listing = make_listing(beds=Absence.NOT_STATED)
    paid_enricher = StubEnricher(
        name="paid-beds",
        provides=frozenset({"beds"}),
        requires=frozenset(),
        cost=CostModel.PER_CALL,
        origin=Origin.TEXT_RULE,
        confidence=0.7,
        produced={"beds": observed_float(2.0, origin=Origin.TEXT_RULE, confidence=0.7)},
        estimate=Decimal("0.25"),
    )

    with pytest.raises(UncappedSpendError):
        run_enricher_chain(listing, [paid_enricher], context={})


def test_non_free_enricher_runs_with_estimate_confirm_and_cap() -> None:
    listing = make_listing(beds=Absence.NOT_STATED)
    paid_enricher = StubEnricher(
        name="paid-beds",
        provides=frozenset({"beds"}),
        requires=frozenset(),
        cost=CostModel.PER_CALL,
        origin=Origin.TEXT_RULE,
        confidence=0.7,
        produced={"beds": observed_float(2.0, origin=Origin.TEXT_RULE, confidence=0.7)},
        estimate=Decimal("0.25"),
    )
    confirms: list[Decimal] = []

    def confirm_once(estimate: Decimal) -> bool:
        confirms.append(estimate)
        return True

    budget = BudgetState(cap=Decimal("1.00"), confirm=confirm_once)
    updated = run_enricher_chain(listing, [paid_enricher], context={}, budget=budget)

    assert confirms == [Decimal("0.25")]
    assert budget.spent == Decimal("0.25")
    assert isinstance(updated.beds, Observed)
