from datetime import timedelta
from typing import Any

import pytest

import nostos.decision as decision_module
from nostos.decision import build_decision_brief
from nostos.rank.criteria import CriterionCheck, MatchStatus
from tests.web.test_query_status import OBSERVED_AT, _listing, _profile


def test_fresh_supported_match_is_only_a_qualified_suggestion() -> None:
    brief = build_decision_brief(_listing(), _profile(), extraction_current=True, now=OBSERVED_AT)
    assert brief.status == "match"
    assert brief.headline == "Saved facts fit your criteria"
    assert "still available" in brief.questions[-1]


@pytest.mark.parametrize(
    "options",
    [
        {"extraction_current": False, "now": OBSERVED_AT},
        {"extraction_current": True, "now": OBSERVED_AT + timedelta(hours=25)},
        {"extraction_current": True, "now": OBSERVED_AT - timedelta(hours=1)},
    ],
)
def test_uncertain_evidence_never_recommends(options: dict[str, Any]) -> None:
    assert build_decision_brief(_listing(), _profile(), **options).status == "unverified"


def test_all_known_failures_are_explained() -> None:
    brief = build_decision_brief(
        _listing(rent=5000, beds=1), _profile(), extraction_current=True, now=OBSERVED_AT
    )
    assert brief.status == "miss"
    assert len(brief.blockers) == 2


@pytest.mark.parametrize("flag", ["excluded", "dismissed"])
def test_user_decision_wins(flag: str) -> None:
    brief = build_decision_brief(
        _listing(), _profile(), extraction_current=True, now=OBSERVED_AT, **{flag: True}
    )
    assert brief.status == flag


def test_unknown_exclusions_need_confirmation_without_changing_filters() -> None:
    brief = build_decision_brief(
        _listing(floor=None),
        _profile(exclude=["basement", "furnished_only"]),
        extraction_current=True,
        now=OBSERVED_AT,
    )
    assert brief.status == "unverified"
    assert any(check.status == "unknown" for check in brief.checks)


def test_legacy_unverified_verdict_cannot_become_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        decision_module,
        "classify_match_status",
        lambda *_: MatchStatus(
            "unverified", ("uncertain",), (CriterionCheck("beds", "pass", "beds stated"),)
        ),
    )
    brief = build_decision_brief(_listing(), _profile(), extraction_current=True, now=OBSERVED_AT)
    assert brief.status == "unverified"


def test_manual_stale_entry_does_not_offer_unavailable_refresh() -> None:
    listing = _listing()
    listing = listing.model_copy(
        update={"identity": listing.identity.model_copy(update={"source": "manual"})}
    )
    brief = build_decision_brief(
        listing, _profile(), extraction_current=True, now=OBSERVED_AT + timedelta(days=2)
    )
    assert any("new manual entry" in question for question in brief.questions)
    assert not any("Refresh listing" in question for question in brief.questions)


def test_conflicting_bedroom_fact_is_not_a_known_failure() -> None:
    brief = build_decision_brief(
        _listing(beds=1, description="Two bedroom apartment."),
        _profile(),
        extraction_current=True,
        now=OBSERVED_AT,
    )
    assert brief.status == "unverified"
    assert brief.blockers == ()
