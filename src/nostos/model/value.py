from __future__ import annotations

from collections.abc import Collection, Mapping
from decimal import Decimal
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Money(BaseModel):
    model_config = ConfigDict(frozen=True)

    amount: Decimal
    currency: NonEmptyStr
    period: NonEmptyStr

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        return value.upper()


class Area(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: float
    unit: NonEmptyStr


class LatLng(BaseModel):
    model_config = ConfigDict(frozen=True)

    lat: Annotated[float, Field(ge=-90.0, le=90.0)]
    lng: Annotated[float, Field(ge=-180.0, le=180.0)]


class Photo(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: NonEmptyStr
    width: Annotated[int | None, Field(ge=1)] = None
    height: Annotated[int | None, Field(ge=1)] = None
    caption: str | None = None


class StructuredAddress(BaseModel):
    model_config = ConfigDict(frozen=True)

    line_1: NonEmptyStr
    line_2: str | None = None
    city: NonEmptyStr
    region: str | None = None
    postal_code: str | None = None
    country: str | None = None


class Place(BaseModel):
    model_config = ConfigDict(frozen=True)

    raw_address: str | None = None
    structured: StructuredAddress | None = None
    point: LatLng | None = None
    area_key: str | None = None

    @field_validator("area_key")
    @classmethod
    def area_key_in_vocabulary(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None

        context: Mapping[str, Any] | None
        if isinstance(info.context, Mapping):
            context = info.context
        else:
            context = None

        if context is None:
            return value

        vocabulary = context.get("area_vocabulary")
        if vocabulary is None:
            return value

        if not isinstance(vocabulary, Collection):
            raise TypeError("area_vocabulary context value must be a collection of strings")

        if value not in vocabulary:
            raise ValueError(f"area_key {value!r} was not found in injected area_vocabulary")

        return value
