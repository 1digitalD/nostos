from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nostos.config.citypack import Citypack
from nostos.config.profile import Profile
from nostos.context import SearchContext
from nostos.model import SourceRecord
from nostos.rank.rescore import rescore_profile
from nostos.sources.craigslist import CraigslistSource
from nostos.store.db import apply_migrations, connect
from nostos.store.repo import ListingRepo, ScoreRepo

NOW = datetime(2026, 1, 1, tzinfo=UTC)
PROFILE_ID = "profile"
LISTING_ID = "craigslist:12345"


def _citypack() -> Citypack:
    return Citypack.model_validate(
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
            "sources": {
                "craigslist": {"enabled": True, "load_bearing": False},
            },
            "address": {"directional": {}, "strip_tokens": [], "region_tokens": []},
        }
    )


def _context(hard: Mapping[str, object]) -> SearchContext:
    profile = Profile.model_validate(
        {
            "city": "vancouver",
            "hard": dict(hard),
            "weights": {"laundry.in_suite": 6, "photo.present": 2},
            "sources": {"craigslist": "on"},
            "schedule": "0 */6 * * *",
        }
    )
    return SearchContext(citypack=_citypack(), profile=profile)


def _seed_listing(db_path: Path) -> None:
    with connect(db_path) as conn:
        apply_migrations(conn)
        listing_repo = ListingRepo(conn)
        listing_repo.ensure_listing(LISTING_ID, seen_at=NOW)
        listing_repo.add_source_record(
            listing_id=LISTING_ID,
            record=SourceRecord(
                source="craigslist",
                source_id=LISTING_ID.split(":", 1)[1],
                url=f"https://example.com/{LISTING_ID}",
                payload={
                    "title": "Sunny 2BR in Kitsilano",
                    "address": "1234 West 4th Ave, Kitsilano",
                    "price": 2800,
                    "beds": 2,
                    "baths": 1,
                    "sqft": 850,
                    "posted": NOW.isoformat(),
                    "photos": ["https://example.com/p1.jpg"],
                    "description": "Bright corner unit with in-suite laundry near the beach.",
                },
                content_hash="hash",
                fetched_at=NOW,
            ),
        )


def test_rescore_writes_scores_for_passing_listings_then_drops_them_when_filtered(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "nostos.db"
    _seed_listing(db_path)
    sources = {"craigslist": CraigslistSource()}

    with connect(db_path) as conn:
        report = rescore_profile(
            conn,
            context=_context({"rent": {"max": 3200, "currency": "CAD"}}),
            profile_id=PROFILE_ID,
            sources=sources,
        )
        assert report.profile_id == PROFILE_ID
        assert report.scored_count == 1
        assert report.skipped == 0
        assert report.rows[0][0] == LISTING_ID

        stored = ScoreRepo(conn).get_score(LISTING_ID, PROFILE_ID)
        assert stored is not None
        assert stored.score == pytest.approx(report.rows[0][1])
        contributions = stored.breakdown_json["contributions"]
        assert isinstance(contributions, list)
        by_key = {
            str(item["rule_key"]): item for item in contributions if isinstance(item, dict)
        }
        assert set(by_key) == {"laundry.in_suite", "photo.present"}
        # Photos come straight from the source record: full confidence, full weight.
        assert by_key["photo.present"]["contribution"] == pytest.approx(2.0)
        # In-suite laundry is inferred from the description by TextRuleEnricher at
        # confidence 0.6, so it contributes 6 * 0.6 = 3.6 of a possible 6.
        assert by_key["laundry.in_suite"]["confidence_factor"] == pytest.approx(0.6)
        assert by_key["laundry.in_suite"]["contribution"] == pytest.approx(3.6)
        # (3.6 + 2.0) / (6 + 2) = 70%.
        assert stored.score == pytest.approx(70.0)

    # Tighten the hard filter below the listing's rent: the row must disappear.
    with connect(db_path) as conn:
        report = rescore_profile(
            conn,
            context=_context({"rent": {"max": 2500, "currency": "CAD"}}),
            profile_id=PROFILE_ID,
            sources=sources,
        )
        assert report.scored_count == 0
        assert report.skipped == 1
        assert ScoreRepo(conn).get_score(LISTING_ID, PROFILE_ID) is None


def test_rescore_skips_listings_whose_source_is_not_instantiated(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    _seed_listing(db_path)

    with connect(db_path) as conn:
        report = rescore_profile(
            conn,
            context=_context({"rent": {"max": 3200, "currency": "CAD"}}),
            profile_id=PROFILE_ID,
            sources={},
        )
        assert report.scored_count == 0
        assert report.skipped == 1
        assert ScoreRepo(conn).get_score(LISTING_ID, PROFILE_ID) is None
