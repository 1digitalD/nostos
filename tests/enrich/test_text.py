from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from nostos.enrich.chain import run_enricher_chain
from nostos.enrich.text import TextRuleEnricher, recover_missing_attributes
from nostos.model.identity import Identity
from nostos.model.listing import Absence, Listing, Observed, Origin
from nostos.model.source_record import SourceRecordRef
from nostos.model.value import Money, Place

OBSERVED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def make_listing(*, description: str, rent: Observed[Money] | Absence = Absence.NOT_STATED) -> Listing:
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


def test_marketing_copy_minutes_from_yaletown_does_not_set_neighbourhood() -> None:
    listing = make_listing(
        description="Beautiful apartment just minutes from Yaletown and downtown nightlife.",
    )

    updates = recover_missing_attributes(
        listing,
        context={"area_keywords": {"kits_beach": ["kitsilano", "kits point"]}},
        observed_at=OBSERVED_AT,
    )

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
