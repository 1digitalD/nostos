"""Deterministic, citation-preserving synthesis for address research."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

REPORT_SCHEMA_VERSION = "address-report-v1"

_MAX_REPORT_SOURCES = 20
_MAX_CLAIMS_PER_SECTION = 6
_MAX_SENTENCES_PER_SOURCE = 3
_MAX_CLAIM_LENGTH = 360

_SECTION_DEFINITIONS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "building",
        "Building",
        "Evidence about the building, its construction, and shared amenities.",
        (
            "What building details should be confirmed with management or at a viewing?",
            "Which amenities are operating and included in the tenancy?",
        ),
    ),
    (
        "management",
        "Management",
        "Source reports about management, maintenance, staff, and building operations.",
        (
            "Who manages the building now, and how are urgent repairs handled?",
            "Are there current elevator or maintenance disruptions?",
        ),
    ),
    (
        "services",
        "Services",
        "Address-tied reports about transit, health care, shopping, and other services.",
        ("Which nearby services matter enough to verify by route and opening hours?",),
    ),
    (
        "groceries",
        "Groceries",
        "Address-tied reports about grocery options in the building or nearby.",
        ("Which grocery options are reachable by the route you would actually use?",),
    ),
    (
        "gyms",
        "Gyms",
        "Address-tied reports about fitness facilities in the building or nearby.",
        ("Is any building gym currently open, equipped, and included in rent?",),
    ),
    (
        "parks",
        "Parks",
        "Address-tied reports about parks and green space near the building.",
        ("Does the walking route to the reported park feel practical at the needed times?",),
    ),
    (
        "safety",
        "Safety",
        (
            "Dated source reports about incidents or security; absence of reports is not proof "
            "of safety."
        ),
        (
            "What do current official incident sources show for this address and immediate area?",
            "Which entrances, routes, and times should be checked in person?",
        ),
    ),
)

_SECTION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "building": (
        "building",
        "condo",
        "condominium",
        "tower",
        "complex",
        "residence",
        "amenity",
        "amenities",
        "storey",
        "story building",
        "built in",
        "constructed",
        "construction",
        "lobby",
        "pool",
        "parking",
        "laundry",
        "heating",
        "air conditioning",
    ),
    "management": (
        "management",
        "manager",
        "landlord",
        "concierge",
        "superintendent",
        "staff",
        "maintenance",
        "repair",
        "repairs",
        "elevator",
        "elevators",
    ),
    "services": (
        "transit",
        "station",
        "subway",
        "streetcar",
        "bus stop",
        "pharmacy",
        "clinic",
        "hospital",
        "shopping",
        "restaurant",
        "school",
    ),
    "groceries": ("grocery", "groceries", "supermarket", "greengrocer", "food store"),
    "gyms": ("gym", "fitness", "workout", "exercise room"),
    "parks": ("park", "green space", "greenspace", "recreation trail"),
    "safety": (
        "safety",
        "safe",
        "unsafe",
        "crime",
        "police",
        "fire",
        "incident",
        "security",
        "break-in",
        "break in",
        "theft",
        "assault",
        "emergency",
    ),
}

_OTHER_UNIT_RE = re.compile(
    r"\b(?:unit|suite|apt\.?|apartment)\b\s*[#-]?\s*(?=[a-z0-9-]*\d)[a-z0-9-]+\b|"
    r"(?<!\w)#\s*\d+[a-z]?\b",
    re.IGNORECASE,
)
_UNIT_LISTING_FACT_RE = re.compile(
    r"(?:[$€£]\s?\d|\b\d+(?:\.\d+)?\s*(?:bed(?:room)?s?|bath(?:room)?s?|sq\.?\s*ft|"
    r"square feet)\b|\b\d+(?:st|nd|rd|th) floor\b|\b(?:available|availability)\b)",
    re.IGNORECASE,
)
_NEARBY_RE = re.compile(
    r"\b(?:nearby|neighbou?rhood|district|immediate area|walking distance|"
    r"minutes? (?:away|from)|blocks? (?:away|from)|close to|steps (?:away|from))\b",
    re.IGNORECASE,
)
_BUILDING_SCOPE_RE = re.compile(
    r"\b(?:building|condo(?:minium)?|tower|complex|residence|lobby|on-site|onsite)\b",
    re.IGNORECASE,
)
_INSTRUCTION_RE = re.compile(
    r"(?:ignore (?:all |any )?(?:previous|prior) instructions?|system prompt|"
    r"assistant(?:'s)? instructions?|you must|do not mention|conclude that|"
    r"<\s*script\b|javascript\s*:)",
    re.IGNORECASE,
)
_MARKETING_RE = re.compile(
    r"\b(?:experience elevated living|welcome home|dream home|luxury living|"
    r"unparalleled|exceptional lifestyle|stunning residence|prestigious address|"
    r"perfect place to call home|enjoy the convenience|world-class dining)\b",
    re.IGNORECASE,
)
_LISTING_BOILERPLATE_RE = re.compile(
    r"(?:#{2,}\s*additional details|\bMLS\b|\bunit no\.?\b|\bliving area\b|"
    r"\bproperty type\s*:|\bproperty mgmt co\s*:|\*\*property management:\*\*|"
    r"\bgym concierge party room\b|\bbuilding name\s*:|"
    r"\bapx age\s*:|#{2,}\s*map\b|\*\*address:\*\*|request more information)",
    re.IGNORECASE,
)
_NEIGHBOURHOOD_LIST_RE = re.compile(r"\bnearby neighbou?rhoods\b", re.IGNORECASE)
_INTERSECTION_ONLY_RE = re.compile(r"\b(?:sits|located) near the intersection\b", re.IGNORECASE)
_ADDRESS_TAUTOLOGY_RE = re.compile(
    r"\bis a (?:condo )?property located in the city of\b", re.IGNORECASE
)
_GENERIC_PROVIDER_RE = re.compile(
    r"\bis an independent property management company\b", re.IGNORECASE
)
_PLACEHOLDER_RE = re.compile(r"^no excerpt was provided", re.IGNORECASE)
_BUILDING_UNIT_COUNT_RE = re.compile(r"\b(?P<count>\d[\d,]*)\s+units?\b", re.IGNORECASE)
_STOREY_COUNT_RE = re.compile(r"\b\d+\s+storey\b", re.IGNORECASE)
_BUILDER_RE = re.compile(r"\b(?:built in|built by|constructed in|developed by)\b", re.IGNORECASE)

_THEMES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("elevators", ("elevator",)),
    ("management responsiveness", ("management", "manager", "landlord", "staff")),
    ("maintenance", ("maintenance", "repair")),
    ("security", ("security", "safe", "unsafe", "crime", "break-in", "theft")),
    ("noise", ("noise", "noisy", "quiet")),
    ("gym", ("gym", "fitness")),
    ("groceries", ("grocery", "supermarket", "greengrocer")),
    ("parks", ("park", "green space", "greenspace")),
    ("transit", ("transit", "station", "subway", "streetcar", "bus stop")),
)
_NEGATIVE_RE = re.compile(
    r"\b(?:not reliable|unreliable|unresponsive|broken|out of service|unsafe|"
    r"poor|dirty|failed|failure|complaints?|problems?|issues?|delays?|"
    r"crime|break-in|theft|assault|incident)\b",
    re.IGNORECASE,
)
_POSITIVE_RE = re.compile(
    r"\b(?:responsive|reliable|well[- ]maintained|safe|secure|good|excellent|"
    r"praised|clean|working|works|quiet)\b",
    re.IGNORECASE,
)
_NO_INCIDENT_RE = re.compile(r"\bno (?:reported )?(?:safety )?incidents?\b", re.IGNORECASE)


def _clean_text(value: object, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _parse_date(value: object) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(raw, "%m/%d/%Y").date()
        except ValueError:
            return None


def _sentences(value: str) -> list[str]:
    pieces = re.split(r"(?<=[.!?])\s+|[\n•]+", value)
    sentences: list[str] = []
    for piece in pieces:
        cleaned = piece.strip(" -")[:_MAX_CLAIM_LENGTH]
        if not cleaned:
            continue
        final_word = re.search(r"([A-Za-z]+)[.!?]?\Z", cleaned)
        looks_truncated = (
            not cleaned.endswith((".", "!", "?"))
            and final_word is not None
            and len(final_word.group(1)) <= 2
        )
        if looks_truncated:
            continue
        sentences.append(cleaned)
    return sentences


def _sentence_priority(sentence: str) -> int:
    """Prefer concrete identity and operator statements within each bounded excerpt."""

    score = 0
    if _BUILDING_UNIT_COUNT_RE.search(sentence):
        score += 6
    if _STOREY_COUNT_RE.search(sentence) or _BUILDER_RE.search(sentence):
        score += 5
    if re.search(r"\b(?:property management|property manager|management is by)\b", sentence, re.I):
        score += 5
    if re.search(
        r"\b(?:grocery|supermarket|transit|station|amenit|gym|fitness)\w*\b",
        sentence,
        re.I,
    ):
        score += 3
    if _NEARBY_RE.search(sentence):
        score += 1
    return score


def _contains_keyword(value: str, keyword: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", value) is not None


def _sections_for(sentence: str) -> list[str]:
    lowered = sentence.casefold()
    matches = [
        key
        for key, keywords in _SECTION_KEYWORDS.items()
        if any(_contains_keyword(lowered, keyword) for keyword in keywords)
    ]
    if matches:
        specific = [key for key in matches if key not in {"building", "services"}]
        return specific[:2] or matches[:1]
    return []


def _scope_for(sentence: str, section: str) -> str:
    if _OTHER_UNIT_RE.search(sentence):
        return "other_unit"
    if section == "building" and _BUILDING_SCOPE_RE.search(sentence):
        return "building"
    if _NEARBY_RE.search(sentence):
        return "nearby"
    if _BUILDING_SCOPE_RE.search(sentence):
        return "building"
    if section in {"services", "groceries", "gyms", "parks", "safety"}:
        return "nearby"
    return "building"


def _theme_for(text: str) -> str | None:
    lowered = text.casefold()
    for theme, keywords in _THEMES:
        if any(keyword in lowered for keyword in keywords):
            return theme
    return None


def _polarity(text: str) -> str | None:
    if _NO_INCIDENT_RE.search(text):
        return "positive"
    negative = bool(_NEGATIVE_RE.search(text))
    positive = bool(_POSITIVE_RE.search(text))
    if negative == positive:
        return None
    return "negative" if negative else "positive"


def _gap_for(key: str) -> str:
    if key == "safety":
        return (
            "No accepted, current safety claim was found. This does not establish that the "
            "building or area is safe."
        )
    labels = {
        "building": "the building itself",
        "management": "building management or operations",
        "services": "nearby services",
        "groceries": "grocery options",
        "gyms": "gym or fitness options",
        "parks": "parks or green space",
    }
    return f"No accepted, current source described {labels[key]}."


def build_research_report(
    subject: str,
    city: str,
    results: list[dict[str, Any]],
    *,
    fetched_at: str,
    filtered_stale_count: int = 0,
    filtered_irrelevant_count: int = 0,
    partial_errors: list[str] | None = None,
) -> dict[str, Any]:
    """Build a bounded report from address-gated source excerpts.

    The function never invents a conclusion: every claim is an extractive sentence
    with at least one citation, and conflicting source language stays visible.
    """

    fetched_date = _parse_date(fetched_at)
    if fetched_date is None:
        raise ValueError("A valid fetched_at date is required to build a research report.")
    cutoff = fetched_date - timedelta(days=730)
    source_limit_omissions = max(0, len(results) - _MAX_REPORT_SOURCES)
    observed_date_omissions = 0
    withheld_sentence_count = 0
    aggregate_unit_count_omissions = 0
    citations: list[dict[str, Any]] = []
    claims_by_section: dict[str, list[dict[str, Any]]] = {
        key: [] for key, _title, _description, _questions in _SECTION_DEFINITIONS
    }

    for raw in results[:_MAX_REPORT_SOURCES]:
        if not isinstance(raw, dict):
            continue
        published = _parse_date(raw.get("published_at"))
        if published is None or published < cutoff or published > fetched_date:
            observed_date_omissions += 1
            continue
        url = str(raw.get("url") or "").strip()
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            continue
        title = _clean_text(raw.get("title"), limit=240)
        excerpt = _clean_text(raw.get("excerpt"), limit=700)
        if not title or not excerpt:
            continue
        citation_id = f"C{len(citations) + 1}"
        citation: dict[str, Any] = {
            "id": citation_id,
            "title": title,
            "url": url,
            "source": _clean_text(raw.get("source"), limit=120) or parsed_url.hostname,
            "published_at": published.isoformat(),
            "excerpt": excerpt,
            "match_reason": _clean_text(raw.get("match_reason"), limit=300),
            "claim_ids": [],
        }
        citations.append(citation)
        accepted_from_source = 0
        ranked_sentences = sorted(
            enumerate(_sentences(excerpt)),
            key=lambda item: (-_sentence_priority(item[1]), item[0]),
        )
        for _sentence_index, sentence in ranked_sentences:
            if accepted_from_source >= _MAX_SENTENCES_PER_SOURCE:
                break
            if len(sentence) < 18 or _PLACEHOLDER_RE.search(sentence):
                continue
            if _BUILDING_UNIT_COUNT_RE.search(sentence):
                aggregate_unit_count_omissions += 1
                continue
            if (
                _INSTRUCTION_RE.search(sentence)
                or _MARKETING_RE.search(sentence)
                or _LISTING_BOILERPLATE_RE.search(sentence)
                or _NEIGHBOURHOOD_LIST_RE.search(sentence)
                or _INTERSECTION_ONLY_RE.search(sentence)
                or _ADDRESS_TAUTOLOGY_RE.search(sentence)
                or _GENERIC_PROVIDER_RE.search(sentence)
                or _OTHER_UNIT_RE.search(sentence)
                or _UNIT_LISTING_FACT_RE.search(sentence)
            ):
                withheld_sentence_count += 1
                continue
            section_keys = _sections_for(sentence)
            if not section_keys:
                continue
            added = False
            for key in section_keys:
                section_claims = claims_by_section[key]
                if len(section_claims) >= _MAX_CLAIMS_PER_SECTION:
                    continue
                claim_id = f"CL{sum(len(items) for items in claims_by_section.values()) + 1}"
                claim: dict[str, Any] = {
                    "id": claim_id,
                    "text": sentence,
                    "scope": _scope_for(sentence, key),
                    "status": "reported",
                    "citation_ids": [citation_id],
                    "published_at": published.isoformat(),
                }
                section_claims.append(claim)
                citation["claim_ids"].append(claim_id)
                added = True
            if added:
                accepted_from_source += 1

    conflicts: list[dict[str, Any]] = []
    for key, section_claims in claims_by_section.items():
        themed: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for claim in section_claims:
            theme = _theme_for(str(claim["text"]))
            polarity = _polarity(str(claim["text"]))
            if theme is None or polarity is None:
                continue
            themed.setdefault(theme, {"positive": [], "negative": []})[polarity].append(claim)
        for theme, sides in themed.items():
            positive_citations = {
                citation_id
                for claim in sides["positive"]
                for citation_id in claim["citation_ids"]
            }
            negative_citations = {
                citation_id
                for claim in sides["negative"]
                for citation_id in claim["citation_ids"]
            }
            if not positive_citations or not negative_citations:
                continue
            if positive_citations == negative_citations and len(positive_citations) == 1:
                continue
            conflict_claims = sides["positive"] + sides["negative"]
            for claim in conflict_claims:
                claim["status"] = "conflicting"
            conflicts.append(
                {
                    "section": key,
                    "theme": theme,
                    "claim_ids": [str(claim["id"]) for claim in conflict_claims],
                    "citation_ids": sorted(positive_citations | negative_citations),
                    "summary": (
                        f"Accepted sources make differing claims about {theme}; compare their "
                        "dates and source context."
                    ),
                }
            )

    sections: list[dict[str, Any]] = []
    for key, title, description, questions in _SECTION_DEFINITIONS:
        claims = claims_by_section[key]
        gaps: list[str] = []
        if not claims:
            gaps.append(_gap_for(key))
        elif len({citation_id for claim in claims for citation_id in claim["citation_ids"]}) == 1:
            gaps.append("These claims come from one source and are not independently corroborated.")
        sections.append(
            {
                "key": key,
                "title": title,
                "description": description,
                "claims": claims,
                "gaps": gaps,
                "questions": list(questions),
            }
        )

    uncertainties = [
        (
            "Source claims are quoted from search excerpts and have not been independently "
            "verified against full source pages."
        ),
        (
            "Nearby or neighbourhood claims describe the area around the address and do not "
            "establish a building condition."
        ),
        (
            "Claims about a named unit or suite describe that unit only and do not establish "
            "facts about the target unit."
        ),
    ]
    total_stale = max(0, filtered_stale_count) + observed_date_omissions
    if total_stale:
        uncertainties.append(
            f"{total_stale} undated, future-dated, or older-than-two-year result(s) were "
            "excluded from claims."
        )
    if filtered_irrelevant_count:
        uncertainties.append(
            f"{max(0, filtered_irrelevant_count)} result(s) without matching address-and-city "
            "evidence were excluded."
        )
    if aggregate_unit_count_omissions:
        uncertainties.append(
            f"{aggregate_unit_count_omissions} aggregate unit-count statement(s) were withheld "
            "because listing indexes may count available listings rather than building units."
        )
    if withheld_sentence_count:
        uncertainties.append(
            f"{withheld_sentence_count} source sentence(s) containing listing-specific facts, "
            "marketing, metadata, or instruction-like text were withheld from claims."
        )
    if source_limit_omissions:
        uncertainties.append(
            f"{source_limit_omissions} source(s) exceeded the bounded report input limit and "
            "were omitted."
        )
    if partial_errors:
        uncertainties.append(
            "One or more focused searches failed, so section coverage is incomplete."
        )
    uncertainties.extend(str(conflict["summary"]) for conflict in conflicts)

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "subject": _clean_text(subject, limit=240),
        "city": _clean_text(city, limit=80),
        "generated_at": fetched_at,
        "scope_note": (
            "Building claims apply only to the normalized street address. Nearby claims are "
            "area context. Source statements about a named unit are withheld from claims because "
            "they do not establish facts about the target unit."
        ),
        "sections": sections,
        "citations": citations,
        "uncertainties": uncertainties,
        "conflicts": conflicts,
        "coverage": {
            "accepted_sources": len(citations),
            "dated_sources": len(citations),
            "section_claim_counts": {key: len(claims) for key, claims in claims_by_section.items()},
            "filtered_stale_count": total_stale,
            "filtered_irrelevant_count": max(0, filtered_irrelevant_count),
            "partial_search_failure_count": len(partial_errors or []),
        },
    }
