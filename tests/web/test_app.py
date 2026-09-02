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
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from nostos.model.source_record import SourceRecord
from nostos.store.actions import ActionRepo
from nostos.store.db import apply_migrations, connect
from nostos.store.repo import ListingRepo, ScoreRepo
from nostos.web import create_app
from nostos.web.static_export import write_static_export


def _seed_profile_and_citypack(
    tmp_path: Path,
    *,
    hard: dict[str, object] | None = None,
    weights: dict[str, object] | None = None,
    area_key_weights: dict[str, float] | None = None,
) -> tuple[Path, Path, Path]:
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
            },
            {
                "key": "brentwood",
                "label": "Brentwood",
                "keywords": ["brentwood"],
                "bbox": [49.262, -123.020, 49.278, -122.990],
            },
        ],
        "sources": {
            "craigslist": {"enabled": True, "load_bearing": False},
            "kijiji": {"enabled": True, "load_bearing": False},
        },
        "address": {"directional": {}, "strip_tokens": [], "region_tokens": []},
    }))

    profile_path.write_text(json.dumps({
        "city": "vancouver",
        "hard": hard
        if hard is not None
        else {"rent": {"max": 3200, "currency": "CAD"}, "beds": {"eq": 2}, "exclude": []},
        "weights": weights or {},
        "area_key_weights": area_key_weights or {},
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


def _seed_listing(
    db_path: Path,
    profile_id: str,
    listing_id: str,
    *,
    payload: dict[str, object] | None = None,
    score: float = 82.5,
    breakdown: dict[str, Any] | None = None,
    fetched_at: datetime | None = None,
) -> None:
    """Insert a realistic listing (row + source_record + score)."""

    now = fetched_at or datetime(2026, 1, 1, tzinfo=UTC)
    base_payload: dict[str, object] = {
        "title": "Sunny 2BR in Kitsilano",
        "address": "1234 West 4th Ave, Kitsilano",
        "price": 2800,
        "beds": 2,
        "baths": 1,
        "sqft": 850,
        "posted": now.isoformat(),
        "images": ["https://example.com/p1.jpg"],
        "description": "Bright corner unit near the beach.",
    }
    if payload:
        base_payload.update(payload)
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
                payload=base_payload,
                content_hash="hash",
                fetched_at=now,
            ),
        )
        ScoreRepo(conn).upsert_score(
            listing_id=listing_id,
            profile_id=profile_id,
            score=score,
            breakdown_json=breakdown or {"score": score, "contributions": []},
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
    assert 'class="card-action-btn is-on"' in body


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

def _client(tmp_path: Path, **profile_kwargs: Any) -> tuple[TestClient, Path, str]:
    db_path, profile_path, citypack_path = _seed_profile_and_citypack(tmp_path, **profile_kwargs)
    with connect(db_path) as conn:
        apply_migrations(conn)
    app = create_app(db_path=db_path, profile_path=profile_path, citypack_path=citypack_path)
    return TestClient(app), db_path, profile_path.stem


def _card_ids(body: str) -> list[str]:
    return re.findall(r'data-listing-id="([^"]+)"', body)


def test_index_filter_bar_renders_chips_sorts_and_profile_summary(tmp_path: Path) -> None:
    client, db_path, profile_id = _client(
        tmp_path,
        hard={
            "rent": {"max": 3200, "min": 1500, "currency": "CAD"},
            "beds": {"eq": 2},
            "floor": {"max": 12},
            "areas": ["kits", "brentwood"],
            "exclude": ["basement"],
        },
        weights={"laundry.in_suite": 10, "pets.allowed": -10, "walk.score": 3},
        area_key_weights={"kits": 15},
    )
    _seed_listing(db_path, profile_id, "craigslist:seed-1")
    body = client.get("/").text

    # Area chips: "All" + one per citypack area, All active by default.
    assert 'data-area=""' in body
    assert 'data-area="kits"' in body and "Kitsilano" in body
    assert 'data-area="brentwood"' in body and "Brentwood" in body
    assert re.search(r'class="toggle-chip is-on"\s+href="/"\s+data-area=""', body)
    assert 'href="/?area_name=kits"' in body

    # Quick toggles + status chips.
    for param in ("starred", "hide_dismissed", "show_excluded"):
        assert f'data-toggle="{param}"' in body
        assert f'href="/?{param}=1"' in body
    for value in ("match", "unverified", "miss"):
        assert f'href="/?status={value}"' in body

    # Sort options in the select + "Sorted by" line.
    for key in ("score", "rent_asc", "rent_desc", "area_desc", "posted_desc", "posted_asc",
                "address"):
        assert f'<option value="{key}"' in body
    assert "Sorted by Match score" in body
    assert "1 listing" in body

    # More-filters block collapsed when no numeric filter is active.
    assert '<details class="more-filters" >' in body
    assert 'name="score_min"' in body

    # Profile summary strip: hard filters + top-3 weights by |value|.
    assert "rent $1,500–$3,200" in body
    assert "floor ≤ 12" in body
    assert "2 areas" in body
    assert "no basement" in body
    assert "Kitsilano +15" in body
    assert "In-suite laundry +10" in body
    assert "Pet friendly −10" in body
    assert "Walk Score" not in body  # 4th weight not shown
    assert 'href="/profile"' in body


def test_index_more_filters_open_when_numeric_active(tmp_path: Path) -> None:
    client, db_path, profile_id = _client(tmp_path)
    _seed_listing(db_path, profile_id, "craigslist:seed-1")
    body = client.get("/", params={"rent_max": 3000}).text
    assert '<details class="more-filters" open>' in body
    assert "rent ≤ $3,000" in body  # active-filter chip
    # Area chip URLs preserve the numeric filter.
    assert 'href="/?rent_max=3000&amp;area_name=kits"' in body


def test_index_starred_status_excluded_and_sort_filters(tmp_path: Path) -> None:
    client, db_path, profile_id = _client(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    # A: matches, cheapest, oldest, starred later.
    _seed_listing(db_path, profile_id, "craigslist:a", payload={"price": 2500}, score=90,
                  fetched_at=now)
    # B: rent over the profile max -> miss; dismissed later.
    _seed_listing(db_path, profile_id, "craigslist:b", payload={"price": 3800}, score=70,
                  fetched_at=now + timedelta(days=1))
    # C: in Brentwood, excluded later.
    _seed_listing(
        db_path, profile_id, "craigslist:c",
        payload={"price": 3000, "address": "4500 Kingsway, Brentwood", "title": "Brentwood 2BR"},
        score=60, fetched_at=now + timedelta(days=2),
    )
    with connect(db_path) as conn:
        repo = ActionRepo(conn)
        repo.record_action(listing_id="craigslist:a", kind="star")
        repo.record_action(listing_id="craigslist:b", kind="dismiss")
        repo.record_action(listing_id="craigslist:c", kind="excluded")

    # Default: excluded dropped, score order.
    assert _card_ids(client.get("/").text) == ["craigslist:a", "craigslist:b"]

    # Miss listing carries the reasons tooltip.
    body = client.get("/").text
    assert 'title="rent $3,800 &gt; max $3,200"' in body

    # starred=1
    assert _card_ids(client.get("/", params={"starred": 1}).text) == ["craigslist:a"]
    # hide_dismissed=1
    assert _card_ids(client.get("/", params={"hide_dismissed": 1}).text) == ["craigslist:a"]
    # status=miss / status=match
    assert _card_ids(client.get("/", params={"status": "miss"}).text) == ["craigslist:b"]
    assert _card_ids(client.get("/", params={"status": "match"}).text) == ["craigslist:a"]
    # show_excluded=1 brings C back with excluded styling.
    body = client.get("/", params={"show_excluded": 1}).text
    assert _card_ids(body) == ["craigslist:a", "craigslist:b", "craigslist:c"]
    assert "is-excluded" in body
    assert "⊘ Excluded" in body
    # Area chip filters (single-select) and renders active.
    body = client.get("/", params={"area_name": "brentwood", "show_excluded": 1}).text
    assert _card_ids(body) == ["craigslist:c"]
    active_chip = r'class="toggle-chip is-on"\s+href="/\?show_excluded=1"\s+data-area="brentwood"'
    assert re.search(active_chip, body)
    assert "area: Brentwood" in body
    # Sorts.
    assert _card_ids(client.get("/", params={"sort": "rent_desc"}).text) == [
        "craigslist:b", "craigslist:a",
    ]
    assert _card_ids(client.get("/", params={"sort": "rent_asc"}).text) == [
        "craigslist:a", "craigslist:b",
    ]
    assert _card_ids(client.get("/", params={"sort": "posted_desc"}).text) == [
        "craigslist:b", "craigslist:a",
    ]
    assert _card_ids(client.get("/", params={"sort": "posted_asc"}).text) == [
        "craigslist:a", "craigslist:b",
    ]
    body = client.get("/", params={"sort": "rent_desc"}).text
    assert "Sorted by Rent ↓" in body
    assert 'value="rent_desc" selected' in body
    # Legacy alias still works; unknown sort falls back to score.
    assert _card_ids(client.get("/", params={"sort": "rent"}).text) == [
        "craigslist:a", "craigslist:b",
    ]
    assert _card_ids(client.get("/", params={"sort": "nope"}).text) == [
        "craigslist:a", "craigslist:b",
    ]
    # Unknown status value is ignored rather than 422.
    assert client.get("/", params={"status": "weird"}).status_code == 200


def test_card_shows_score_badge_and_top_contributors(tmp_path: Path) -> None:
    client, db_path, profile_id = _client(tmp_path)
    breakdown = {
        "score": 82.5,
        "contributions": [
            {"rule_key": "laundry.in_suite", "category": "amenities", "label": "In-suite laundry",
             "weight": 6, "signal": {"fired": True, "magnitude": 1, "confidence": 1,
                                     "evidence": "washer/dryer in suite"},
             "max_possible": 6, "contribution": 6.0},
            {"rule_key": "pets.allowed", "category": "amenities", "label": "Pet friendly",
             "weight": -10, "signal": {"fired": True, "magnitude": 1, "confidence": 1,
                                       "evidence": "no pets"},
             "max_possible": 0, "contribution": -10.0},
            {"rule_key": "walk.score", "category": "proximity", "label": "Walk Score",
             "weight": 3, "signal": None, "max_possible": 3, "contribution": 0.0},
        ],
    }
    _seed_listing(db_path, profile_id, "craigslist:seed-1", breakdown=breakdown)
    body = client.get("/").text
    assert '<span class="score score-good">82.5</span>' in body
    assert "−10 pet friendly" in body
    assert "+6 in-suite laundry" in body
    assert "Amenities" in body  # category label, not raw key


def test_detail_renders_rule_table_and_reasons(tmp_path: Path) -> None:
    client, db_path, profile_id = _client(tmp_path, hard={
        "rent": {"max": 3200, "currency": "CAD"},
        "beds": {"eq": 2},
        "floor": {"max": 12},
        "exclude": [],
    })
    breakdown = {
        "score": 40.0,
        "contributions": [
            {"rule_key": "pets.allowed", "category": "amenities", "label": "Pet friendly",
             "weight": -10, "signal": {"fired": True, "magnitude": 1, "confidence": 1,
                                       "evidence": "no pets"},
             "max_possible": 0, "contribution": -10.0},
            {"rule_key": "laundry.in_suite", "category": "amenities", "label": "In-suite laundry",
             "weight": 6, "signal": {"fired": True, "magnitude": 1, "confidence": 1,
                                     "evidence": "washer/dryer in suite"},
             "max_possible": 6, "contribution": 6.0},
            {"rule_key": "walk.score", "category": "proximity", "label": "Walk Score",
             "weight": 3, "signal": None, "max_possible": 3, "contribution": 0.0},
            {"rule_key": "area.over_minimum", "category": "space", "label": "Space over minimum",
             "weight": {"per_100_sqft": 2, "cap": 10}, "signal": None,
             "max_possible": 10, "contribution": 0.0},
        ],
    }
    # Rent over max + floor unstated -> miss with two reasons.
    _seed_listing(db_path, profile_id, "craigslist:seed-1", payload={"price": 3500},
                  score=40.0, breakdown=breakdown)
    body = client.get("/listings/craigslist:seed-1").text
    assert "✕ Miss" in body
    assert "<li>rent $3,500 &gt; max $3,200</li>" in body
    assert "<li>floor unstated</li>" in body
    # Rule table: fired rows sorted by |contribution|, evidence + weight shown.
    fired_pos = [body.index(f'data-rule="{k}"') for k in ("pets.allowed", "laundry.in_suite")]
    assert fired_pos == sorted(fired_pos)
    assert "no pets" in body
    assert "washer/dryer in suite" in body
    assert "-10.0" in body and "+6.0" in body
    assert "+2 per 100 sqft, cap 10" in body
    assert "Rules that didn&#39;t fire (2)" in body or "Rules that didn't fire (2)" in body
    assert 'data-rule="walk.score"' in body
    assert 'href="/profile"' in body
    assert "Edit ranking profile" in body
    assert "Location &amp; proximity" in body


def test_static_export_has_new_sorts_and_area_select(tmp_path: Path) -> None:
    client, db_path, profile_id = _client(tmp_path)
    _seed_listing(db_path, profile_id, "craigslist:seed-1")
    del client
    output = tmp_path / "export.html"
    write_static_export(rows=(), output_path=output, profile_id="p")
    contents = output.read_text(encoding="utf-8")
    for key in ("rent_asc", "rent_desc", "area_desc", "posted_desc", "posted_asc", "address"):
        assert f'<option value="{key}">' in contents
    assert '<select id="area">' in contents
    assert '<select id="status">' in contents
