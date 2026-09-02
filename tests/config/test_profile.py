from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

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
    citypack = load_citypack(repo_root / "src" / "nostos" / "citypacks" / "vancouver.yaml")
    example = load_profile(repo_root / "profiles" / "example-vancouver.yaml")

    assert set(example.area_key_weights).issubset(citypack.area_keys)


def test_example_profile_hard_areas_are_present_in_citypack() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    citypack = load_citypack(repo_root / "src" / "nostos" / "citypacks" / "vancouver.yaml")
    example = load_profile(repo_root / "profiles" / "example-vancouver.yaml")

    assert example.hard.areas
    assert set(example.hard.areas).issubset(citypack.area_keys)
    assert example.hard.rent is not None
    assert example.hard.rent.min == 2500
    assert example.hard.floor is not None
    assert example.hard.floor.max == 12
    assert example.weights["photo.present"] == 2


def test_rent_min_above_max_is_rejected() -> None:
    with pytest.raises(ValidationError, match="min must be less than or equal to max"):
        Profile.model_validate(
            {
                "city": "vancouver",
                "hard": {"rent": {"min": 4000, "max": 3600, "currency": "CAD"}},
                "schedule": "0 */6 * * *",
            }
        )


def test_floor_and_areas_hard_filters_round_trip(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.yaml"
    _write_yaml(
        profile_path,
        {
            "city": "vancouver",
            "hard": {
                "rent": {"min": 2500, "max": 3600, "currency": "CAD"},
                "floor": {"max": 12},
                "areas": ["kits_beach", "downtown_van"],
            },
            "schedule": "0 */6 * * *",
        },
    )

    loaded = load_profile(profile_path)

    assert loaded.hard.rent is not None
    assert loaded.hard.rent.min == 2500
    assert loaded.hard.floor is not None
    assert loaded.hard.floor.max == 12
    assert loaded.hard.floor.min is None
    assert loaded.hard.areas == ["kits_beach", "downtown_van"]

    dumped = loaded.model_dump(mode="json")
    reloaded = Profile.model_validate(dumped)
    assert reloaded == loaded
    assert dumped["hard"]["floor"] == {"eq": None, "min": None, "max": 12}
    assert dumped["hard"]["areas"] == ["kits_beach", "downtown_van"]


def test_hard_filters_default_to_no_floor_bound_and_any_area() -> None:
    profile = Profile.model_validate({"city": "vancouver", "schedule": "0 */6 * * *"})
    assert profile.hard.floor is None
    assert profile.hard.areas == []
