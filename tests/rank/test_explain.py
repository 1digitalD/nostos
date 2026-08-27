from __future__ import annotations

from typing import cast

from nostos.config.profile import Profile
from nostos.model import Listing
from nostos.rank.engine import RankEngine
from nostos.rank.explain import render_score_explanation
from nostos.rank.rules import RuleRegistry, Signal


def test_render_score_explanation_is_human_readable() -> None:
    registry = RuleRegistry()

    @registry.rule("laundry.in_suite", category="amenities", label="In-suite laundry")
    def detect_laundry(_: Listing, __: object) -> Signal:
        return Signal(
            fired=True,
            magnitude=1.0,
            confidence=0.9,
            evidence="In-suite washer and dryer",
        )

    @registry.rule("parking.available", category="amenities", label="Parking")
    def detect_parking(_: Listing, __: object) -> Signal:
        return Signal(
            fired=False,
            magnitude=0.0,
            confidence=1.0,
            evidence=None,
        )

    profile = Profile.model_validate(
        {
            "city": "vancouver",
            "weights": {"laundry.in_suite": 8.0, "parking.available": 4.0},
            "schedule": "0 */6 * * *",
        }
    )
    result = RankEngine(profile, registry=registry).score_listing(cast(Listing, object()))

    text = render_score_explanation(result)

    assert "Overall score:" in text
    assert "In-suite laundry helped this listing" in text
    assert "In-suite washer and dryer" in text
    assert "Parking had no effect" in text
