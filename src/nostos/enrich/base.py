from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from nostos.model import Listing, Observed, Origin


class CostModel(StrEnum):
    FREE = "free"
    PER_CALL = "per_call"
    PER_TOKEN = "per_token"


class Enricher(Protocol):
    name: str
    provides: frozenset[str]
    requires: frozenset[str]
    cost: CostModel
    origin: Origin
    confidence: float

    def estimate_cost(self, listing: Listing, context: object) -> Decimal:
        ...

    def enrich(self, listing: Listing, context: object) -> Mapping[str, Observed[Any]]:
        ...
