from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from nostos.config.citypack import Citypack
from nostos.config.profile import Profile
from nostos.context import SearchContext
from nostos.model import Absence, Area, Listing, Observed, SourceRecord
from nostos.rank.profile_scoring import passes_hard_filters
from nostos.sources.base import Liveness
from nostos.sources.kijiji import KijijiSource, _detail_payload

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "kijiji"
SEARCH_HTML = (FIXTURE_DIR / "search_vancouver_kitsilano.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURE_DIR / "detail_1234567890.html").read_text(encoding="utf-8")


def test_discover_parses_jsonld_itemlist_from_fixture() -> None:
    fetcher = FixtureFetcher()
    source = KijijiSource(fetcher=fetcher, now_provider=_fixed_now)

    records = list(source.discover(_build_context()))

    assert len(records) == 2
    assert records[0].source == "kijiji"
    assert records[0].source_id == "1234567890"
    assert records[1].source_id == "9876543210"
    payload = _payload_mapping(records[0].payload)
    assert payload["title"] == "Bright 2 Bedroom in Kits"
    assert payload["price"] == 2895
    assert payload["full_unit"] is True
    assert payload["furnishing"] == "furnished"
    second_payload = _payload_mapping(records[1].payload)
    assert second_payload["full_unit"] is False
    assert second_payload["furnishing"] == "unfurnished"
    assert second_payload["area_sqft"] is None
    assert fetcher.urls == [
        "https://www.kijiji.ca/b-apartments-condos/vancouver/kitsilano/k0c37l1700287"
    ]


def test_fetch_detail_enriches_discovery_record() -> None:
    fetcher = FixtureFetcher()
    source = KijijiSource(fetcher=fetcher, now_provider=_fixed_now)
    context = _build_context()
    record = next(source.discover(context))

    enriched = source.fetch_detail(record)

    payload = _payload_mapping(enriched.payload)
    assert enriched.source_id == record.source_id
    assert (
        payload["description"]
        == "Updated detail page description with parking and furnished options."
    )
    assert payload["price"] == 2900
    assert payload["baths"] == 2.0
    assert payload["area_sqft"] == 840
    assert payload["photos"] == [
        "https://images.example/1234567890-detail-1.jpg",
        "https://images.example/1234567890-detail-2.jpg",
    ]
    assert source.check_liveness(enriched) is Liveness.OK
    assert fetcher.urls[1] == record.url


def test_to_listing_is_pure_and_structured() -> None:
    fetcher = FixtureFetcher()
    source = KijijiSource(fetcher=fetcher, now_provider=_fixed_now)
    context = _build_context()
    record = source.fetch_detail(next(source.discover(context)))
    dump_before = record.model_dump(mode="python")

    listing = source.to_listing(record, context)

    dump_after = record.model_dump(mode="python")
    assert dump_before == dump_after
    assert isinstance(listing, Listing)
    assert listing.identity.source == "kijiji"
    assert listing.identity.source_id == "1234567890"
    assert listing.place.area_key == "kits_beach"

    assert isinstance(listing.rent, Observed)
    assert listing.rent.value.amount == Decimal("2900")
    assert isinstance(listing.beds, Observed)
    assert listing.beds.value == 2.0
    assert isinstance(listing.baths, Observed)
    assert listing.baths.value == 2.0
    assert isinstance(listing.area, Observed)
    assert isinstance(listing.area.value, Area)
    assert listing.area.value.value == 840.0
    assert listing.parking == Absence.NOT_STATED
    assert isinstance(listing.furnishing, Observed)
    assert listing.furnishing.value == "furnished"


def test_to_listing_does_not_infer_area_key_from_description_only() -> None:
    source = KijijiSource(now_provider=_fixed_now)
    context = _build_context()
    record = SourceRecord(
        source="kijiji",
        source_id="1742684283",
        url="https://www.kijiji.ca/v-apartments-condos/vancouver/spacious-2-bedroom-suite/1742684283",
        content_hash="hash-description-only",
        fetched_at=_fixed_now(),
        payload={
            "title": "Spacious 2-bedroom suite in Fraserview",
            "description": "Beautiful apartment just minutes from Yaletown nightlife.",
            "address": "2200 East 54th Avenue Vancouver BC",
            "price": 2450,
        },
    )

    listing = source.to_listing(record, context)

    assert listing.place.area_key is None


def test_to_listing_removes_browser_title_suffix() -> None:
    source = KijijiSource(now_provider=_fixed_now)
    record = SourceRecord(
        source="kijiji",
        source_id="1741049008",
        url="https://www.kijiji.ca/v-apartments-condos/1741049008",
        content_hash="hash-title-suffix",
        fetched_at=_fixed_now(),
        payload={
            "title": (
                "Spacious 2-bedroom apartment | Long Term Rentals | "
                "City of Toronto | Free local classifieds - Kijiji"
            ),
            "price": 2800,
        },
    )

    listing = source.to_listing(record, _build_context())

    assert listing.attributes["title"].value == "Spacious 2-bedroom apartment"


def test_to_listing_infers_area_key_from_title_or_address() -> None:
    source = KijijiSource(now_provider=_fixed_now)
    context = _build_context()
    record = SourceRecord(
        source="kijiji",
        source_id="1742684284",
        url="https://www.kijiji.ca/v-apartments-condos/vancouver/yaletown-2-bedroom-rental/1742684284",
        content_hash="hash-title-address",
        fetched_at=_fixed_now(),
        payload={
            "title": "Yaletown 2-bedroom rental",
            "description": "Quiet unit with in-suite laundry.",
            "address": "1000 Homer Street Vancouver BC",
            "price": 3200,
        },
    )

    listing = source.to_listing(record, context)

    assert listing.place.area_key == "downtown_van"


def test_to_listing_basement_suite_title_fails_hard_filter_when_excluded() -> None:
    source = KijijiSource(now_provider=_fixed_now)
    context = _build_context(exclude=["basement"])
    record = SourceRecord(
        source="kijiji",
        source_id="1742684285",
        url=(
            "https://www.kijiji.ca/v-apartments-condos/vancouver/"
            "spacious-2-bedroom-basement-suite-in-fraserview/1742684285"
        ),
        content_hash="hash-basement-suite",
        fetched_at=_fixed_now(),
        payload={
            "title": "Spacious 2-bedroom basement suite in Fraserview",
            "description": "Private entrance and in-suite laundry.",
            "address": "2200 East 54th Avenue Vancouver BC",
            "price": 2450,
        },
    )

    listing = source.to_listing(record, context)

    assert passes_hard_filters(listing, context.profile) is False


def test_to_listing_basement_storage_text_passes_hard_filter_when_excluded() -> None:
    source = KijijiSource(now_provider=_fixed_now)
    context = _build_context(exclude=["basement"])
    record = SourceRecord(
        source="kijiji",
        source_id="1742684286",
        url=(
            "https://www.kijiji.ca/v-apartments-condos/vancouver/"
            "main-floor-rental-with-basement-storage/1742684286"
        ),
        content_hash="hash-basement-storage",
        fetched_at=_fixed_now(),
        payload={
            "title": "Main floor 2-bedroom rental",
            "description": "Includes basement storage locker and one underground parking stall.",
            "address": "123 Main Street Vancouver BC",
            "price": 2450,
        },
    )

    listing = source.to_listing(record, context)

    assert passes_hard_filters(listing, context.profile) is True


def test_to_listing_rejects_legacy_parking_boolean_without_raw_evidence() -> None:
    record = SourceRecord(
        source="kijiji",
        source_id="1742684287",
        url="https://www.kijiji.ca/v-apartments-condos/1742684287",
        content_hash="hash-legacy-parking",
        fetched_at=_fixed_now(),
        payload={
            "title": "Bright two bedroom apartment",
            "description": "Secure bicycle storage in the garage.",
            "address": "123 Main Street Vancouver BC",
            "parking": True,
            "price": 2500,
        },
    )

    listing = KijijiSource(now_provider=_fixed_now).to_listing(record, _build_context())

    assert listing.parking == Absence.NOT_STATED


def test_to_listing_rederives_negative_furnishing_label_from_raw_text() -> None:
    record = SourceRecord(
        source="kijiji",
        source_id="1742684290",
        url="https://www.kijiji.ca/v-apartments-condos/1742684290",
        content_hash="hash-furnished-no",
        fetched_at=_fixed_now(),
        payload={
            "title": "Bright apartment",
            "description": "Furnished: No; one roommate sought.",
            "furnishing": "furnished",
        },
    )

    listing = KijijiSource(now_provider=_fixed_now).to_listing(record, _build_context())

    assert isinstance(listing.furnishing, Observed)
    assert listing.furnishing.value == "unfurnished"


def test_to_listing_preserves_explicit_negative_and_paid_parking_evidence() -> None:
    source = KijijiSource(now_provider=_fixed_now)

    no_parking = SourceRecord(
        source="kijiji",
        source_id="1742684288",
        url="https://www.kijiji.ca/v-apartments-condos/1742684288",
        content_hash="hash-no-parking",
        fetched_at=_fixed_now(),
        payload={"description": "No parking available.", "parking": True},
    )
    paid_parking = SourceRecord(
        source="kijiji",
        source_id="1742684289",
        url="https://www.kijiji.ca/v-apartments-condos/1742684289",
        content_hash="hash-paid-parking",
        fetched_at=_fixed_now(),
        payload={"description": "Parking available for $150/month.", "parking": True},
    )

    no_parking_listing = source.to_listing(no_parking, _build_context())
    paid_parking_listing = source.to_listing(paid_parking, _build_context())

    assert isinstance(no_parking_listing.parking, Observed)
    assert no_parking_listing.parking.value == "Unavailable"
    assert isinstance(paid_parking_listing.parking, Observed)
    assert paid_parking_listing.parking.value == "Available"


def test_detail_parser_does_not_turn_negated_parking_into_a_positive() -> None:
    html = """
    <html><head><script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "Apartment",
      "url": "https://www.kijiji.ca/v-apartments-condos/1742684290",
      "name": "Two bedroom apartment",
      "description": "No parking available for this unit.",
      "offers": {"price": "2500"}
    }
    </script>
    <script>window.messages = {"removed": "This ad is no longer available."};</script>
    </head></html>
    """

    payload = _detail_payload(
        html=html,
        base_url="https://www.kijiji.ca/v-apartments-condos/1742684290",
    )

    assert payload["parking"] == "Unavailable"


def test_detail_parser_extracts_image_objects_and_bounds_gallery() -> None:
    images = [
        {"@type": "ImageObject", "contentUrl": f"https://images.example/{index}.jpg"}
        for index in range(55)
    ]
    node = {
        "@context": "https://schema.org",
        "@type": "Apartment",
        "url": "https://www.kijiji.ca/v-apartments-condos/1742684290",
        "name": "Two bedroom apartment",
        "description": "A" * 100_100,
        "offers": {"price": "2500"},
        "image": images,
    }
    html = (
        '<html><head><script type="application/ld+json">'
        f"{json.dumps(node)}</script></head></html>"
    )

    payload = _detail_payload(
        html=html,
        base_url="https://www.kijiji.ca/v-apartments-condos/1742684290",
    )

    assert len(str(payload["description"]).encode("utf-8")) <= 100_000
    assert payload["description_truncated"] is True
    photos = payload["photos"]
    assert isinstance(photos, list)
    assert len(photos) == 50
    assert payload["photos_truncated"] is True


def test_fetch_detail_preserves_nonempty_discovery_fields() -> None:
    record = SourceRecord(
        source="kijiji",
        source_id="1742684290",
        url="https://www.kijiji.ca/v-apartments-condos/1742684290",
        content_hash="discovery-hash",
        fetched_at=_fixed_now(),
        payload={
            "title": "Useful discovery title",
            "description": "Useful discovery description",
            "photos": ["https://images.example/discovery.jpg"],
        },
    )
    html = """
    <html><head><script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "Apartment",
      "url": "https://www.kijiji.ca/v-apartments-condos/1742684290",
      "name": "",
      "description": "",
      "offers": {"price": "2500"},
      "image": []
    }
    </script>
    <script>window.messages = {"removed": "This ad is no longer available."};</script>
    </head></html>
    """

    detailed = KijijiSource(fetcher=lambda _: html, now_provider=_fixed_now).fetch_detail(record)
    payload = _payload_mapping(detailed.payload)

    assert payload["title"] == "Useful discovery title"
    assert payload["description"] == "Useful discovery description"
    assert payload["photos"] == ["https://images.example/discovery.jpg"]
    assert payload["detail_status"] == "complete"


def test_fetch_detail_rejects_wrong_listing_identity() -> None:
    record = SourceRecord(
        source="kijiji",
        source_id="1742684290",
        url="https://www.kijiji.ca/v-apartments-condos/1742684290",
        content_hash="discovery-hash",
        fetched_at=_fixed_now(),
        payload={"title": "Discovery title"},
    )
    html = """
    <html><head><script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "Apartment",
      "url": "https://www.kijiji.ca/v-apartments-condos/9999999999",
      "name": "Wrong unit",
      "offers": {"price": "2500"}
    }
    </script></head></html>
    """

    detailed = KijijiSource(fetcher=lambda _: html, now_provider=_fixed_now).fetch_detail(record)
    payload = _payload_mapping(detailed.payload)

    assert payload["detail_status"] == "failed"
    assert payload["title"] == "Discovery title"


def test_fetch_detail_reports_blocked_removed_and_sanitized_failures() -> None:
    record = SourceRecord(
        source="kijiji",
        source_id="1742684290",
        url="https://www.kijiji.ca/v-apartments-condos/1742684290",
        content_hash="discovery-hash",
        fetched_at=_fixed_now(),
        payload={"title": "Discovery title"},
    )
    blocked = KijijiSource(
        fetcher=lambda _: "<html><body>Verify you are human to continue.</body></html>",
        now_provider=_fixed_now,
    ).fetch_detail(record)
    removed = KijijiSource(
        fetcher=lambda _: "<html><body>This ad is no longer available.</body></html>",
        now_provider=_fixed_now,
    ).fetch_detail(record)

    def fail(_: str) -> str:
        raise RuntimeError("credential from /private/secret")

    failed = KijijiSource(fetcher=fail, now_provider=_fixed_now).fetch_detail(record)

    assert _payload_mapping(blocked.payload)["detail_status"] == "blocked"
    assert _payload_mapping(removed.payload)["detail_status"] == "removed"
    failed_payload = _payload_mapping(failed.payload)
    assert failed_payload["detail_status"] == "failed"
    assert "/private/secret" not in str(failed_payload["detail_error"])


class FixtureFetcher:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, url: str) -> str:
        self.urls.append(url)
        if "/b-apartments-condos/" in url:
            return SEARCH_HTML
        if "/v-apartments-condos/" in url:
            return DETAIL_HTML
        raise AssertionError(f"Unexpected URL {url!r}")


def _payload_mapping(payload: Any) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        return payload
    raise AssertionError("payload must be a mapping")


def _fixed_now() -> datetime:
    return datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _build_context(*, exclude: list[str] | None = None) -> SearchContext:
    citypack = Citypack.model_validate(
        {
            "name": "vancouver",
            "locale": {
                "language": "en-CA",
                "timezone": "America/Vancouver",
                "currency": "CAD",
                "area_unit": "sqft",
            },
            "areas": [
                {
                    "key": "kits_beach",
                    "label": "Kitsilano",
                    "keywords": ["kitsilano", "kits"],
                    "bbox": [49.262, -123.190, 49.278, -123.145],
                },
                {
                    "key": "downtown_van",
                    "label": "Downtown",
                    "keywords": ["downtown", "yaletown"],
                    "bbox": [49.275, -123.130, 49.290, -123.105],
                },
            ],
            "sources": {
                "kijiji": {
                    "enabled": True,
                    "load_bearing": False,
                    "regions": [
                        {
                            "path": "vancouver",
                            "id": "c37l1700287",
                            "keywords": ["kitsilano"],
                        }
                    ],
                }
            },
            "address": {
                "directional": {"w": "west"},
                "strip_tokens": ["vancouver"],
                "region_tokens": ["bc"],
            },
        }
    )
    profile = Profile.model_validate(
        {
            "city": "vancouver",
            "hard": {"exclude": exclude or []},
            "weights": {},
            "sources": {"kijiji": "on"},
            "notify": [],
            "schedule": "0 */6 * * *",
        }
    )
    return SearchContext(citypack=citypack, profile=profile)
