from __future__ import annotations

from dataclasses import dataclass

from nostos.config.profile import Profile, ScaledWeight, WeightValue
from nostos.model import Listing
from nostos.rank.rules import DEFAULT_REGISTRY, RuleRegistry, Signal


@dataclass(frozen=True, slots=True)
class NormalizationWindow:
    min_possible: float
    max_possible: float

    @property
    def span(self) -> float:
        return self.max_possible - self.min_possible


@dataclass(frozen=True, slots=True)
class RuleContribution:
    rule_key: str
    category: str
    label: str
    weight: WeightValue
    signal: Signal | None
    shaped_magnitude: float
    confidence_factor: float
    min_possible: float
    max_possible: float
    contribution: float


@dataclass(frozen=True, slots=True)
class ScoreResult:
    score: float
    total_contribution: float
    normalization: NormalizationWindow
    contributions: tuple[RuleContribution, ...]


class RankEngine:
    def __init__(self, profile: Profile, *, registry: RuleRegistry | None = None) -> None:
        self._profile = profile
        self._registry = registry or DEFAULT_REGISTRY

    def score_listing(self, listing: Listing, *, context: object | None = None) -> ScoreResult:
        contributions: list[RuleContribution] = []
        min_possible = 0.0
        max_possible = 0.0
        total = 0.0

        for key, weight in self._enabled_weight_items():
            registered_rule = self._registry.get(key)
            if registered_rule is None:
                raise ValueError(f"No rule registered for enabled weight {key!r}")

            signal = registered_rule.detector(listing, context)
            min_for_rule, max_for_rule = _possible_range(weight)
            shaped = (
                0.0
                if signal is None
                else registered_rule.shaped_magnitude(signal.magnitude)
            )
            contribution, shaped_magnitude, confidence_factor = _compute_contribution(
                weight=weight,
                signal=signal,
                shaped_magnitude=shaped,
            )

            min_possible += min_for_rule
            max_possible += max_for_rule
            total += contribution
            contributions.append(
                RuleContribution(
                    rule_key=registered_rule.key,
                    category=registered_rule.category,
                    label=registered_rule.label,
                    weight=weight,
                    signal=signal,
                    shaped_magnitude=shaped_magnitude,
                    confidence_factor=confidence_factor,
                    min_possible=min_for_rule,
                    max_possible=max_for_rule,
                    contribution=contribution,
                )
            )

        normalization = NormalizationWindow(min_possible=min_possible, max_possible=max_possible)
        score = _normalize(total=total, window=normalization)

        return ScoreResult(
            score=score,
            total_contribution=total,
            normalization=normalization,
            contributions=tuple(contributions),
        )

    def _enabled_weight_items(self) -> tuple[tuple[str, WeightValue], ...]:
        enabled: list[tuple[str, WeightValue]] = []
        for key, weight in self._profile.weights.items():
            if _is_enabled(weight):
                enabled.append((key, weight))
        return tuple(enabled)


def _compute_contribution(
    *,
    weight: WeightValue,
    signal: Signal | None,
    shaped_magnitude: float,
) -> tuple[float, float, float]:
    if signal is None or not signal.fired:
        return 0.0, 0.0, 0.0

    confidence_factor = _clamp(signal.confidence, minimum=0.0, maximum=1.0)
    if isinstance(weight, ScaledWeight):
        rate = _scaled_rate(weight)
        cap = abs(weight.cap)
        uncapped = (max(shaped_magnitude, 0.0) / 100.0) * rate
        bounded = _clamp(uncapped, minimum=-cap, maximum=cap)
        return bounded * confidence_factor, shaped_magnitude, confidence_factor

    intensity = _clamp(shaped_magnitude, minimum=0.0, maximum=1.0)
    return weight * intensity * confidence_factor, intensity, confidence_factor


def _possible_range(weight: WeightValue) -> tuple[float, float]:
    if isinstance(weight, ScaledWeight):
        rate = _scaled_rate(weight)
        cap = abs(weight.cap)
        if rate >= 0:
            return 0.0, cap
        return -cap, 0.0
    return min(weight, 0.0), max(weight, 0.0)


def _normalize(*, total: float, window: NormalizationWindow) -> float:
    if window.span == 0:
        return 100.0
    raw = 100.0 * ((total - window.min_possible) / window.span)
    return _clamp(raw, minimum=0.0, maximum=100.0)


def _scaled_rate(weight: ScaledWeight) -> float:
    if weight.per_100_sqft is not None:
        return weight.per_100_sqft
    assert weight.per_100 is not None
    return weight.per_100


def _is_enabled(weight: WeightValue) -> bool:
    if isinstance(weight, ScaledWeight):
        return _scaled_rate(weight) != 0 and weight.cap != 0
    return weight != 0


def _clamp(value: float, *, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
