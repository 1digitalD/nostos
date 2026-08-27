from __future__ import annotations

import json
from pathlib import Path

from nostos.config.profile import Profile, load_profile


def _write_yaml(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_profile_loads_and_normalizes_source_switches(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.yaml"
    _write_yaml(
        profile_path,
        {
            "city": "vancouver",
            "hard": {
                "rent": {"max": 3600, "currency": "cad"},
                "beds": {"eq": 2},
                "baths": {"min": 1, "max": 2},
                "area": {"min": 750, "unit": "sqft"},
                "exclude": ["basement"],
            },
            "weights": {"laundry.in_suite": 6, "pets.allowed": 8},
            "sources": {"craigslist": "on", "kijiji": "off"},
            "notify": ["ntfy://nostos/demo"],
            "schedule": "0 */6 * * *",
        },
    )

    loaded = load_profile(profile_path)

    assert isinstance(loaded, Profile)
    assert loaded.city == "vancouver"
    assert loaded.hard.rent is not None
    assert loaded.hard.rent.currency == "CAD"
    assert loaded.sources["craigslist"] is True
    assert loaded.sources["kijiji"] is False


def test_repo_balanced_profile_example_loads() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    profile_path = repo_root / "profiles" / "balanced.yaml"

    loaded = load_profile(profile_path)

    assert loaded.city == "vancouver"
    assert loaded.weights
