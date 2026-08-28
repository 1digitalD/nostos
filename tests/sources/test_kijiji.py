from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from nostos.config.citypack import Citypack
from nostos.config.profile import Profile
from nostos.context import SearchContext
from nostos.model import Area, Listing, Observed, SourceRecord
from nostos.rank.profile_scoring import passes_hard_filters
from nostos.sources.base import Liveness
from nostos.sources.kijiji import KijijiSource

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
    assert isinstance(listing.parking, Observed)
    assert listing.parking.value == "available"
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
