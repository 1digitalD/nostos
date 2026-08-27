from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Discriminator, Tag
from pydantic import Field as PydanticField
from typing_extensions import TypeAliasType

from nostos.model.identity import Identity
from nostos.model.source_record import SourceRecordRef
from nostos.model.value import Area, Money, Photo, Place

T = TypeVar("T")


class Origin(StrEnum):
    USER = "user"
    SOURCE_FIELD = "source_field"
    DETAIL_PAGE = "detail_page"
    TEXT_RULE = "text_rule"
    GEO_PROVIDER = "geo_provider"
    VISION = "vision"

    @property
    def precedence(self) -> int:
        ordering = {
            Origin.USER: 120,
            Origin.SOURCE_FIELD: 100,
            Origin.DETAIL_PAGE: 80,
            Origin.TEXT_RULE: 60,
            Origin.GEO_PROVIDER: 55,
            Origin.VISION: 30,
        }
        return ordering[self]

    def can_overwrite(self, existing: Origin) -> bool:
        return self.precedence >= existing.precedence

    def assert_can_overwrite(self, existing: Origin) -> None:
        if not self.can_overwrite(existing):
            raise ValueError(
                f"{self.value} (precedence={self.precedence}) cannot overwrite "
                f"{existing.value} (precedence={existing.precedence})"
            )


class Observed(BaseModel, Generic[T]):
    model_config = ConfigDict(frozen=True)

    value: T
    origin: Origin
    confidence: Annotated[float, PydanticField(ge=0.0, le=1.0)]
    evidence: str | None = None
    observed_at: datetime
    detail: dict[str, str] = PydanticField(default_factory=dict)


class Absence(StrEnum):
    NOT_STATED = "not_stated"
    NOT_APPLICABLE = "not_applicable"
    CONTRADICTORY = "contradictory"


def _field_discriminator(value: object) -> str:
    if isinstance(value, Observed):
        return "observed"
    if isinstance(value, Absence):
        return "absence"
    if isinstance(value, str):
        return "absence"
    if isinstance(value, Mapping):
        return "observed" if "origin" in value else "absence"
    msg = f"Unsupported field shape for discrimination: {type(value)!r}"
    raise TypeError(msg)


Field = TypeAliasType(
    "Field",
    Annotated[
        Annotated[Observed[T], Tag("observed")] | Annotated[Absence, Tag("absence")],
        Discriminator(_field_discriminator),
    ],
    type_params=(T,),
)


def merge_field(existing: Field[T], incoming: Field[T]) -> Field[T]:
    if isinstance(existing, Observed) and isinstance(incoming, Observed):
        incoming.origin.assert_can_overwrite(existing.origin)
        return incoming
    if isinstance(existing, Observed) and isinstance(incoming, Absence):
        return existing
    if isinstance(existing, Absence) and isinstance(incoming, Observed):
        return incoming
    return incoming


class Listing(BaseModel):
    model_config = ConfigDict(frozen=True)

    identity: Identity
    place: Place
    rent: Field[Money]
    beds: Field[float]
    baths: Field[float]
    area: Field[Area]
    floor: Field[int]
    parking: Field[str]
    furnishing: Field[str]
    photos: list[Photo]
    attributes: dict[str, Field[Any]] = PydanticField(default_factory=dict)
    raw_ref: SourceRecordRef
    schema_version: int
