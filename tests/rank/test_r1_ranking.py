from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from nostos.config.profile import Profile
from nostos.model import (
    Absence,
    Area,
    Identity,
    Listing,
    Money,
    Observed,
    Origin,
    Place,
    SourceRecordRef,
)
from nostos.rank.engine import RankEngine
from nostos.rank.rules import DEFAULT_REGISTRY, Signal

OBSERVED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _Context:
    profile: Profile


def _profile(
    *,
    weights: Mapping[str, object],
    hard: Mapping[str, object] | None = None,
    area_key_weights: Mapping[str, float] | None = None,
) -> Profile:
    payload: dict[str, object] = {
        "city": "vancouver",
        "weights": dict(weights),
        "schedule": "0 */6 * * *",
    }
    if hard is not None:
        payload["hard"] = dict(hard)
    if area_key_weights is not None:
        payload["area_key_weights"] = dict(area_key_weights)
    return Profile.model_validate(payload)


def _make_listing(
    *,
    listing_id: str = "listing-1",
    title: str = "",
    description: str = "",
    area_key: str | None = None,
    rent_amount: int | None = None,
    area_sqft: int | None = None,
    floor: int | None = None,
    parking: str | None = None,
    pet_policy: str | None = None,
) -> Listing:
    attributes: dict[str, Observed[Any] | Absence] = {}
    if title:
        attributes["title"] = _observed_text(title, evidence="title")
    if description:
        attributes["description"] = _observed_text(description, evidence="description")
    if pet_policy is not None:
        attributes["pet_policy"] = Observed[str](
            value=pet_policy,
            origin=Origin.SOURCE_FIELD,
            confidence=1.0,
            evidence="pet policy attribute",
            observed_at=OBSERVED_AT,
        )

    rent_field = (
        Observed[Money](
            value=Money(amount=Decimal(str(rent_amount)), currency="CAD", period="month"),
            origin=Origin.SOURCE_FIELD,
            confidence=1.0,
            evidence="rent field",
            observed_at=OBSERVED_AT,
        )
        if rent_amount is not None
        else Absence.NOT_STATED
    )
    area_field = (
        Observed[Area](
            value=Area(value=float(area_sqft), unit="sqft"),
            origin=Origin.SOURCE_FIELD,
            confidence=1.0,
            evidence="area field",
            observed_at=OBSERVED_AT,
        )
        if area_sqft is not None
        else Absence.NOT_STATED
    )
    floor_field = (
        Observed[int](
            value=floor,
            origin=Origin.SOURCE_FIELD,
            confidence=1.0,
            evidence="floor field",
            observed_at=OBSERVED_AT,
        )
        if floor is not None
        else Absence.NOT_STATED
    )
    parking_field = (
        Observed[str](
            value=parking,
            origin=Origin.SOURCE_FIELD,
            confidence=1.0,
            evidence="parking field",
            observed_at=OBSERVED_AT,
        )
        if parking is not None
        else Absence.NOT_STATED
    )

    return Listing(
        identity=Identity(
            listing_id=listing_id,
            source="stub",
            source_id=listing_id,
            url=f"https://example.test/{listing_id}",
            signature=f"sig-{listing_id}",
        ),
        place=Place(raw_address="123 Main St, Vancouver, BC", area_key=area_key),
        rent=rent_field,
        beds=Absence.NOT_STATED,
        baths=Absence.NOT_STATED,
        area=area_field,
        floor=floor_field,
        parking=parking_field,
        furnishing=Absence.NOT_STATED,
        photos=[],
        attributes=attributes,
        raw_ref=SourceRecordRef(
            source="stub",
            source_id=listing_id,
            url=f"https://example.test/{listing_id}",
            content_hash=f"hash-{listing_id}",
            fetched_at=OBSERVED_AT,
        ),
        schema_version=1,
    )


def _observed_text(value: str, *, evidence: str) -> Observed[str]:
    return Observed[str](
        value=value,
        origin=Origin.SOURCE_FIELD,
        confidence=1.0,
        evidence=evidence,
        observed_at=OBSERVED_AT,
    )


def _detect(rule_key: str, listing: Listing, profile: Profile | None = None) -> Signal | None:
    rule = DEFAULT_REGISTRY.get(rule_key)
    assert rule is not None
    context: object = object() if profile is None else _Context(profile=profile)
    return rule.detector(listing, context)


def test_parking_detector_fires_for_positive_text_without_negation_false_positive() -> None:
    positive = _make_listing(description="Comes with one underground parking stall included.")
    positive_signal = _detect("parking.available", positive)
    assert positive_signal is not None
    assert positive_signal.fired is True
    assert positive_signal.magnitude == pytest.approx(1.0)
    assert positive_signal.evidence is not None
    assert "parking" in positive_signal.evidence.lower()

    negated = _make_listing(description="No parking available for this unit.")
    negated_signal = _detect("parking.available", negated)
    assert negated_signal is None

    missing = _make_listing()
    missing_signal = _detect("parking.available", missing)
    assert missing_signal is None


def test_pets_detector_distinguishes_friendly_conditional_and_no_pets() -> None:
    friendly = _make_listing(description="Pet-friendly building, cats and dogs welcome.")
    friendly_signal = _detect("pets.allowed", friendly)
    assert friendly_signal is not None
    assert friendly_signal.fired is True
    assert friendly_signal.magnitude == pytest.approx(1.0)

    conditional = _make_listing(description="Pets considered with landlord approval.")
    conditional_signal = _detect("pets.allowed", conditional)
    assert conditional_signal is not None
    assert conditional_signal.fired is True
    assert 0.0 < conditional_signal.magnitude < 1.0

    no_pets = _make_listing(description="Sorry, no pets allowed.")
    no_pets_signal = _detect("pets.allowed", no_pets)
    assert no_pets_signal is not None
    assert no_pets_signal.fired is True
    assert no_pets_signal.magnitude == pytest.approx(0.0)

    missing = _make_listing()
    missing_signal = _detect("pets.allowed", missing)
    assert missing_signal is None


def test_area_and_rent_detectors_require_known_values() -> None:
    profile = _profile(
        hard={
            "area": {"min": 750, "unit": "sqft"},
            "rent": {"max": 3200, "currency": "CAD"},
        },
        weights={},
    )
    listing = _make_listing(area_sqft=980, rent_amount=2750)

    area_signal = _detect("area.over_minimum", listing, profile)
    rent_signal = _detect("rent.headroom", listing, profile)
    assert area_signal is not None
    assert rent_signal is not None
    assert area_signal.magnitude == pytest.approx(230.0)
    assert rent_signal.magnitude == pytest.approx(450.0)

    missing_listing = _make_listing()
    assert _detect("area.over_minimum", missing_listing, profile) is None
    assert _detect("rent.headroom", missing_listing, profile) is None


def test_floor_low_prefers_lower_floors_under_positive_weight_profile() -> None:
    profile = _profile(weights={"floor.low": 10.0})
    engine = RankEngine(profile)
    context = _Context(profile=profile)

    floor_2 = _make_listing(listing_id="floor-2", floor=2)
    floor_11 = _make_listing(listing_id="floor-11", floor=11)

    score_floor_2 = engine.score_listing(floor_2, context=context)
    score_floor_11 = engine.score_listing(floor_11, context=context)

    assert score_floor_2.score > score_floor_11.score
    assert (
        score_floor_2.contributions[0].contribution
        > score_floor_11.contributions[0].contribution
    )


def test_area_and_rent_scaled_weights_respect_profile_thresholds_and_caps() -> None:
    listing = _make_listing(area_sqft=1200, rent_amount=1800)
    base_weights = {
        "area.over_minimum": {"per_100_sqft": 4, "cap": 12},
        "rent.headroom": {"per_100": 1, "cap": 15},
    }

    relaxed_profile = _profile(
        hard={
            "area": {"min": 700, "unit": "sqft"},
            "rent": {"max": 3000, "currency": "CAD"},
        },
        weights=base_weights,
    )
    strict_profile = _profile(
        hard={
            "area": {"min": 1100, "unit": "sqft"},
            "rent": {"max": 1900, "currency": "CAD"},
        },
        weights=base_weights,
    )

    relaxed_result = RankEngine(relaxed_profile).score_listing(
        listing, context=_Context(profile=relaxed_profile)
    )
    strict_result = RankEngine(strict_profile).score_listing(
        listing, context=_Context(profile=strict_profile)
    )
    relaxed_by_key = {item.rule_key: item for item in relaxed_result.contributions}
    strict_by_key = {item.rule_key: item for item in strict_result.contributions}

    assert relaxed_by_key["area.over_minimum"].contribution == pytest.approx(12.0)
    assert strict_by_key["area.over_minimum"].contribution == pytest.approx(4.0)
    assert relaxed_by_key["rent.headroom"].contribution == pytest.approx(12.0)
    assert strict_by_key["rent.headroom"].contribution == pytest.approx(1.0)


def test_profile_location_bonus_changes_order_for_two_area_keys() -> None:
    profile = _profile(
        weights={},
        area_key_weights={"downtown_van": 8.0, "burnaby_brentwood": -4.0},
    )
    engine = RankEngine(profile)

    downtown_listing = _make_listing(listing_id="downtown", area_key="downtown_van")
    brentwood_listing = _make_listing(listing_id="brentwood", area_key="burnaby_brentwood")

    downtown_score = engine.score_listing(downtown_listing, context=_Context(profile=profile))
    brentwood_score = engine.score_listing(brentwood_listing, context=_Context(profile=profile))

    assert downtown_score.score > brentwood_score.score


def test_two_profiles_produce_different_orders_and_explanatory_breakdowns() -> None:
    listings = [
        _make_listing(
            listing_id="friendly-downtown",
            area_key="downtown_van",
            description="Pet-friendly rental, pets allowed, parking included.",
            floor=11,
        ),
        _make_listing(
            listing_id="no-pets-kits",
            area_key="kits_beach",
            description="Sorry, no pets allowed. Parking included.",
            floor=2,
        ),
    ]
    profile_prefers_pets = _profile(
        weights={"pets.allowed": 8.0, "floor.low": 2.0},
        area_key_weights={"downtown_van": 6.0, "kits_beach": -2.0},
    )
    profile_avoids_pets = _profile(
        weights={"pets.allowed": -8.0, "floor.low": 2.0},
        area_key_weights={"downtown_van": -2.0, "kits_beach": 6.0},
    )

    pets_engine = RankEngine(profile_prefers_pets)
    avoid_engine = RankEngine(profile_avoids_pets)
    pets_results = sorted(
        (
            (
                listing.identity.listing_id,
                pets_engine.score_listing(
                    listing,
                    context=_Context(profile_prefers_pets),
                ),
            )
            for listing in listings
        ),
        key=lambda item: item[1].score,
        reverse=True,
    )
    avoid_results = sorted(
        (
            (
                listing.identity.listing_id,
                avoid_engine.score_listing(
                    listing,
                    context=_Context(profile_avoids_pets),
                ),
            )
            for listing in listings
        ),
        key=lambda item: item[1].score,
        reverse=True,
    )

    assert pets_results[0][0] == "friendly-downtown"
    assert avoid_results[0][0] == "no-pets-kits"

    pets_top_breakdown = {item.rule_key: item for item in pets_results[0][1].contributions}
    avoid_top_breakdown = {item.rule_key: item for item in avoid_results[0][1].contributions}
    assert pets_top_breakdown["pets.allowed"].contribution > 0
    assert pets_top_breakdown["location.area_key"].contribution > 0
    assert avoid_top_breakdown["pets.allowed"].contribution == pytest.approx(0.0)
    assert avoid_top_breakdown["location.area_key"].contribution > 0
