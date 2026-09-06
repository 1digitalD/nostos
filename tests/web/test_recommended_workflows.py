from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_profile_page import _client, _seed_listing, _valid_form

from nostos.config.profile import Landmark, Profile, load_profile
from nostos.enrich.location import distance_km, point_from_html
from nostos.model import (
    Absence,
    Identity,
    LatLng,
    Listing,
    Place,
    SourceRecordRef,
)
from nostos.rank.criteria import classify_match_status
from nostos.rank.engine import RankEngine
from nostos.store.db import connect


def test_rogers_centre_point_and_proximity_score() -> None:
    point = point_from_html(
        '<script type="application/ld+json">'
        '{"geo":{"latitude":43.65,"longitude":-79.39}}</script>'
    )
    assert point == {"lat": 43.65, "lng": -79.39}
    listing = Listing(
        identity=Identity(listing_id="manual:x", source="manual", source_id="x",
                          url="https://example.com/x", signature="x"),
        place=Place(point=LatLng.model_validate(point)),
        rent=Absence.NOT_STATED,
        beds=Absence.NOT_STATED,
        baths=Absence.NOT_STATED, area=Absence.NOT_STATED, floor=Absence.NOT_STATED,
        parking=Absence.NOT_STATED, furnishing=Absence.NOT_STATED, photos=[], attributes={},
        raw_ref=SourceRecordRef(source="manual", source_id="x", url="https://example.com/x",
                                content_hash="x", fetched_at=datetime.now(UTC)), schema_version=1,
    )
    landmark = Landmark(name="Rogers Centre", lat=43.6416598, lng=-79.3891976,
                        source_url="https://www.openstreetmap.org/way/7969701",
                        weight=12, within_km=5)
    profile = Profile(city="toronto", landmark=landmark, schedule="0 */6 * * *")
    assert distance_km(listing, landmark) == pytest.approx(0.93, abs=0.1)
    result = RankEngine(profile).score_listing(listing)
    assert result.contributions[-1].rule_key == "landmark.distance"
    assert result.contributions[-1].contribution > 9


def test_unknown_review_and_actual_laundry_requirement() -> None:
    listing = Listing(
        identity=Identity(listing_id="manual:x", source="manual", source_id="x",
                          url="https://example.com/x", signature="x"),
        place=Place(), rent=Absence.NOT_STATED, beds=Absence.NOT_STATED,
        baths=Absence.NOT_STATED, area=Absence.NOT_STATED, floor=Absence.NOT_STATED,
        parking=Absence.NOT_STATED, furnishing=Absence.NOT_STATED, photos=[], attributes={},
        raw_ref=SourceRecordRef(source="manual", source_id="x", url="https://example.com/x",
                                content_hash="x", fetched_at=datetime.now(UTC)), schema_version=1,
    )
    profile = Profile.model_validate({
        "city": "toronto", "unknown_policy": "review",
        "hard": {"require_laundry": True}, "schedule": "0 */6 * * *",
    })
    status = classify_match_status(listing, profile)
    assert status.status == "unverified"
    assert "in-suite laundry unstated" in status.reasons


def test_preview_apply_stale_guard_manual_add_and_viewing(tmp_path: Path) -> None:
    client, db_path, profile_path = _client(tmp_path)
    _seed_listing(db_path, profile_path.stem, "craigslist:seed")
    form = _valid_form()
    page = client.get("/profile")
    assert page.status_code == 200
    import re
    expected = re.search(r'name="expected_revision" value="([^"]+)"', page.text)
    assert expected
    form["expected_revision"] = expected.group(1)
    preview = client.post("/profile/preview", data=form)
    assert preview.status_code == 200
    assert "Criteria impact preview" in preview.text
    assert "Apply this preview" in preview.text

    manual = client.post("/manual", data={
        "url": "https://example.com/other", "title": "Owner supplied condo",
        "rent": "2500", "beds": "2", "baths": "1", "area": "800",
    }, follow_redirects=False)
    assert manual.status_code == 303
    manual_id = manual.headers["location"].split("/")[-1]
    detail = client.get(manual.headers["location"])
    assert "Owner supplied condo" in detail.text
    progress = client.post(f"/listings/{manual_id}/progress", data={
        "stage": "viewing_booked", "viewing_at": "2026-10-01T18:00",
    }, follow_redirects=False)
    assert progress.status_code == 303
    calendar = client.get(f"/listings/{manual_id}/viewing.ics")
    assert calendar.status_code == 200
    assert "BEGIN:VCALENDAR" in calendar.text
    assert "Apartment viewing: Owner supplied condo" in calendar.text

    # A concurrent change makes the original preview ineligible for activation.
    profile_path.write_text(profile_path.read_text().replace("3200", "3199"))
    apply = client.post("/profile/apply-preview", data={
        "expected_revision": expected.group(1),
        "proposed_json": load_profile(profile_path).model_dump_json(),
    })
    assert apply.status_code == 400 or apply.status_code == 409


def test_listing_correction_and_research_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db_path, profile_path = _client(tmp_path)
    first = "craigslist:unit-a"
    second = "craigslist:unit-b"
    _seed_listing(db_path, profile_path.stem, first, rent=2800)
    _seed_listing(db_path, profile_path.stem, second, rent=2800)

    corrected = client.post(
        f"/listings/{first}/correct",
        data={"field": "rent", "value": "2450"},
        follow_redirects=False,
    )
    assert corrected.status_code == 303
    page = client.get(corrected.headers["location"])
    assert "Correction applied and ranking updated" in page.text
    assert "2450.00 CAD" in page.text
    assert "Open research workspace" in page.text

    research = client.get(f"/listings/{first}/research")
    assert research.status_code == 200
    assert "Possible matching advertisements" in research.text
    assert "Building report" in research.text
    assert "0</strong><span>matching source excerpts" in research.text
    assert second in research.text
    assert "unit-b" in research.text
    assert "Continue your investigation" in research.text
    assert "Time-sensitive web searches are limited to the past year" in research.text
    assert "Nearby places" in research.text
    address_research = research.text.split("Continue your investigation", 1)[1]
    address_research = address_research.split("Nearby places", 1)[0]
    assert "2450" not in address_research

    calls = 0

    def compiled_results(
        _subject: str, _city: str, **_kwargs: object
    ) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {
            "provider": "perplexity",
            "fetched_at": datetime.now(UTC).isoformat(),
            "filtered_stale_count": 3,
            "results": [{
                "topic": "Building management",
                "title": "Current building management review",
                "url": "https://example.test/current-review",
                "source": "Example Local News",
                "published_at": "2026-08-15",
                "excerpt": "The property manager changed in 2026.",
            }],
        }

    monkeypatch.setattr("nostos.web.app.compile_web_research", compiled_results)
    compiled = client.post(f"/listings/{first}/web-research.json")
    assert compiled.status_code == 200
    assert compiled.json()["count"] == 1
    assert calls == 1

    compiled_page = client.get(f"/listings/{first}/research")
    assert "1</strong><span>matching source excerpts" in compiled_page.text
    assert "Current building management review" in compiled_page.text
    assert "Example Local News" in compiled_page.text
    assert "2026-08-15" in compiled_page.text
    assert "The property manager changed in 2026" in compiled_page.text
    assert (
        "3 undated, future-dated, or older-than-two-year results were excluded"
        in compiled_page.text
    )

    cached = client.post(f"/listings/{first}/web-research.json")
    assert cached.json()["status"] == "cached"
    assert calls == 1

    reset = client.post(
        f"/listings/{first}/corrections/reset",
        data={"field": "rent"},
        follow_redirects=False,
    )
    assert reset.status_code == 303
    with connect(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM observation WHERE listing_id=? AND origin='user'",
            (first,),
        ).fetchone()[0]
    assert count == 0
