from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from nostos.enrich.base import CostModel
from nostos.model import Absence, Area, Field, Listing, Money, Observed, Origin

_PRICE_RE = re.compile(
    r"\$\s*((?:[1-9]\d{0,2}(?:,\d{3})+)|(?:[1-9]\d{2,3}))\s*"
    r"(?:/\s*(?:mo(?:nth)?|month)|monthly|per month)?",
    re.IGNORECASE,
)
_BEDS_RE = re.compile(r"\b(\d+(?:\.5)?)\s*(?:bed(?:room)?s?|br|bd)\b", re.IGNORECASE)
_BATHS_RE = re.compile(r"\b(\d+(?:\.5)?)\s*(?:bath(?:room)?s?|ba)\b", re.IGNORECASE)
_SQFT_RE = re.compile(
    r"\b([7-9]\d{2}|[1-9]\d{3})\s*(?:sq\.?\s*ft\.?|sqft|square\s+feet)\b",
    re.IGNORECASE,
)
_FLOOR_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+floor\b|\bfloor\s+(\d{1,2})\b",
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
_BASEMENT_EXCLUSIONS_RE = re.compile(
    r"\bbasement\s+(?:storage|locker|parking|stall|parkade)\b",
    re.IGNORECASE,
)

_UNFURNISHED_OPTION_RE = re.compile(
    r"\b(?:can|may|could)\s+be\s+unfurnished\b|\bunfurnished\s+(?:option|available)\b",
    re.IGNORECASE,
)
_UNFURNISHED_RE = re.compile(r"\bunfurnished\b", re.IGNORECASE)
_FURNISHED_RE = re.compile(r"\bfurnished(?:\s+only)?\b", re.IGNORECASE)

_NO_PARKING_RE = re.compile(
    r"\b(?:no|without)\s+(?:on[- ]site\s+)?(?:parking|garage|stall)\b",
    re.IGNORECASE,
)
_PARKING_RE = re.compile(
    r"\b(?:parking|garage|stall)\s+(?:is\s+)?(?:included|available)\b|"
    r"\b(?:includes?|comes?\s+with)\s+(?:an?\s+|one\s+)?(?:parking|garage|stall)\b|"
    r"\b(?:one|1)\s+(?:underground\s+|secured\s+)?parking\s+stall\b",
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
    r"private\s+laundry|own\s+laundry|washer[\s/\\]+dryer\s+in\s+"
    r"(?:the|a|an)?\s*(?:suite|unit|apartment|home)|"
    r"washer[\s\-/]+dryer\s+included|washer\s+and\s+dryer|"
    r"washer\s*&\s*dryer|washer\s*/\s*dryer)\b",
    re.IGNORECASE,
)
_BUILDING_LAUNDRY_RE = re.compile(
    r"\b(coin[\s-]?(?:op(?:erated)?|[\s-]?laundry)|shared\s+laundry|"
    r"common[\s-]area\s+laundry|common\s+laundry|building\s+laundry|"
    r"on[\s-]site\s+laundry|onsite\s+laundry|laundry\s+in\s+"
    r"(?:the|a|an|this|my)\s+building|laundry\s+in\s+building|"
    r"central\s+laundry|communal\s+laundry|public\s+laundry)\b",
    re.IGNORECASE,
)
_NEGATED_BUILDING_LAUNDRY_RE = re.compile(
    r"\bno\s+(?:shared|coin|common|building|on[\s-]site|public)\s+laundry"
    r"|\bprivate\s+laundry\b"
    r"|\bno\s+(?:shared|common)\s+washer",
    re.IGNORECASE,
)
_DEN_OR_SOLARIUM_RE = re.compile(r"\b(den|solarium)\b", re.IGNORECASE)

_TEXT_PART_KEYS = ("title", "description", "address", "notes", "listingText", "listing_text")
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

    basement_evidence = basement_unit_evidence(full_text)
    if basement_evidence is not None and _attribute_is_fillable(listing, "basement"):
        updates["attributes.basement"] = _observed(
            True,
            evidence=basement_evidence,
            observed_at=observed_time,
            confidence=confidence,
        )

    if _is_missing(listing.rent):
        match = _PRICE_RE.search(full_text)
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

    if _is_missing(listing.beds):
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

    if _is_missing(listing.area):
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
        match = _FLOOR_RE.search(full_text)
        if match is not None:
            floor_raw = match.group(1) or match.group(2)
            updates["floor"] = _observed(
                int(floor_raw),
                evidence=match.group(0).strip(),
                observed_at=observed_time,
                confidence=confidence,
            )

    if _is_missing(listing.furnishing):
        match = _UNFURNISHED_OPTION_RE.search(full_text) or _UNFURNISHED_RE.search(full_text)
        if match is not None:
            updates["furnishing"] = _observed(
                "Unfurnished",
                evidence=match.group(0).strip(),
                observed_at=observed_time,
                confidence=confidence,
            )
        else:
            match = _FURNISHED_RE.search(full_text)
            if match is not None:
                updates["furnishing"] = _observed(
                    "Furnished",
                    evidence=match.group(0).strip(),
                    observed_at=observed_time,
                    confidence=confidence,
                )

    if _is_missing(listing.parking):
        no_parking_match = _NO_PARKING_RE.search(full_text)
        if no_parking_match is not None:
            updates["parking"] = _observed(
                "Unavailable",
                evidence=no_parking_match.group(0).strip(),
                observed_at=observed_time,
                confidence=confidence,
            )
        else:
            parking_match = _PARKING_RE.search(full_text)
            if parking_match is not None:
                updates["parking"] = _observed(
                    "Available",
                    evidence=parking_match.group(0).strip(),
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
        nb_text = " ".join(
            text_parts[key] for key in _NB_TEXT_PART_KEYS if key in text_parts
        ).strip()
        area_match = _area_key_from_text(nb_text, context)
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
        laundry_match = _in_suite_laundry(full_text)
        if laundry_match is not None:
            updates["attributes.in_suite_laundry"] = _observed(
                True,
                evidence=laundry_match,
                observed_at=observed_time,
                confidence=confidence,
            )

    if _attribute_is_fillable(listing, "building_laundry"):
        laundry_match = _building_laundry(full_text)
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

    strong_match = _BASEMENT_STRONG_RE.search(text)
    if strong_match is not None:
        return strong_match.group(0).strip()

    for basement_match in _BASEMENT_RE.finditer(text):
        trailing_text = text[basement_match.start() : basement_match.end() + 24]
        if _BASEMENT_EXCLUSIONS_RE.search(trailing_text):
            continue
        return basement_match.group(0).strip()
    return None


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
    return isinstance(field, Absence)


def _attribute_is_fillable(listing: Listing, key: str) -> bool:
    value = listing.attributes.get(key)
    if value is None:
        return True
    return isinstance(value, Absence)


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


def _in_suite_laundry(text: str) -> str | None:
    if not text:
        return None
    if _NEGATED_BUILDING_LAUNDRY_RE.search(text):
        return "no shared laundry"
    laundry_match = _IN_SUITE_LAUNDRY_RE.search(text)
    if laundry_match is None:
        return None
    return laundry_match.group(0).strip()


def _building_laundry(text: str) -> str | None:
    if not text:
        return None
    if _NEGATED_BUILDING_LAUNDRY_RE.search(text):
        return None
    laundry_match = _BUILDING_LAUNDRY_RE.search(text)
    if laundry_match is None:
        return None
    return laundry_match.group(0).strip()


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
