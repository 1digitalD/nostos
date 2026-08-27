from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, TypeAlias

from pydantic import BaseModel, ConfigDict, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

JSONPrimitive: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONPrimitive | list[Any] | dict[str, Any]


class SourceRecordRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: NonEmptyStr
    source_id: NonEmptyStr
    url: NonEmptyStr
    content_hash: NonEmptyStr
    fetched_at: datetime


class SourceRecord(SourceRecordRef):
    payload: JSONValue

    def to_ref(self) -> SourceRecordRef:
        return SourceRecordRef.model_validate(self.model_dump(mode="python"))
