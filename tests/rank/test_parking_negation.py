from __future__ import annotations

from datetime import UTC, datetime

from nostos.config.citypack import Citypack
from nostos.config.profile import Profile
from nostos.context import SearchContext
from nostos.enrich.text import TextRuleEnricher
from nostos.model import SourceRecord
from nostos.rank.engine import RankEngine
from nostos.rank.profile_scoring import score_listing_for_profile
from nostos.sources.craigslist import CraigslistSource

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _context() -> SearchContext:
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
                    "keywords": ["kitsilano"],
                    "bbox": [49.262, -123.190, 49.278, -123.145],
                }
            ],
            "sources": {"craigslist": {"enabled": True, "load_bearing": False}},
            "address": {"directional": {}, "strip_tokens": [], "region_tokens": []},
        }
    )
    profile = Profile.model_validate(
        {
            "city": "vancouver",
            "hard": {},
            "weights": {"parking.available": 5},
            "sources": {"craigslist": "on"},
            "schedule": "0 */6 * * *",
        }
    )
    return SearchContext(citypack=citypack, profile=profile)


def _parking_contribution(description: str) -> float:
    context = _context()
    record = SourceRecord(
        source="craigslist",
        source_id="x1",
        url="https://example.com/x1",
        payload={
            "title": "2BR apartment",
            "address": "1 Main St, Vancouver",
            "price": 3000,
            "beds": 2,
            "baths": 1,
            "sqft": 800,
            "posted": NOW.isoformat(),
            "images": [],
            "description": description,
        },
        content_hash="h",
        fetched_at=NOW,
    )
    listing = CraigslistSource().to_listing(record, context)
    scored = score_listing_for_profile(
        listing,
        context=context,
        enrichers=(TextRuleEnricher(),),
        rank_engine=RankEngine(context.profile),
    )
    assert scored is not None
    by_key = {item.rule_key: item for item in scored.result.contributions}
    return by_key["parking.available"].contribution


def test_no_parking_does_not_score_as_parking_available() -> None:
    # The enricher normalizes "no parking" to the field value "Unavailable";
    # a substring check for "available" used to treat that as a match.
    assert _parking_contribution("Coin laundry, no parking. Available Oct 1.") == 0.0


def test_parking_included_scores_positive() -> None:
    assert _parking_contribution("Parking included, in-suite laundry.") > 0.0
