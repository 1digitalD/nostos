from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

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
from nostos.rank.criteria import CriterionCheck, MatchStatus, classify_match_status

OBSERVED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _obs(
    value: Any,
    *,
    origin: Origin = Origin.SOURCE_FIELD,
    evidence: str | None = None,
) -> Observed[Any]:
    return Observed[Any](
        value=value,
        origin=origin,
        confidence=1.0,
        evidence=evidence,
        observed_at=OBSERVED_AT,
    )


def _profile(hard: dict[str, object], *, weights: dict[str, float] | None = None) -> Profile:
    return Profile.model_validate(
        {
            "city": "vancouver",
            "hard": hard,
            "weights": weights or {},
            "sources": {},
            "schedule": "0 */6 * * *",
        }
    )


def _listing(
    *,
    rent_currency: str = "CAD",
    rent_period: str = "month",
    area_unit: str = "sqft",
    furnishing: str | Absence = "Unfurnished",
    attributes: dict[str, Observed[Any] | Absence] | None = None,
) -> Listing:
    return Listing(
        identity=Identity(
            listing_id="listing-1",
            source="stub",
            source_id="stub-1",
            url="https://example.test/listing-1",
            signature="sig-1",
        ),
        place=Place(raw_address="123 Main St", area_key="kits"),
        rent=_obs(
            Money(amount=Decimal("3000"), currency=rent_currency, period=rent_period),
            evidence="monthly rent field",
        ),
        beds=_obs(2.0, evidence="bedroom field"),
        baths=_obs(1.0, evidence="bathroom field"),
        area=_obs(Area(value=800, unit=area_unit), evidence="size field"),
        floor=_obs(3, evidence="floor field"),
        parking=Absence.NOT_STATED,
        furnishing=(
            furnishing
            if isinstance(furnishing, Absence)
            else _obs(furnishing, evidence="furnishing field")
        ),
        photos=[],
        attributes=attributes or {},
        raw_ref=SourceRecordRef(
            source="stub",
            source_id="stub-1",
            url="https://example.test/listing-1",
            content_hash="hash-1",
            fetched_at=OBSERVED_AT,
        ),
        schema_version=1,
    )


def _checks(result: MatchStatus) -> dict[str, CriterionCheck]:
    return {check.field: check for check in result.checks}


def test_every_configured_hard_criterion_emits_one_typed_check() -> None:
    profile = _profile(
        {
            "rent": {"max": 3600, "currency": "CAD"},
            "beds": {"eq": 2},
            "baths": {"min": 1},
            "area": {"min": 750, "unit": "sqft"},
            "floor": {"max": 12},
            "areas": ["kits"],
            "exclude": ["basement", "furnished_only"],
            "require_laundry": True,
            "require_parking": True,
            "lease_months_min": 12,
            "total_monthly_max": 3500,
            "available_by": "2026-03-01",
        }
    )
    listing = _listing(
        attributes={
            "basement": _obs(False, origin=Origin.USER, evidence="user correction"),
            "in_suite_laundry": _obs(True, evidence="laundry field"),
            "parking_available": _obs(True, evidence="parking field"),
            "lease_months": _obs(12, evidence="lease field"),
            "total_monthly": _obs(3200, evidence="total field"),
            "available_date": _obs("2026-02-01", evidence="availability field"),
        }
    )

    result = classify_match_status(listing, profile)

    assert [check.field for check in result.checks] == [
        "rent",
        "beds",
        "baths",
        "area",
        "floor",
        "neighbourhood",
        "basement",
        "furnishing",
        "in_suite_laundry",
        "parking",
        "lease_months",
        "total_monthly",
        "available_date",
    ]
    assert {check.status for check in result.checks} == {"pass"}
    assert _checks(result)["basement"].origin == "user"
    assert _checks(result)["rent"].evidence == "monthly rent field"


def test_explicit_no_laundry_is_a_known_miss_and_user_value_overrides_prose() -> None:
    listing = _listing(
        attributes={
            "in_suite_laundry": _obs(
                False,
                origin=Origin.USER,
                evidence="user marked no",
            ),
            "description": _obs("Private washer and dryer in the suite."),
        }
    )

    result = classify_match_status(listing, _profile({"require_laundry": True}))

    assert result.status == "miss"
    assert result.reasons == ("in-suite laundry required",)
    assert result.checks == (
        CriterionCheck(
            "in_suite_laundry",
            "fail",
            "in-suite laundry required",
            "user marked no",
            "user",
        ),
    )


def test_source_field_false_laundry_is_also_a_known_miss() -> None:
    listing = _listing(
        attributes={
            "in_suite_laundry": _obs(False, evidence="source says unavailable"),
        }
    )

    check = classify_match_status(
        listing, _profile({"require_laundry": True})
    ).checks[0]

    assert check.status == "fail"
    assert check.origin == "source_field"


def test_unknown_exclusions_are_typed_unknown_without_changing_legacy_match() -> None:
    result = classify_match_status(
        _listing(furnishing=Absence.NOT_STATED),
        _profile({"exclude": ["basement", "furnished_only"]}),
    )

    assert result.status == "match"
    assert result.reasons == ()
    assert [(check.field, check.status) for check in result.checks] == [
        ("basement", "unknown"),
        ("furnishing", "unknown"),
    ]


def test_furnished_optional_is_not_a_furnished_only_exclusion() -> None:
    result = classify_match_status(
        _listing(furnishing="Optional"),
        _profile({"exclude": ["furnished_only"]}),
    )

    assert result.status == "match"
    assert result.reasons == ()
    assert result.checks[0].status == "pass"


def test_hard_failure_is_not_offset_by_high_score_data_or_weights() -> None:
    listing = _listing(attributes={"score": _obs(999)})
    listing = listing.model_copy(
        update={
            "rent": _obs(
                Money(amount=Decimal("4000"), currency="CAD", period="month")
            )
        }
    )

    result = classify_match_status(
        listing,
        _profile(
            {"rent": {"max": 3600, "currency": "CAD"}},
            weights={"laundry.in_suite": 1_000_000},
        ),
    )

    assert result.status == "miss"
    assert result.checks[0].status == "fail"


def test_currency_and_area_unit_uncertainty_never_emit_positive_checks() -> None:
    listing = _listing(rent_currency="USD", area_unit="sqm")
    listing = listing.model_copy(
        update={
            "rent": _obs(
                Money(amount=Decimal("4000"), currency="USD", period="month")
            )
        }
    )
    result = classify_match_status(
        listing,
        _profile(
            {
                "rent": {"max": 3600, "currency": "CAD"},
                "area": {"min": 750, "unit": "sqft"},
            }
        ),
    )

    assert result.status == "miss"
    assert [(check.field, check.status) for check in result.checks] == [
        ("rent", "unknown"),
        ("area", "unknown"),
    ]
    assert "rent currency or period needs verification" in result.reasons
    assert "area in sqm, profile uses sqft" in result.reasons


def test_contradictory_numeric_check_keeps_legacy_unstated_reason() -> None:
    listing = _listing().model_copy(update={"beds": Absence.CONTRADICTORY})

    result = classify_match_status(listing, _profile({"beds": {"eq": 2}}))

    assert result.reasons == ("beds unstated",)
    assert result.checks[0].reason == "beds evidence is contradictory"


def test_source_field_positive_laundry_is_stated_not_confirmed() -> None:
    result = classify_match_status(
        _listing(
            attributes={
                "in_suite_laundry": _obs(True, evidence="source laundry field"),
            }
        ),
        _profile({"require_laundry": True}),
    )

    assert result.checks[0].reason == "in-suite laundry stated"


def test_contradictory_laundry_evidence_stays_unknown_in_typed_check() -> None:
    result = classify_match_status(
        _listing(
            attributes={
                "in_suite_laundry": Absence.CONTRADICTORY,
                "description": _obs("Private washer and dryer in the suite."),
            }
        ),
        _profile({"require_laundry": True}),
    )

    assert result.status == "match"
    assert result.reasons == ()
    assert result.checks[0].status == "unknown"


def test_checks_do_not_change_legacy_match_status_equality() -> None:
    assert MatchStatus("match") == MatchStatus(
        "match",
        checks=(CriterionCheck("rent", "pass", "rent meets requirement"),),
    )
