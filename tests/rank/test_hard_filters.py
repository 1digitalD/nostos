from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal

from nostos.config.profile import Profile
from nostos.model import (
    Absence,
    Identity,
    Listing,
    Money,
    Observed,
    Origin,
    Place,
    SourceRecordRef,
)
from nostos.rank.profile_scoring import passes_hard_filters

OBSERVED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _profile(hard: Mapping[str, object]) -> Profile:
    return Profile.model_validate(
        {
            "city": "vancouver",
            "hard": dict(hard),
            "weights": {},
            "schedule": "0 */6 * * *",
        }
    )


def _make_listing(
    *,
    rent_amount: int | None = None,
    floor: int | None = None,
    area_key: str | None = None,
) -> Listing:
    rent_field: Observed[Money] | Absence = (
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
    floor_field: Observed[int] | Absence = (
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
    return Listing(
        identity=Identity(
            listing_id="listing-1",
            source="stub",
            source_id="listing-1",
            url="https://example.test/listing-1",
            signature="sig-listing-1",
        ),
        place=Place(raw_address="123 Main St, Vancouver, BC", area_key=area_key),
        rent=rent_field,
        beds=Absence.NOT_STATED,
        baths=Absence.NOT_STATED,
        area=Absence.NOT_STATED,
        floor=floor_field,
        parking=Absence.NOT_STATED,
        furnishing=Absence.NOT_STATED,
        photos=[],
        attributes={},
        raw_ref=SourceRecordRef(
            source="stub",
            source_id="listing-1",
            url="https://example.test/listing-1",
            content_hash="hash-listing-1",
            fetched_at=OBSERVED_AT,
        ),
        schema_version=1,
    )


def test_rent_min_rejects_listings_below_the_floor_and_keeps_those_within_range() -> None:
    profile = _profile({"rent": {"min": 2500, "max": 3600, "currency": "CAD"}})

    assert passes_hard_filters(_make_listing(rent_amount=2400), profile) is False
    assert passes_hard_filters(_make_listing(rent_amount=2500), profile) is True
    assert passes_hard_filters(_make_listing(rent_amount=3600), profile) is True
    assert passes_hard_filters(_make_listing(rent_amount=3700), profile) is False


def test_rent_filter_still_requires_a_stated_rent() -> None:
    profile = _profile({"rent": {"min": 2500, "max": 3600, "currency": "CAD"}})
    assert passes_hard_filters(_make_listing(rent_amount=None), profile) is False


def test_floor_max_fails_only_a_stated_floor_above_the_bound() -> None:
    profile = _profile({"floor": {"max": 12}})

    assert passes_hard_filters(_make_listing(floor=13), profile) is False
    assert passes_hard_filters(_make_listing(floor=12), profile) is True
    assert passes_hard_filters(_make_listing(floor=1), profile) is True
    # Unknown floor passes (included, flagged unverified upstream).
    assert passes_hard_filters(_make_listing(floor=None), profile) is True


def test_floor_min_and_eq_are_honoured() -> None:
    assert passes_hard_filters(_make_listing(floor=2), _profile({"floor": {"min": 3}})) is False
    assert passes_hard_filters(_make_listing(floor=3), _profile({"floor": {"min": 3}})) is True
    assert passes_hard_filters(_make_listing(floor=4), _profile({"floor": {"eq": 3}})) is False
    assert passes_hard_filters(_make_listing(floor=3), _profile({"floor": {"eq": 3}})) is True


def test_areas_filter_rejects_known_area_outside_the_list_and_passes_unknown() -> None:
    profile = _profile({"areas": ["kits_beach", "downtown_van"]})

    assert passes_hard_filters(_make_listing(area_key="kits_beach"), profile) is True
    assert passes_hard_filters(_make_listing(area_key="burnaby_other"), profile) is False
    # Unknown area passes.
    assert passes_hard_filters(_make_listing(area_key=None), profile) is True


def test_area_key_attribute_is_used_when_place_has_no_area_key() -> None:
    profile = _profile({"areas": ["kits_beach"]})
    listing = _make_listing(area_key=None).model_copy(
        update={
            "attributes": {
                "area_key": Observed[str](
                    value="west_van",
                    origin=Origin.SOURCE_FIELD,
                    confidence=1.0,
                    evidence="area key attribute",
                    observed_at=OBSERVED_AT,
                )
            }
        }
    )
    assert passes_hard_filters(listing, profile) is False


def test_empty_areas_list_means_any_area() -> None:
    profile = _profile({"areas": []})
    assert passes_hard_filters(_make_listing(area_key="burnaby_other"), profile) is True
