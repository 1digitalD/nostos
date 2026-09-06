from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nostos.config.citypack import Citypack
from nostos.config.profile import Profile
from nostos.context import SearchContext
from nostos.enrich.review import (
    ExtractionPreview,
    StaleExtractionPreview,
    apply_extraction_revision,
    load_current_extraction_revision,
    preview_extraction_revision,
)
from nostos.model import Origin, SourceRecord
from nostos.sources.craigslist import CraigslistSource
from nostos.store.db import apply_migrations, connect
from nostos.store.repo import ListingRepo, ObservationRepo, ScoreRepo
from nostos.workflows import clear_listing_correction

NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)
LISTING_ID = "canonical-listing"
PROFILE_ID = "balanced"


def _context(*, max_rent: float = 3_200) -> SearchContext:
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
                    "key": "downtown",
                    "label": "Downtown",
                    "keywords": ["downtown"],
                    "bbox": [49.0, -124.0, 50.0, -123.0],
                }
            ],
            "sources": {"craigslist": {"enabled": True, "load_bearing": False}},
            "address": {"directional": {}, "strip_tokens": [], "region_tokens": []},
        }
    )
    profile = Profile.model_validate(
        {
            "city": "vancouver",
            "unknown_policy": "review",
            "hard": {"rent": {"max": max_rent, "currency": "CAD"}},
            "weights": {"laundry.in_suite": 5},
            "sources": {"craigslist": "on"},
            "schedule": "0 */6 * * *",
        }
    )
    return SearchContext(citypack=citypack, profile=profile)


def _seed(conn: sqlite3.Connection, *, rent: int = 2_800, content_hash: str = "saved-v1") -> None:
    repo = ListingRepo(conn)
    repo.ensure_listing(LISTING_ID, seen_at=NOW)
    repo.add_source_record(
        listing_id=LISTING_ID,
        record=SourceRecord(
            source="craigslist",
            source_id="abc123",
            url="https://vancouver.craigslist.org/van/apa/d/example/abc123.html",
            payload={
                "title": "TOP-FLOOR 2-BEDROOM apartment",
                "price": rent,
                "beds": 2,
                "baths": 1,
                "description": "Bright home with private in-suite laundry.",
                "photos": ["https://images.example/one.jpg"],
            },
            content_hash=content_hash,
            fetched_at=NOW,
        ),
    )


def _preview(
    conn: sqlite3.Connection, *, context: SearchContext | None = None
) -> ExtractionPreview:
    return preview_extraction_revision(
        conn,
        listing_id=LISTING_ID,
        context=context or _context(),
        profile_id=PROFILE_ID,
        sources={"craigslist": CraigslistSource()},
    )


def test_preview_apply_persists_canonical_snapshot_and_retires_legacy_claim(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "nostos.db") as conn:
        apply_migrations(conn)
        _seed(conn)
        ObservationRepo(conn).record_observation(
            listing_id=LISTING_ID,
            field="floor",
            value_json=2,
            origin=Origin.SOURCE_FIELD,
            confidence=1.0,
            evidence="legacy top-floor heuristic",
            observed_at=NOW,
        )

        preview = _preview(conn)
        floor_change = next(change for change in preview.changes if change.field == "floor")
        assert floor_change.before is not None and floor_change.before.value == 2
        assert floor_change.after is not None and floor_change.after.value == "not_stated"
        assert preview.machine_listing.identity.listing_id == LISTING_ID
        assert isinstance(preview.source.payload, Mapping)
        assert preview.source.payload["description"] == (
            "Bright home with private in-suite laundry."
        )

        applied = apply_extraction_revision(
            conn,
            listing_id=LISTING_ID,
            preview_token=preview.preview_token,
            context=_context(),
        )
        current = load_current_extraction_revision(conn, listing_id=LISTING_ID)
        projection = ListingRepo(conn).get_fields_projection(LISTING_ID)

        assert not applied.repeated
        assert current is not None
        assert current.machine_listing.identity.listing_id == LISTING_ID
        assert "floor" not in projection
        laundry = projection["attributes.in_suite_laundry"]
        assert isinstance(laundry, dict)
        assert laundry["value"] is True
        assert ScoreRepo(conn).get_score(LISTING_ID, PROFILE_ID) is not None


def test_apply_rejects_correction_after_preview_and_repeat_apply_is_safe(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "nostos.db") as conn:
        apply_migrations(conn)
        _seed(conn)
        preview = _preview(conn)
        ObservationRepo(conn).record_observation(
            listing_id=LISTING_ID,
            field="attributes.in_suite_laundry",
            value_json=False,
            origin=Origin.USER,
            confidence=1.0,
            evidence="user correction",
            observed_at=datetime(2026, 9, 6, 13, tzinfo=UTC),
        )

        with pytest.raises(StaleExtractionPreview, match="user correction"):
            apply_extraction_revision(
                conn,
                listing_id=LISTING_ID,
                preview_token=preview.preview_token,
                context=_context(),
            )

        fresh = _preview(conn)
        first = apply_extraction_revision(
            conn,
            listing_id=LISTING_ID,
            preview_token=fresh.preview_token,
            context=_context(),
        )
        second = apply_extraction_revision(
            conn,
            listing_id=LISTING_ID,
            preview_token=fresh.preview_token,
            context=_context(),
        )
        revision_rows = conn.execute(
            "SELECT COUNT(*) FROM observation WHERE extraction_review_id=?",
            (fresh.preview_token,),
        ).fetchone()[0]

        assert not first.repeated
        assert second.repeated
        assert revision_rows > 0
        assert conn.execute(
            "SELECT COUNT(DISTINCT extraction_review_id) FROM observation "
            "WHERE extraction_review_id IS NOT NULL"
        ).fetchone()[0] == 1
        clear_listing_correction(
            conn,
            listing_id=LISTING_ID,
            field="attributes.in_suite_laundry",
        )
        projection = ListingRepo(conn).get_fields_projection(LISTING_ID)
        laundry = projection["attributes.in_suite_laundry"]
        assert isinstance(laundry, dict)
        assert laundry["origin"] == Origin.TEXT_RULE.value
        assert laundry["value"] is True


def test_hard_filter_miss_still_persists_enrichment_and_preserves_user_state(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "nostos.db") as conn:
        apply_migrations(conn)
        _seed(conn, rent=4_000)
        conn.execute(
            """
            INSERT INTO user_state(
                listing_id,profile_id,shortlisted,excluded,contact_status,notes,
                viewing_at,viewing_done
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (LISTING_ID, PROFILE_ID, 1, 0, "contacted", "Keep this note", None, 0),
        )
        preview = _preview(conn)
        assert not preview.eligible

        apply_extraction_revision(
            conn,
            listing_id=LISTING_ID,
            preview_token=preview.preview_token,
            context=_context(),
        )
        projection = ListingRepo(conn).get_fields_projection(LISTING_ID)
        state = conn.execute(
            "SELECT shortlisted,contact_status,notes FROM user_state WHERE listing_id=?",
            (LISTING_ID,),
        ).fetchone()

        laundry = projection["attributes.in_suite_laundry"]
        assert isinstance(laundry, dict)
        assert laundry["value"] is True
        assert ScoreRepo(conn).get_score(LISTING_ID, PROFILE_ID) is None
        assert tuple(state) == (1, "contacted", "Keep this note")


def test_failed_score_write_rolls_back_revision_and_machine_observations(tmp_path: Path) -> None:
    with connect(tmp_path / "nostos.db") as conn:
        apply_migrations(conn)
        _seed(conn)
        preview = _preview(conn)
        conn.execute(
            """
            CREATE TRIGGER reject_review_score BEFORE INSERT ON score
            BEGIN SELECT RAISE(ABORT, 'test score failure'); END
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="test score failure"):
            apply_extraction_revision(
                conn,
                listing_id=LISTING_ID,
                preview_token=preview.preview_token,
                context=_context(),
            )

        review = conn.execute(
            "SELECT applied_at FROM extraction_review WHERE preview_token=?",
            (preview.preview_token,),
        ).fetchone()
        observations = conn.execute(
            "SELECT COUNT(*) FROM observation WHERE extraction_review_id=?",
            (preview.preview_token,),
        ).fetchone()[0]
        assert review["applied_at"] is None
        assert observations == 0
        assert load_current_extraction_revision(conn, listing_id=LISTING_ID) is None


def test_source_or_profile_change_makes_preview_stale_and_current_snapshot_unavailable(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "nostos.db") as conn:
        apply_migrations(conn)
        _seed(conn)
        preview = _preview(conn)
        with pytest.raises(StaleExtractionPreview, match="profile changed"):
            apply_extraction_revision(
                conn,
                listing_id=LISTING_ID,
                preview_token=preview.preview_token,
                context=_context(max_rent=3_100),
            )

        applied = apply_extraction_revision(
            conn,
            listing_id=LISTING_ID,
            preview_token=preview.preview_token,
            context=_context(),
        )
        assert applied.preview_token == preview.preview_token
        _seed(conn, content_hash="saved-v2")
        assert load_current_extraction_revision(conn, listing_id=LISTING_ID) is None
