from __future__ import annotations

import json
from pathlib import Path

import pytest

from nostos.config.citypack import Citypack, load_citypack

REQUIRED_VANCOUVER_AREA_KEYS = frozenset(
    {
        "downtown_van",
        "burnaby_brentwood",
        "burnaby_other",
        "kits_beach",
        "downtown_other",
        "mount_pleasant",
        "n_van_lonsdale",
        "west_van",
    }
)


def _write_yaml(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_citypack_loads_with_optional_adapter_sections_missing(tmp_path: Path) -> None:
    citypack_path = tmp_path / "citypack.yaml"
    _write_yaml(
        citypack_path,
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
                    "key": "kits_beach",
                    "label": "Kitsilano",
                    "keywords": ["kitsilano", "kits point"],
                    "bbox": [49.262, -123.190, 49.278, -123.145],
                }
            ],
            "sources": {
                "craigslist": {
                    "enabled": True,
                    "load_bearing": True,
                    "base_url": "https://vancouver.craigslist.org",
                }
            },
            "address": {
                "directional": {"w": "west", "e": "east"},
                "strip_tokens": ["vancouver"],
                "region_tokens": ["bc"],
            },
        },
    )

    loaded = load_citypack(citypack_path)

    assert isinstance(loaded, Citypack)
    assert loaded.name == "vancouver"
    assert set(loaded.sources) == {"craigslist"}
    assert loaded.area_keys == frozenset({"kits_beach"})


def test_malformed_citypack_raises_with_dotted_field_path(tmp_path: Path) -> None:
    citypack_path = tmp_path / "citypack.yaml"
    _write_yaml(
        citypack_path,
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
                    "key": "kits_beach",
                    "label": "Kitsilano",
                    "keywords": ["kitsilano"],
                    "bbox": [49.262, -123.190, 49.278],
                }
            ],
            "sources": {},
            "address": {
                "directional": {"w": "west"},
                "strip_tokens": ["vancouver"],
                "region_tokens": ["bc"],
            },
        },
    )

    with pytest.raises(ValueError, match=r"areas\.0\.bbox"):
        load_citypack(citypack_path)


def test_citypack_rejects_inline_credentials(tmp_path: Path) -> None:
    citypack_path = tmp_path / "citypack.yaml"
    _write_yaml(
        citypack_path,
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
                    "key": "kits_beach",
                    "label": "Kitsilano",
                    "keywords": ["kitsilano"],
                    "bbox": [49.262, -123.190, 49.278, -123.145],
                }
            ],
            "sources": {
                "craigslist": {
                    "enabled": True,
                    "load_bearing": True,
                    "api_token": "do-not-store-secrets-in-yaml",
                }
            },
            "address": {
                "directional": {"w": "west"},
                "strip_tokens": ["vancouver"],
                "region_tokens": ["bc"],
            },
        },
    )

    with pytest.raises(ValueError, match=r"sources\.craigslist\.api_token"):
        load_citypack(citypack_path)


def test_repo_vancouver_example_loads() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    citypack_path = repo_root / "citypacks" / "vancouver.yaml"

    loaded = load_citypack(citypack_path)

    assert loaded.name == "vancouver"
    assert loaded.area_keys
    assert REQUIRED_VANCOUVER_AREA_KEYS.issubset(loaded.area_keys)


def test_packaged_citypack_matches_repo_citypack_area_keys() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    repo_citypack = load_citypack(repo_root / "citypacks" / "vancouver.yaml")
    packaged_citypack = load_citypack(repo_root / "src" / "nostos" / "citypacks" / "vancouver.yaml")

    assert packaged_citypack.area_keys == repo_citypack.area_keys
