from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from nostos.context import SearchContext
from nostos.model import Listing, SourceRecord


@dataclass(frozen=True, slots=True)
class Capabilities:
    requires_credentials: bool = False
    supports_detail_fetch: bool = False
    requires_browser: bool = False
    rate_limit_per_minute: float | None = None


class Liveness(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"


@runtime_checkable
class Source(Protocol):
    name: str
    capabilities: Capabilities

    def discover(self, ctx: SearchContext) -> Iterator[SourceRecord]: ...

    def fetch_detail(self, rec: SourceRecord) -> SourceRecord: ...

    def check_liveness(self, rec: SourceRecord) -> Liveness: ...

    def to_listing(self, rec: SourceRecord, ctx: SearchContext) -> Listing: ...
