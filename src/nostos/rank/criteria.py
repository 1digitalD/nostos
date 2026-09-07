"""Typed hard-criteria assessment shared by ranking and browsing."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from nostos.config.profile import Profile
from nostos.enrich.issues import saved_evidence_issues
from nostos.model import Absence, Area, Listing, Observed
from nostos.rank.profile_scoring import _is_basement_listing, _is_furnished, listing_area_key
from nostos.rank.rules import DEFAULT_REGISTRY, Signal


@dataclass(frozen=True, slots=True)
class CriterionCheck:
    field: str
    status: Literal["pass", "fail", "unknown"]
    reason: str
    evidence: str | None = None
    origin: str | None = None


@dataclass(frozen=True, slots=True)
class MatchStatus:
    status: Literal["match", "unverified", "miss"]
    reasons: tuple[str, ...] = ()
    checks: tuple[CriterionCheck, ...] = field(default=(), compare=False)

    @property
    def evidence_status(self) -> Literal["match", "unverified", "miss"]:
        """User-facing evidence outcome, separate from legacy processing eligibility."""
        if any(check.status == "fail" for check in self.checks):
            return "miss"
        if any(check.status == "unknown" for check in self.checks):
            return "unverified"
        return self.status


def _details(value: object) -> tuple[str | None, str | None]:
    if isinstance(value, Observed):
        return value.evidence, value.origin.value
    return None, None


def _scalar(value: object) -> float | None:
    return float(value.value) if isinstance(value, Observed) else None


def _fmt_money(value: float) -> str:
    return f"${value:,.0f}"


def _fmt_num(value: float) -> str:
    return f"{value:g}"


def _numeric_check(
    *,
    field_name: str,
    label: str,
    observed: object,
    eq: float | None,
    minimum: float | None,
    maximum: float | None,
    misses: list[str],
    unknowns: list[str],
) -> CriterionCheck:
    value = _scalar(observed)
    evidence, origin = _details(observed)
    if value is None:
        legacy_reason = f"{label} unstated"
        typed_reason = (
            f"{label} evidence is contradictory"
            if observed == Absence.CONTRADICTORY
            else legacy_reason
        )
        unknowns.append(legacy_reason)
        return CriterionCheck(field_name, "unknown", typed_reason)
    if eq is not None and value != eq:
        reason = f"{label} {_fmt_num(value)} ≠ {_fmt_num(eq)}"
    elif minimum is not None and value < minimum:
        reason = f"{label} {_fmt_num(value)} < min {_fmt_num(minimum)}"
    elif maximum is not None and value > maximum:
        reason = f"{label} {_fmt_num(value)} > max {_fmt_num(maximum)}"
    else:
        return CriterionCheck(
            field_name, "pass", f"{label} meets requirement", evidence, origin
        )
    misses.append(reason)
    return CriterionCheck(field_name, "fail", reason, evidence, origin)


def _best_attribute(listing: Listing, *keys: str) -> Observed[Any] | None:
    found: list[Observed[Any]] = []
    for key in keys:
        value = listing.attributes.get(key)
        if isinstance(value, Observed):
            found.append(value)
    return max(found, key=lambda item: item.origin.precedence) if found else None


def _contradictory(listing: Listing, *keys: str) -> bool:
    return any(listing.attributes.get(key) == Absence.CONTRADICTORY for key in keys)


def _signal_check(
    field_name: str,
    label: str,
    signal: Signal | None,
    evidence: str | None,
    origin: str | None,
) -> CriterionCheck:
    if signal is None:
        return CriterionCheck(field_name, "unknown", f"{label} unstated", evidence, origin)
    if not signal.fired or signal.magnitude <= 0:
        return CriterionCheck(field_name, "fail", f"{label} required", evidence, origin)
    verb = "stated" if origin == "source_field" else "confirmed"
    return CriterionCheck(field_name, "pass", f"{label} {verb}", evidence, origin)


def classify_match_status(listing: Listing, profile: Profile) -> MatchStatus:
    """Evaluate every configured hard criterion once and explain the verdict."""

    hard = profile.hard
    misses: list[str] = []
    unknowns: list[str] = []
    checks: list[CriterionCheck] = []
    rent_units_unknown = False

    if hard.rent is not None:
        evidence, origin = _details(listing.rent)
        if not isinstance(listing.rent, Observed):
            legacy_reason = "rent unstated"
            typed_reason = (
                "rent evidence is contradictory"
                if listing.rent == Absence.CONTRADICTORY
                else legacy_reason
            )
            unknowns.append(legacy_reason)
            checks.append(CriterionCheck("rent", "unknown", typed_reason))
        else:
            amount = float(listing.rent.value.amount)
            failure: str | None = None
            if amount > hard.rent.max:
                failure = f"rent {_fmt_money(amount)} > max {_fmt_money(hard.rent.max)}"
                misses.append(failure)
            if hard.rent.min is not None and amount < hard.rent.min:
                failure = f"rent {_fmt_money(amount)} < min {_fmt_money(hard.rent.min)}"
                misses.append(failure)
            rent_units_unknown = (
                listing.rent.value.currency != hard.rent.currency
                or listing.rent.value.period != "month"
            )
            if rent_units_unknown:
                checks.append(
                    CriterionCheck(
                        "rent",
                        "unknown",
                        "rent currency or period needs verification",
                        evidence,
                        origin,
                    )
                )
            elif failure is not None:
                checks.append(CriterionCheck("rent", "fail", failure, evidence, origin))
            else:
                checks.append(
                    CriterionCheck("rent", "pass", "rent meets requirement", evidence, origin)
                )

    for field_name, configured, observed in (
        ("beds", hard.beds, listing.beds),
        ("baths", hard.baths, listing.baths),
    ):
        if configured is not None:
            checks.append(
                _numeric_check(
                    field_name=field_name,
                    label=field_name,
                    observed=observed,
                    eq=configured.eq,
                    minimum=configured.min,
                    maximum=configured.max,
                    misses=misses,
                    unknowns=unknowns,
                )
            )

    if hard.area is not None:
        evidence, origin = _details(listing.area)
        if not isinstance(listing.area, Observed) or not isinstance(listing.area.value, Area):
            legacy_reason = "area unstated"
            typed_reason = (
                "area evidence is contradictory"
                if listing.area == Absence.CONTRADICTORY
                else legacy_reason
            )
            unknowns.append(legacy_reason)
            checks.append(CriterionCheck("area", "unknown", typed_reason))
        elif listing.area.value.unit.lower() != hard.area.unit.lower():
            reason = f"area in {listing.area.value.unit}, profile uses {hard.area.unit}"
            unknowns.append(reason)
            checks.append(CriterionCheck("area", "unknown", reason, evidence, origin))
        elif listing.area.value.value < hard.area.min:
            reason = (
                f"area {listing.area.value.value:,.0f} "
                f"< min {hard.area.min:,.0f} {hard.area.unit}"
            )
            misses.append(reason)
            checks.append(CriterionCheck("area", "fail", reason, evidence, origin))
        else:
            checks.append(
                CriterionCheck("area", "pass", "area meets requirement", evidence, origin)
            )

    if hard.floor is not None:
        checks.append(
            _numeric_check(
                field_name="floor",
                label="floor",
                observed=listing.floor,
                eq=hard.floor.eq,
                minimum=hard.floor.min,
                maximum=hard.floor.max,
                misses=misses,
                unknowns=unknowns,
            )
        )

    if hard.areas:
        area_key = listing_area_key(listing)
        area_observation = (
            None
            if listing.place.area_key is not None and listing.place.area_key.strip()
            else listing.attributes.get("area_key")
        )
        evidence, origin = _details(area_observation)
        if area_key is None:
            reason = "area unknown"
            unknowns.append(reason)
            checks.append(CriterionCheck("neighbourhood", "unknown", reason))
        elif area_key not in set(hard.areas):
            reason = "area not in allowed list"
            misses.append(reason)
            checks.append(CriterionCheck("neighbourhood", "fail", reason, evidence, origin))
        else:
            checks.append(
                CriterionCheck(
                    "neighbourhood",
                    "pass",
                    "neighbourhood is in the allowed list",
                    evidence,
                    origin,
                )
            )

    excludes = {token.strip().lower() for token in hard.exclude}
    if "basement" in excludes:
        basement = listing.attributes.get("basement")
        evidence, origin = _details(basement)
        if _is_basement_listing(listing):
            reason = "basement unit"
            misses.append(reason)
            checks.append(CriterionCheck("basement", "fail", reason, evidence, origin))
        elif isinstance(basement, Observed) and isinstance(basement.value, bool):
            checks.append(
                CriterionCheck("basement", "pass", "not a basement unit", evidence, origin)
            )
        else:
            checks.append(CriterionCheck("basement", "unknown", "basement status unstated"))

    if "furnished_only" in excludes:
        evidence, origin = _details(listing.furnishing)
        furnishing = (
            listing.furnishing.value.strip().lower()
            if isinstance(listing.furnishing, Observed)
            and isinstance(listing.furnishing.value, str)
            else None
        )
        optional = furnishing is not None and "optional" in furnishing
        if _is_furnished(listing) and not optional:
            reason = "furnished only"
            misses.append(reason)
            checks.append(CriterionCheck("furnishing", "fail", reason, evidence, origin))
        elif furnishing is None:
            checks.append(
                CriterionCheck("furnishing", "unknown", "furnishing status unstated")
            )
        elif optional or "unfurnished" in furnishing:
            checks.append(
                CriterionCheck(
                    "furnishing",
                    "pass",
                    "not restricted to furnished",
                    evidence,
                    origin,
                )
            )
        else:
            checks.append(
                CriterionCheck(
                    "furnishing",
                    "unknown",
                    "furnishing status unclear",
                    evidence,
                    origin,
                )
            )

    for required, key, label, field_name, attribute_keys in (
        (
            hard.require_laundry,
            "laundry.in_suite",
            "in-suite laundry",
            "in_suite_laundry",
            ("attributes.in_suite_laundry", "in_suite_laundry"),
        ),
        (
            hard.require_parking,
            "parking.available",
            "parking",
            "parking",
            ("attributes.parking_available", "parking_available"),
        ),
    ):
        if not required:
            continue
        observation = _best_attribute(listing, *attribute_keys)
        evidence, origin = _details(observation)
        if (
            key == "laundry.in_suite"
            and observation is not None
            and isinstance(observation.value, bool)
        ):
            signal = None
            check = CriterionCheck(
                field_name,
                "pass" if observation.value else "fail",
                (
                    f"{label} stated"
                    if observation.value and origin == "source_field"
                    else f"{label} confirmed"
                    if observation.value
                    else f"{label} required"
                ),
                evidence,
                origin,
            )
        else:
            rule = DEFAULT_REGISTRY.get(key)
            signal = rule.detector(listing, None) if rule else None
            if observation is None and key == "parking.available":
                observation = listing.parking if isinstance(listing.parking, Observed) else None
                evidence, origin = _details(observation)
            if evidence is None and signal is not None:
                evidence = signal.evidence
            if origin is None and signal is not None:
                origin = "text_rule"
            check = _signal_check(field_name, label, signal, evidence, origin)
            if _contradictory(listing, *attribute_keys):
                if key == "parking.available":
                    claims = [
                        f"{claim.evidence or claim_key}: {claim.value}"
                        for claim_key in ("parking_source_claim", "parking_text_claim")
                        if isinstance(claim := listing.attributes.get(claim_key), Observed)
                    ]
                    if claims:
                        evidence = "; ".join(claims)
                check = CriterionCheck(
                    field_name,
                    "unknown",
                    f"{label} evidence is contradictory",
                    evidence,
                    origin,
                )

        if key == "laundry.in_suite" and observation is not None and isinstance(
            observation.value, bool
        ):
            if not observation.value:
                misses.append(f"{label} required")
        elif signal is None:
            unknowns.append(f"{label} unstated")
        elif not signal.fired or signal.magnitude <= 0:
            misses.append(f"{label} required")
        checks.append(check)

    for field_name, threshold, label in (
        ("lease_months", hard.lease_months_min, "lease months"),
        ("total_monthly", hard.total_monthly_max, "total monthly cost"),
    ):
        if threshold is None:
            continue
        attribute_observed = listing.attributes.get(field_name)
        value = _scalar(attribute_observed)
        evidence, origin = _details(attribute_observed)
        if value is None:
            legacy_reason = f"{label} unstated"
            typed_reason = (
                f"{label} evidence is contradictory"
                if attribute_observed == Absence.CONTRADICTORY
                else legacy_reason
            )
            unknowns.append(legacy_reason)
            checks.append(CriterionCheck(field_name, "unknown", typed_reason))
        elif (
            field_name == "lease_months"
            and value < threshold
            or field_name == "total_monthly"
            and value > threshold
        ):
            reason = f"{label} outside requirement"
            misses.append(reason)
            checks.append(CriterionCheck(field_name, "fail", reason, evidence, origin))
        else:
            checks.append(
                CriterionCheck(
                    field_name,
                    "pass",
                    f"{label} meets requirement",
                    evidence,
                    origin,
                )
            )

    if hard.available_by is not None:
        available = listing.attributes.get("available_date")
        evidence, origin = _details(available)
        try:
            actual = (
                date.fromisoformat(str(available.value))
                if isinstance(available, Observed)
                else None
            )
        except ValueError:
            actual = None
        if actual is None:
            reason = "availability date unstated"
            unknowns.append(reason)
            checks.append(CriterionCheck("available_date", "unknown", reason))
        elif actual > hard.available_by:
            reason = f"available after {hard.available_by}"
            misses.append(reason)
            checks.append(CriterionCheck("available_date", "fail", reason, evidence, origin))
        else:
            checks.append(
                CriterionCheck(
                    "available_date",
                    "pass",
                    "availability date meets requirement",
                    evidence,
                    origin,
                )
            )

    for issue in saved_evidence_issues(listing):
        checks = [check for check in checks if check.field != issue.field]
        checks.append(CriterionCheck(
            issue.field, "unknown", issue.reason, issue.evidence, "text_rule"
        ))

    if rent_units_unknown:
        unknowns.append("rent currency or period needs verification")
    if misses:
        return MatchStatus("miss", tuple(misses + unknowns), tuple(checks))
    if unknowns:
        return MatchStatus("unverified", tuple(unknowns), tuple(checks))
    return MatchStatus("match", (), tuple(checks))
