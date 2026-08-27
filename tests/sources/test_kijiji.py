from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from nostos.config.citypack import Citypack
from nostos.config.profile import Profile
from nostos.context import SearchContext
from nostos.model import Area, Listing, Observed
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


def _build_context() -> SearchContext:
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
                    "key": "downtown",
                    "label": "Downtown",
                    "keywords": ["downtown"],
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
            "hard": {"exclude": []},
            "weights": {},
            "sources": {"kijiji": "on"},
            "notify": [],
            "schedule": "0 */6 * * *",
        }
    )
    return SearchContext(citypack=citypack, profile=profile)
