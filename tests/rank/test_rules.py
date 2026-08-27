from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from nostos.model import (
    Absence,
    Identity,
    Listing,
    Observed,
    Origin,
    Place,
    SourceRecordRef,
)
from nostos.rank.rules import DEFAULT_REGISTRY, RuleRegistry, Signal

OBSERVED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_rule_registry_decorator_registers_rule() -> None:
    registry = RuleRegistry()

    @registry.rule("pets.allowed", category="amenities", label="Pet friendly")
    def detect_pets(_: object, __: object) -> Signal:
        return Signal(fired=True, magnitude=1.0, confidence=0.9, evidence="Pets allowed")

    registered = registry.get("pets.allowed")
    assert registered is not None
    assert registered.key == "pets.allowed"
    assert registered.category == "amenities"
    assert registered.label == "Pet friendly"
    signal = registered.detector(cast(Listing, object()), object())
    assert signal is not None
    assert signal.fired is True


def test_rule_registry_rejects_duplicate_keys() -> None:
    registry = RuleRegistry()

    @registry.rule("parking.available", category="amenities")
    def first(_: object, __: object) -> Signal:
        return Signal(fired=True, magnitude=1.0, confidence=1.0)

    with pytest.raises(ValueError, match="already registered"):

        @registry.rule("parking.available", category="amenities")
        def second(_: object, __: object) -> Signal:
            return Signal(fired=False, magnitude=0.0, confidence=1.0)


def _make_listing(
    *,
    title: str = "",
    description: str = "",
    raw_address: str = "123 Main St, Vancouver, BC",
) -> Listing:
    attributes: dict[str, object] = {}
    if title:
        attributes["title"] = Observed[str](
            value=title,
            origin=Origin.SOURCE_FIELD,
            confidence=1.0,
            evidence="title",
            observed_at=OBSERVED_AT,
        )
    if description:
        attributes["description"] = Observed[str](
            value=description,
            origin=Origin.SOURCE_FIELD,
            confidence=1.0,
            evidence="description",
            observed_at=OBSERVED_AT,
        )
    return Listing(
        identity=Identity(
            listing_id="listing-1",
            source="stub",
            source_id="stub-1",
            url="https://example.test/listing-1",
            signature="sig-1",
        ),
        place=Place(raw_address=raw_address),
        rent=Absence.NOT_STATED,
        beds=Absence.NOT_STATED,
        baths=Absence.NOT_STATED,
        area=Absence.NOT_STATED,
        floor=Absence.NOT_STATED,
        parking=Absence.NOT_STATED,
        furnishing=Absence.NOT_STATED,
        photos=[],
        attributes=cast(dict[str, Any], attributes),
        raw_ref=SourceRecordRef(
            source="stub",
            source_id="stub-1",
            url="https://example.test/listing-1",
            content_hash="hash-1",
            fetched_at=OBSERVED_AT,
        ),
        schema_version=1,
    )


def _detect(rule_key: str, listing: Listing) -> Signal | None:
    entry = DEFAULT_REGISTRY.get(rule_key)
    assert entry is not None
    return entry.detector(listing, object())


def test_laundry_negation_counts_as_in_suite_and_blocks_building_signal() -> None:
    listing = _make_listing(description="No shared laundry room; private laundry in unit.")

    in_suite = _detect("laundry.in_suite", listing)
    building = _detect("laundry.building", listing)

    assert in_suite is not None
    assert in_suite.fired is True
    assert in_suite.evidence == "No shared laundry"
    assert in_suite.magnitude == pytest.approx(1.0)
    assert building is None


def test_shared_laundry_phrase_fires_building_detector() -> None:
    listing = _make_listing(description="Shared laundry in the building.")

    building = _detect("laundry.building", listing)
    in_suite = _detect("laundry.in_suite", listing)

    assert building is not None
    assert building.fired is True
    assert building.evidence == "Shared laundry"
    assert building.magnitude == pytest.approx(1.0)
    assert in_suite is None


def test_floor_detector_infers_floor_from_unit_number_marker() -> None:
    listing = _make_listing(title="#1408 - 938 Smithe St")

    floor = _detect("floor.low", listing)

    assert floor is not None
    assert floor.fired is True
    assert floor.evidence == "#1408"
    assert floor.magnitude == pytest.approx(14.0)


def test_floor_detector_does_not_treat_street_number_as_unit_number() -> None:
    listing = _make_listing(raw_address="2744 West 10th Ave, Vancouver, BC")

    floor = _detect("floor.low", listing)

    assert floor is None


def test_walk_score_detector_returns_numeric_signal_with_evidence() -> None:
    listing = _make_listing(description="Walk Score: 88/100. Transit Score: 74.")

    walk_score = _detect("walk.score", listing)

    assert walk_score is not None
    assert walk_score.fired is True
    assert walk_score.evidence == "Walk Score: 88"
    assert walk_score.magnitude == pytest.approx(88.0)


def test_den_or_solarium_detector_fires_with_matched_phrase() -> None:
    listing = _make_listing(title="2 bedroom + den in Kits")

    den = _detect("space.den_or_solarium", listing)

    assert den is not None
    assert den.fired is True
    assert den.evidence == "den"
    assert den.magnitude == pytest.approx(1.0)


def test_walkable_phrase_detector_wins_when_both_phrase_types_exist() -> None:
    listing = _make_listing(
        description=(
            "Walking distance to shops and cafes, on a quiet neighborhood side street."
        )
    )

    walkable = _detect("density.walkable", listing)
    sparse = _detect("density.sparse", listing)

    assert walkable is not None
    assert walkable.fired is True
    assert walkable.evidence == "Walking distance"
    assert walkable.magnitude == pytest.approx(1.0)
    assert sparse is None


def test_sparse_phrase_detector_fires_without_walkable_phrase() -> None:
    listing = _make_listing(description="Quiet neighborhood on a tree-lined street.")

    sparse = _detect("density.sparse", listing)
    walkable = _detect("density.walkable", listing)

    assert sparse is not None
    assert sparse.fired is True
    assert sparse.evidence == "Quiet neighborhood"
    assert sparse.magnitude == pytest.approx(1.0)
    assert walkable is None
