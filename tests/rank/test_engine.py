from __future__ import annotations

from typing import cast

import pytest

from nostos.config.profile import Profile
from nostos.model import Listing
from nostos.rank.engine import RankEngine
from nostos.rank.rules import RuleRegistry, Signal


def _profile(weights: dict[str, object]) -> Profile:
    return Profile.model_validate(
        {
            "city": "vancouver",
            "weights": weights,
            "schedule": "0 */6 * * *",
        }
    )


def test_score_normalizes_against_enabled_weight_set() -> None:
    registry = RuleRegistry()

    @registry.rule("amenity.light", category="amenities", label="Natural light")
    def detect_light(_: Listing, __: object) -> Signal:
        return Signal(fired=True, magnitude=1.0, confidence=1.0, evidence="South-facing windows")

    @registry.rule("amenity.storage", category="amenities", label="Storage space")
    def detect_storage(_: Listing, __: object) -> Signal:
        return Signal(fired=True, magnitude=1.0, confidence=1.0, evidence="Large hallway closet")

    listing = cast(Listing, object())

    full_profile = _profile({"amenity.light": 10.0, "amenity.storage": 10.0})
    full_result = RankEngine(full_profile, registry=registry).score_listing(listing)
    assert full_result.score == pytest.approx(100.0)
    assert full_result.normalization.max_possible == pytest.approx(20.0)

    half_profile = _profile({"amenity.light": 10.0})
    half_result = RankEngine(half_profile, registry=registry).score_listing(listing)
    assert half_result.score == pytest.approx(100.0)
    assert half_result.normalization.max_possible == pytest.approx(10.0)


def test_scaled_weight_uses_rate_and_cap() -> None:
    registry = RuleRegistry()

    @registry.rule("space.over_minimum", category="space", label="Space over minimum")
    def detect_space(_: Listing, __: object) -> Signal:
        return Signal(fired=True, magnitude=700.0, confidence=1.0, evidence="700 sqft over target")

    profile = _profile({"space.over_minimum": {"per_100_sqft": 4, "cap": 12}})
    result = RankEngine(profile, registry=registry).score_listing(cast(Listing, object()))

    assert result.score == pytest.approx(100.0)
    assert result.contributions[0].contribution == pytest.approx(12.0)
    assert result.normalization.max_possible == pytest.approx(12.0)


def test_missing_rule_for_enabled_weight_raises() -> None:
    profile = _profile({"amenity.nonexistent": 5.0})
    with pytest.raises(ValueError, match="No rule registered for enabled weight"):
        RankEngine(profile, registry=RuleRegistry()).score_listing(cast(Listing, object()))
