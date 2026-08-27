from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from nostos.model import Listing

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


@rule("laundry.in_suite", category="amenities", label="In-suite laundry")
def _stub_laundry_in_suite(_: Listing, __: RuleContext) -> Signal:
    return Signal(fired=False, magnitude=0.0, confidence=1.0, evidence=None)


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
