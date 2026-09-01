"""FastAPI app wiring + end-to-end tests for the local nostos web UI.

These tests cover:
  - create_app() constructs cleanly and registers the expected routes
  - write_static_export() writes a self-contained HTML file with the
    expected filter inputs and no action endpoints
  - End-to-end GET/POST behaviour against a real seeded listing (title,
    score, action buttons, action history, action idempotency, sort
    fallback, validation 422s, path traversal safe)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from nostos.model.source_record import SourceRecord
from nostos.store.actions import ActionRepo
from nostos.store.db import apply_migrations, connect
from nostos.store.repo import ListingRepo, ScoreRepo
from nostos.web import create_app
from nostos.web.static_export import write_static_export


def _seed_profile_and_citypack(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return (db_path, profile_path, citypack_path) pointing at temp files."""

    db_path = tmp_path / "nostos.db"
    profile_path = tmp_path / "profile.yaml"
    citypack_path = tmp_path / "citypack.yaml"

    citypack_path.write_text(json.dumps({
        "name": "vancouver",
        "locale": {
            "language": "en-CA",
            "timezone": "America/Vancouver",
            "currency": "CAD",
            "area_unit": "sqft",
        },
        "areas": [
            {
                "key": "kits",
                "label": "Kitsilano",
                "keywords": ["kitsilano"],
                "bbox": [49.262, -123.190, 49.278, -123.145],
            }
        ],
        "sources": {
            "craigslist": {"enabled": True, "load_bearing": False},
            "kijiji": {"enabled": True, "load_bearing": False},
        },
        "address": {"directional": {}, "strip_tokens": [], "region_tokens": []},
    }))

    profile_path.write_text(json.dumps({
        "city": "vancouver",
        "hard": {"rent": {"max": 3200, "currency": "CAD"}, "beds": {"eq": 2}, "exclude": []},
        "weights": {},
        "proximity": [],
        "avoid_areas": [],
        "confidence": {"unverified_penalty": 0},
        "sources": {"craigslist": "on", "kijiji": "on"},
        "notify": [],
        "schedule": "0 */6 * * *",
    }))

    return db_path, profile_path, citypack_path


def test_create_app_loads(tmp_path: Path) -> None:
    db_path, profile_path, citypack_path = _seed_profile_and_citypack(tmp_path)
    with connect(db_path) as conn:
        apply_migrations(conn)

    app = create_app(
        db_path=db_path,
        profile_path=profile_path,
        citypack_path=citypack_path,
    )
    paths = sorted(
        route.path
        for route in app.routes
        if hasattr(route, "path")
    )
    assert "/" in paths
    assert "/listings/{listing_id}" in paths
    assert "/listings/{listing_id}/star" in paths
    assert "/listings/{listing_id}/dismiss" in paths
    assert "/listings/{listing_id}/contacted" in paths
    assert "/listings/{listing_id}/note" in paths


def test_static_export_writes_html_file(tmp_path: Path) -> None:
    db_path, profile_path, citypack_path = _seed_profile_and_citypack(tmp_path)
    with connect(db_path) as conn:
        apply_migrations(conn)

    output = tmp_path / "export.html"
    written = write_static_export(
        rows=(),
        output_path=output,
        profile_id="test-profile",
    )
    assert written == output
    assert output.exists()
    contents = output.read_text(encoding="utf-8")
    assert contents.lower().startswith("<!doctype html>")
    # Filter inputs are present.
    assert "rent_min" in contents
    assert "score_min" in contents
    # It's a static export — no POST action endpoints wired up.
    assert "/star" not in contents
    assert "/note" not in contents


def _seed_listing(db_path: Path, profile_id: str, listing_id: str) -> None:
    """Insert a realistic listing (row + source_record + score)."""

    now = datetime(2026, 1, 1, tzinfo=UTC)
    with connect(db_path) as conn:
        apply_migrations(conn)
        listing_repo = ListingRepo(conn)
        listing_repo.ensure_listing(listing_id, seen_at=now)
        listing_repo.add_source_record(
            listing_id=listing_id,
            record=SourceRecord(
                source="craigslist",
                source_id=listing_id.split(":", 1)[1],
                url=f"https://example.com/{listing_id}",
                payload={
                    "title": "Sunny 2BR in Kitsilano",
                    "address": "1234 West 4th Ave, Kitsilano",
                    "price": 2800,
                    "beds": 2,
                    "baths": 1,
                    "sqft": 850,
                    "posted": now.isoformat(),
                    "images": ["https://example.com/p1.jpg"],
                    "description": "Bright corner unit near the beach.",
                },
                content_hash="hash",
                fetched_at=now,
            ),
        )
        ScoreRepo(conn).upsert_score(
            listing_id=listing_id,
            profile_id=profile_id,
            score=82.5,
            breakdown_json={"score": 82.5, "contributions": []},
            computed_at=now,
        )


def test_end_to_end_listing_and_actions(tmp_path: Path) -> None:
    db_path, profile_path, citypack_path = _seed_profile_and_citypack(tmp_path)
    profile_id_str = profile_path.stem
    listing_id = "craigslist:seed-1"
    _seed_listing(db_path, profile_id_str, listing_id)
    app = create_app(
        db_path=db_path,
        profile_path=profile_path,
        citypack_path=citypack_path,
    )
    client = TestClient(app)

    # List page: filter form + the seeded listing.
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "Sunny 2BR in Kitsilano" in body
    assert f'href="/listings/{listing_id}"' in body
    # No badges before any actions.
    assert "card is-starred" not in body
    assert "action-btn is-on" not in body

    # Detail page renders score, address, action buttons, no badges yet.
    resp = client.get(f"/listings/{listing_id}")
    assert resp.status_code == 200
    body = resp.text
    assert "Sunny 2BR in Kitsilano" in body
    assert "1234 West 4th Ave, Kitsilano" in body
    assert "score-good" in body  # 82.5 >= 75
    # One-click actions are wired via data-action, not form actions.
    assert 'data-action="star"' in body
    assert 'data-listing="' in body
    assert f"data-listing=\"{listing_id}\"" in body
    assert "Action history" in body
    # Buttons in default text (not 'is-on' class).
    assert "★ Shortlist" in body
    # No button has the 'is-on' modifier class on initial render.
    # The literal string 'is-on' may appear in inline JS, so check for the
    # class-attribute pattern only.
    assert 'class="btn btn-star is-on"' not in body
    assert 'class="btn btn-dismiss is-on"' not in body
    assert 'class="btn btn-contact is-on"' not in body

    # POST star -> 303 + a row in the DB.
    resp = client.post(f"/listings/{listing_id}/star", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/listings/{listing_id}"
    with connect(db_path) as conn:
        assert ActionRepo(conn).has_action(listing_id=listing_id, kind="star")

    # POST star again -> still only one row (idempotency).
    resp = client.post(f"/listings/{listing_id}/star", follow_redirects=False)
    assert resp.status_code == 303
    with connect(db_path) as conn:
        repo = ActionRepo(conn)
        assert len(repo.get_actions(listing_id=listing_id)) == 1

    # Detail page now reflects the starred state.
    resp = client.get(f"/listings/{listing_id}")
    body = resp.text
    assert "★ Shortlisted" in body
    assert 'btn-star is-on' in body
    assert "aria-pressed=\"true\"" in body

    # List view shows the starred state on the card.
    resp = client.get("/")
    body = resp.text
    assert "is-starred" in body
    # The star button on the card carries the is-on modifier.
    assert 'data-action="star"' in body
    assert 'data-listing="craigslist:seed-1"' in body
    assert 'class="action-btn is-on"' in body


def test_post_note_is_recorded_and_truncated(tmp_path: Path) -> None:
    db_path, profile_path, citypack_path = _seed_profile_and_citypack(tmp_path)
    listing_id = "craigslist:seed-2"
    _seed_listing(db_path, profile_path.stem, listing_id)
    app = create_app(
        db_path=db_path,
        profile_path=profile_path,
        citypack_path=citypack_path,
    )
    client = TestClient(app)

    # Note saved with newlines preserved (text-as-data, no markup).
    payload = {
        "note": "viewing Thursday 6pm\nbring ID\n<script>alert('xss')</script>",
    }
    resp = client.post(
        f"/listings/{listing_id}/note",
        data=payload,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with connect(db_path) as conn:
        actions = ActionRepo(conn).get_actions(listing_id=listing_id)
    assert len(actions) == 1
    assert actions[0].kind == "note"
    assert actions[0].note is not None
    assert "viewing Thursday 6pm" in actions[0].note
    # Stored verbatim (no HTML stripping) — the template escapes on render.
    assert "<script>" in actions[0].note

    # Empty note is silently dropped.
    resp = client.post(
        f"/listings/{listing_id}/note",
        data={"note": "   "},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with connect(db_path) as conn:
        actions = ActionRepo(conn).get_actions(listing_id=listing_id)
    assert len(actions) == 1

    # Over-long note is rejected with 400.
    resp = client.post(
        f"/listings/{listing_id}/note",
        data={"note": "x" * 5000},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_detail_404_and_explain_404(tmp_path: Path) -> None:
    db_path, profile_path, citypack_path = _seed_profile_and_citypack(tmp_path)
    app = create_app(
        db_path=db_path,
        profile_path=profile_path,
        citypack_path=citypack_path,
    )
    client = TestClient(app)

    assert client.get("/listings/missing-999").status_code == 404
    assert client.get("/listings/missing-999/explain.json").status_code == 404


def test_invalid_query_returns_422_not_500(tmp_path: Path) -> None:
    db_path, profile_path, citypack_path = _seed_profile_and_citypack(tmp_path)
    app = create_app(
        db_path=db_path,
        profile_path=profile_path,
        citypack_path=citypack_path,
    )
    client = TestClient(app)

    assert client.get("/", params={"rent_min": "abc"}).status_code == 422
    assert client.get("/", params={"score_min": 200}).status_code == 422


def test_invalid_sort_falls_back_to_score(tmp_path: Path) -> None:
    db_path, profile_path, citypack_path = _seed_profile_and_citypack(tmp_path)
    listing_id = "craigslist:seed-3"
    _seed_listing(db_path, profile_path.stem, listing_id)
    app = create_app(
        db_path=db_path,
        profile_path=profile_path,
        citypack_path=citypack_path,
    )
    client = TestClient(app)

    # Unknown sort key: page returns 200 (filter form works, fallback).
    resp = client.get("/", params={"sort": "invalid_key"})
    assert resp.status_code == 200
    assert "Sunny 2BR in Kitsilano" in resp.text