from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from nostos.enrich.base import CostModel
from nostos.model import Absence, Area, Field, Listing, Money, Observed, Origin

_PRICE_RE = re.compile(
    r"\$\s*((?:[1-9]\d{0,2}(?:,\d{3})+)|(?:[1-9]\d{2,3}))\s*"
    r"(?:/\s*(?:mo(?:nth)?|month)|monthly|per month)?",
    re.IGNORECASE,
)
_AVAILABLE_ISO_RE = re.compile(
    r"\b(?:available|availability|move[- ]in(?:\s+date)?)"
    r"(?:\s+(?:on|from|as\s+of))?\s*:?\s*(\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)
_MONTH_NAME_RE = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)"
)
_AVAILABLE_TEXT_RE = re.compile(
    r"\b(?:available|availability|move[- ]in(?:\s+date)?)"
    r"(?:\s+(?:on|from|as\s+of))?\s*:?\s*(?:"
    + _MONTH_NAME_RE
    + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?|now|immediately)\b",
    re.IGNORECASE,
)
_LEASE_MONTHS_RE = re.compile(
    r"\b(?:(?P<months>\d{1,2})[-\s]+months?|(?P<one_year>one[-\s]+year))\s+lease\b",
    re.IGNORECASE,
)
_UTILITY_ITEM_PATTERN = (
    r"(?:hot\s+water|heat|water|hydro|electricity|electric|gas|internet|"
    r"wi[- ]?fi|cable)"
)
_UTILITY_SEPARATOR_PATTERN = r"(?:\s*,\s*(?:and\s+)?|\s+(?:and|&)\s+)"
_UTILITY_LIST_PATTERN = (
    rf"{_UTILITY_ITEM_PATTERN}(?:{_UTILITY_SEPARATOR_PATTERN}{_UTILITY_ITEM_PATTERN})*"
)
_ALL_UTILITIES_INCLUDED_RE = re.compile(
    r"\ball\s+utilities\s+(?:are\s+)?included\b",
    re.IGNORECASE,
)
_SPECIFIC_UTILITIES_INCLUDED_RES = (
    re.compile(
        rf"\b(?P<items>{_UTILITY_LIST_PATTERN})\s+"
        r"(?:(?:is|are)\s+)?included\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:utilities|rent)\s+includes?\s+(?P<items>{_UTILITY_LIST_PATTERN})\b",
        re.IGNORECASE,
    ),
)
_UNSPECIFIED_UTILITIES_INCLUDED_RE = re.compile(
    r"\butilities\s+(?:are\s+)?included\b",
    re.IGNORECASE,
)
_UTILITY_ITEM_RE = re.compile(_UTILITY_ITEM_PATTERN, re.IGNORECASE)
_TOTAL_MONTHLY_RE = re.compile(
    r"\b(?:total\s+monthly\s+(?:cost|rent)|monthly\s+total)\s*"
    r"(?:(?:is|of)\s+|:\s*)?\$\s*"
    r"(?P<amount>(?:[1-9]\d{0,2}(?:,\d{3})+)|(?:[1-9]\d{2,4}))"
    r"(?:\s*(?:/\s*(?:mo(?:nth)?|month)|monthly|per\s+month))?\b",
    re.IGNORECASE,
)
_BEDS_RE = re.compile(r"\b(\d+(?:\.5)?)\s*(?:bed(?:room)?s?|br|bd)\b", re.IGNORECASE)
_BEDS_RANGE_RE = re.compile(
    r"\b\d(?:\.5)?\s*(?:&|and|or|/|[-–]|to)\s*\d(?:\.5)?\s*"
    r"(?:bed(?:room)?s?|br|bd)\b",
    re.IGNORECASE,
)
_BATHS_RE = re.compile(r"\b(\d+(?:\.5)?)\s*(?:bath(?:room)?s?|ba)\b", re.IGNORECASE)
_SQFT_RE = re.compile(
    r"\b([7-9]\d{2}|[1-9]\d{3})\s*(?:sq\.?\s*ft\.?|sqft|square\s+feet)\b",
    re.IGNORECASE,
)
_SQFT_RANGE_RE = re.compile(
    r"\b\d{2,5}\s*(?:[-–]|to)\s*\d{2,5}\s*"
    r"(?:sq\.?\s*ft\.?|sqft|square\s+feet)\b",
    re.IGNORECASE,
)
_ORDINAL_FLOORS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
}
_FLOOR_RE = re.compile(
    r"\b(?:floor|level|storey|story)\s*(\d{1,2})\b"
    r"(?!\s*[- ]?(?:bed(?:room)?s?|br|bd)\b)|"
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:floor|level|storey|story)\b|"
    r"\b("
    + "|".join(_ORDINAL_FLOORS)
    + r")\s+(?:floor|level|storey|story)\b",
    re.IGNORECASE,
)

_BASEMENT_STRONG_RE = re.compile(
    r"\b(?:basement\s+(?:suite|unit|apartment|apt|level|floor)|"
    r"below[- ]grade|"
    r"lower[- ]level\s+(?:suite|unit|apartment)|"
    r"garden[- ]level\s+(?:suite|unit|apartment)|"
    r"suite\s+in\s+(?:the\s+)?basement)\b",
    re.IGNORECASE,
)
_BASEMENT_RE = re.compile(r"\bbasement\b", re.IGNORECASE)
_BASEMENT_NON_UNIT_RE = re.compile(
    r"\bbasement\s+(?:storage|locker|parking|stall|parkade|garage)\b|"
    r"\bbasement\s+is\s+(?:(?:used|reserved)\s+)?(?:for\s+)?storage(?:\s+only)?\b|"
    r"\bbasement\s+(?:suite|unit|apartment|apt)(?:\s+is)?\s+(?:a\s+)?"
    r"(?:separate\s+)?optional(?:\s+(?:add[- ]?on|rental|space))?\b|"
    r"\bbasement\s+(?:suite|unit|apartment|apt)\b[^.!?]{0,160}"
    r"\bcan\s+be\s+added\b|"
    r"\b(?:optional|separate)\s+basement\s+(?:suite|unit|apartment|apt)\b",
    re.IGNORECASE,
)
_BASEMENT_NEGATED_RE = re.compile(
    r"\b(?:not|isn['’]t)\s+(?:a\s+)?basement\b|\bno\s+basement\b",
    re.IGNORECASE,
)

_FURNISHED_NO_RE = re.compile(
    r"\bfurnished\s*[:=-]\s*(?:no|false)\b|\bnot\s+furnished\b",
    re.IGNORECASE,
)
_FURNISHED_YES_RE = re.compile(
    r"\bfurnished\s*[:=-]\s*(?:yes|true)\b",
    re.IGNORECASE,
)
_UNFURNISHED_OPTION_RE = re.compile(
    r"\b(?:can|may|could)\s+be\s+unfurnished\b|\bunfurnished\s+(?:option|available)\b",
    re.IGNORECASE,
)
_UNFURNISHED_RE = re.compile(r"\bunfurnished\b", re.IGNORECASE)
_FURNISHED_RE = re.compile(r"\bfurnished(?:\s+only)?\b", re.IGNORECASE)

_PARKING_UNAVAILABLE_RE = re.compile(
    r"\bno\s+dedicated\s+parking(?:\s+(?:is\s+)?)?included\b|"
    r"\bno\s+(?:dedicated\s+|assigned\s+|on[- ]site\s+|off[- ]street\s+)?"
    r"(?:parking|garage|stall)\b"
    r"(?!\s+(?:(?:stall|space|spot)\s+)?(?:is\s+)?included\b)|"
    r"\bwithout\s+(?:assigned\s+|on[- ]site\s+|off[- ]street\s+)?"
    r"(?:parking|garage|stall)\b|"
    r"\b(?:parking|garage|stall)(?:\s+(?:space|spot))?\s+"
    r"(?:is\s+)?(?:not\s+available|unavailable)\b",
    re.IGNORECASE,
)
_PARKING_INCLUDED_RE = re.compile(
    r"\bfree\s+parking\b|"
    r"\bparking\s*(?:&|and)\s*locker\s+(?:is\s+)?included\b|"
    r"\b(?:parking(?:\s+(?:stall|space|spot))?|garage|stall)\s+(?:is\s+)?included\b|"
    r"\b(?:includes?|comes?\s+with)\s+(?:an?\s+|one\s+|1\s+)?"
    r"(?:underground\s+|secured\s+)?(?:parking(?:\s+(?:stall|space|spot))?|garage|stall)\b",
    re.IGNORECASE,
)
_PARKING_AVAILABLE_RE = re.compile(
    r"(?:✔|✓|☑)\s*parking\b|"
    r"\b(?:one|1)\s+(?:covered\s+)?car\s*port\b|"
    r"\b(?:parking(?:\s+(?:stall|space|spot))?|garage|stall)\s+"
    r"(?:is\s+)?(?:available|optional)\b|"
    r"\b(?:one|1)\s+(?:underground\s+|secured\s+|assigned\s+)?"
    r"parking\s+(?:stall|space|spot)\b|"
    r"\b(?:parking(?:\s+(?:stall|space|spot))?|garage|stall)\s+"
    r"(?:can\s+be\s+)?(?:rented|reserved)\b",
    re.IGNORECASE,
)

_ROOM_ONLY_RE = re.compile(
    r"\b(?:room for rent|shared (?:room|home|house|apartment|unit)|"
    r"roommate (?:wanted|needed))\b",
    re.IGNORECASE,
)
_FULL_UNIT_RE = re.compile(
    r"\b(?:entire|full)\s+(?:apartment|condo|townhome|house|suite|unit)\b",
    re.IGNORECASE,
)

_WALK_SCORE_RE = re.compile(r"[Ww]alk\s*[Ss]core[:\s/]*(\d{2,3})", re.IGNORECASE)
_WALKABLE_PHRASE_RE = re.compile(
    r"\b(steps?\s+from|walking\s+distance|heart\s+of|in\s+the\s+core|"
    r"vibrant|retail\s+strip|on\s+the\s+strip|main\s+street\s+location|"
    r"walk\s+to\s+everything)\b",
    re.IGNORECASE,
)
_SPARSE_PHRASE_RE = re.compile(
    r"\b(suburban|single[\s-]family|tree[\s-]lined\s+street|"
    r"primarily\s+residential|residential\s+area|cul[\s-]de[\s-]sac|"
    r"quiet\s+neighborhood|family\s+neighborhood|low[\s-]density)\b",
    re.IGNORECASE,
)
_PET_NO_RE = re.compile(
    r"\b(no\s+pets?|pet[- ]free|sorry\s+no\s+pets?|pets?\s+not\s+allowed|pets?\s+prohibited)\b",
    re.IGNORECASE,
)
_PET_FRIENDLY_RE = re.compile(
    r"\b(pet[- ]friendly|pets?\s+(?:allowed|welcome)|"
    r"cats?\s+(?:ok|welcome|allowed)|dogs?\s+(?:ok|welcome|allowed))\b",
    re.IGNORECASE,
)
_PET_CONDITIONAL_RE = re.compile(
    r"\b(pets?\s+considered|case[\s-]by[\s-]case|with\s+approval|"
    r"landlord\s+approval|pet\s+deposit|pet\s+restrictions)\b",
    re.IGNORECASE,
)
_IN_SUITE_LAUNDRY_RE = re.compile(
    r"\b(in[\s-]suite\s+laundry|in[\s-]unit\s+laundry|ensuite\s+laundry|"
    r"private\s+laundry|own\s+laundry|"
    r"(?:in[\s-]suite|in[\s-]unit)\s+washer(?:\s*(?:/|&|and)\s*|\s+)dryer|"
    r"w\s*/\s*d\s+(?:in|inside)\s+(?:the\s+)?(?:suite|unit|apartment|home)|"
    r"washer(?:\s*(?:/|&|and)\s*|\s+)dryer\s+(?:in|inside)\s+"
    r"(?:the|a|an)?\s*(?:suite|unit|apartment|home)|"
    r"(?:suite|unit|apartment|home)\s+(?:has|includes?|comes?\s+with)\s+"
    r"(?:an?\s+)?washer(?:[\s/&-]+and)?[\s/&-]+dryer)\b",
    re.IGNORECASE,
)
_NEGATED_IN_SUITE_LAUNDRY_RE = re.compile(
    r"\b(?:no|without)\s+(?:in[\s-]suite|in[\s-]unit|ensuite|private)\s+laundry\b|"
    r"\b(?:in[\s-]suite|in[\s-]unit|ensuite|private)\s+laundry\s+"
    r"(?:is\s+)?(?:not\s+available|not\s+included|unavailable)\b|"
    r"\b(?:no|without)\s+washer(?:\s*(?:/|&|and)\s*|\s+)dryer\s+"
    r"(?:in|inside)\s+(?:the|a|an)?\s*(?:suite|unit|apartment|home)\b|"
    r"\b(?:no|without)\s+(?:in[\s-]suite|in[\s-]unit)\s+"
    r"washer(?:\s*(?:/|&|and)\s*|\s+)dryer\b|"
    r"\b(?:in[\s-]suite|in[\s-]unit)\s+"
    r"washer(?:\s*(?:/|&|and)\s*|\s+)dryer\s+(?:is\s+)?shared\b",
    re.IGNORECASE,
)
_BUILDING_LAUNDRY_RE = re.compile(
    r"\b(coin[\s-]?(?:op(?:erated)?|[\s-]?laundry)|shared\s+laundry|"
    r"common[\s-]area\s+laundry|common\s+laundry|building\s+laundry|"
    r"on[\s-]site\s+laundry|onsite\s+laundry|laundry\s+in\s+"
    r"(?:the|a|an|this|my)\s+building|laundry\s+in\s+(?:the\s+)?(?:building|bldg)|"
    r"central\s+laundry|communal\s+laundry|public\s+laundry)\b",
    re.IGNORECASE,
)
_NEGATED_BUILDING_LAUNDRY_RE = re.compile(
    r"\b(?:no|without)\s+(?:shared|coin(?:[\s-]op(?:erated)?)?|common|building|"
    r"on[\s-]site|public|communal)\s+laundry\b|"
    r"\b(?:shared|common|building|on[\s-]site|public|communal)\s+laundry\s+"
    r"(?:is\s+)?(?:not\s+available|unavailable)\b|"
    r"\bno\s+(?:shared|common)\s+washer",
    re.IGNORECASE,
)
_DEN_OR_SOLARIUM_RE = re.compile(r"\b(den|solarium)\b", re.IGNORECASE)

_TEXT_PART_KEYS = (
    "title",
    "description",
    "source_attributes",
    "address",
    "notes",
    "listingText",
    "listing_text",
)
_NB_TEXT_PART_KEYS = ("title", "address", "structuredLocation", "structured_location")


@dataclass(slots=True)
class TextRuleEnricher:
    name: str = "text-rule"
    provides: frozenset[str] = frozenset(
        {
            "rent",
            "beds",
            "baths",
            "area",
            "floor",
            "parking",
            "furnishing",
            "attributes.basement",
            "attributes.full_unit",
            "attributes.area_key",
            "attributes.walk_score",
            "attributes.pet_policy",
            "attributes.density_signal",
            "attributes.in_suite_laundry",
            "attributes.building_laundry",
            "attributes.has_den_or_solarium",
            "attributes.available_date",
            "attributes.available_text",
            "attributes.lease_months",
            "attributes.utilities_included",
            "attributes.total_monthly",
        }
    )
    requires: frozenset[str] = frozenset()
    cost: CostModel = CostModel.FREE
    origin: Origin = Origin.TEXT_RULE
    confidence: float = 0.6

    def estimate_cost(self, listing: Listing, context: object) -> Decimal:
        del listing, context
        return Decimal("0")

    def enrich(self, listing: Listing, context: object) -> dict[str, Observed[Any]]:
        observed_at = datetime.now(tz=UTC)
        return recover_missing_attributes(
            listing,
            context,
            observed_at=observed_at,
            confidence=self.confidence,
        )


def recover_missing_attributes(
    listing: Listing,
    context: object,
    *,
    observed_at: datetime | None = None,
    confidence: float = 0.6,
) -> dict[str, Observed[Any]]:
    observed_time = observed_at or datetime.now(tz=UTC)
    text_parts = _extract_text_parts(listing)
    full_text = " ".join(text_parts.values()).strip()
    if not full_text:
        return {}

    updates: dict[str, Observed[Any]] = {}
    total_monthly_match = _TOTAL_MONTHLY_RE.search(full_text)

    basement_evidence = basement_unit_evidence(full_text)
    if basement_evidence is not None and _attribute_is_fillable(listing, "basement"):
        updates["attributes.basement"] = _observed(
            True,
            evidence=basement_evidence,
            observed_at=observed_time,
            confidence=confidence,
        )

    availability_match = availability_date_from_text(full_text)
    if availability_match is not None and _attribute_is_fillable(listing, "available_date"):
        available_date, evidence = availability_match
        updates["attributes.available_date"] = _observed(
            available_date,
            evidence=evidence,
            observed_at=observed_time,
            confidence=confidence,
        )
    elif (
        _attribute_is_fillable(listing, "available_date")
        and _attribute_is_fillable(listing, "available_text")
    ):
        ambiguous_availability = ambiguous_availability_from_text(full_text)
        if ambiguous_availability is not None:
            updates["attributes.available_text"] = _observed(
                ambiguous_availability,
                evidence=ambiguous_availability,
                observed_at=observed_time,
                confidence=confidence,
            )

    lease_match = lease_months_from_text(full_text)
    if lease_match is not None and _attribute_is_fillable(listing, "lease_months"):
        lease_months, evidence = lease_match
        updates["attributes.lease_months"] = _observed(
            lease_months,
            evidence=evidence,
            observed_at=observed_time,
            confidence=confidence,
        )

    utilities_match = utilities_included_from_text(full_text)
    if utilities_match is not None and _attribute_is_fillable(listing, "utilities_included"):
        utilities, evidence = utilities_match
        updates["attributes.utilities_included"] = _observed(
            utilities,
            evidence=evidence,
            observed_at=observed_time,
            confidence=confidence,
        )

    if total_monthly_match is not None and _attribute_is_fillable(listing, "total_monthly"):
        updates["attributes.total_monthly"] = _observed(
            int(total_monthly_match.group("amount").replace(",", "")),
            evidence=total_monthly_match.group(0).strip(),
            observed_at=observed_time,
            confidence=confidence,
        )

    if _is_missing(listing.rent):
        match = next(
            (
                candidate
                for candidate in _PRICE_RE.finditer(full_text)
                if total_monthly_match is None
                or not _spans_overlap(candidate.span(), total_monthly_match.span())
            ),
            None,
        )
        if match is not None:
            currency = _context_currency(context)
            updates["rent"] = _observed(
                Money(
                    amount=Decimal(match.group(1).replace(",", "")),
                    currency=currency,
                    period="month",
                ),
                evidence=match.group(0).strip(),
                observed_at=observed_time,
                confidence=confidence,
            )

    if _is_missing(listing.beds) and not has_ambiguous_bedroom_range(full_text):
        match = _BEDS_RE.search(full_text)
        if match is not None:
            beds_text = match.group(1)
            beds_value = float(beds_text)
            updates["beds"] = _observed(
                beds_value,
                evidence=match.group(0).strip(),
                observed_at=observed_time,
                confidence=confidence,
            )

    if _is_missing(listing.baths):
        match = _BATHS_RE.search(full_text)
        if match is not None:
            updates["baths"] = _observed(
                float(match.group(1)),
                evidence=match.group(0).strip(),
                observed_at=observed_time,
                confidence=confidence,
            )

    if _is_missing(listing.area) and not has_ambiguous_area_range(full_text):
        match = _SQFT_RE.search(full_text)
        if match is not None:
            area_unit = _context_area_unit(context)
            updates["area"] = _observed(
                Area(value=float(match.group(1)), unit=area_unit),
                evidence=match.group(0).strip(),
                observed_at=observed_time,
                confidence=confidence,
            )

    if _is_missing(listing.floor):
        floor_match = floor_from_text(full_text)
        if floor_match is not None:
            floor_value, evidence = floor_match
            updates["floor"] = _observed(
                floor_value,
                evidence=evidence,
                observed_at=observed_time,
                confidence=confidence,
            )

    if _is_missing(listing.furnishing):
        furnishing_match = furnishing_from_text(full_text)
        if furnishing_match is not None:
            furnishing, evidence = furnishing_match
            updates["furnishing"] = _observed(
                furnishing,
                evidence=evidence,
                observed_at=observed_time,
                confidence=confidence,
            )

    if _is_missing(listing.parking):
        parking_match = parking_from_text(full_text)
        if parking_match is not None:
            parking_value, evidence = parking_match
            updates["parking"] = _observed(
                parking_value,
                evidence=evidence,
                observed_at=observed_time,
                confidence=confidence,
            )

    if _attribute_is_fillable(listing, "full_unit"):
        room_only_match = _ROOM_ONLY_RE.search(full_text)
        if room_only_match is not None:
            updates["attributes.full_unit"] = _observed(
                False,
                evidence=room_only_match.group(0).strip(),
                observed_at=observed_time,
                confidence=confidence,
            )
        else:
            full_unit_match = _FULL_UNIT_RE.search(full_text)
            if full_unit_match is not None:
                updates["attributes.full_unit"] = _observed(
                    True,
                    evidence=full_unit_match.group(0).strip(),
                    observed_at=observed_time,
                    confidence=confidence,
                )

    if listing.place.area_key is None and _attribute_is_fillable(listing, "area_key"):
        nb_text = neighborhood_haystack(*(text_parts.get(key) for key in _NB_TEXT_PART_KEYS))
        area_match = match_area_key_from_neighborhood_text(nb_text, context)
        if area_match is not None:
            area_key, evidence = area_match
            updates["attributes.area_key"] = _observed(
                area_key,
                evidence=evidence,
                observed_at=observed_time,
                confidence=confidence,
            )

    if _attribute_is_fillable(listing, "walk_score"):
        walk_score_match = _WALK_SCORE_RE.search(full_text)
        if walk_score_match is not None:
            walk_score = int(walk_score_match.group(1))
            if 0 <= walk_score <= 100:
                updates["attributes.walk_score"] = _observed(
                    walk_score,
                    evidence=walk_score_match.group(0).strip(),
                    observed_at=observed_time,
                    confidence=confidence,
                )

    if _attribute_is_fillable(listing, "pet_policy"):
        pet_policy_match = _pet_policy(full_text)
        if pet_policy_match is not None:
            policy, evidence = pet_policy_match
            updates["attributes.pet_policy"] = _observed(
                policy,
                evidence=evidence,
                observed_at=observed_time,
                confidence=confidence,
            )

    if _attribute_is_fillable(listing, "density_signal"):
        density_signal_match = _density_signal(full_text)
        if density_signal_match is not None:
            signal, evidence = density_signal_match
            updates["attributes.density_signal"] = _observed(
                signal,
                evidence=evidence,
                observed_at=observed_time,
                confidence=confidence,
            )

    if _attribute_is_fillable(listing, "in_suite_laundry"):
        laundry_match = in_suite_laundry_evidence(full_text)
        if laundry_match is not None:
            updates["attributes.in_suite_laundry"] = _observed(
                True,
                evidence=laundry_match,
                observed_at=observed_time,
                confidence=confidence,
            )

    if _attribute_is_fillable(listing, "building_laundry"):
        laundry_match = building_laundry_evidence(full_text)
        if laundry_match is not None:
            updates["attributes.building_laundry"] = _observed(
                True,
                evidence=laundry_match,
                observed_at=observed_time,
                confidence=confidence,
            )

    if _attribute_is_fillable(listing, "has_den_or_solarium"):
        den_match = _DEN_OR_SOLARIUM_RE.search(full_text)
        if den_match is not None:
            updates["attributes.has_den_or_solarium"] = _observed(
                True,
                evidence=den_match.group(0).strip(),
                observed_at=observed_time,
                confidence=confidence,
            )

    return updates


def basement_unit_evidence(text: str) -> str | None:
    if not text:
        return None

    for strong_match in _BASEMENT_STRONG_RE.finditer(text):
        if not _basement_match_is_excluded(text, strong_match):
            return strong_match.group(0).strip()

    for basement_match in _BASEMENT_RE.finditer(text):
        if _basement_match_is_excluded(text, basement_match):
            continue
        return basement_match.group(0).strip()
    return None


def _basement_match_is_excluded(text: str, candidate: re.Match[str]) -> bool:
    for pattern in (_BASEMENT_NON_UNIT_RE, _BASEMENT_NEGATED_RE):
        for exclusion in pattern.finditer(text):
            if _spans_overlap(candidate.span(), exclusion.span()):
                return True
    return False


def furnishing_from_text(text: str) -> tuple[str, str] | None:
    """Classify furnishing only after explicit negative and flexible wording."""

    unfurnished_match = (
        _FURNISHED_NO_RE.search(text)
        or _UNFURNISHED_OPTION_RE.search(text)
        or _UNFURNISHED_RE.search(text)
    )
    if unfurnished_match is not None:
        return "Unfurnished", unfurnished_match.group(0).strip()

    furnished_match = _FURNISHED_YES_RE.search(text) or _FURNISHED_RE.search(text)
    if furnished_match is not None:
        return "Furnished", furnished_match.group(0).strip()
    return None


def availability_date_from_text(text: str) -> tuple[str, str] | None:
    """Return only a valid ISO date explicitly labelled as listing availability."""

    match = _AVAILABLE_ISO_RE.search(text)
    if match is None:
        return None
    try:
        available_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None
    return available_date, match.group(0).strip()


def ambiguous_availability_from_text(text: str) -> str | None:
    """Preserve labelled non-ISO availability wording without inferring a year."""

    match = _AVAILABLE_TEXT_RE.search(text)
    return match.group(0).strip() if match is not None else None


def lease_months_from_text(text: str) -> tuple[int, str] | None:
    """Normalize an explicitly stated month or one-year lease term."""

    match = _LEASE_MONTHS_RE.search(text)
    if match is None:
        return None
    months = 12 if match.group("one_year") else int(match.group("months"))
    if not 1 <= months <= 60:
        return None
    return months, match.group(0).strip()


def utilities_included_from_text(text: str) -> tuple[list[str], str] | None:
    """Return the utilities explicitly said to be included, preserving their scope."""

    all_match = _ALL_UTILITIES_INCLUDED_RE.search(text)
    if all_match is not None:
        return ["all"], all_match.group(0).strip()

    for pattern in _SPECIFIC_UTILITIES_INCLUDED_RES:
        match = pattern.search(text)
        if match is None:
            continue
        utilities: dict[str, None] = {}
        for item_match in _UTILITY_ITEM_RE.finditer(match.group("items")):
            normalized = re.sub(r"[-\s]+", " ", item_match.group(0).lower()).strip()
            canonical = {
                "electric": "electricity",
                "wi fi": "internet",
                "wifi": "internet",
            }.get(normalized, normalized)
            utilities.setdefault(canonical, None)
        if utilities:
            return list(utilities), match.group(0).strip()

    unspecified_match = _UNSPECIFIED_UTILITIES_INCLUDED_RE.search(text)
    if unspecified_match is not None:
        return ["unspecified"], unspecified_match.group(0).strip()
    return None


def _spans_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def has_ambiguous_bedroom_range(text: str) -> bool:
    return bool(_BEDS_RANGE_RE.search(text))


def has_ambiguous_area_range(text: str) -> bool:
    return bool(_SQFT_RANGE_RE.search(text))


def floor_from_text(text: str) -> tuple[int, str] | None:
    """Return only a floor number explicitly tied to a floor label."""

    match = _FLOOR_RE.search(text)
    if match is None:
        return None
    raw_value = match.group(1) or match.group(2) or match.group(3)
    if raw_value is None:
        return None
    normalized = raw_value.lower()
    floor_value = _ORDINAL_FLOORS.get(normalized)
    if floor_value is None:
        floor_value = int(raw_value)
    if not 1 <= floor_value <= 80:
        return None
    return floor_value, match.group(0).strip()


def parking_from_text(text: str) -> tuple[str, str] | None:
    """Classify parking only when text states availability or inclusion explicitly."""

    if not text:
        return None
    unavailable_match = _PARKING_UNAVAILABLE_RE.search(text)
    if unavailable_match is not None:
        return "Unavailable", unavailable_match.group(0).strip()

    included_match = _first_unnegated_match(_PARKING_INCLUDED_RE, text)
    if included_match is not None:
        return "Included", included_match.group(0).strip()

    available_match = _PARKING_AVAILABLE_RE.search(text)
    if available_match is not None:
        return "Available", available_match.group(0).strip()
    return None


def in_suite_laundry_evidence(text: str) -> str | None:
    """Return evidence only for laundry explicitly scoped to the advertised unit."""

    if not text or _NEGATED_IN_SUITE_LAUNDRY_RE.search(text):
        return None
    laundry_match = _IN_SUITE_LAUNDRY_RE.search(text)
    if laundry_match is None:
        return None
    return laundry_match.group(0).strip()


def building_laundry_evidence(text: str) -> str | None:
    """Return evidence only for explicitly shared or building laundry."""

    if not text or _NEGATED_BUILDING_LAUNDRY_RE.search(text):
        return None
    laundry_match = _BUILDING_LAUNDRY_RE.search(text)
    if laundry_match is None:
        return None
    return laundry_match.group(0).strip()


def _first_unnegated_match(pattern: re.Pattern[str], text: str) -> re.Match[str] | None:
    for match in pattern.finditer(text):
        prefix = text[max(0, match.start() - 12) : match.start()]
        if re.search(r"\b(?:no|not)\s+$", prefix, re.IGNORECASE):
            continue
        return match
    return None


def neighborhood_haystack(*parts: str | None) -> str:
    normalized_parts: list[str] = []
    for part in parts:
        if part is None:
            continue
        normalized = part.strip()
        if normalized:
            normalized_parts.append(normalized)
    return " ".join(normalized_parts)


def match_area_key_from_neighborhood_text(text: str, context: object) -> tuple[str, str] | None:
    return _area_key_from_text(text, context)


def infer_area_key_from_neighborhood_text(text: str, context: object) -> str | None:
    matched = match_area_key_from_neighborhood_text(text, context)
    if matched is None:
        return None
    area_key, _evidence = matched
    return area_key


def _extract_text_parts(listing: Listing) -> dict[str, str]:
    parts: dict[str, str] = {}
    for key in _TEXT_PART_KEYS + _NB_TEXT_PART_KEYS:
        text_value = _text_attribute_value(listing, key)
        if text_value is not None:
            parts[key] = text_value
    if listing.place.raw_address:
        parts.setdefault("address", listing.place.raw_address)
    return parts


def _text_attribute_value(listing: Listing, key: str) -> str | None:
    value = listing.attributes.get(key)
    if isinstance(value, Observed) and isinstance(value.value, str):
        normalized = value.value.strip()
        return normalized if normalized else None
    return None


def _is_missing(field: Field[Any]) -> bool:
    return field == Absence.NOT_STATED


def _attribute_is_fillable(listing: Listing, key: str) -> bool:
    value = listing.attributes.get(key)
    if value is None:
        return True
    return value == Absence.NOT_STATED


def _context_currency(context: object) -> str:
    from_mapping = _mapping_lookup(context, "currency")
    if isinstance(from_mapping, str) and from_mapping.strip():
        return from_mapping.strip().upper()

    locale_currency = _nested_mapping_lookup(context, "locale", "currency")
    if isinstance(locale_currency, str) and locale_currency.strip():
        return locale_currency.strip().upper()

    citypack = getattr(context, "citypack", None)
    locale = getattr(citypack, "locale", None)
    currency = getattr(locale, "currency", None)
    if isinstance(currency, str) and currency.strip():
        return currency.strip().upper()

    return "CAD"


def _context_area_unit(context: object) -> str:
    from_mapping = _mapping_lookup(context, "area_unit")
    if isinstance(from_mapping, str) and from_mapping.strip():
        return from_mapping.strip()

    locale_area_unit = _nested_mapping_lookup(context, "locale", "area_unit")
    if isinstance(locale_area_unit, str) and locale_area_unit.strip():
        return locale_area_unit.strip()

    citypack = getattr(context, "citypack", None)
    locale = getattr(citypack, "locale", None)
    area_unit = getattr(locale, "area_unit", None)
    if isinstance(area_unit, str) and area_unit.strip():
        return area_unit.strip()

    return "sqft"


def _area_key_from_text(text: str, context: object) -> tuple[str, str] | None:
    if not text:
        return None
    area_keywords = _context_area_keywords(context)
    if not area_keywords:
        return None

    lowered_text = text.lower()
    for area_key in sorted(area_keywords):
        for keyword in area_keywords[area_key]:
            keyword_normalized = keyword.strip().lower()
            if not keyword_normalized:
                continue
            if _is_broad_city_keyword(keyword_normalized, context):
                continue
            if re.search(rf"\b{re.escape(keyword_normalized)}\b", lowered_text):
                return area_key, keyword.strip()
    return None


def _context_area_keywords(context: object) -> dict[str, tuple[str, ...]]:
    from_mapping = _mapping_lookup(context, "area_keywords")
    mapping_keywords = _normalize_area_keywords(from_mapping)
    if mapping_keywords:
        return mapping_keywords

    citypack = getattr(context, "citypack", None)
    areas = getattr(citypack, "areas", None)
    if isinstance(areas, list):
        keywords: dict[str, tuple[str, ...]] = {}
        for area in areas:
            key = getattr(area, "key", None)
            area_keywords = getattr(area, "keywords", None)
            if isinstance(key, str):
                normalized = _normalize_keyword_sequence(area_keywords)
                if normalized:
                    keywords[key] = normalized
        if keywords:
            return keywords
    return {}


def _normalize_area_keywords(value: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, tuple[str, ...]] = {}
    for key, raw_keywords in value.items():
        if not isinstance(key, str):
            continue
        keywords = _normalize_keyword_sequence(raw_keywords)
        if keywords:
            normalized[key] = keywords
    return normalized


def _normalize_keyword_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    keywords: list[str] = []
    for raw_keyword in value:
        if isinstance(raw_keyword, str):
            normalized = raw_keyword.strip()
            if normalized:
                keywords.append(normalized)
    return tuple(keywords)


def _is_broad_city_keyword(keyword: str, context: object) -> bool:
    city_name = _context_city_name(context)
    return bool(city_name and keyword == city_name.lower())


def _context_city_name(context: object) -> str | None:
    from_mapping = _mapping_lookup(context, "city_name")
    if isinstance(from_mapping, str) and from_mapping.strip():
        return from_mapping.strip()

    citypack = getattr(context, "citypack", None)
    citypack_name = getattr(citypack, "name", None)
    if isinstance(citypack_name, str) and citypack_name.strip():
        return citypack_name.strip()
    return None


def _mapping_lookup(context: object, key: str) -> object:
    if isinstance(context, dict):
        return context.get(key)
    return None


def _nested_mapping_lookup(context: object, *path: str) -> object:
    if not isinstance(context, dict):
        return None
    current: object = context
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _pet_policy(text: str) -> tuple[str, str] | None:
    if not text:
        return None
    no_match = _PET_NO_RE.search(text)
    if no_match is not None:
        return "no_pets", no_match.group(0).strip()
    friendly_match = _PET_FRIENDLY_RE.search(text)
    if friendly_match is not None:
        return "friendly", friendly_match.group(0).strip()
    conditional_match = _PET_CONDITIONAL_RE.search(text)
    if conditional_match is not None:
        return "conditional", conditional_match.group(0).strip()
    return None


def _density_signal(text: str) -> tuple[str, str] | None:
    if not text:
        return None
    walkable_match = _WALKABLE_PHRASE_RE.search(text)
    sparse_match = _SPARSE_PHRASE_RE.search(text)
    if walkable_match is not None and sparse_match is None:
        return "walkable", walkable_match.group(0).strip()
    if sparse_match is not None and walkable_match is None:
        return "sparse", sparse_match.group(0).strip()
    if walkable_match is not None and sparse_match is not None:
        return "walkable", walkable_match.group(0).strip()
    return None


def _observed(
    value: Any,
    *,
    evidence: str,
    observed_at: datetime,
    confidence: float,
) -> Observed[Any]:
    return Observed[Any](
        value=value,
        origin=Origin.TEXT_RULE,
        confidence=confidence,
        evidence=evidence,
        observed_at=observed_at,
    )
