"""Address normalization and matching helpers.

Ported from apartment-hunt's address heuristics with one key change:
city/province/postal token vocabularies are caller-provided so each
citypack can inject local geography.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

DEFAULT_DIRECTIONAL_ABBREVIATIONS: Mapping[str, str] = MappingProxyType(
    {
        "w": "west",
        "e": "east",
        "n": "north",
        "s": "south",
    }
)

DEFAULT_STREET_TYPE_TOKENS = frozenset(
    {
        "street",
        "st",
        "st.",
        "avenue",
        "ave",
        "ave.",
        "road",
        "rd",
        "rd.",
        "boulevard",
        "blvd",
        "blvd.",
        "drive",
        "dr",
        "dr.",
        "lane",
        "ln",
        "crescent",
        "cres",
        "court",
        "ct",
        "place",
        "pl",
        "highway",
        "hwy",
        "terrace",
        "ter",
        "way",
    }
)


@dataclass(frozen=True, slots=True)
class AddressNormalizationTokens:
    """Token lists used by the address normalizer.

    `city_province_postal_tokens` and `city_directional_prefixes` are
    citypack-owned data and must be injected by the caller.
    """

    city_province_postal_tokens: frozenset[str]
    city_directional_prefixes: frozenset[str]
    street_type_tokens: frozenset[str] = DEFAULT_STREET_TYPE_TOKENS
    directional_abbreviations: Mapping[str, str] = field(
        default_factory=lambda: DEFAULT_DIRECTIONAL_ABBREVIATIONS
    )


DEFAULT_ADDRESS_TOKENS = AddressNormalizationTokens(
    city_province_postal_tokens=frozenset(),
    city_directional_prefixes=frozenset(),
)


def normalize_address(
    street: str | None,
    city: str | None = None,
    province: str | None = None,
    postal: str | None = None,
) -> str:
    """Build a canonical address string from structured parts."""
    parts: list[str] = []
    for part in (street, city, province, postal):
        if not part:
            continue
        cleaned = re.sub(r"\s+", " ", str(part).strip())
        cleaned = cleaned.strip(",.;:")
        if not cleaned or cleaned.lower() in {"unknown", "n/a", "na", "-"}:
            continue
        parts.append(cleaned)
    return ", ".join(parts) if parts else ""


def _normalize_tokens(tokens: list[str], directional_abbreviations: Mapping[str, str]) -> list[str]:
    """Expand single-letter directionals (e.g. ``W`` -> ``west``)."""
    out: list[str] = []
    for tok in tokens:
        out.append(directional_abbreviations.get(tok, tok))
    return out


def _strip_context_tokens(
    tokens: list[str],
    *,
    city_province_postal_tokens: frozenset[str],
    city_directional_prefixes: frozenset[str],
    all_directionals: frozenset[str],
) -> list[str]:
    """Remove city / province / postal tokens from a token list."""
    dir_count = sum(1 for token in tokens if token in all_directionals)
    has_earlier_dir = dir_count >= 2
    deferred: tuple[int, str] | None = None
    tail_stripped: list[tuple[int, str]] = []
    j = len(tokens) - 1

    while j >= 0:
        tok = tokens[j]

        if (
            deferred is not None
            and deferred[1] in city_province_postal_tokens
            and tok in city_directional_prefixes
            and has_earlier_dir
        ):
            deferred = None
            j -= 1
            continue

        if (
            deferred is not None
            and deferred[1] in city_directional_prefixes
            and tok in city_province_postal_tokens
            and has_earlier_dir
        ):
            deferred = None
            j -= 1
            continue

        if deferred is not None and deferred[1] in city_province_postal_tokens:
            deferred = None
        elif deferred is not None:
            tail_stripped.append(deferred)
            deferred = None

        if tok in city_province_postal_tokens:
            deferred = (j, tok)
            j -= 1
            continue

        if (
            tok in city_directional_prefixes
            and j + 1 < len(tokens)
            and tokens[j + 1] in city_province_postal_tokens
        ):
            deferred = (j, tok)
            j -= 1
            continue

        if re.fullmatch(r"\d{5}(?:-\d{4})?", tok):
            j -= 1
            continue
        if re.fullmatch(r"[a-z]\d[a-z]", tok):
            j -= 1
            continue
        if re.fullmatch(r"\d[a-z]\d", tok):
            j -= 1
            continue
        if re.fullmatch(r"[a-z]\d[a-z]\d[a-z]\d", tok):
            j -= 1
            continue

        tail_stripped.append((j, tok))
        j -= 1

    if deferred is not None and tail_stripped:
        tail_stripped.append(deferred)
    if not tail_stripped:
        return []

    min_tail_idx = min(idx for idx, _ in tail_stripped)
    head = tokens[:min_tail_idx]
    kept_tail = [token for _, token in sorted(tail_stripped)]
    return head + kept_tail


def match_key(
    address: str | None,
    *,
    tokens: AddressNormalizationTokens = DEFAULT_ADDRESS_TOKENS,
) -> str:
    """Lowercase + trim + drop trailing street-type token for forgiving match."""
    if not address:
        return ""

    lowered = address.lower()
    lowered = re.sub(r"[.,;:()'\"/#&]", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    if not lowered:
        return ""

    normalized_tokens = _normalize_tokens(lowered.split(), tokens.directional_abbreviations)
    if normalized_tokens and normalized_tokens[-1] in tokens.street_type_tokens:
        normalized_tokens = normalized_tokens[:-1]
    return " ".join(normalized_tokens)


def sig_token(
    address: str | None,
    *,
    tokens: AddressNormalizationTokens = DEFAULT_ADDRESS_TOKENS,
) -> str:
    """Street-signature token for cross-source dedupe."""
    if not address:
        return ""

    lowered = address.lower()
    lowered = re.sub(r"[.,;:()'\"/#&]", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    if not lowered:
        return ""

    kept_tokens: list[str] = []
    for token in _normalize_tokens(lowered.split(), tokens.directional_abbreviations):
        if token in tokens.street_type_tokens:
            continue
        kept_tokens.append(token)

    all_directionals = frozenset(
        {
            "north",
            "south",
            "east",
            "west",
            *tokens.directional_abbreviations.keys(),
            *tokens.directional_abbreviations.values(),
        }
    )
    stripped = _strip_context_tokens(
        kept_tokens,
        city_province_postal_tokens=tokens.city_province_postal_tokens,
        city_directional_prefixes=tokens.city_directional_prefixes,
        all_directionals=all_directionals,
    )
    return " ".join(stripped)


def addresses_match(
    a: str | None,
    b: str | None,
    min_confidence: float = 0.85,
    *,
    tokens: AddressNormalizationTokens = DEFAULT_ADDRESS_TOKENS,
) -> bool:
    """Two-pass match: exact match on `match_key()` OR containment."""
    _ = min_confidence  # reserved for a future scoring-based matcher.
    if not a or not b:
        return False

    ma = match_key(a, tokens=tokens)
    mb = match_key(b, tokens=tokens)
    if not ma or not mb:
        return False
    if ma == mb:
        return True

    if len(ma) >= len(mb):
        return mb in ma
    return ma in mb


def address_from_postingbody(
    posting_body: str | None, *, tokens: AddressNormalizationTokens = DEFAULT_ADDRESS_TOKENS
) -> str:
    """Extract a plausible street address from a free-text posting body."""
    if not posting_body:
        return ""

    explicit_match = re.search(r"(?im)^\s*address\s*:\s*(.+?)\s*$", posting_body)
    if explicit_match:
        candidate = explicit_match.group(1).strip()
        if _is_plausible_labeled_address(candidate):
            return candidate

    street_type_pattern = "|".join(re.escape(token) for token in sorted(tokens.street_type_tokens))
    implicit_re = re.compile(
        rf"^\s*\d[\w\s#\-]*\b(?:{street_type_pattern})\b.*$",
        flags=re.IGNORECASE,
    )
    for line in posting_body.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        if implicit_re.match(candidate):
            return candidate
    return ""


def _is_plausible_labeled_address(candidate: str) -> bool:
    lowered = candidate.lower().strip()
    if len(lowered) < 6:
        return False
    banned_values = {"contact", "please contact", "tbd", "n/a", "na", "unknown"}
    if lowered in banned_values:
        return False
    if any(phrase in lowered for phrase in ("please contact", "contact for details", "call for")):
        return False
    return bool(re.search(r"\d", lowered))


__all__ = [
    "AddressNormalizationTokens",
    "DEFAULT_ADDRESS_TOKENS",
    "addresses_match",
    "address_from_postingbody",
    "match_key",
    "normalize_address",
    "sig_token",
]
