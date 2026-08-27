from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from nostos.config.citypack import Citypack
from nostos.config.profile import Profile
from nostos.context import SearchContext
from nostos.model import Area, Money, Observed
from nostos.sources.base import Liveness
from nostos.sources.craigslist import CraigslistSource, cl_posted_iso, parse_cl_rss

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "craigslist"
FIXED_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_parse_cl_rss_filters_by_cutoff_and_preserves_base62_case() -> None:
    items = parse_cl_rss(_fixture("rss.xml"), last_scan_iso="2024-08-06T12:00:00Z")
    assert [item["id"] for item in items] == ["AbC123xYz9"]
    assert items[0]["source"] == "craigslist"
    assert items[0]["price"] == 2950
    assert items[0]["posted"] == "2024-08-06T18:23:40Z"


def test_cl_posted_iso_handles_absolute_relative_and_month_day_labels() -> None:
    assert (
        cl_posted_iso("2024-08-06T18:23:40+00:00", label="", current=FIXED_NOW)
        == "2024-08-06T18:23:40Z"
    )
    assert cl_posted_iso("", label="2 hr ago", current=FIXED_NOW) == "2026-01-02T01:04:05Z"
    assert cl_posted_iso("", label="8/06", current=FIXED_NOW) == "2025-08-06T00:00:00Z"


def test_to_listing_is_pure_and_uses_fixture_payloads() -> None:
    source = CraigslistSource(fetch_text=_fixture_fetch_text, now=lambda: FIXED_NOW)
    context = _build_context()
    records = list(source.discover(context))
    assert records
    record = next(rec for rec in records if rec.source_id == "AbC123xYz9")

    detailed = source.fetch_detail(record)
    assert source.check_liveness(detailed) is Liveness.OK

    before_dump = detailed.model_dump(mode="python")
    listing = source.to_listing(detailed, context)
    after_dump = detailed.model_dump(mode="python")

    assert before_dump == after_dump
    assert source.to_listing(detailed, context) == listing
    assert listing.identity.source_id == "AbC123xYz9"
    assert listing.identity.listing_id == "craigslist:AbC123xYz9"
    assert listing.place.raw_address == "1234 W 10th Ave, Vancouver, BC"

    assert isinstance(listing.rent, Observed)
    assert listing.rent.value == Money(amount=Decimal("2950"), currency="CAD", period="month")
    assert isinstance(listing.beds, Observed)
    assert listing.beds.value == 2.0
    assert isinstance(listing.baths, Observed)
    assert listing.baths.value == 1.5
    assert isinstance(listing.area, Observed)
    assert listing.area.value == Area(value=850.0, unit="sqft")
    assert isinstance(listing.floor, Observed)
    assert listing.floor.value == 3
    assert isinstance(listing.parking, Observed)
    assert listing.parking.value == "Included"
    assert isinstance(listing.furnishing, Observed)
    assert listing.furnishing.value == "Unfurnished"
    assert len(listing.photos) == 1
    assert listing.photos[0].url.endswith(".jpg")


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _fixture_fetch_text(url: str) -> str:
    if "format=rss" in url:
        return _fixture("rss.xml")
    if "AbC123xYz9" in url:
        return _fixture("detail.html")
    if "zZ9yY8xX7w" in url:
        return _fixture("detail.html")
    raise AssertionError(f"unexpected craigslist fixture URL: {url}")


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
                    "keywords": ["kits beach", "kitsilano"],
                    "bbox": [49.262, -123.190, 49.278, -123.145],
                }
            ],
            "sources": {
                "craigslist": {
                    "enabled": True,
                    "load_bearing": True,
                    "base_url": "https://vancouver.craigslist.org",
                    "areas": ["van"],
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
            "hard": {
                "rent": {"max": 3600, "currency": "CAD"},
                "beds": {"eq": 2},
                "area": {"min": 700, "unit": "sqft"},
                "exclude": [],
            },
            "weights": {},
            "sources": {"craigslist": "on"},
            "notify": [],
            "schedule": "0 */6 * * *",
        }
    )
    return SearchContext(citypack=citypack, profile=profile)
