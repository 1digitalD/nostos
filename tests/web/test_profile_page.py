"""End-to-end tests for the editable profile page (`/profile`).

Covers: every registered rule and every citypack area is rendered; a valid
save writes the expected YAML (hard filters, weights, area weights, exclude
tokens), reloads the in-memory profile, re-scores stored listings, and
redirects; `/profile/rescore` regenerates score rows; invalid input is a 400
that preserves the submitted values and leaves the file untouched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from nostos.config.profile import ScaledWeight, load_profile
from nostos.model.source_record import SourceRecord
from nostos.rank.rules import DEFAULT_REGISTRY
from nostos.store.db import apply_migrations, connect
from nostos.store.repo import ListingRepo, ScoreRepo
from nostos.web import create_app


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
            },
            {
                "key": "mount_pleasant",
                "label": "Mount Pleasant",
                "keywords": ["mount pleasant"],
                "bbox": [49.255, -123.115, 49.270, -123.090],
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
        "hard": {"rent": {"max": 3200, "currency": "CAD"}, "beds": {"eq": 2}, "exclude": []},
        "weights": {"laundry.in_suite": 10, "pets.allowed": -4},
        "area_key_weights": {"kits": 5},
        "proximity": [],
        "avoid_areas": [],
        "confidence": {"unverified_penalty": 0},
        "sources": {"craigslist": "on", "kijiji": "on"},
        "notify": ["mailto://someone@example.com"],
        "schedule": "0 */6 * * *",
    }))

    return db_path, profile_path, citypack_path


def _seed_listing(db_path: Path, profile_id: str, listing_id: str, *, rent: int = 2800) -> None:
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
                    "price": rent,
                    "beds": 2,
                    "baths": 1,
                    "sqft": 850,
                    "posted": now.isoformat(),
                    "images": ["https://example.com/p1.jpg"],
                    "description": "Bright corner unit near the beach. In-suite laundry.",
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


def _client(tmp_path: Path) -> tuple[TestClient, Path, Path]:
    db_path, profile_path, citypack_path = _seed_profile_and_citypack(tmp_path)
    app = create_app(db_path=db_path, profile_path=profile_path, citypack_path=citypack_path)
    return TestClient(app), db_path, profile_path


def _score_ids(db_path: Path, profile_id: str) -> set[str]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT listing_id FROM score WHERE profile_id = ?", (profile_id,)
        ).fetchall()
    return {str(row["listing_id"]) for row in rows}


def _valid_form() -> dict[str, str | list[str]]:
    """A complete, valid submission that changes most of the profile."""

    return {
        "rent_max": "2500",
        "rent_min": "1500",
        "beds_eq": "2",
        "beds_min": "1",  # ignored: eq wins
        "baths_min": "1",
        "baths_max": "2",
        "floor_max": "12",
        "area_min": "700",
        "areas": ["kits"],
        "exclude_basement": "on",
        "exclude_furnished_only": "on",
        "weight_laundry.in_suite": "12",  # changed
        "weight_pets.allowed": "0",  # removed
        "weight_parking.available": "6",  # newly enabled
        "weight_area.over_minimum_rate": "4",
        "weight_area.over_minimum_cap": "12",
        "weight_rent.headroom_rate": "",
        "weight_rent.headroom_cap": "15",  # rate blank -> off
        "area_weight_kits": "7",
        "area_weight_mount_pleasant": "0",
        "unverified_penalty": "3",
        "src_craigslist": "on",
    }


def test_get_profile_renders_every_rule_and_area(tmp_path: Path) -> None:
    client, _db_path, _profile_path = _client(tmp_path)
    resp = client.get("/profile")
    assert resp.status_code == 200
    body = resp.text

    for rule in DEFAULT_REGISTRY.all():
        assert f'name="weight_{rule.key}"' in body or f'name="weight_{rule.key}_rate"' in body
        assert rule.label in body
    # Scaled rules expose rate + cap inputs.
    assert 'name="weight_area.over_minimum_rate"' in body
    assert 'name="weight_area.over_minimum_cap"' in body
    assert 'name="weight_rent.headroom_rate"' in body

    # Category headings (fallback or registry-provided).
    assert "Amenities" in body

    # Every citypack area: allowed-areas checkbox + preference slider.
    for key, label in (("kits", "Kitsilano"), ("mount_pleasant", "Mount Pleasant")):
        assert f'name="areas" value="{key}"' in body
        assert f'name="area_weight_{key}"' in body
        assert label in body

    # Hard filter inputs and current values.
    for name in (
        "rent_max", "rent_min", "beds_eq", "beds_min", "beds_max",
        "baths_min", "baths_max", "floor_max", "area_min",
        "exclude_basement", "exclude_furnished_only", "unverified_penalty",
    ):
        assert f'name="{name}"' in body
    assert 'name="rent_max"' in body and 'value="3200"' in body
    assert 'name="beds_eq"' in body and 'value="2"' in body
    # Current weights are pre-filled; area weight too.
    assert 'name="weight_laundry.in_suite"' in body and 'value="10"' in body
    assert 'name="weight_pets.allowed"' in body and 'value="-4"' in body
    assert 'name="area_weight_kits"' in body and 'value="5"' in body

    # Sources come from the citypack ∪ profile.
    assert 'name="src_craigslist"' in body
    assert 'name="src_kijiji"' in body

    # Action bar + helper copy.
    assert 'action="/profile/rescore"' in body
    assert "Hard filters remove listings" in body
    assert "Unsaved changes" in body


def test_post_profile_saves_reloads_rescores_and_redirects(tmp_path: Path) -> None:
    client, db_path, profile_path = _client(tmp_path)
    profile_id = profile_path.stem
    listing_id = "craigslist:seed-1"
    _seed_listing(db_path, profile_id, listing_id, rent=2800)

    # Sanity: the listing shows on the index under the original profile.
    assert "Sunny 2BR in Kitsilano" in client.get("/").text
    assert _score_ids(db_path, profile_id) == {listing_id}

    resp = client.post("/profile", data=_valid_form(), follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    parsed = urlparse(location)
    assert parsed.path == "/profile"
    params = parse_qs(parsed.query)
    assert params["saved"] == ["1"]
    # The listing's rent (2800) exceeds the new max (2500): scored 0, skipped 1.
    assert params["rescored"] == ["0"]
    assert params["skipped"] == ["1"]

    # The YAML on disk reflects the submission.
    saved = load_profile(profile_path)
    assert saved.hard.rent is not None
    assert saved.hard.rent.max == 2500
    assert saved.hard.rent.min == 1500
    assert saved.hard.rent.currency == "CAD"
    assert saved.hard.beds is not None
    assert saved.hard.beds.eq == 2
    assert saved.hard.beds.min is None  # eq wins over min
    assert saved.hard.baths is not None
    assert (saved.hard.baths.min, saved.hard.baths.max) == (1, 2)
    assert saved.hard.floor is not None
    assert saved.hard.floor.max == 12
    assert saved.hard.area is not None
    assert saved.hard.area.min == 700
    assert saved.hard.area.unit == "sqft"
    assert saved.hard.areas == ["kits"]
    assert saved.hard.exclude == ["basement", "furnished_only"]
    assert saved.weights["laundry.in_suite"] == 12
    assert saved.weights["parking.available"] == 6
    assert "pets.allowed" not in saved.weights  # zero -> removed
    assert "rent.headroom" not in saved.weights  # blank rate -> removed
    scaled = saved.weights["area.over_minimum"]
    assert isinstance(scaled, ScaledWeight)
    assert (scaled.per_100_sqft, scaled.cap) == (4, 12)
    assert saved.area_key_weights == {"kits": 7}
    assert saved.confidence.unverified_penalty == 3
    assert saved.sources == {"craigslist": True, "kijiji": False}
    # Keys the form does not render survive the round-trip.
    assert saved.notify == ["mailto://someone@example.com"]
    assert saved.schedule == "0 */6 * * *"
    assert saved.city == "vancouver"
    text = profile_path.read_text(encoding="utf-8")
    assert "pets.allowed" not in text
    assert not (tmp_path / "profile.yaml.tmp").exists()

    # In-memory state was reloaded and the score table recomputed: the listing
    # is gone from the index without a restart.
    body = client.get("/").text
    assert "Sunny 2BR in Kitsilano" not in body
    assert _score_ids(db_path, profile_id) == set()

    # Following the redirect renders the banner with the counts and the new values.
    page = client.get(location)
    assert page.status_code == 200
    assert "Re-scored 0 listings (1 filtered out by hard filters)" in page.text
    assert 'name="rent_max"' in page.text and 'value="2500"' in page.text
    assert 'name="areas" value="kits"' in page.text
    assert 'name="rent_min"' in page.text and 'value="1500"' in page.text


def test_post_rescore_regenerates_score_rows(tmp_path: Path) -> None:
    client, db_path, profile_path = _client(tmp_path)
    profile_id = profile_path.stem
    listing_id = "craigslist:seed-2"
    _seed_listing(db_path, profile_id, listing_id, rent=2800)

    with connect(db_path) as conn, conn:
        conn.execute("DELETE FROM score WHERE profile_id = ?", (profile_id,))
    assert _score_ids(db_path, profile_id) == set()
    assert "Sunny 2BR in Kitsilano" not in client.get("/").text

    resp = client.post("/profile/rescore", follow_redirects=False)
    assert resp.status_code == 303
    params = parse_qs(urlparse(resp.headers["location"]).query)
    assert params["rescored"] == ["1"]
    assert params["skipped"] == ["0"]
    assert "saved" not in params

    assert _score_ids(db_path, profile_id) == {listing_id}
    with connect(db_path) as conn:
        row = ScoreRepo(conn).get_score(listing_id, profile_id)
    assert row is not None
    assert 0 <= row.score <= 100
    assert "Sunny 2BR in Kitsilano" in client.get("/").text

    page = client.get(resp.headers["location"])
    assert "Re-scored 1 listing (0 filtered out by hard filters)" in page.text


def test_invalid_rent_range_is_400_and_leaves_file_untouched(tmp_path: Path) -> None:
    client, _db_path, profile_path = _client(tmp_path)
    before = profile_path.read_text(encoding="utf-8")

    form = _valid_form()
    form["rent_min"] = "4000"  # > rent_max 2500
    resp = client.post("/profile", data=form, follow_redirects=False)
    assert resp.status_code == 400
    body = resp.text
    assert "Not saved." in body
    assert "Rent min must be less than or equal to rent max" in body
    # Submitted values are preserved in the re-rendered form.
    assert 'name="rent_min"' in body and 'value="4000"' in body
    assert 'name="weight_laundry.in_suite"' in body and 'value="12"' in body
    assert 'name="areas" value="kits"' in body
    assert profile_path.read_text(encoding="utf-8") == before
    assert not (tmp_path / "profile.yaml.tmp").exists()

    # The in-memory profile is unchanged too: the page still shows the old max.
    assert 'value="3200"' in client.get("/profile").text


def test_negative_and_malformed_numbers_are_rejected(tmp_path: Path) -> None:
    client, _db_path, profile_path = _client(tmp_path)
    before = profile_path.read_text(encoding="utf-8")

    form = _valid_form()
    form["rent_max"] = "-100"
    resp = client.post("/profile", data=form, follow_redirects=False)
    assert resp.status_code == 400
    assert "Rent max cannot be negative" in resp.text

    form = _valid_form()
    form["area_min"] = "lots"
    resp = client.post("/profile", data=form, follow_redirects=False)
    assert resp.status_code == 400
    assert "Area min must be a number" in resp.text

    form = _valid_form()
    form["areas"] = ["nowhere"]
    resp = client.post("/profile", data=form, follow_redirects=False)
    assert resp.status_code == 400
    assert "Unknown area key" in resp.text

    assert profile_path.read_text(encoding="utf-8") == before


def test_blank_form_clears_optional_filters(tmp_path: Path) -> None:
    client, _db_path, profile_path = _client(tmp_path)
    resp = client.post(
        "/profile",
        data={"src_craigslist": "on", "src_kijiji": "on"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    saved = load_profile(profile_path)
    assert saved.hard.rent is None
    assert saved.hard.beds is None
    assert saved.hard.areas == []
    assert saved.hard.exclude == []
    assert saved.weights == {}
    assert saved.area_key_weights == {}
    assert saved.confidence.unverified_penalty == 0
    assert saved.sources == {"craigslist": True, "kijiji": True}


def test_profile_routes_registered(tmp_path: Path) -> None:
    client, _db_path, _profile_path = _client(tmp_path)
    paths = {getattr(route, "path", None) for route in client.app.routes}  # type: ignore[attr-defined]
    assert "/profile" in paths
    assert "/profile/rescore" in paths
