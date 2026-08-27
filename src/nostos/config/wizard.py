from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from nostos.config.citypack import Citypack


class _YamlModule(Protocol):
    @staticmethod
    def safe_dump(
        data: object,
        *,
        sort_keys: bool = ...,
        default_flow_style: bool = ...,
    ) -> str: ...


class PreferenceLevel(StrEnum):
    DEAL_BREAKER = "deal-breaker"
    NICE_TO_HAVE = "nice-to-have"
    DONT_CARE = "dont-care"


class PetsPreference(StrEnum):
    PREFER = "prefer"
    AVOID = "avoid"
    DONT_CARE = "dont-care"


@dataclass(frozen=True, slots=True)
class WizardAnswers:
    city: str
    max_rent: float
    beds: float
    laundry: PreferenceLevel
    parking: PreferenceLevel | None
    pets: PetsPreference | None
    source_names: tuple[str, ...]
    notify_urls: tuple[str, ...]
    baths_min: float | None = None
    baths_max: float | None = None
    min_area: float | None = None
    avoid_basement: bool = True
    require_unfurnished: bool = True
    schedule: str = "0 */6 * * *"


def missing_required_values(
    *,
    max_rent: float | None,
    beds: float | None,
    laundry: PreferenceLevel | None,
) -> tuple[str, ...]:
    missing: list[str] = []
    if max_rent is None:
        missing.append("--max-rent")
    if beds is None:
        missing.append("--beds")
    if laundry is None:
        missing.append("--laundry")
    return tuple(missing)


def build_profile_payload(*, answers: WizardAnswers, citypack: Citypack) -> dict[str, object]:
    excludes: list[str] = []
    if answers.avoid_basement:
        excludes.append("basement")
    if answers.require_unfurnished:
        excludes.append("furnished_only")

    hard_payload: dict[str, object] = {
        "rent": {"max": answers.max_rent, "currency": citypack.locale.currency},
        "beds": {"eq": answers.beds},
        "exclude": excludes,
    }
    if answers.baths_min is not None or answers.baths_max is not None:
        baths_payload: dict[str, float] = {}
        if answers.baths_min is not None:
            baths_payload["min"] = answers.baths_min
        if answers.baths_max is not None:
            baths_payload["max"] = answers.baths_max
        hard_payload["baths"] = baths_payload
    if answers.min_area is not None:
        hard_payload["area"] = {"min": answers.min_area, "unit": citypack.locale.area_unit}

    weights: dict[str, object] = {}
    laundry_weight = _laundry_weight(answers.laundry)
    if laundry_weight != 0:
        weights["laundry.in_suite"] = laundry_weight

    building_laundry_weight = _building_laundry_weight(answers.laundry)
    if building_laundry_weight != 0:
        weights["laundry.building"] = building_laundry_weight

    if answers.min_area is not None:
        weights["area.over_minimum"] = {"per_100_sqft": 4, "cap": 12}

    selected_sources = {name for name in answers.source_names}
    source_toggles: dict[str, str] = {}
    for source_name in citypack.sources:
        source_toggles[source_name] = "on" if source_name in selected_sources else "off"

    return {
        "city": answers.city,
        "hard": hard_payload,
        "weights": weights,
        "proximity": [],
        "avoid_areas": [],
        "confidence": {"unverified_penalty": 0},
        "sources": source_toggles,
        "notify": [url for url in answers.notify_urls if url.strip()],
        "schedule": answers.schedule,
    }


def dump_profile_yaml(payload: Mapping[str, object]) -> str:
    yaml_module = _maybe_yaml_module()
    if yaml_module is None:
        return json.dumps(payload, indent=2) + "\n"
    return yaml_module.safe_dump(
        dict(payload),
        sort_keys=False,
        default_flow_style=False,
    )


def _laundry_weight(value: PreferenceLevel) -> int:
    if value == PreferenceLevel.DEAL_BREAKER:
        return 12
    if value == PreferenceLevel.NICE_TO_HAVE:
        return 6
    return 0


def _building_laundry_weight(value: PreferenceLevel) -> int:
    if value == PreferenceLevel.DEAL_BREAKER:
        return -8
    if value == PreferenceLevel.NICE_TO_HAVE:
        return -3
    return 0


def _parking_weight(value: PreferenceLevel) -> int:
    if value == PreferenceLevel.DEAL_BREAKER:
        return 8
    if value == PreferenceLevel.NICE_TO_HAVE:
        return 5
    return 0


def _pets_weight(value: PetsPreference) -> int:
    if value == PetsPreference.PREFER:
        return 8
    if value == PetsPreference.AVOID:
        return -8
    return 0


def _maybe_yaml_module() -> _YamlModule | None:
    try:
        module = importlib.import_module("yaml")
    except ModuleNotFoundError:
        return None
    return cast(_YamlModule, module)
