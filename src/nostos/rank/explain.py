from __future__ import annotations

from nostos.config.profile import ScaledWeight, WeightValue
from nostos.rank.engine import RuleContribution, ScoreResult


def render_score_explanation(result: ScoreResult) -> str:
    lines: list[str] = [
        f"Overall score: {result.score:.1f}/100",
        (
            "This score is relative to your enabled preferences "
            f"({len(result.contributions)} total)."
        ),
        (
            "For this profile, the contribution range is "
            f"{result.normalization.min_possible:+.1f} to {result.normalization.max_possible:+.1f}."
        ),
        "",
    ]

    helped = sorted(
        (item for item in result.contributions if item.contribution > 0),
        key=lambda item: item.contribution,
        reverse=True,
    )
    hurt = sorted(
        (item for item in result.contributions if item.contribution < 0),
        key=lambda item: item.contribution,
    )
    neutral = sorted(
        (item for item in result.contributions if item.contribution == 0),
        key=lambda item: item.label,
    )

    if helped:
        lines.append("What helped:")
        lines.extend(_render_section(helped, direction="helped"))
        lines.append("")

    if hurt:
        lines.append("What hurt:")
        lines.extend(_render_section(hurt, direction="hurt"))
        lines.append("")

    if neutral:
        lines.append("No visible effect:")
        lines.extend(_render_neutral(neutral))

    return "\n".join(lines).strip()


def _render_section(items: list[RuleContribution], *, direction: str) -> list[str]:
    rendered: list[str] = []
    for item in items:
        weight_text = _describe_weight(item.weight)
        confidence_text = _confidence_label(item.confidence_factor)
        line = (
            f"- {item.label} {direction} this listing "
            f"({item.contribution:+.1f} points, {confidence_text} confidence, {weight_text})."
        )
        rendered.append(line)
        if item.signal is not None and item.signal.evidence:
            rendered.append(f'  Evidence: "{item.signal.evidence}"')
    return rendered


def _render_neutral(items: list[RuleContribution]) -> list[str]:
    rendered: list[str] = []
    for item in items:
        reason = "the rule did not fire" if item.signal is None or not item.signal.fired else "signal was weak"
        rendered.append(f"- {item.label} had no effect ({reason}).")
    return rendered


def _describe_weight(weight: WeightValue) -> str:
    if isinstance(weight, ScaledWeight):
        if weight.per_100_sqft is not None:
            return f"{weight.per_100_sqft:+.2f} per 100 sqft, capped at {weight.cap:+.1f}"
        assert weight.per_100 is not None
        return f"{weight.per_100:+.2f} per 100 units, capped at {weight.cap:+.1f}"
    return f"weight {weight:+.2f}"


def _confidence_label(value: float) -> str:
    if value >= 0.85:
        return "high"
    if value >= 0.6:
        return "medium"
    return "low"
