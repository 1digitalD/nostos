from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

import pytest
from pydantic import TypeAdapter, ValidationError

from nostos.model.identity import Identity
from nostos.model.listing import Absence, Field, Listing, Observed, Origin, merge_field
from nostos.model.source_record import SourceRecord, SourceRecordRef
from nostos.model.value import Area, LatLng, Money, Photo, Place, StructuredAddress

OBSERVED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_money_round_trip() -> None:
    value = Money(amount=Decimal("2499.00"), currency="cad", period="month")
    restored = Money.model_validate_json(value.model_dump_json())
    assert restored == Money(amount=Decimal("2499.00"), currency="CAD", period="month")


def test_area_round_trip() -> None:
    value = Area(value=685.5, unit="sqft")
    restored = Area.model_validate_json(value.model_dump_json())
    assert restored == value


def test_lat_lng_round_trip() -> None:
    value = LatLng(lat=49.2827, lng=-123.1207)
    restored = LatLng.model_validate_json(value.model_dump_json())
    assert restored == value


def test_structured_address_round_trip() -> None:
    value = StructuredAddress(
        line_1="123 Main St",
        line_2="Unit 4",
        city="Vancouver",
        region="BC",
        postal_code="V6B 1A1",
        country="CA",
    )
    restored = StructuredAddress.model_validate_json(value.model_dump_json())
    assert restored == value


def test_photo_round_trip() -> None:
    value = Photo(
        url="https://example.com/photo-1.jpg",
        width=1200,
        height=900,
        caption="Living room",
    )
    restored = Photo.model_validate_json(value.model_dump_json())
    assert restored == value


def test_place_round_trip_with_injected_area_vocabulary() -> None:
    value = Place(
        raw_address="123 Main St, Vancouver, BC",
        structured=StructuredAddress(
            line_1="123 Main St",
            line_2=None,
            city="Vancouver",
            region="BC",
            postal_code="V6B 1A1",
            country="CA",
        ),
        point=LatLng(lat=49.2827, lng=-123.1207),
        area_key="kitsilano",
    )
    restored = Place.model_validate(
        value.model_dump(mode="json"),
        context={"area_vocabulary": {"kitsilano", "mount-pleasant"}},
    )
    assert restored == value
    assert isinstance(restored.area_key, str)
    assert not isinstance(restored.area_key, Enum)


def test_place_rejects_area_key_not_in_injected_vocabulary() -> None:
    with pytest.raises(ValidationError):
        Place.model_validate(
            {
                "raw_address": "123 Main St",
                "structured": None,
                "point": None,
                "area_key": "unknown-area",
            },
            context={"area_vocabulary": {"kitsilano"}},
        )


def test_identity_round_trip() -> None:
    value = Identity(
        listing_id="listing-123",
        source="craigslist",
        source_id="abc123",
        url="https://example.com/listing/abc123",
        signature="sig:abc123",
    )
    restored = Identity.model_validate_json(value.model_dump_json())
    assert restored == value


def test_source_record_ref_round_trip() -> None:
    value = SourceRecordRef(
        source="craigslist",
        source_id="abc123",
        url="https://example.com/listing/abc123",
        content_hash="hash-1",
        fetched_at=OBSERVED_AT,
    )
    restored = SourceRecordRef.model_validate_json(value.model_dump_json())
    assert restored == value


def test_source_record_round_trip() -> None:
    value = SourceRecord(
        source="craigslist",
        source_id="abc123",
        url="https://example.com/listing/abc123",
        content_hash="hash-1",
        fetched_at=OBSERVED_AT,
        payload={"title": "1 bed apartment", "price": 2499, "available": True},
    )
    restored = SourceRecord.model_validate_json(value.model_dump_json())
    assert restored == value


def test_observed_round_trip() -> None:
    value = Observed[Money](
        value=Money(amount=Decimal("2400"), currency="CAD", period="month"),
        origin=Origin.DETAIL_PAGE,
        confidence=0.82,
        evidence="$2400/mo",
        observed_at=OBSERVED_AT,
        detail={"parser": "detail"},
    )
    restored = Observed[Money].model_validate_json(value.model_dump_json())
    assert restored == value


def test_field_discriminates_observed_and_absence_both_directions() -> None:
    adapter: TypeAdapter[Field[Money]] = TypeAdapter(Field[Money])
    observed_payload = {
        "value": {"amount": "2200", "currency": "CAD", "period": "month"},
        "origin": "source_field",
        "confidence": 0.95,
        "evidence": "$2200",
        "observed_at": OBSERVED_AT.isoformat(),
        "detail": {"selector": "price"},
    }

    observed_value = adapter.validate_python(observed_payload)
    assert isinstance(observed_value, Observed)
    observed_round_trip = adapter.validate_python(adapter.dump_python(observed_value, mode="json"))
    assert observed_round_trip == observed_value

    absence_value = adapter.validate_python("not_applicable")
    assert absence_value is Absence.NOT_APPLICABLE
    absence_round_trip = adapter.validate_json(adapter.dump_json(absence_value))
    assert absence_round_trip is Absence.NOT_APPLICABLE


def test_lower_precedence_origin_cannot_overwrite_higher() -> None:
    higher = Observed[int](
        value=10,
        origin=Origin.SOURCE_FIELD,
        confidence=0.9,
        evidence="structured source field",
        observed_at=OBSERVED_AT,
    )
    lower = Observed[int](
        value=9,
        origin=Origin.TEXT_RULE,
        confidence=0.6,
        evidence="regex match",
        observed_at=OBSERVED_AT,
    )
    with pytest.raises(ValueError):
        merge_field(higher, lower)


def test_higher_precedence_origin_can_overwrite_lower() -> None:
    lower = Observed[int](
        value=9,
        origin=Origin.TEXT_RULE,
        confidence=0.6,
        evidence="regex match",
        observed_at=OBSERVED_AT,
    )
    higher = Observed[int](
        value=10,
        origin=Origin.SOURCE_FIELD,
        confidence=0.9,
        evidence="structured source field",
        observed_at=OBSERVED_AT,
    )
    result = merge_field(lower, higher)
    assert result == higher


def test_absence_is_typed_and_not_collapsed_to_none() -> None:
    assert Absence.NOT_STATED.value == "not_stated"
    assert Absence.NOT_APPLICABLE.value == "not_applicable"
    assert Absence.CONTRADICTORY.value == "contradictory"
    assert Absence.NOT_STATED is not None


def test_listing_round_trip() -> None:
    listing = Listing(
        identity=Identity(
            listing_id="listing-123",
            source="craigslist",
            source_id="abc123",
            url="https://example.com/listing/abc123",
            signature="sig:abc123",
        ),
        place=Place(
            raw_address="123 Main St, Vancouver, BC",
            structured=None,
            point=LatLng(lat=49.2827, lng=-123.1207),
            area_key="kitsilano",
        ),
        rent=Observed[Money](
            value=Money(amount=Decimal("2400"), currency="CAD", period="month"),
            origin=Origin.SOURCE_FIELD,
            confidence=0.98,
            evidence="$2400",
            observed_at=OBSERVED_AT,
        ),
        beds=Observed[float](
            value=1.0,
            origin=Origin.SOURCE_FIELD,
            confidence=0.95,
            evidence="1 bed",
            observed_at=OBSERVED_AT,
        ),
        baths=Absence.NOT_STATED,
        area=Observed[Area](
            value=Area(value=650.0, unit="sqft"),
            origin=Origin.DETAIL_PAGE,
            confidence=0.8,
            evidence="650 sqft",
            observed_at=OBSERVED_AT,
        ),
        floor=Absence.NOT_APPLICABLE,
        parking=Absence.NOT_STATED,
        furnishing=Absence.CONTRADICTORY,
        photos=[
            Photo(
                url="https://example.com/photo-1.jpg",
                width=1200,
                height=900,
                caption="Living room",
            )
        ],
        attributes={
            "pet_friendly": Observed[bool](
                value=True,
                origin=Origin.TEXT_RULE,
                confidence=0.7,
                evidence="pets allowed",
                observed_at=OBSERVED_AT,
            ),
            "has_den": Absence.NOT_STATED,
        },
        raw_ref=SourceRecordRef(
            source="craigslist",
            source_id="abc123",
            url="https://example.com/listing/abc123",
            content_hash="hash-1",
            fetched_at=OBSERVED_AT,
        ),
        schema_version=1,
    )

    payload = listing.model_dump(mode="json")
    restored = Listing.model_validate(payload, context={"area_vocabulary": {"kitsilano"}})
    assert restored == listing
