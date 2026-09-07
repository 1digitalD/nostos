"""A conservative viewing brief, computed from saved facts without side effects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from nostos.config.profile import Profile
from nostos.model import Listing
from nostos.rank.criteria import CriterionCheck, classify_match_status


@dataclass(frozen=True)
class DecisionBrief:
    headline: str
    status: str
    blockers: tuple[str, ...]
    questions: tuple[str, ...]
    checks: tuple[CriterionCheck, ...]
    captured_at: datetime
    stale: bool


def build_decision_brief(
    listing: Listing,
    profile: Profile,
    *,
    extraction_current: bool,
    excluded: bool = False,
    dismissed: bool = False,
    now: datetime | None = None,
) -> DecisionBrief:
    """Explain readiness, not desirability; source claims are not verification.

    A 24-hour capture-age guard is a conservative presentation policy, not a
    refresh schedule or a claim that a more recent listing is still available.
    """
    captured = listing.raw_ref.fetched_at
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    stale = current - captured > timedelta(hours=24) or captured > current
    verdict = classify_match_status(listing, profile)
    blockers = tuple(c.reason for c in verdict.checks if c.status == "fail")
    questions = [c.reason for c in verdict.checks if c.status == "unknown"]
    if not extraction_current:
        questions.append("Review extracted details before relying on these facts.")
    if stale:
        questions.append(
            "This saved entry is over 24 hours old or its date is uncertain. "
            "Check the original and add a new manual entry if it has changed."
            if listing.identity.source == "manual"
            else "Refresh listing details: the saved source is over 24 hours old "
            "or its date is uncertain."
        )
    questions.append(
        "Confirm this unit is still available and the total monthly cost "
        "before arranging a viewing."
    )
    if excluded:
        headline, status = "You excluded this listing", "excluded"
    elif dismissed:
        headline, status = "You dismissed this listing", "dismissed"
    elif blockers:
        headline, status = (
            "Last saved facts don’t meet your criteria" if stale else "Doesn’t meet your criteria",
            "miss",
        )
    elif not verdict.checks:
        headline, status = "Set your criteria first", "unverified"
    elif (
        verdict.status != "match"
        or stale
        or not extraction_current
        or any(c.status == "unknown" for c in verdict.checks)
    ):
        headline, status = "Confirm essentials first", "unverified"
    else:
        headline, status = "Saved facts fit your criteria", "match"
    return DecisionBrief(
        headline, status, blockers, tuple(questions), verdict.checks, captured, stale
    )
