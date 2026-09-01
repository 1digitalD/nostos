"""FastAPI app wiring tests for the local nostos web UI.

These tests cover what can be verified without spinning up a live server:
  - create_app() constructs cleanly and registers the expected routes
  - write_static_export() writes a self-contained HTML file with the
    expected filter inputs and no action endpoints

End-to-end behaviour (live HTTP, DB writes) is covered by manual smoke tests
documented in `docs/10-live-smoke.md` and exercised by the `nostos web` CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

from nostos.store.db import apply_migrations, connect
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