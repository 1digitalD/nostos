from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from nostos.config.citypack import Citypack
from nostos.config.profile import Profile
from nostos.context import SearchContext, SourceScanState
from nostos.model import Area, Money, Observed, SourceRecord
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


def test_discover_uses_previous_watermark_for_rss_prefilter() -> None:
    source = CraigslistSource(fetch_text=_fixture_fetch_text, now=lambda: FIXED_NOW)
    context = _build_context(
        source_scan_state={
            "craigslist": SourceScanState(previous_watermark="2024-08-06T19:00:00Z")
        }
    )

    records = list(source.discover(context))
    assert records == []


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


@pytest.mark.parametrize("rss_mode", ["403", "blocked_html"])
def test_discover_falls_back_to_html_when_rss_is_blocked(rss_mode: str) -> None:
    source = CraigslistSource(fetch_text=_fallback_fixture_fetcher(rss_mode), now=lambda: FIXED_NOW)
    records = list(source.discover(_build_context()))

    assert [record.source_id for record in records] == ["AbC123xYz9", "zZ9yY8xX7w"]
    first = records[0]
    first_payload = _record_payload(first)
    assert first.url == "https://www.craigslist.org/view/d/vancouver-bright-2br/AbC123xYz9"
    assert first_payload["title"] == "Bright 2BR near Kits Beach"
    assert first_payload["price"] == 2400


def test_discover_blocked_html_falls_back_even_with_previous_watermark() -> None:
    source = CraigslistSource(
        fetch_text=_fallback_fixture_fetcher("blocked_html"),
        now=lambda: FIXED_NOW,
    )
    context = _build_context(
        source_scan_state={
            "craigslist": SourceScanState(previous_watermark="2024-08-06T19:00:00Z")
        }
    )
    records = list(source.discover(context))
    assert [record.source_id for record in records] == ["AbC123xYz9", "zZ9yY8xX7w"]


def test_discover_html_fallback_omits_format_and_extracts_fields() -> None:
    seen_urls: list[str] = []

    def fetch_text(url: str) -> str:
        seen_urls.append(url)
        if "format=rss" in url:
            return _fixture("rss_blocked.html")
        if "/search/van/apa?" in url:
            return _fixture("search_results.html")
        raise AssertionError(f"unexpected craigslist fixture URL: {url}")

    source = CraigslistSource(fetch_text=fetch_text, now=lambda: FIXED_NOW)
    records = list(source.discover(_build_context()))

    assert records
    html_search_url = next(
        url for url in seen_urls if "format=rss" not in url and "/search/van/apa?" in url
    )
    assert "format" not in parse_qs(urlparse(html_search_url).query)
    assert records[0].source_id == "AbC123xYz9"
    first_payload = _record_payload(records[0])
    assert first_payload["title"] == "Bright 2BR near Kits Beach"
    assert first_payload["price"] == 2400
    assert first_payload["location"] == "Vancouver Westside near UBC"
    assert records[0].url == "https://www.craigslist.org/view/d/vancouver-bright-2br/AbC123xYz9"


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


def _fallback_fixture_fetcher(rss_mode: str) -> Callable[[str], str]:
    def fetch_text(url: str) -> str:
        if "format=rss" in url:
            if rss_mode == "blocked_html":
                return _fixture("rss_blocked.html")
            request = httpx.Request("GET", url)
            response = httpx.Response(status_code=403, request=request, text="blocked")
            raise httpx.HTTPStatusError("403 blocked", request=request, response=response)
        if "/search/van/apa?" in url:
            return _fixture("search_results.html")
        raise AssertionError(f"unexpected craigslist fallback fixture URL: {url}")

    return fetch_text


def _record_payload(record: SourceRecord) -> Mapping[str, Any]:
    payload = record.payload
    if not isinstance(payload, Mapping):
        raise AssertionError("expected craigslist test payload to be a mapping")
    return payload


def _build_context(
    source_scan_state: dict[str, SourceScanState] | None = None,
) -> SearchContext:
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
    return SearchContext(
        citypack=citypack,
        profile=profile,
        source_scan_state=source_scan_state or {},
    )
