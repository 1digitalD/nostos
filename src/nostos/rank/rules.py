from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from nostos.model import Listing, Observed

RuleContext = object
ShapeFn = Callable[[float], float]


@dataclass(frozen=True, slots=True)
class Signal:
    fired: bool
    magnitude: float
    confidence: float
    evidence: str | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.magnitude):
            raise ValueError("Signal magnitude must be finite")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Signal confidence must be between 0 and 1")


RuleDetector = Callable[[Listing, RuleContext], Signal | None]
DetectorT = TypeVar("DetectorT", bound=RuleDetector)


@dataclass(frozen=True, slots=True)
class Rule:
    key: str
    category: str
    label: str
    detector: RuleDetector
    shape: ShapeFn

    def shaped_magnitude(self, magnitude: float) -> float:
        shaped = self.shape(magnitude)
        if not math.isfinite(shaped):
            raise ValueError(f"Rule {self.key!r} shape produced a non-finite value")
        return shaped


class RuleRegistry:
    def __init__(self) -> None:
        self._rules: dict[str, Rule] = {}

    def register(self, entry: Rule) -> None:
        if entry.key in self._rules:
            raise ValueError(f"Rule {entry.key!r} is already registered")
        self._rules[entry.key] = entry

    def rule(
        self,
        key: str,
        *,
        category: str,
        label: str | None = None,
        shape: ShapeFn | None = None,
    ) -> Callable[[DetectorT], DetectorT]:
        display = label or _humanize_rule_key(key)
        shape_fn = shape or _identity_shape

        def decorator(detector: DetectorT) -> DetectorT:
            self.register(
                Rule(
                    key=key,
                    category=category,
                    label=display,
                    detector=detector,
                    shape=shape_fn,
                )
            )
            return detector

        return decorator

    def get(self, key: str) -> Rule | None:
        return self._rules.get(key)

    def all(self) -> tuple[Rule, ...]:
        return tuple(self._rules.values())


def _identity_shape(magnitude: float) -> float:
    return magnitude


def _humanize_rule_key(key: str) -> str:
    title = key.replace(".", " ").replace("_", " ")
    return " ".join(part.capitalize() for part in title.split())


DEFAULT_REGISTRY = RuleRegistry()


def rule(
    key: str,
    *,
    category: str,
    label: str | None = None,
    shape: ShapeFn | None = None,
) -> Callable[[DetectorT], DetectorT]:
    return DEFAULT_REGISTRY.rule(key, category=category, label=label, shape=shape)


_TEXT_PART_KEYS = (
    "title",
    "description",
    "address",
    "notes",
    "listingText",
    "listing_text",
    "structuredLocation",
    "structured_location",
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

_ORDINAL_WORDS = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
    "fifth": 5,
    "5th": 5,
    "sixth": 6,
    "6th": 6,
    "seventh": 7,
    "7th": 7,
    "eighth": 8,
    "8th": 8,
    "ninth": 9,
    "9th": 9,
    "tenth": 10,
    "10th": 10,
    "eleventh": 11,
    "11th": 11,
    "twelfth": 12,
    "12th": 12,
}
_FLOOR_EXPLICIT_RE = re.compile(
    r"\b(?:floor|level|storey|story)\s*(\d{1,2})\b"
    r"|\b(\d{1,2})(?:st|nd|rd|th)\s+(?:floor|level|storey|story)\b"
    r"|\b("
    + "|".join(re.escape(word) for word in _ORDINAL_WORDS)
    + r")\s+(?:floor|level|storey|story)\b",
    re.IGNORECASE,
)
_UNIT_NUMBER_RE = re.compile(
    r"(?:#\s*(\d{1,4})\b)"
    r"|\b(?:unit|apt|suite|ph|penthouse)\s*#?\s*(\d{1,4})\b"
    r"|\b(?:unit|apt|suite)\s+(?:no\.?|#)?\s*(\d{3,4})\s*[-–]\s*\d{2,5}",
    re.IGNORECASE,
)


def _text_attribute_value(listing: Listing, key: str) -> str | None:
    value = listing.attributes.get(key)
    if isinstance(value, Observed) and isinstance(value.value, str):
        normalized = value.value.strip()
        if normalized:
            return normalized
    return None


def _combined_text(listing: Listing) -> str:
    text_parts: list[str] = []
    for key in _TEXT_PART_KEYS:
        part = _text_attribute_value(listing, key)
        if part is not None:
            text_parts.append(part)
    if listing.place.raw_address:
        text_parts.append(listing.place.raw_address)
    return " ".join(text_parts).strip()


def _bool_attribute(
    listing: Listing,
    *keys: str,
) -> tuple[bool, float, str | None] | None:
    for key in keys:
        value = listing.attributes.get(key)
        if isinstance(value, Observed) and isinstance(value.value, bool):
            return value.value, value.confidence, value.evidence
    return None


def _numeric_attribute(
    listing: Listing,
    *keys: str,
) -> tuple[float, float, str | None] | None:
    for key in keys:
        value = listing.attributes.get(key)
        if isinstance(value, Observed):
            raw = value.value
            if isinstance(raw, bool):
                continue
            if isinstance(raw, (int, float)):
                return float(raw), value.confidence, value.evidence
    return None


def _string_attribute(
    listing: Listing,
    *keys: str,
) -> tuple[str, float, str | None] | None:
    for key in keys:
        value = listing.attributes.get(key)
        if isinstance(value, Observed) and isinstance(value.value, str):
            normalized = value.value.strip()
            if normalized:
                return normalized, value.confidence, value.evidence
    return None


def _signal_from_presence(evidence: str, *, confidence: float = 1.0) -> Signal:
    return Signal(
        fired=True,
        magnitude=float(True),
        confidence=confidence,
        evidence=evidence,
    )


def _in_suite_laundry_evidence(text: str) -> str | None:
    if not text:
        return None
    negated_match = _NEGATED_BUILDING_LAUNDRY_RE.search(text)
    if negated_match is not None:
        return negated_match.group(0).strip()
    match = _IN_SUITE_LAUNDRY_RE.search(text)
    if match is None:
        return None
    return match.group(0).strip()


def _building_laundry_evidence(text: str) -> str | None:
    if not text:
        return None
    if _NEGATED_BUILDING_LAUNDRY_RE.search(text):
        return None
    match = _BUILDING_LAUNDRY_RE.search(text)
    if match is None:
        return None
    return match.group(0).strip()


def _floor_from_text(text: str) -> tuple[float, str] | None:
    if not text:
        return None

    explicit_match = _FLOOR_EXPLICIT_RE.search(text)
    if explicit_match is not None:
        for group in explicit_match.groups():
            if group is None:
                continue
            key = group.lower()
            if key in _ORDINAL_WORDS:
                return float(_ORDINAL_WORDS[key]), explicit_match.group(0).strip()
            floor_value = int(group)
            return float(floor_value), explicit_match.group(0).strip()

    unit_match = _UNIT_NUMBER_RE.search(text)
    if unit_match is None:
        return None

    unit_text = unit_match.group(1) or unit_match.group(2) or unit_match.group(3)
    if unit_text is None:
        return None
    unit_number = int(unit_text)
    if not 100 <= unit_number <= 9999:
        return None
    floor_value = unit_number // 100
    if not 1 <= floor_value <= 80:
        return None
    return float(floor_value), unit_match.group(0).strip()


def _walk_score_from_text(text: str) -> tuple[float, str] | None:
    if not text:
        return None
    match = _WALK_SCORE_RE.search(text)
    if match is None:
        return None
    score_value = int(match.group(1))
    if not 0 <= score_value <= 100:
        return None
    return float(score_value), match.group(0).strip()


def _density_phrase(text: str) -> tuple[str, str] | None:
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


@rule("laundry.in_suite", category="amenities", label="In-suite laundry")
def _detect_laundry_in_suite(listing: Listing, _: RuleContext) -> Signal | None:
    attr_value = _bool_attribute(
        listing,
        "attributes.in_suite_laundry",
        "in_suite_laundry",
    )
    if attr_value is not None:
        has_in_suite, confidence, evidence = attr_value
        if has_in_suite:
            return _signal_from_presence(evidence or "in-suite laundry", confidence=confidence)
        return None

    text = _combined_text(listing)
    evidence = _in_suite_laundry_evidence(text)
    if evidence is None:
        return None
    return _signal_from_presence(evidence)


@rule("laundry.building", category="amenities", label="Shared building laundry")
def _detect_laundry_building(listing: Listing, _: RuleContext) -> Signal | None:
    attr_value = _bool_attribute(
        listing,
        "attributes.building_laundry",
        "building_laundry",
    )
    if attr_value is not None:
        has_building_laundry, confidence, evidence = attr_value
        if has_building_laundry:
            return _signal_from_presence(evidence or "shared laundry", confidence=confidence)
        return None

    text = _combined_text(listing)
    evidence = _building_laundry_evidence(text)
    if evidence is None:
        return None
    return _signal_from_presence(evidence)


@rule("floor.low", category="space", label="Lower floor")
def _detect_floor_low(listing: Listing, _: RuleContext) -> Signal | None:
    floor_field = listing.floor
    if isinstance(floor_field, Observed):
        floor_value = floor_field.value
        if isinstance(floor_value, int) and not isinstance(floor_value, bool):
            if floor_value >= 1:
                return Signal(
                    fired=True,
                    magnitude=float(floor_value),
                    confidence=floor_field.confidence,
                    evidence=floor_field.evidence or str(floor_value),
                )
            return None

    text = _combined_text(listing)
    inferred = _floor_from_text(text)
    if inferred is None:
        return None
    magnitude, evidence = inferred
    return Signal(
        fired=True,
        magnitude=magnitude,
        confidence=1.0,
        evidence=evidence,
    )


@rule("walk.score", category="proximity", label="Walk Score")
def _detect_walk_score(listing: Listing, _: RuleContext) -> Signal | None:
    walk_score_attr = _numeric_attribute(
        listing,
        "attributes.walk_score",
        "walk_score",
    )
    if walk_score_attr is not None:
        magnitude, confidence, evidence = walk_score_attr
        if 0 <= magnitude <= 100:
            return Signal(
                fired=True,
                magnitude=magnitude,
                confidence=confidence,
                evidence=evidence or "walk score",
            )

    text = _combined_text(listing)
    parsed = _walk_score_from_text(text)
    if parsed is None:
        return None
    magnitude, evidence = parsed
    return Signal(
        fired=True,
        magnitude=magnitude,
        confidence=1.0,
        evidence=evidence,
    )


@rule("space.den_or_solarium", category="space", label="Den or solarium")
def _detect_den_or_solarium(listing: Listing, _: RuleContext) -> Signal | None:
    attr_value = _bool_attribute(
        listing,
        "attributes.has_den_or_solarium",
        "has_den_or_solarium",
    )
    if attr_value is not None:
        has_den_or_solarium, confidence, evidence = attr_value
        if has_den_or_solarium:
            return _signal_from_presence(evidence or "den", confidence=confidence)
        return None

    text = _combined_text(listing)
    match = _DEN_OR_SOLARIUM_RE.search(text)
    if match is None:
        return None
    return _signal_from_presence(match.group(0).strip())


@rule("density.walkable", category="proximity", label="Walkable phrases")
def _detect_walkable_phrase(listing: Listing, _: RuleContext) -> Signal | None:
    attr_value = _string_attribute(
        listing,
        "attributes.density_signal",
        "density_signal",
    )
    if attr_value is not None:
        density_value, confidence, evidence = attr_value
        if density_value == "walkable":
            return _signal_from_presence(evidence or "walkable", confidence=confidence)
        return None

    text = _combined_text(listing)
    detected = _density_phrase(text)
    if detected is None:
        return None
    density_value, evidence = detected
    if density_value != "walkable":
        return None
    return _signal_from_presence(evidence)


@rule("density.sparse", category="proximity", label="Sparse residential phrases")
def _detect_sparse_phrase(listing: Listing, _: RuleContext) -> Signal | None:
    attr_value = _string_attribute(
        listing,
        "attributes.density_signal",
        "density_signal",
    )
    if attr_value is not None:
        density_value, confidence, evidence = attr_value
        if density_value == "sparse":
            return _signal_from_presence(evidence or "sparse", confidence=confidence)
        return None

    text = _combined_text(listing)
    detected = _density_phrase(text)
    if detected is None:
        return None
    density_value, evidence = detected
    if density_value != "sparse":
        return None
    return _signal_from_presence(evidence)


@rule("parking.available", category="amenities", label="Parking")
def _stub_parking_available(_: Listing, __: RuleContext) -> Signal:
    return Signal(fired=False, magnitude=0.0, confidence=1.0, evidence=None)


@rule("pets.allowed", category="amenities", label="Pet friendly")
def _stub_pets_allowed(_: Listing, __: RuleContext) -> Signal:
    return Signal(fired=False, magnitude=0.0, confidence=1.0, evidence=None)


@rule("area.over_minimum", category="space", label="Space over minimum")
def _stub_area_over_minimum(_: Listing, __: RuleContext) -> Signal:
    return Signal(fired=False, magnitude=0.0, confidence=1.0, evidence=None)


@rule("rent.headroom", category="cost", label="Rent headroom")
def _stub_rent_headroom(_: Listing, __: RuleContext) -> Signal:
    return Signal(fired=False, magnitude=0.0, confidence=1.0, evidence=None)
