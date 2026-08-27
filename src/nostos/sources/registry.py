from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from nostos.context import SearchContext
from nostos.sources.base import Source

CredentialsPresence = Mapping[str, bool] | Callable[[str], bool] | None


class SourceOffReason(StrEnum):
    PROFILE_DISABLED = "profile_disabled"
    NOT_IN_CITYPACK = "not_in_citypack"
    MISSING_CREDENTIALS = "missing_credentials"


@dataclass(frozen=True, slots=True)
class SourceResolution:
    source: Source
    enabled: bool
    reason: SourceOffReason | None = None
    detail: str | None = None

    @property
    def name(self) -> str:
        return self.source.name


def resolve_source_registry(
    *,
    context: SearchContext,
    sources: Iterable[Source],
    credentials_present: CredentialsPresence,
) -> tuple[SourceResolution, ...]:
    checker = _credential_checker(credentials_present)
    resolutions: list[SourceResolution] = []
    for source in sources:
        reason, detail = _resolve_off_reason(
            source=source,
            context=context,
            has_credentials=checker,
        )
        resolutions.append(
            SourceResolution(
                source=source,
                enabled=reason is None,
                reason=reason,
                detail=detail,
            )
        )
    return tuple(resolutions)


def enabled_sources(resolutions: Iterable[SourceResolution]) -> tuple[Source, ...]:
    return tuple(resolution.source for resolution in resolutions if resolution.enabled)


def _resolve_off_reason(
    *,
    source: Source,
    context: SearchContext,
    has_credentials: Callable[[str], bool],
) -> tuple[SourceOffReason | None, str | None]:
    profile_enabled = bool(context.profile.sources.get(source.name, False))
    if not profile_enabled:
        return SourceOffReason.PROFILE_DISABLED, f"profile.sources.{source.name} is off or not set"

    citypack_config = context.citypack.sources.get(source.name)
    if citypack_config is None:
        return SourceOffReason.NOT_IN_CITYPACK, f"citypack.sources has no {source.name!r} entry"
    if not citypack_config.enabled:
        return (
            SourceOffReason.NOT_IN_CITYPACK,
            f"citypack.sources.{source.name}.enabled is false",
        )

    if source.capabilities.requires_credentials and not has_credentials(source.name):
        return (
            SourceOffReason.MISSING_CREDENTIALS,
            f"source {source.name!r} requires credentials that are not present",
        )

    return None, None


def _credential_checker(presence: CredentialsPresence) -> Callable[[str], bool]:
    if callable(presence):
        return presence
    if isinstance(presence, Mapping):
        return lambda source_name: bool(presence.get(source_name, False))
    return lambda source_name: False
