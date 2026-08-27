from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Protocol, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _YamlModule(Protocol):
    @staticmethod
    def safe_load(value: str) -> object: ...


class Locale(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    language: NonEmptyStr
    timezone: NonEmptyStr
    currency: NonEmptyStr
    area_unit: NonEmptyStr

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        return value.upper()


class AreaDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: NonEmptyStr
    label: NonEmptyStr
    keywords: list[NonEmptyStr] = Field(default_factory=list, min_length=1)
    bbox: tuple[float, float, float, float]


class SourceAdapterConfig(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    enabled: bool
    load_bearing: bool


class AddressConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    directional: dict[NonEmptyStr, NonEmptyStr] = Field(default_factory=dict)
    strip_tokens: list[NonEmptyStr] = Field(default_factory=list)
    region_tokens: list[NonEmptyStr] = Field(default_factory=list)


class Citypack(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: NonEmptyStr
    locale: Locale
    areas: list[AreaDefinition] = Field(min_length=1)
    sources: dict[NonEmptyStr, SourceAdapterConfig] = Field(default_factory=dict)
    address: AddressConfig

    @field_validator("areas")
    @classmethod
    def ensure_unique_area_keys(cls, value: list[AreaDefinition]) -> list[AreaDefinition]:
        seen: set[str] = set()
        for area in value:
            if area.key in seen:
                raise ValueError(f"duplicate area key {area.key!r}")
            seen.add(area.key)
        return value

    @field_validator("sources")
    @classmethod
    def reject_inline_credentials(
        cls, value: dict[str, SourceAdapterConfig]
    ) -> dict[str, SourceAdapterConfig]:
        blocked_tokens = ("password", "secret", "token", "apikey", "api_key", "credential")
        for source_name, source_cfg in value.items():
            for key in (source_cfg.model_extra or {}).keys():
                normalized = key.lower().replace("-", "_")
                if any(token in normalized for token in blocked_tokens):
                    dotted_path = f"sources.{source_name}.{key}"
                    raise ValueError(
                        f"{dotted_path} looks like a credential field; store secrets in OS keychain"
                    )
        return value

    @property
    def area_keys(self) -> frozenset[str]:
        return frozenset(area.key for area in self.areas)


def load_citypack(path: str | Path) -> Citypack:
    loaded = _load_mapping_document(Path(path))
    try:
        return Citypack.model_validate(loaded)
    except ValidationError as exc:
        raise ValueError(_format_validation_error("citypack", exc)) from exc


def _load_mapping_document(path: Path) -> Mapping[str, object]:
    payload = _load_yaml_or_json(path)
    if not isinstance(payload, Mapping):
        raise ValueError(f"Malformed citypack at <root>: expected an object mapping in {path}")
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
            "Malformed citypack at <root>: invalid YAML/JSON syntax "
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
