from __future__ import annotations

from collections.abc import Iterator

from nostos.config.citypack import Citypack
from nostos.config.profile import Profile
from nostos.context import SearchContext
from nostos.model import Listing, SourceRecord
from nostos.sources.base import Capabilities, Liveness
from nostos.sources.registry import SourceOffReason, enabled_sources, resolve_source_registry


def test_registry_reports_why_sources_are_off_instead_of_omitting() -> None:
    context = _build_context(
        profile_sources={
            "active": True,
            "profile_off": False,
            "not_in_citypack": True,
            "needs_credentials": True,
        },
        citypack_sources={
            "active": True,
            "profile_off": True,
            "needs_credentials": True,
        },
    )
    sources = (
        StubSource(name="active"),
        StubSource(name="profile_off"),
        StubSource(name="not_in_citypack"),
        StubSource(name="needs_credentials", requires_credentials=True),
    )

    resolutions = resolve_source_registry(
        context=context,
        sources=sources,
        credentials_present={"needs_credentials": False},
    )

    assert [item.name for item in resolutions] == [source.name for source in sources]
    assert resolutions[0].enabled is True
    assert resolutions[0].reason is None
    assert resolutions[1].enabled is False
    assert resolutions[1].reason is SourceOffReason.PROFILE_DISABLED
    assert resolutions[2].enabled is False
    assert resolutions[2].reason is SourceOffReason.NOT_IN_CITYPACK
    assert resolutions[3].enabled is False
    assert resolutions[3].reason is SourceOffReason.MISSING_CREDENTIALS

    assert [source.name for source in enabled_sources(resolutions)] == ["active"]


def test_registry_treats_citypack_disabled_source_as_not_covered() -> None:
    context = _build_context(
        profile_sources={"craigslist": True},
        citypack_sources={"craigslist": False},
    )
    source = StubSource(name="craigslist")

    (resolution,) = resolve_source_registry(
        context=context,
        sources=(source,),
        credentials_present=None,
    )

    assert resolution.enabled is False
    assert resolution.reason is SourceOffReason.NOT_IN_CITYPACK
    assert "citypack" in (resolution.detail or "")


class StubSource:
    def __init__(self, *, name: str, requires_credentials: bool = False) -> None:
        self.name = name
        self.capabilities = Capabilities(
            requires_credentials=requires_credentials,
            supports_detail_fetch=False,
            requires_browser=False,
            rate_limit_per_minute=60.0,
        )

    def discover(self, ctx: SearchContext) -> Iterator[SourceRecord]:
        del ctx
        return iter(())

    def fetch_detail(self, rec: SourceRecord) -> SourceRecord:
        return rec

    def check_liveness(self, rec: SourceRecord) -> Liveness:
        del rec
        return Liveness.OK

    def to_listing(self, rec: SourceRecord, ctx: SearchContext) -> Listing:
        del rec
        del ctx
        raise NotImplementedError


def _build_context(
    *,
    profile_sources: dict[str, bool],
    citypack_sources: dict[str, bool],
) -> SearchContext:
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
            "sources": {
                name: {"enabled": is_enabled, "load_bearing": False}
                for name, is_enabled in citypack_sources.items()
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
            "sources": profile_sources,
            "notify": [],
            "schedule": "0 */6 * * *",
        }
    )
    return SearchContext(citypack=citypack, profile=profile)
