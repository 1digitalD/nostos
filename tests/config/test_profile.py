from __future__ import annotations

import json
from pathlib import Path

from nostos.config.citypack import load_citypack
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
            "area_key_weights": {"kits_beach": 5, "burnaby_brentwood": -3},
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
    assert loaded.area_key_weights["kits_beach"] == 5
    assert loaded.sources["craigslist"] is True
    assert loaded.sources["kijiji"] is False


def test_repo_balanced_profile_example_loads() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    balanced_path = repo_root / "profiles" / "balanced.yaml"
    example_path = repo_root / "profiles" / "example-vancouver.yaml"

    balanced = load_profile(balanced_path)
    example = load_profile(example_path)

    assert balanced.city == "vancouver"
    assert balanced.weights
    assert example.city == "vancouver"
    assert example.area_key_weights


def test_example_profile_area_keys_are_present_in_citypack() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    citypack = load_citypack(repo_root / "citypacks" / "vancouver.yaml")
    example = load_profile(repo_root / "profiles" / "example-vancouver.yaml")

    assert set(example.area_key_weights).issubset(citypack.area_keys)
