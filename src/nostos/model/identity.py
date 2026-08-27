from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

ListingId = NonEmptyStr
Signature = NonEmptyStr


class Identity(BaseModel):
    model_config = ConfigDict(frozen=True)

    listing_id: ListingId
    source: NonEmptyStr
    source_id: NonEmptyStr
    url: NonEmptyStr
    signature: Signature
