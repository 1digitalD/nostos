"""One explained criteria verdict shared by ranking and browsing."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from nostos.config.profile import Profile
from nostos.model import Area, Listing, Observed
from nostos.rank.profile_scoring import _is_basement_listing, _is_furnished, listing_area_key
from nostos.rank.rules import DEFAULT_REGISTRY


@dataclass(frozen=True, slots=True)
class MatchStatus:
    status: Literal["match", "unverified", "miss"]
    reasons: tuple[str, ...] = ()

def _rent_amount(listing: Listing) -> float | None:
    return float(listing.rent.value.amount) if isinstance(listing.rent, Observed) else None

def _observed_scalar(value: object) -> float | None:
    return float(value.value) if isinstance(value, Observed) else None

def _area_values(listing: Listing) -> tuple[float | None, str | None]:
    if isinstance(listing.area, Observed) and isinstance(listing.area.value, Area):
        return listing.area.value.value, listing.area.value.unit
    return None, None

def _fmt_money(value: float) -> str:
    return f"${value:,.0f}"


def _fmt_num(value: float) -> str:
    return f"{value:g}"


def _check_numeric(
    *,
    name: str,
    value: float | None,
    eq: float | None,
    minimum: float | None,
    maximum: float | None,
    unstated_is_miss: bool,
    misses: list[str],
    unknowns: list[str],
) -> None:
    """Append a reason to ``misses`` or ``unknowns`` for one numeric hard filter."""

    if value is None:
        (misses if unstated_is_miss else unknowns).append(f"{name} unstated")
        return
    if eq is not None and value != eq:
        misses.append(f"{name} {_fmt_num(value)} ≠ {_fmt_num(eq)}")
        return
    if minimum is not None and value < minimum:
        misses.append(f"{name} {_fmt_num(value)} < min {_fmt_num(minimum)}")
    if maximum is not None and value > maximum:
        misses.append(f"{name} {_fmt_num(value)} > max {_fmt_num(maximum)}")


def classify_match_status(listing: Listing, profile: Profile) -> MatchStatus:
    """Compare the listing against every hard filter and explain the verdict.

    - ``miss``       — at least one stated criterion fails (reasons list each)
    - ``unverified`` — nothing fails, but a criterion-relevant field is
                       unstated (reasons list what is missing)
    - ``match``      — every criterion passes with the data available

    ``miss`` wins over ``unverified``; the reasons tuple carries the misses
    first, then the unknowns, so the tooltip leads with the decisive facts.
    """

    hard = profile.hard
    misses: list[str] = []
    unknowns: list[str] = []

    if hard.rent is not None:
        rent_value = _rent_amount(listing)
        if rent_value is None:
            unknowns.append("rent unstated")
        else:
            if rent_value > hard.rent.max:
                misses.append(f"rent {_fmt_money(rent_value)} > max {_fmt_money(hard.rent.max)}")
            if hard.rent.min is not None and rent_value < hard.rent.min:
                misses.append(f"rent {_fmt_money(rent_value)} < min {_fmt_money(hard.rent.min)}")

    if hard.beds is not None:
        _check_numeric(
            name="beds",
            value=_observed_scalar(listing.beds),
            eq=hard.beds.eq,
            minimum=hard.beds.min,
            maximum=hard.beds.max,
            unstated_is_miss=False,
            misses=misses,
            unknowns=unknowns,
        )

    if hard.baths is not None:
        _check_numeric(
            name="baths",
            value=_observed_scalar(listing.baths),
            eq=hard.baths.eq,
            minimum=hard.baths.min,
            maximum=hard.baths.max,
            unstated_is_miss=False,
            misses=misses,
            unknowns=unknowns,
        )

    if hard.area is not None:
        area_value, area_unit = _area_values(listing)
        if area_value is None:
            unknowns.append("area unstated")
        elif area_unit is not None and area_unit.lower() != hard.area.unit.lower():
            unknowns.append(f"area in {area_unit}, profile uses {hard.area.unit}")
        elif area_value < hard.area.min:
            misses.append(
                f"area {area_value:,.0f} < min {hard.area.min:,.0f} {hard.area.unit}"
            )

    if hard.floor is not None:
        _check_numeric(
            name="floor",
            value=_observed_scalar(listing.floor),
            eq=hard.floor.eq,
            minimum=hard.floor.min,
            maximum=hard.floor.max,
            unstated_is_miss=False,
            misses=misses,
            unknowns=unknowns,
        )

    if hard.areas:
        area_key = listing_area_key(listing)
        if area_key is None:
            unknowns.append("area unknown")
        elif area_key not in set(hard.areas):
            misses.append("area not in allowed list")

    excludes = {token.strip().lower() for token in hard.exclude}
    if "basement" in excludes and _is_basement_listing(listing):
        misses.append("basement unit")
    if "furnished_only" in excludes and _is_furnished(listing):
        misses.append("furnished only")

    for required, key, label in (
        (hard.require_laundry, "laundry.in_suite", "in-suite laundry"),
        (hard.require_parking, "parking.available", "parking"),
    ):
        if required:
            rule = DEFAULT_REGISTRY.get(key)
            signal = rule.detector(listing, None) if rule else None
            if signal is None:
                unknowns.append(f"{label} unstated")
            elif not signal.fired or signal.magnitude <= 0:
                misses.append(f"{label} required")
    for field, threshold, label in (
        ("lease_months", hard.lease_months_min, "lease months"),
        ("total_monthly", hard.total_monthly_max, "total monthly cost"),
    ):
        if threshold is not None:
            value = _observed_scalar(listing.attributes.get(field))
            if value is None:
                unknowns.append(f"{label} unstated")
            elif (field == "lease_months" and value < threshold
                  or field == "total_monthly" and value > threshold):
                misses.append(f"{label} outside requirement")
    if hard.available_by is not None:
        available_value = listing.attributes.get("available_date")
        try:
            actual = (
                date.fromisoformat(str(available_value.value))
                if isinstance(available_value, Observed)
                else None
            )
        except ValueError:
            actual = None
        if actual is None:
            unknowns.append("availability date unstated")
        elif actual > hard.available_by:
            misses.append(f"available after {hard.available_by}")
    if hard.rent is not None and isinstance(listing.rent, Observed):
        if (
            listing.rent.value.currency != hard.rent.currency
            or listing.rent.value.period != "month"
        ):
            unknowns.append("rent currency or period needs verification")

    if misses:
        return MatchStatus(status="miss", reasons=tuple(misses + unknowns))
    if unknowns:
        return MatchStatus(status="unverified", reasons=tuple(unknowns))
    return MatchStatus(status="match", reasons=())
