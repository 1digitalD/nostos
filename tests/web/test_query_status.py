"""Unit tests for the hard-filter match-status classification in nostos.web.query."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

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
from nostos.web.query import (
    SORT_OPTIONS,
    MatchStatus,
    classify_match_status,
    normalize_sort,
    rule_rows_from_breakdown,
    sort_label,
)

OBSERVED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _obs(value: Any) -> Observed[Any]:
    return Observed[Any](
        value=value,
        origin=Origin.SOURCE_FIELD,
        confidence=1.0,
        observed_at=OBSERVED_AT,
    )


def _profile(**hard: object) -> Profile:
    base: dict[str, object] = {
        "rent": {"max": 3600, "currency": "CAD"},
        "beds": {"eq": 2},
        "exclude": [],
    }
    base.update(hard)
    return Profile.model_validate(
        {
            "city": "vancouver",
            "hard": base,
            "weights": {},
            "sources": {},
            "schedule": "0 */6 * * *",
        }
    )


def _listing(
    *,
    rent: float | None = 3000,
    beds: float | None = 2,
    baths: float | None = 1,
    area: float | None = 800,
    floor: int | None = 3,
    furnishing: str | None = None,
    area_key: str | None = "kits",
    description: str = "Bright corner unit near the beach.",
) -> Listing:
    attributes: dict[str, object] = {"description": _obs(description)}
    return Listing(
        identity=Identity(
            listing_id="listing-1",
            source="stub",
            source_id="stub-1",
            url="https://example.test/listing-1",
            signature="sig-1",
        ),
        place=Place(raw_address="123 Main St", area_key=area_key),
        rent=(
            _obs(Money(amount=Decimal(str(rent)), currency="CAD", period="month"))
            if rent is not None
            else Absence.NOT_STATED
        ),
        beds=_obs(float(beds)) if beds is not None else Absence.NOT_STATED,
        baths=_obs(float(baths)) if baths is not None else Absence.NOT_STATED,
        area=_obs(Area(value=area, unit="sqft")) if area is not None else Absence.NOT_STATED,
        floor=_obs(floor) if floor is not None else Absence.NOT_STATED,
        parking=Absence.NOT_STATED,
        furnishing=_obs(furnishing) if furnishing is not None else Absence.NOT_STATED,
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


def test_match_when_every_criterion_passes() -> None:
    result = classify_match_status(_listing(), _profile(floor={"max": 12}, areas=["kits"]))
    assert result == MatchStatus(status="match", reasons=())


def test_unverified_when_floor_unstated() -> None:
    result = classify_match_status(_listing(floor=None), _profile(floor={"max": 12}))
    assert result.status == "unverified"
    assert result.reasons == ("floor unstated",)


def test_miss_when_rent_over_max_with_readable_reason() -> None:
    result = classify_match_status(_listing(rent=3800), _profile())
    assert result.status == "miss"
    assert result.reasons == ("rent $3,800 > max $3,600",)


def test_miss_when_rent_under_min() -> None:
    result = classify_match_status(
        _listing(rent=1200), _profile(rent={"max": 3600, "min": 1500, "currency": "CAD"})
    )
    assert result.status == "miss"
    assert "rent $1,200 < min $1,500" in result.reasons


def test_miss_when_area_not_in_allowed_list_and_unverified_when_unknown() -> None:
    profile = _profile(areas=["downtown"])
    miss = classify_match_status(_listing(area_key="kits"), profile)
    assert miss.status == "miss"
    assert "area not in allowed list" in miss.reasons

    # No profile floor filter here, so the only open question is the area.
    unknown = classify_match_status(_listing(area_key=None), profile)
    assert unknown.status == "unverified"
    assert unknown.reasons == ("area unknown",)


def test_miss_when_basement_excluded() -> None:
    listing = _listing(description="Cozy basement suite with separate entrance.")
    result = classify_match_status(listing, _profile(exclude=["basement"]))
    assert result.status == "miss"
    assert "basement unit" in result.reasons

    # Same text without the exclude token is a match.
    assert classify_match_status(listing, _profile()).status == "match"


def test_miss_when_furnished_only_excluded() -> None:
    listing = _listing(furnishing="Furnished")
    result = classify_match_status(listing, _profile(exclude=["furnished_only"]))
    assert result.status == "miss"
    assert result.reasons == ("furnished only",)
    unfurnished = _listing(furnishing="Unfurnished")
    profile = _profile(exclude=["furnished_only"])
    assert classify_match_status(unfurnished, profile).status == "match"


def test_beds_eq_mismatch_and_min_max_bounds() -> None:
    assert classify_match_status(_listing(beds=1), _profile()).reasons == ("beds 1 ≠ 2",)
    result = classify_match_status(_listing(baths=1), _profile(baths={"min": 1.5}))
    assert result.status == "miss"
    assert result.reasons == ("baths 1 < min 1.5",)
    result = classify_match_status(_listing(floor=20), _profile(floor={"max": 12}))
    assert result.reasons == ("floor 20 > max 12",)


def test_miss_reasons_come_before_unknowns() -> None:
    result = classify_match_status(
        _listing(rent=4000, floor=None), _profile(floor={"max": 12})
    )
    assert result.status == "miss"
    assert result.reasons == ("rent $4,000 > max $3,600", "floor unstated")


def test_area_min_miss_and_unstated() -> None:
    profile = _profile(area={"min": 900, "unit": "sqft"})
    assert classify_match_status(_listing(area=800), profile).reasons == (
        "area 800 < min 900 sqft",
    )
    unstated = classify_match_status(_listing(area=None), profile)
    assert unstated.status == "unverified"
    assert unstated.reasons == ("area unstated",)


def test_beds_unstated_is_unverified_not_crash() -> None:
    # Regression: the old implementation left `target` unbound / unreachable.
    result = classify_match_status(_listing(beds=None), _profile())
    assert result.status == "unverified"
    assert result.reasons == ("beds unstated",)


def test_sort_normalization_and_labels() -> None:
    keys = [key for key, _label in SORT_OPTIONS]
    assert keys == [
        "score", "rent_asc", "rent_desc", "area_desc", "posted_desc", "posted_asc", "address",
    ]
    assert normalize_sort("rent") == "rent_asc"
    assert normalize_sort("posted") == "posted_desc"
    assert normalize_sort("bogus") == "score"
    assert normalize_sort(None) == "score"
    assert sort_label("rent_desc") == "Rent ↓"


def test_rule_rows_from_breakdown_sorts_by_impact_and_reads_evidence() -> None:
    breakdown = {
        "contributions": [
            {
                "rule_key": "pets.allowed",
                "category": "amenities",
                "label": "Pet friendly",
                "weight": -10,
                "signal": {"fired": True, "magnitude": 1, "confidence": 1, "evidence": "no pets"},
                "contribution": -10.0,
            },
            {
                "rule_key": "laundry.in_suite",
                "category": "amenities",
                "label": "In-suite laundry",
                "weight": 6,
                "signal": {"fired": True, "magnitude": 1, "confidence": 1,
                           "evidence": "washer/dryer"},
                "contribution": 6.0,
            },
            {
                "rule_key": "area.over_minimum",
                "category": "space",
                "label": "Space over minimum",
                "weight": {"per_100_sqft": 2, "cap": 10},
                "signal": None,
                "contribution": 0.0,
            },
        ]
    }
    rows = rule_rows_from_breakdown(breakdown)
    assert [r.rule_key for r in rows] == ["pets.allowed", "laundry.in_suite", "area.over_minimum"]
    assert rows[0].evidence == "no pets"
    assert rows[0].contribution_text == "-10.0"
    assert rows[1].weight_text == "+6"
    assert rows[2].weight_text == "+2 per 100 sqft, cap 10"
    assert rows[2].category_label == "Space & layout"
    assert rows[2].fired is False
    assert rule_rows_from_breakdown(None) == ()
