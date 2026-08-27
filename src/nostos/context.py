from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nostos.config.citypack import Citypack, load_citypack
from nostos.config.profile import Profile, load_profile


@dataclass(frozen=True, slots=True)
class SearchContext:
    citypack: Citypack
    profile: Profile

    def __post_init__(self) -> None:
        if self.profile.city != self.citypack.name:
            raise ValueError(
                "profile.city must match citypack.name "
                f"(got profile.city={self.profile.city!r}, citypack.name={self.citypack.name!r})"
            )

    @property
    def area_vocabulary(self) -> frozenset[str]:
        return self.citypack.area_keys

    def has_area(self, key: str) -> bool:
        return key in self.citypack.area_keys


def load_search_context(*, citypack_path: str | Path, profile_path: str | Path) -> SearchContext:
    citypack = load_citypack(citypack_path)
    profile = load_profile(profile_path)
    return SearchContext(citypack=citypack, profile=profile)
