from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from nostos.config.citypack import Citypack
from nostos.config.profile import Profile
from nostos.context import SearchContext
from nostos.corrections import apply_user_corrections
from nostos.decision import build_decision_brief
from nostos.enrich.chain import run_enricher_chain
from nostos.enrich.review import load_current_extraction_revision
from nostos.enrich.text import TextRuleEnricher
from nostos.model import Absence, Area, Observed, Origin, SourceRecord
from nostos.sources.kijiji import KijijiSource
from nostos.store.db import apply_migrations, connect
from nostos.store.repo import ListingRepo

NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)
LISTING_ID = "saved-evidence-listing"


def _context() -> SearchContext:
    return SearchContext(
        citypack=Citypack.model_validate(
            {
                "name": "toronto",
                "locale": {
                    "language": "en-CA",
                    "timezone": "America/Toronto",
                    "currency": "CAD",
                    "area_unit": "sqft",
                },
                "areas": [
                    {
                        "key": "downtown",
                        "label": "Downtown",
                        "keywords": ["downtown"],
                        "bbox": [43.63, -79.42, 43.67, -79.36],
                    }
                ],
                "sources": {"kijiji": {"enabled": True, "load_bearing": False}},
                "address": {"directional": {}, "strip_tokens": [], "region_tokens": []},
            }
        ),
        profile=Profile.model_validate(
            {
                "city": "toronto",
                "hard": {},
                "weights": {},
                "sources": {"kijiji": "on"},
                "schedule": "0 */6 * * *",
            }
        ),
    )


def test_saved_content_v1_snapshot_requires_review_after_parser_contract_change(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "nostos.db") as conn:
        apply_migrations(conn)
        source_record_id = ListingRepo(conn).add_source_record(
            listing_id=LISTING_ID,
            record=SourceRecord(
                source="kijiji",
                source_id="1742000099",
                url="https://www.kijiji.ca/v-apartments-condos/1742000099",
                content_hash="saved-source-hash",
                fetched_at=NOW,
                payload={"title": "Quiet apartment", "area_sqft": 0},
            ),
        )
        conn.execute(
            """
            INSERT INTO extraction_review(
                preview_token,listing_id,source_record_id,source,source_id,source_url,
                source_content_hash,source_snapshot_json,extractor_revision,
                correction_revision,supporting_fingerprint,profile_id,profile_fingerprint,
                machine_listing_json,resolved_listing_json,changes_json,previous_eligible,
                eligible,score_json,created_at,applied_at,superseded_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "legacy-preview",
                LISTING_ID,
                source_record_id,
                "kijiji",
                "1742000099",
                "https://www.kijiji.ca/v-apartments-condos/1742000099",
                "saved-source-hash",
                "{}",
                "saved-content-v1",
                0,
                "supporting-fingerprint",
                "toronto",
                "profile-fingerprint",
                "{}",
                "{}",
                "[]",
                0,
                0,
                None,
                NOW.isoformat(),
                NOW.isoformat(),
                None,
            ),
        )

        assert load_current_extraction_revision(conn, listing_id=LISTING_ID) is None


def test_user_corrections_still_override_reparsed_saved_evidence(tmp_path: Path) -> None:
    source_listing = KijijiSource(now_provider=lambda: NOW).to_listing(
        SourceRecord(
            source="kijiji",
            source_id="1742000100",
            url="https://www.kijiji.ca/v-apartments-condos/1742000100",
            content_hash="saved-source-with-parking",
            fetched_at=NOW,
            payload={
                "title": "Quiet apartment",
                "area_sqft": 800,
                "description": "Parking available for $150 per month.",
                "parking": False,
            },
        ),
        _context(),
    )
    assert source_listing.parking == Absence.CONTRADICTORY
    assert source_listing.attributes["parking_available"] == Absence.CONTRADICTORY
    context = _context()
    source_listing = run_enricher_chain(source_listing, [TextRuleEnricher()], context)
    assert source_listing.parking == Absence.CONTRADICTORY
    assert source_listing.attributes["parking_available"] == Absence.CONTRADICTORY
    profile = context.profile.model_copy(
        update={"hard": context.profile.hard.model_copy(update={"require_parking": True})}
    )
    brief = build_decision_brief(source_listing, profile, extraction_current=True, now=NOW)
    assert brief.status == "unverified"
    parking_check = next(check for check in brief.checks if check.field == "parking")
    assert parking_check.evidence is not None
    assert "False" in parking_check.evidence and "True" in parking_check.evidence

    with connect(tmp_path / "corrections.db") as conn:
        corrected = apply_user_corrections(
            conn,
            listing_id="kijiji:1742000100",
            listing=source_listing,
            rows=(
                {
                    "field": "area",
                    "value_json": '{"value":950,"unit":"sqft"}',
                    "observed_at": NOW.isoformat(),
                },
                {
                    "field": "attributes.parking_available",
                    "value_json": "true",
                    "observed_at": NOW.isoformat(),
                },
            ),
        )

    assert isinstance(corrected.area, Observed)
    assert corrected.area.value == Area(value=950, unit="sqft")
    assert corrected.area.origin is Origin.USER
    parking_correction = corrected.attributes["parking_available"]
    assert isinstance(parking_correction, Observed)
    assert parking_correction.value is True
    assert parking_correction.origin is Origin.USER
    corrected = run_enricher_chain(corrected, [TextRuleEnricher()], context)
    corrected_brief = build_decision_brief(corrected, profile, extraction_current=True, now=NOW)
    assert corrected_brief.status == "match"
