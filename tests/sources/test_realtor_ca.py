from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from nostos.config.citypack import load_citypack
from nostos.config.profile import Profile
from nostos.context import SearchContext
from nostos.model import Listing, Observed
from nostos.sources.base import Liveness
from nostos.sources.realtor_ca import RealtorCaSource

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "realtor_ca"
SEARCH_HTML = (FIXTURE_DIR / "search_toronto.html").read_text(encoding="utf-8")
SEARCH_JSON = (FIXTURE_DIR / "search_toronto.json").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURE_DIR / "detail_30240799.html").read_text(encoding="utf-8")
FIXED_NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)


class FixtureFetcher:
    def __init__(self, search_response: str = SEARCH_JSON) -> None:
        self.urls: list[str] = []
        self.search_response = search_response

    def __call__(self, url: str) -> str:
        self.urls.append(url)
        if "/map#" in url:
            return self.search_response
        if "/real-estate/30240799/" in url:
            return DETAIL_HTML
        raise AssertionError(f"Unexpected Realtor.ca URL: {url}")


def test_discovery_is_toronto_scoped_and_uses_profile_bedrooms() -> None:
    fetcher = FixtureFetcher()
    source = RealtorCaSource(fetcher=fetcher, now_provider=lambda: FIXED_NOW)

    records = list(source.discover(_context()))

    assert len(records) == 1
    assert records[0].source_id == "30240799"
    payload = _payload(records[0].payload)
    assert payload["address"] == (
        "622 - 585 BLOOR STREET E, Toronto (North St. James Town), Ontario M4W0B3"
    )
    assert payload["price"] == 3300
    assert payload["beds"] == 2
    assert payload["baths"] == 2
    assert payload["area_sqft"] == 800
    assert payload["point"] == {"lat": 43.6717031, "lng": -79.3722379}
    assert "BedRange=2-2" in fetcher.urls[0]


def test_discovery_keeps_rendered_html_fallback_for_recorded_pages() -> None:
    source = RealtorCaSource(fetcher=FixtureFetcher(SEARCH_HTML), now_provider=lambda: FIXED_NOW)

    records = list(source.discover(_context()))

    assert [record.source_id for record in records] == ["30240799"]


def test_detail_and_listing_capture_required_amenities() -> None:
    source = RealtorCaSource(fetcher=FixtureFetcher(), now_provider=lambda: FIXED_NOW)
    context = _context()
    record = source.fetch_detail(next(source.discover(context)))

    payload = _payload(record.payload)
    assert payload["management"] == "Del Property Management Inc"
    assert payload["parking_spaces"] == 1
    assert "In suite Laundry" in str(payload["description"])
    assert "Exercise Centre" in str(payload["description"])
    assert source.check_liveness(record) is Liveness.OK

    listing = source.to_listing(record, context)
    assert isinstance(listing, Listing)
    assert isinstance(listing.rent, Observed)
    assert listing.rent.value.amount == Decimal("3300.0")
    assert isinstance(listing.parking, Observed)
    assert listing.parking.value == "Included"
    assert listing.attributes["management_company"].value == "Del Property Management Inc"
    assert len(listing.photos) == 2


def _context() -> SearchContext:
    citypack_path = (
        Path(__file__).resolve().parents[2] / "src" / "nostos" / "citypacks" / "toronto.yaml"
    )
    profile = Profile.model_validate(
        {
            "city": "toronto",
            "hard": {"beds": {"eq": 2}, "exclude": []},
            "weights": {},
            "sources": {"realtor_ca": "on"},
            "notify": [],
            "schedule": "0 */6 * * *",
        }
    )
    return SearchContext(citypack=load_citypack(citypack_path), profile=profile)


def _payload(value: object) -> Mapping[str, Any]:
    assert isinstance(value, Mapping)
    return value
