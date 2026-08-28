from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from nostos.config.profile import Profile
from nostos.context import SearchContext
from nostos.enrich.base import Enricher
from nostos.enrich.chain import run_enricher_chain
from nostos.enrich.text import basement_unit_evidence
from nostos.model import Absence, Area, Listing, Money, Observed
from nostos.rank.engine import RankEngine, ScoreResult


@dataclass(frozen=True, slots=True)
class ScoredProfileListing:
    listing: Listing
    result: ScoreResult


def prepare_listing_for_profile(
    listing: Listing,
    *,
    context: SearchContext,
    enrichers: Iterable[Enricher],
) -> Listing | None:
    active_enrichers = tuple(enrichers)
    enriched = (
        run_enricher_chain(listing, active_enrichers, context)
        if active_enrichers
        else listing
    )
    if not passes_hard_filters(enriched, context.profile):
        return None
    return enriched


def score_listing_for_profile(
    listing: Listing,
    *,
    context: SearchContext,
    enrichers: Iterable[Enricher],
    rank_engine: RankEngine,
) -> ScoredProfileListing | None:
    prepared = prepare_listing_for_profile(
        listing,
        context=context,
        enrichers=enrichers,
    )
    if prepared is None:
        return None
    result = rank_engine.score_listing(prepared, context=context)
    return ScoredProfileListing(listing=prepared, result=result)


def passes_hard_filters(listing: Listing, profile: Profile) -> bool:
    hard = profile.hard
    if hard.rent is not None:
        rent_value = _money_amount(listing)
        if rent_value is None or rent_value > hard.rent.max:
            return False

    if hard.beds is not None:
        beds_value = _observed_float(listing.beds)
        if beds_value is None or not _matches_numeric_filter(
            beds_value,
            eq=hard.beds.eq,
            minimum=hard.beds.min,
            maximum=hard.beds.max,
        ):
            return False

    if hard.baths is not None:
        baths_value = _observed_float(listing.baths)
        if baths_value is None or not _matches_numeric_filter(
            baths_value,
            eq=hard.baths.eq,
            minimum=hard.baths.min,
            maximum=hard.baths.max,
        ):
            return False

    if hard.area is not None:
        area_value = _observed_area(listing)
        if area_value is None:
            return False
        if hard.area.unit.lower() != area_value.unit.lower():
            return False
        if area_value.value < hard.area.min:
            return False

    excludes = {token.strip().lower() for token in hard.exclude}
    if "basement" in excludes and _is_basement_listing(listing):
        return False
    if "furnished_only" in excludes and _is_furnished(listing):
        return False
    return True


def _matches_numeric_filter(
    value: float,
    *,
    eq: float | None,
    minimum: float | None,
    maximum: float | None,
) -> bool:
    if eq is not None:
        return value == eq
    if minimum is not None and value < minimum:
        return False
    if maximum is not None and value > maximum:
        return False
    return True


def _money_amount(listing: Listing) -> float | None:
    field = listing.rent
    if isinstance(field, Observed):
        return float(field.value.amount)
    return None


def _observed_float(field: Observed[float] | Absence) -> float | None:
    if isinstance(field, Observed):
        return float(field.value)
    return None


def _observed_area(listing: Listing) -> Area | None:
    field = listing.area
    if isinstance(field, Observed) and isinstance(field.value, Area):
        return field.value
    return None


def _is_basement_listing(listing: Listing) -> bool:
    basement_attr = listing.attributes.get("basement")
    if isinstance(basement_attr, Observed) and isinstance(basement_attr.value, bool):
        return basement_attr.value
    return basement_unit_evidence(_listing_text_blob(listing)) is not None


def _is_furnished(listing: Listing) -> bool:
    field = listing.furnishing
    if not isinstance(field, Observed):
        return False
    value = field.value.strip().lower()
    return "furnished" in value and "unfurnished" not in value


def _listing_text_blob(listing: Listing) -> str:
    parts: list[str] = []
    if listing.place.raw_address:
        parts.append(listing.place.raw_address)
    for attribute in listing.attributes.values():
        if isinstance(attribute, Observed) and isinstance(attribute.value, str):
            parts.append(attribute.value)
    return " ".join(parts)


def rent_display(listing: Listing) -> str:
    if isinstance(listing.rent, Observed):
        value = listing.rent.value
        if isinstance(value, Money):
            return f"{value.amount:.2f} {value.currency}"
    return "unknown"


def listing_title(listing: Listing) -> str:
    attr = listing.attributes.get("title")
    if isinstance(attr, Observed) and isinstance(attr.value, str):
        text = attr.value.strip()
        if text:
            return text
    return listing.identity.listing_id
