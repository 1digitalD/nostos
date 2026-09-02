from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nostos.model import (
    Absence,
    Identity,
    Listing,
    Photo,
    Place,
    SourceRecordRef,
)
from nostos.rank import CATEGORY_LABELS, DEFAULT_REGISTRY
from nostos.rank.rules import RuleRegistry, Signal

OBSERVED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _make_listing(*, photos: list[Photo]) -> Listing:
    return Listing(
        identity=Identity(
            listing_id="listing-1",
            source="stub",
            source_id="listing-1",
            url="https://example.test/listing-1",
            signature="sig-listing-1",
        ),
        place=Place(raw_address="123 Main St, Vancouver, BC"),
        rent=Absence.NOT_STATED,
        beds=Absence.NOT_STATED,
        baths=Absence.NOT_STATED,
        area=Absence.NOT_STATED,
        floor=Absence.NOT_STATED,
        parking=Absence.NOT_STATED,
        furnishing=Absence.NOT_STATED,
        photos=photos,
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


def test_photo_present_fires_when_listing_has_photos() -> None:
    rule = DEFAULT_REGISTRY.get("photo.present")
    assert rule is not None
    assert rule.category == "amenities"
    assert rule.label == "Has photos"

    listing = _make_listing(
        photos=[Photo(url="https://example.test/a.jpg"), Photo(url="https://example.test/b.jpg")]
    )
    signal = rule.detector(listing, object())
    assert signal is not None
    assert signal.fired is True
    assert signal.magnitude == pytest.approx(1.0)
    assert signal.confidence == pytest.approx(1.0)
    assert signal.evidence == "2 photos"


def test_photo_present_does_not_fire_without_photos() -> None:
    rule = DEFAULT_REGISTRY.get("photo.present")
    assert rule is not None
    assert rule.detector(_make_listing(photos=[]), object()) is None


def test_every_default_rule_has_a_description_and_known_category() -> None:
    rules = DEFAULT_REGISTRY.all()
    assert rules
    for entry in rules:
        assert entry.description.strip(), f"{entry.key} has no description"
        assert entry.description.rstrip().endswith("."), f"{entry.key}: not a sentence"
        assert entry.category in CATEGORY_LABELS, f"{entry.key}: unlabeled category"


def test_registry_decorator_accepts_description_and_defaults_to_empty() -> None:
    registry = RuleRegistry()

    @registry.rule("a.described", category="amenities", description="Fires on A.")
    def described(_: object, __: object) -> Signal:
        return Signal(fired=True, magnitude=1.0, confidence=1.0)

    @registry.rule("a.bare", category="amenities")
    def bare(_: object, __: object) -> Signal:
        return Signal(fired=True, magnitude=1.0, confidence=1.0)

    first = registry.get("a.described")
    second = registry.get("a.bare")
    assert first is not None and first.description == "Fires on A."
    assert second is not None and second.description == ""
