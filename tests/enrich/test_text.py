from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from nostos.config.citypack import load_citypack
from nostos.config.profile import Profile
from nostos.context import SearchContext
from nostos.enrich.chain import run_enricher_chain
from nostos.enrich.text import TextRuleEnricher, recover_missing_attributes
from nostos.model.identity import Identity
from nostos.model.listing import Absence, Listing, Observed, Origin
from nostos.model.source_record import SourceRecordRef
from nostos.model.value import Money, Place

OBSERVED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def make_listing(
    *,
    description: str,
    rent: Observed[Money] | Absence = Absence.NOT_STATED,
) -> Listing:
    return Listing(
        identity=Identity(
            listing_id="listing-1",
            source="stub",
            source_id="stub-1",
            url="https://example.test/listing-1",
            signature="sig-1",
        ),
        place=Place(raw_address="123 Main St, Vancouver, BC"),
        rent=rent,
        beds=Absence.NOT_STATED,
        baths=Absence.NOT_STATED,
        area=Absence.NOT_STATED,
        floor=Absence.NOT_STATED,
        parking=Absence.NOT_STATED,
        furnishing=Absence.NOT_STATED,
        photos=[],
        attributes={
            "description": Observed[str](
                value=description,
                origin=Origin.SOURCE_FIELD,
                confidence=1.0,
                evidence="source description",
                observed_at=OBSERVED_AT,
            )
        },
        raw_ref=SourceRecordRef(
            source="stub",
            source_id="stub-1",
            url="https://example.test/listing-1",
            content_hash="hash-1",
            fetched_at=OBSERVED_AT,
        ),
        schema_version=1,
    )


def test_recover_missing_attributes_emits_text_rule_observed_with_evidence() -> None:
    listing = make_listing(description="Bright unit for $2,650 / month with 2 bed 1.5 bath")

    updates = recover_missing_attributes(
        listing,
        context={"currency": "CAD", "area_unit": "sqft"},
        observed_at=OBSERVED_AT,
    )

    rent = updates["rent"]
    assert isinstance(rent, Observed)
    assert rent.origin is Origin.TEXT_RULE
    assert rent.evidence == "$2,650 / month"

    beds = updates["beds"]
    assert isinstance(beds, Observed)
    assert beds.value == 2.0
    assert beds.evidence == "2 bed"

    baths = updates["baths"]
    assert isinstance(baths, Observed)
    assert baths.value == 1.5
    assert baths.evidence == "1.5 bath"


def test_basement_storage_and_underground_parking_do_not_set_basement() -> None:
    listing = make_listing(
        description="Includes basement storage locker and one underground parking stall.",
    )

    updates = recover_missing_attributes(listing, context={}, observed_at=OBSERVED_AT)

    assert "attributes.basement" not in updates
    parking = updates["parking"]
    assert isinstance(parking, Observed)
    assert parking.value == "Available"
    assert parking.evidence == "one underground parking stall"


@pytest.mark.parametrize(
    "description",
    [
        "Free parking under basement garage; in-suite laundry.",
        "Second-floor apartment; basement is storage only; parking included.",
        "Main-floor house; basement suite is a separate optional add-on.",
        (
            "BONUS SPACE OPTION: A separate 2-bedroom basement suite with its own "
            "private entrance, kitchen, and bathroom can be added for +$800/month. "
            "RENT & AVAILABILITY: Main Floor: $3,499/month."
        ),
        "Ground level (not a basement); parking included.",
    ],
)
def test_non_unit_basement_references_do_not_flag_the_advertised_unit(
    description: str,
) -> None:
    updates = recover_missing_attributes(
        make_listing(description=description),
        context={},
        observed_at=OBSERVED_AT,
    )

    assert "attributes.basement" not in updates


def test_ambiguous_bsmt_abbreviation_does_not_flag_a_basement_unit() -> None:
    updates = recover_missing_attributes(
        make_listing(description="Bsmt B with separate access."),
        context={},
        observed_at=OBSERVED_AT,
    )

    assert "attributes.basement" not in updates


@pytest.mark.parametrize(
    ("description", "expected_value"),
    [
        ("No parking available for this unit.", "Unavailable"),
        ("Parking available for $150/month.", "Available"),
        ("One parking stall is included in the rent.", "Included"),
    ],
)
def test_parking_extraction_preserves_availability_and_cost_scope(
    description: str,
    expected_value: str,
) -> None:
    updates = recover_missing_attributes(
        make_listing(description=description),
        context={},
        observed_at=OBSERVED_AT,
    )

    parking = updates["parking"]
    assert isinstance(parking, Observed)
    assert parking.value == expected_value


@pytest.mark.parametrize(
    "description",
    [
        "Free Parking.",
        "Parking & Locker Included.",
    ],
)
def test_explicit_included_parking_aliases_are_recognized(description: str) -> None:
    updates = recover_missing_attributes(
        make_listing(description=description),
        context={},
        observed_at=OBSERVED_AT,
    )

    parking = updates["parking"]
    assert isinstance(parking, Observed)
    assert parking.value == "Included"


@pytest.mark.parametrize(
    "description",
    [
        (
            "FEATURES: ✔ 2 bedrooms ✔ 1 bathroom ✔ Approx. 900 sq ft "
            "✔ In-suite laundry ✔ Parking ✔ Quiet residential neighbourhood"
        ),
        (
            "Two bedrooms, one full bathroom, gas range, fridge, dishwasher, "
            "and on demand hot water. 1 covered car port and street parking."
        ),
    ],
)
def test_explicit_available_parking_aliases_are_recognized(description: str) -> None:
    updates = recover_missing_attributes(
        make_listing(description=description),
        context={},
        observed_at=OBSERVED_AT,
    )

    parking = updates["parking"]
    assert isinstance(parking, Observed)
    assert parking.value == "Available"


def test_no_dedicated_parking_is_not_reversed_by_included_word() -> None:
    updates = recover_missing_attributes(
        make_listing(
            description="No dedicated parking included; street parking may be available."
        ),
        context={},
        observed_at=OBSERVED_AT,
    )

    parking = updates["parking"]
    assert isinstance(parking, Observed)
    assert parking.value == "Unavailable"


def test_generic_parking_mention_is_not_positive_evidence() -> None:
    updates = recover_missing_attributes(
        make_listing(description="Secure garage storage for bicycles."),
        context={},
        observed_at=OBSERVED_AT,
    )

    assert "parking" not in updates


def test_not_included_parking_is_only_available_when_paid_option_is_explicit() -> None:
    not_included = recover_missing_attributes(
        make_listing(description="A parking spot is not included in the rent."),
        context={},
        observed_at=OBSERVED_AT,
    )
    paid_option = recover_missing_attributes(
        make_listing(
            description=(
                "No parking spot is included in the rent; parking is available for $150/month."
            )
        ),
        context={},
        observed_at=OBSERVED_AT,
    )

    assert "parking" not in not_included
    parking = paid_option["parking"]
    assert isinstance(parking, Observed)
    assert parking.value == "Available"


@pytest.mark.parametrize(
    "description",
    [
        "No shared laundry in the building.",
        "Washer and dryer hookups may be discussed with the landlord.",
        "No in-suite laundry; shared laundry is available in the building.",
        "In-suite washer and dryer shared.",
    ],
)
def test_ambiguous_or_negated_laundry_is_not_in_suite_evidence(description: str) -> None:
    updates = recover_missing_attributes(
        make_listing(description=description),
        context={},
        observed_at=OBSERVED_AT,
    )

    assert "attributes.in_suite_laundry" not in updates


def test_explicit_private_laundry_remains_positive_when_shared_laundry_is_absent() -> None:
    updates = recover_missing_attributes(
        make_listing(description="Private in-suite laundry; no shared laundry."),
        context={},
        observed_at=OBSERVED_AT,
    )

    laundry = updates["attributes.in_suite_laundry"]
    assert isinstance(laundry, Observed)
    assert laundry.value is True


def test_washer_and_dryer_with_explicit_unit_scope_is_in_suite_evidence() -> None:
    updates = recover_missing_attributes(
        make_listing(description="Washer and dryer inside the unit."),
        context={},
        observed_at=OBSERVED_AT,
    )

    laundry = updates["attributes.in_suite_laundry"]
    assert isinstance(laundry, Observed)
    assert laundry.value is True


def test_in_suite_washer_and_dryer_alias_is_private_laundry_evidence() -> None:
    updates = recover_missing_attributes(
        make_listing(description="In-suite washer and dryer."),
        context={},
        observed_at=OBSERVED_AT,
    )

    laundry = updates["attributes.in_suite_laundry"]
    assert isinstance(laundry, Observed)
    assert laundry.value is True


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Furnished: No; basement suite.", "Unfurnished"),
        ("Furnished: Yes; move-in ready.", "Furnished"),
    ],
)
def test_explicit_furnishing_labels_are_respected(description: str, expected: str) -> None:
    updates = recover_missing_attributes(
        make_listing(description=description),
        context={},
        observed_at=OBSERVED_AT,
    )

    furnishing = updates["furnishing"]
    assert isinstance(furnishing, Observed)
    assert furnishing.value == expected


def test_top_floor_bedroom_count_is_not_extracted_as_numeric_floor() -> None:
    updates = recover_missing_attributes(
        make_listing(description="TOP-FLOOR 2-BEDROOM with skyline views."),
        context={},
        observed_at=OBSERVED_AT,
    )

    assert "floor" not in updates


@pytest.mark.parametrize(
    ("description", "missing_field"),
    [
        ("Choose from 1 & 2 bedroom apartments.", "beds"),
        ("Several layouts range from 700-900 sqft.", "area"),
    ],
)
def test_multi_unit_ranges_do_not_become_scalar_unit_facts(
    description: str,
    missing_field: str,
) -> None:
    updates = recover_missing_attributes(
        make_listing(description=description),
        context={},
        observed_at=OBSERVED_AT,
    )

    assert missing_field not in updates


def test_marketing_copy_minutes_from_yaletown_does_not_set_neighbourhood() -> None:
    listing = make_listing(
        description="Beautiful apartment just minutes from Yaletown and downtown nightlife.",
    )

    updates = recover_missing_attributes(
        listing,
        context={
            "area_keywords": {
                "kits_beach": ["kitsilano", "kits point"],
                "downtown_van": ["yaletown", "downtown"],
            }
        },
        observed_at=OBSERVED_AT,
    )

    assert "attributes.area_key" not in updates


def test_marketing_copy_minutes_from_yaletown_does_not_set_neighbourhood_with_shipped_citypack(
) -> None:
    listing = make_listing(
        description="Beautiful apartment just minutes from Yaletown and downtown nightlife.",
    )
    repo_root = Path(__file__).resolve().parents[2]
    citypack = load_citypack(repo_root / "src" / "nostos" / "citypacks" / "vancouver.yaml")
    profile = Profile.model_validate(
        {"city": "vancouver", "weights": {}, "schedule": "0 */6 * * *"}
    )
    context = SearchContext(citypack=citypack, profile=profile)

    updates = recover_missing_attributes(listing, context=context, observed_at=OBSERVED_AT)

    assert "attributes.area_key" not in updates


def test_text_content_never_overwrites_structured_value() -> None:
    listing = make_listing(
        description="Special price now $2,650/month.",
        rent=Observed[Money](
            value=Money(amount=Decimal("2800"), currency="CAD", period="month"),
            origin=Origin.SOURCE_FIELD,
            confidence=0.95,
            evidence="structured source field",
            observed_at=OBSERVED_AT,
        ),
    )
    enricher = TextRuleEnricher()

    updated = run_enricher_chain(listing, [enricher], context={"currency": "CAD"})

    assert updated.rent == listing.rent


def test_explicit_iso_availability_becomes_a_date_fact() -> None:
    updates = recover_missing_attributes(
        make_listing(description="Available 2026-10-15."),
        context={},
        observed_at=OBSERVED_AT,
    )

    available = updates["attributes.available_date"]
    assert isinstance(available, Observed)
    assert available.value == "2026-10-15"
    assert available.evidence == "Available 2026-10-15"
    assert "attributes.available_text" not in updates


def test_ambiguous_availability_preserves_text_without_inventing_a_year() -> None:
    updates = recover_missing_attributes(
        make_listing(description="Available Oct 1."),
        context={},
        observed_at=OBSERVED_AT,
    )

    assert "attributes.available_date" not in updates
    available = updates["attributes.available_text"]
    assert isinstance(available, Observed)
    assert available.value == "Available Oct 1"
    assert available.evidence == "Available Oct 1"


@pytest.mark.parametrize("phrase", ["12-month lease", "one-year lease"])
def test_explicit_twelve_month_lease_terms_are_normalized(phrase: str) -> None:
    updates = recover_missing_attributes(
        make_listing(description=f"A {phrase} is required."),
        context={},
        observed_at=OBSERVED_AT,
    )

    lease = updates["attributes.lease_months"]
    assert isinstance(lease, Observed)
    assert lease.value == 12
    assert lease.evidence == phrase


@pytest.mark.parametrize(
    ("description", "expected", "evidence"),
    [
        ("All utilities included.", ["all"], "All utilities included"),
        (
            "Heat and water are included in rent.",
            ["heat", "water"],
            "Heat and water are included",
        ),
        (
            "Rent includes hydro and internet.",
            ["hydro", "internet"],
            "Rent includes hydro and internet",
        ),
        ("Utilities are included.", ["unspecified"], "Utilities are included"),
    ],
)
def test_utilities_preserve_explicit_scope_and_evidence(
    description: str,
    expected: list[str],
    evidence: str,
) -> None:
    updates = recover_missing_attributes(
        make_listing(description=description),
        context={},
        observed_at=OBSERVED_AT,
    )

    utilities = updates["attributes.utilities_included"]
    assert isinstance(utilities, Observed)
    assert utilities.value == expected
    assert utilities.evidence == evidence


def test_total_monthly_requires_an_exact_stated_total() -> None:
    exact = recover_missing_attributes(
        make_listing(
            description="Base rent is $3,000. Total monthly cost is $3,100.",
        ),
        context={},
        observed_at=OBSERVED_AT,
    )
    guessed = recover_missing_attributes(
        make_listing(description="Rent is $3,000 plus $100 for parking."),
        context={},
        observed_at=OBSERVED_AT,
    )

    total = exact["attributes.total_monthly"]
    assert isinstance(total, Observed)
    assert total.value == 3100
    assert total.evidence == "Total monthly cost is $3,100"
    assert "attributes.total_monthly" not in guessed


def test_total_monthly_statement_does_not_become_base_rent() -> None:
    updates = recover_missing_attributes(
        make_listing(description="Total monthly cost is $3,100."),
        context={},
        observed_at=OBSERVED_AT,
    )

    assert "attributes.total_monthly" in updates
    assert "rent" not in updates


def test_saved_text_facts_do_not_overwrite_existing_observations() -> None:
    listing = make_listing(
        description=(
            "Available 2026-10-15. 12-month lease. All utilities included. "
            "Total monthly cost is $3,100."
        )
    )
    existing = Observed[Any](
        value="user-owned",
        origin=Origin.USER,
        confidence=1.0,
        evidence="user correction",
        observed_at=OBSERVED_AT,
    )
    listing = listing.model_copy(
        update={
            "attributes": {
                **listing.attributes,
                "available_date": existing,
                "lease_months": existing,
                "utilities_included": existing,
                "total_monthly": existing,
            }
        }
    )

    updates = recover_missing_attributes(listing, context={}, observed_at=OBSERVED_AT)

    assert not {
        "attributes.available_date",
        "attributes.available_text",
        "attributes.lease_months",
        "attributes.utilities_included",
        "attributes.total_monthly",
    } & updates.keys()


def test_text_enricher_declares_saved_text_fact_outputs() -> None:
    assert {
        "attributes.available_date",
        "attributes.available_text",
        "attributes.lease_months",
        "attributes.utilities_included",
        "attributes.total_monthly",
    } <= TextRuleEnricher().provides
