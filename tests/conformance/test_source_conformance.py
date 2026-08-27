from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from nostos.config.citypack import Citypack
from nostos.config.profile import Profile
from nostos.context import SearchContext
from nostos.model import (
    Absence,
    Identity,
    Listing,
    Money,
    Observed,
    Origin,
    Place,
    SourceRecord,
)
from nostos.sources.base import Capabilities, Liveness, Source
from nostos.sources.kijiji import KijijiSource


def test_stub_source_passes_source_conformance_suite() -> None:
    context = _build_context()
    source = StubSource(name="stub")
    assert_source_conforms(source=source, context=context)


def test_kijiji_source_passes_conformance_with_jsonld_discovery() -> None:
    fixture_dir = Path(__file__).resolve().parent.parent / "fixtures" / "kijiji"
    search_html = (fixture_dir / "search_vancouver_kitsilano.html").read_text(encoding="utf-8")
    detail_html = (fixture_dir / "detail_1234567890.html").read_text(encoding="utf-8")
    source = KijijiSource(fetcher=ConformanceFixtureFetcher(search_html, detail_html))
    context = _build_context_for_kijiji()
    assert_source_conforms(source=source, context=context)


def assert_source_conforms(*, source: Source, context: SearchContext) -> None:
    assert isinstance(source, Source)
    assert source.name
    assert isinstance(source.capabilities, Capabilities)

    records = list(source.discover(context))
    assert records
    for record in records:
        _assert_record_conforms(source=source, record=record)

        detailed = source.fetch_detail(record)
        assert isinstance(detailed, SourceRecord)
        assert detailed.source == source.name
        if not source.capabilities.supports_detail_fetch:
            assert detailed == record

        liveness = source.check_liveness(detailed)
        assert isinstance(liveness, Liveness)

        dump_before = detailed.model_dump(mode="python")
        listing = source.to_listing(detailed, context)
        dump_after = detailed.model_dump(mode="python")

        assert dump_before == dump_after
        assert isinstance(listing, Listing)
        assert listing.identity.source == source.name
        assert listing.identity.source_id == detailed.source_id
        assert listing.raw_ref == detailed.to_ref()
        assert source.to_listing(detailed, context) == listing


def _assert_record_conforms(*, source: Source, record: SourceRecord) -> None:
    assert isinstance(record, SourceRecord)
    assert record.source == source.name


class StubSource:
    def __init__(self, *, name: str) -> None:
        self.name = name
        self.capabilities = Capabilities(
            requires_credentials=False,
            supports_detail_fetch=False,
            requires_browser=False,
            rate_limit_per_minute=60.0,
        )

    def discover(self, ctx: SearchContext) -> Iterator[SourceRecord]:
        del ctx
        yield _default_record(self.name)

    def fetch_detail(self, rec: SourceRecord) -> SourceRecord:
        return rec

    def check_liveness(self, rec: SourceRecord) -> Liveness:
        payload = rec.payload
        if isinstance(payload, Mapping):
            marker = payload.get("liveness")
            if marker in {"ok", "degraded", "failed"}:
                return Liveness(marker)
        return Liveness.OK

    def to_listing(self, rec: SourceRecord, ctx: SearchContext) -> Listing:
        del ctx
        payload = _payload_mapping(rec.payload)
        observed_at = rec.fetched_at
        return Listing(
            identity=Identity(
                listing_id=f"{self.name}:{rec.source_id}",
                source=self.name,
                source_id=rec.source_id,
                url=rec.url,
                signature=f"sig:{self.name}:{rec.source_id}",
            ),
            place=Place(
                raw_address=_text(payload, "address"),
                structured=None,
                point=None,
                area_key=None,
            ),
            rent=Observed[Money](
                value=Money(amount=Decimal(str(payload["rent"])), currency="CAD", period="month"),
                origin=Origin.SOURCE_FIELD,
                confidence=1.0,
                evidence="stub rent",
                observed_at=observed_at,
            ),
            beds=Observed[float](
                value=float(payload["beds"]),
                origin=Origin.SOURCE_FIELD,
                confidence=1.0,
                evidence="stub beds",
                observed_at=observed_at,
            ),
            baths=Observed[float](
                value=float(payload["baths"]),
                origin=Origin.SOURCE_FIELD,
                confidence=1.0,
                evidence="stub baths",
                observed_at=observed_at,
            ),
            area=Absence.NOT_STATED,
            floor=Absence.NOT_STATED,
            parking=Absence.NOT_STATED,
            furnishing=Absence.NOT_STATED,
            photos=[],
            attributes={},
            raw_ref=rec.to_ref(),
            schema_version=1,
        )


def _default_record(name: str) -> SourceRecord:
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    return SourceRecord(
        source=name,
        source_id="stub-1",
        url=f"https://example.com/{name}/stub-1",
        content_hash=f"{name}-stub-1",
        fetched_at=now,
        payload={
            "address": "123 Main St",
            "rent": 2400,
            "beds": 1,
            "baths": 1,
            "liveness": "ok",
        },
    )


def _payload_mapping(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("SourceRecord.payload must be a mapping for StubSource.to_listing")
    return payload


def _text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    return str(value)


def _build_context() -> SearchContext:
    citypack = Citypack.model_validate(
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
            "sources": {"stub": {"enabled": True, "load_bearing": False}},
            "address": {
                "directional": {"w": "west"},
                "strip_tokens": ["vancouver"],
                "region_tokens": ["bc"],
            },
        }
    )
    profile = Profile.model_validate(
        {
            "city": "vancouver",
            "hard": {"exclude": []},
            "weights": {},
            "sources": {"stub": "on"},
            "notify": [],
            "schedule": "0 */6 * * *",
        }
    )
    return SearchContext(citypack=citypack, profile=profile)


class ConformanceFixtureFetcher:
    def __init__(self, search_html: str, detail_html: str) -> None:
        self._search_html = search_html
        self._detail_html = detail_html

    def __call__(self, url: str) -> str:
        if "/b-apartments-condos/" in url:
            return self._search_html
        if "/v-apartments-condos/" in url:
            return self._detail_html
        raise AssertionError(f"Unexpected URL {url!r}")


def _build_context_for_kijiji() -> SearchContext:
    citypack = Citypack.model_validate(
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
                    "keywords": ["kitsilano", "kits"],
                    "bbox": [49.262, -123.190, 49.278, -123.145],
                }
            ],
            "sources": {
                "kijiji": {
                    "enabled": True,
                    "load_bearing": False,
                    "regions": [
                        {
                            "path": "vancouver",
                            "id": "c37l1700287",
                            "keywords": ["kitsilano"],
                        }
                    ],
                }
            },
            "address": {
                "directional": {"w": "west"},
                "strip_tokens": ["vancouver"],
                "region_tokens": ["bc"],
            },
        }
    )
    profile = Profile.model_validate(
        {
            "city": "vancouver",
            "hard": {"exclude": []},
            "weights": {},
            "sources": {"kijiji": "on"},
            "notify": [],
            "schedule": "0 */6 * * *",
        }
    )
    return SearchContext(citypack=citypack, profile=profile)
