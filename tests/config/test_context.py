from __future__ import annotations

import json
from pathlib import Path

import pytest

from nostos.context import SearchContext, load_search_context


def _write_yaml(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_search_context_resolves_and_has_area(tmp_path: Path) -> None:
    citypack_path = tmp_path / "citypack.yaml"
    profile_path = tmp_path / "profile.yaml"

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
            "sources": {"craigslist": {"enabled": True, "load_bearing": True}},
            "address": {
                "directional": {"w": "west"},
                "strip_tokens": ["vancouver"],
                "region_tokens": ["bc"],
            },
        },
    )
    _write_yaml(
        profile_path,
        {
            "city": "vancouver",
            "hard": {"exclude": []},
            "weights": {"laundry.in_suite": 6},
            "sources": {"craigslist": True},
            "notify": [],
            "schedule": "0 */6 * * *",
        },
    )

    context = load_search_context(citypack_path=citypack_path, profile_path=profile_path)

    assert isinstance(context, SearchContext)
    assert context.has_area("kits_beach")
    assert not context.has_area("unknown")


def test_search_context_rejects_city_mismatch(tmp_path: Path) -> None:
    citypack_path = tmp_path / "citypack.yaml"
    profile_path = tmp_path / "profile.yaml"
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
            "sources": {"craigslist": {"enabled": True, "load_bearing": True}},
            "address": {
                "directional": {"w": "west"},
                "strip_tokens": ["vancouver"],
                "region_tokens": ["bc"],
            },
        },
    )
    _write_yaml(
        profile_path,
        {
            "city": "toronto",
            "hard": {"exclude": []},
            "weights": {"laundry.in_suite": 6},
            "sources": {"craigslist": True},
            "notify": [],
            "schedule": "0 */6 * * *",
        },
    )

    with pytest.raises(ValueError, match=r"profile\.city"):
        load_search_context(citypack_path=citypack_path, profile_path=profile_path)
