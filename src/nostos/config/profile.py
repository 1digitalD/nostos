from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Protocol, cast

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _YamlModule(Protocol):
    @staticmethod
    def safe_load(value: str) -> object: ...


def _parse_source_toggle(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"on", "true", "yes", "1"}:
            return True
        if normalized in {"off", "false", "no", "0"}:
            return False
    raise ValueError("expected bool or on/off style string")


SourceToggle = Annotated[bool, BeforeValidator(_parse_source_toggle)]


class RentHardFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max: float
    currency: NonEmptyStr

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        return value.upper()


class NumericHardFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    eq: float | None = None
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def validate_bounds(self) -> NumericHardFilter:
        has_eq = self.eq is not None
        has_range = self.min is not None or self.max is not None
        if not has_eq and not has_range:
            raise ValueError("at least one of eq/min/max must be set")
        if has_eq and has_range:
            raise ValueError("eq cannot be combined with min/max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min must be less than or equal to max")
        return self


class AreaHardFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min: float
    unit: NonEmptyStr


class HardFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rent: RentHardFilter | None = None
    beds: NumericHardFilter | None = None
    baths: NumericHardFilter | None = None
    area: AreaHardFilter | None = None
    exclude: list[NonEmptyStr] = Field(default_factory=list)


class ScaledWeight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    per_100_sqft: float | None = None
    per_100: float | None = None
    cap: float

    @model_validator(mode="after")
    def one_rate_is_required(self) -> ScaledWeight:
        rates = [self.per_100_sqft is not None, self.per_100 is not None]
        if sum(rates) != 1:
            raise ValueError("exactly one of per_100_sqft or per_100 is required")
        return self


class ProximityPreference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: NonEmptyStr
    within_min: int
    weight: float


class AvoidAreaPreference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bbox: tuple[float, float, float, float]
    weight: float


class ConfidenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    unverified_penalty: float = 0.0


WeightValue = float | ScaledWeight


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    city: NonEmptyStr
    hard: HardFilters = Field(default_factory=HardFilters)
    weights: dict[NonEmptyStr, WeightValue] = Field(default_factory=dict)
    area_key_weights: dict[NonEmptyStr, float] = Field(default_factory=dict)
    proximity: list[ProximityPreference] = Field(default_factory=list)
    avoid_areas: list[AvoidAreaPreference] = Field(default_factory=list)
    confidence: ConfidenceConfig = Field(default_factory=ConfidenceConfig)
    sources: dict[NonEmptyStr, SourceToggle] = Field(default_factory=dict)
    notify: list[NonEmptyStr] = Field(default_factory=list)
    schedule: NonEmptyStr


def load_profile(path: str | Path) -> Profile:
    loaded = _load_mapping_document(Path(path))
    try:
        return Profile.model_validate(loaded)
    except ValidationError as exc:
        raise ValueError(_format_validation_error("profile", exc)) from exc


def _load_mapping_document(path: Path) -> Mapping[str, object]:
    payload = _load_yaml_or_json(path)
    if not isinstance(payload, Mapping):
        raise ValueError(f"Malformed profile at <root>: expected an object mapping in {path}")
    return payload


def _load_yaml_or_json(path: Path) -> object:
    raw = path.read_text(encoding="utf-8")
    yaml_module = _maybe_load_yaml_module()
    if yaml_module is not None:
        return yaml_module.safe_load(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        message = (
            "Malformed profile at <root>: invalid YAML/JSON syntax "
            f"(line {exc.lineno}, column {exc.colno}) in {path}"
        )
        raise ValueError(message) from exc


def _maybe_load_yaml_module() -> _YamlModule | None:
    try:
        module = importlib.import_module("yaml")
    except ModuleNotFoundError:
        return None
    return cast(_YamlModule, module)


def _format_validation_error(kind: str, error: ValidationError) -> str:
    parts: list[str] = []
    for item in error.errors():
        loc = ".".join(str(token) for token in item.get("loc", ())) or "<root>"
        message = str(item.get("msg", "invalid value"))
        parts.append(f"{loc}: {message}")
    return f"Malformed {kind}: " + "; ".join(parts)
