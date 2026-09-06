"""Address-centred research helpers for the listing research workspace."""

from __future__ import annotations

import math
import os
import re
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlparse

import httpx

_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_RADIUS_METRES = 1500
RESEARCH_RELEVANCE_VERSION = "address-v2"
_SEARCH_RESULT_LIMIT = 5
_MAX_ACCEPTED_RESULTS = 10
_SUPPORTED_RESEARCH_CITIES = frozenset({"toronto", "vancouver"})
_EXTERNAL_WRAPPER_RE = re.compile(
    r"<<<(?:END_)?EXTERNAL_UNTRUSTED_CONTENT[^>]*>>>|Source: Web Search\s*---",
    re.IGNORECASE,
)

_STREET_TYPE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"boulevard|blvd\.?", "Boulevard"),
    (r"avenue|ave\.?", "Avenue"),
    (r"street|st\.?", "Street"),
    (r"road|rd\.?", "Road"),
    (r"drive|dr\.?", "Drive"),
    (r"lane|ln\.?", "Lane"),
    (r"court|ct\.?", "Court"),
    (r"crescent|cres\.?", "Crescent"),
    (r"place|pl\.?", "Place"),
    (r"terrace|ter\.?", "Terrace"),
    (r"highway|hwy\.?", "Highway"),
    (r"parkway|pkwy\.?", "Parkway"),
    (r"trail|trl\.?", "Trail"),
    (r"way", "Way"),
)
_STREET_TYPES = {
    spelling.rstrip("."): canonical
    for pattern, canonical in _STREET_TYPE_PATTERNS
    for spelling in pattern.replace("\\.?", "").split("|")
}
_DIRECTIONS = {
    "n": "North",
    "north": "North",
    "s": "South",
    "south": "South",
    "e": "East",
    "east": "East",
    "w": "West",
    "west": "West",
}
_STREET_TYPE_PATTERN = "|".join(pattern for pattern, _ in _STREET_TYPE_PATTERNS)
_DIRECTION_PATTERN = "|".join(_DIRECTIONS)
_RESEARCH_ADDRESS_RE = re.compile(
    rf"(?<!\d)(?P<number>\d{{1,6}})\s+"
    rf"(?:(?P<prefix>{_DIRECTION_PATTERN})\.?\s+)?"
    rf"(?P<name>[A-Za-z0-9][A-Za-z0-9 .'-]{{0,70}}?)\s+"
    rf"(?P<street_type>{_STREET_TYPE_PATTERN})"
    rf"(?:\s+(?P<suffix>{_DIRECTION_PATTERN})\.?)?(?=$|[^A-Za-z])",
    re.IGNORECASE,
)


def _canonical_city(city: str) -> str | None:
    cleaned = " ".join(str(city or "").strip(" ,.;:").split())
    if not cleaned or len(cleaned) > 80:
        return None
    if not re.search(r"[A-Za-z]", cleaned):
        return None
    return cleaned.title()


def _canonical_address_match(match: re.Match[str]) -> str:
    street_type = _STREET_TYPES[match.group("street_type").casefold().rstrip(".")]
    prefix = _DIRECTIONS.get((match.group("prefix") or "").casefold().rstrip("."))
    suffix = _DIRECTIONS.get((match.group("suffix") or "").casefold().rstrip("."))
    name = " ".join(match.group("name").split()).strip(" ,.;:-").title()
    name = re.sub(
        r"\b(\d+)(St|Nd|Rd|Th)\b",
        lambda ordinal: ordinal.group(1) + ordinal.group(2).casefold(),
        name,
    )
    parts = [match.group("number"), *(item for item in (prefix, name) if item), street_type]
    if suffix:
        parts.append(suffix)
    return " ".join(parts)


def normalize_research_subject(subject: str, city: str) -> str | None:
    """Return the first precise canonical street address, without unit or locality text."""

    normalized_city = _canonical_city(city)
    if normalized_city is None:
        return None
    value = " ".join(str(subject or "").split())[:500]
    for number_pair in re.finditer(
        r"\b(?P<first>\d{1,6})\s*(?P<separator>[-–—/&]|\b(?:and|to)\b)\s*"
        r"(?P<second>\d{1,6})\b",
        value,
        re.IGNORECASE,
    ):
        first_number = int(number_pair.group("first"))
        second_number = int(number_pair.group("second"))
        separator = number_pair.group("separator")
        is_probable_unit = separator in {"-", "–", "—"} and first_number > second_number
        if not is_probable_unit:
            return None
    match = _RESEARCH_ADDRESS_RE.search(value)
    if match is None:
        return None
    address_context = f"{value[: match.start()]} {value[match.end() :]}"
    expected_city = normalized_city.casefold()
    if any(
        supported_city != expected_city and _contains_city(address_context, supported_city)
        for supported_city in _SUPPORTED_RESEARCH_CITIES
    ):
        return None
    return _canonical_address_match(match)


def research_cache_key(subject: str, city: str) -> str:
    """Return the relevance-versioned identity for one address research report."""

    normalized_subject = normalize_research_subject(subject, city)
    normalized_city = _canonical_city(city)
    if normalized_subject is None or normalized_city is None:
        raise ValueError("A precise street address and city are required for web research.")
    return (
        f"{RESEARCH_RELEVANCE_VERSION}|{normalized_city.casefold()}|"
        f"{normalized_subject.casefold()}"
    )


def _distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    lat1_rad, lat2_rad = math.radians(lat1), math.radians(lat2)
    dlat = lat2_rad - lat1_rad
    dlng = math.radians(lng2 - lng1)
    value = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlng / 2) ** 2
    )
    return 6371 * 2 * math.asin(min(1, math.sqrt(value)))


def _category(tags: dict[str, Any]) -> str | None:
    leisure = str(tags.get("leisure", ""))
    shop = str(tags.get("shop", ""))
    amenity = str(tags.get("amenity", ""))
    public_transport = str(tags.get("public_transport", ""))
    railway = str(tags.get("railway", ""))
    if leisure == "fitness_centre" or amenity == "gym":
        return "gyms"
    if shop in {"supermarket", "greengrocer"}:
        return "groceries"
    if leisure == "park":
        return "parks"
    if shop in {"mall", "department_store"}:
        return "shopping"
    if amenity in {"pharmacy", "clinic", "hospital", "doctors"}:
        return "services"
    if public_transport in {"station", "stop_position", "platform"} or railway in {
        "station",
        "subway_entrance",
        "tram_stop",
    }:
        return "transit"
    return None


@lru_cache(maxsize=256)
def nearby_places(lat: float, lng: float) -> dict[str, Any]:
    """Return named OpenStreetMap places within a walkable radius.

    Results are cached for the lifetime of the web process so revisiting a
    listing does not repeatedly call the public Overpass service.
    """

    query = f"""
    [out:json][timeout:12];
    (
      nwr(around:{_RADIUS_METRES},{lat},{lng})[leisure~"^(fitness_centre|park)$"];
      nwr(around:{_RADIUS_METRES},{lat},{lng})[shop~"^(supermarket|greengrocer|mall|department_store)$"];
      nwr(around:{_RADIUS_METRES},{lat},{lng})[amenity~"^(gym|pharmacy|clinic|hospital|doctors)$"];
      nwr(around:{_RADIUS_METRES},{lat},{lng})[public_transport~"^(station|stop_position|platform)$"];
      nwr(around:{_RADIUS_METRES},{lat},{lng})[railway~"^(station|subway_entrance|tram_stop)$"];
    );
    out center tags;
    """
    response = httpx.post(
        _OVERPASS_URL,
        content=query,
        timeout=15,
        headers={"User-Agent": "Nostos/0.4 personal rental research"},
    )
    response.raise_for_status()
    payload = response.json()
    groups: dict[str, list[dict[str, Any]]] = {
        key: [] for key in ("gyms", "groceries", "parks", "shopping", "services", "transit")
    }
    seen: set[tuple[str, str]] = set()
    for element in payload.get("elements", []):
        if not isinstance(element, dict):
            continue
        element_data = cast(dict[str, Any], element)
        tags = element_data.get("tags")
        if not isinstance(tags, dict):
            continue
        category = _category(tags)
        name = str(tags.get("name") or tags.get("brand") or tags.get("operator") or "").strip()
        raw_center = element_data.get("center")
        center = cast(dict[str, Any], raw_center) if isinstance(raw_center, dict) else element_data
        try:
            place_lat, place_lng = float(center["lat"]), float(center["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if category is None or not name or (category, name.casefold()) in seen:
            continue
        seen.add((category, name.casefold()))
        element_type = str(element_data.get("type", "node"))
        element_id = element_data.get("id")
        groups[category].append(
            {
                "name": name,
                "distance_km": round(_distance_km(lat, lng, place_lat, place_lng), 2),
                "url": f"https://www.openstreetmap.org/{element_type}/{element_id}",
            }
        )
    for places in groups.values():
        places.sort(key=lambda item: float(item["distance_km"]))
        del places[5:]
    return {
        "status": "ok",
        "radius_m": _RADIUS_METRES,
        "source": "OpenStreetMap contributors",
        "checked_at": datetime.now(UTC).isoformat(),
        "groups": groups,
    }


def _clean_external_text(value: object, *, limit: int) -> str:
    text = _EXTERNAL_WRAPPER_RE.sub(" ", str(value or ""))
    return " ".join(text.split())[:limit]


class ResearchProvider(Protocol):
    """Host-independent boundary for structured web search."""

    name: str

    def search(
        self,
        query: str,
        *,
        limit: int,
        date_after: str,
        date_before: str,
    ) -> list[dict[str, Any]]: ...


class PerplexityResearchProvider:
    """Standalone adapter for Perplexity's structured Search API."""

    name = "perplexity"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def search(
        self,
        query: str,
        *,
        limit: int,
        date_after: str,
        date_before: str,
    ) -> list[dict[str, Any]]:
        after = datetime.fromisoformat(date_after).strftime("%-m/%-d/%Y")
        before = datetime.fromisoformat(date_before).strftime("%-m/%-d/%Y")
        response = httpx.post(
            "https://api.perplexity.ai/search",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/1digitalD/nostos",
                "X-Title": "Nostos address research",
            },
            json={
                "query": query,
                "max_results": limit,
                "search_after_date_filter": after,
                "search_before_date_filter": before,
                "max_tokens_per_page": 1024,
            },
            timeout=30,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("The research provider returned an invalid response.") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise RuntimeError("The research provider returned an invalid response.")
        return cast(list[dict[str, Any]], payload["results"])


def _research_env_value(name: str) -> str | None:
    configured = os.environ.get(name, "").strip()
    if configured:
        return configured
    env_path = Path(
        os.environ.get(
            "NOSTOS_RESEARCH_ENV_FILE",
            str(Path.home() / ".config" / "nostos" / "research.env"),
        )
    ).expanduser()
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip() == name:
            return value.strip().strip('"\'') or None
    return None


def configured_research_provider() -> ResearchProvider:
    provider_name = _research_env_value("NOSTOS_RESEARCH_PROVIDER") or "perplexity"
    if provider_name.casefold() != "perplexity":
        raise RuntimeError(f"Unsupported research provider: {provider_name}")
    api_key = _research_env_value("NOSTOS_PERPLEXITY_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Research is not configured. Set NOSTOS_PERPLEXITY_API_KEY or add it to "
            "~/.config/nostos/research.env."
        )
    return PerplexityResearchProvider(api_key)


def _topic_for(title: str, excerpt: str) -> str:
    text = f"{title} {excerpt}".casefold()
    if any(word in text for word in ("police", "fire", "incident", "security", "crime")):
        return "Safety"
    if any(word in text for word in ("management", "concierge", "elevator", "maintenance")):
        return "Building management"
    if any(word in text for word in ("gym", "grocery", "transit", "park", "shopping")):
        return "Services and access"
    if any(word in text for word in ("neighbourhood", "neighborhood", "district", "area")):
        return "Area and locality"
    return "Address and building"


def _published_date(value: object) -> date:
    raw = str(value or "").strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return datetime.strptime(raw, "%m/%d/%Y").date()


def _address_identity(match: re.Match[str]) -> tuple[str, str, str, str]:
    street_type = _STREET_TYPES[match.group("street_type").casefold().rstrip(".")]
    direction = match.group("suffix") or match.group("prefix") or ""
    canonical_direction = _DIRECTIONS.get(direction.casefold().rstrip("."), "")
    name = " ".join(match.group("name").casefold().split()).strip(" ,.;:-")
    return match.group("number"), name, street_type.casefold(), canonical_direction.casefold()


def _contains_city(value: str, city: str) -> bool:
    value_tokens = re.findall(r"[a-z0-9]+", value.casefold())
    city_tokens = re.findall(r"[a-z0-9]+", city.casefold())
    if not city_tokens:
        return False
    width = len(city_tokens)
    return any(
        value_tokens[index : index + width] == city_tokens
        for index in range(len(value_tokens))
    )


def _match_reason(title: str, excerpt: str, *, subject: str, city: str) -> str | None:
    subject_match = _RESEARCH_ADDRESS_RE.search(subject)
    if subject_match is None:
        return None
    expected = _address_identity(subject_match)
    field_values = (("title", title), ("excerpt", excerpt))
    identities_by_field = {
        label: [_address_identity(match) for match in _RESEARCH_ADDRESS_RE.finditer(value)]
        for label, value in field_values
    }
    if any(
        identity != expected
        for identities in identities_by_field.values()
        for identity in identities
    ):
        return None
    fields = [
        label
        for label, value in (("title", title), ("excerpt", excerpt))
        if expected in identities_by_field[label] and _contains_city(value, city)
    ]
    if not fields:
        return None
    return f"Normalized address and city appear together in the source {' and '.join(fields)}."


def _search_error(label: str, exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        detail = "the provider timed out"
    else:
        detail = "the provider request failed"
    return f"{label} search failed: {detail}."


def compile_web_research(
    subject: str,
    city: str,
    *,
    provider: ResearchProvider | None = None,
) -> dict[str, Any]:
    """Compile bounded, exact-address source excerpts through a pluggable provider."""

    now = datetime.now(UTC)
    cutoff = now.date() - timedelta(days=730)
    normalized_subject = normalize_research_subject(subject, city)
    normalized_city = _canonical_city(city)
    if normalized_subject is None or normalized_city is None:
        raise ValueError("A precise street address and city are required for web research.")
    active_provider = provider or configured_research_provider()
    searches = (
        ("Address", f'"{normalized_subject}" "{normalized_city}"'),
        (
            "Management and reviews",
            f'"{normalized_subject}" "{normalized_city}" property management building reviews',
        ),
        (
            "Safety",
            f'"{normalized_subject}" "{normalized_city}" safety incidents fire police',
        ),
    )
    raw_batches: list[list[dict[str, Any]]] = []
    partial_errors: list[str] = []
    for label, query in searches:
        try:
            batch = active_provider.search(
                query,
                limit=_SEARCH_RESULT_LIMIT,
                date_after=cutoff.isoformat(),
                date_before=now.date().isoformat(),
            )
            if not isinstance(batch, list):
                raise RuntimeError("the provider returned an invalid result list")
            raw_batches.append(batch[:_SEARCH_RESULT_LIMIT])
        except Exception as exc:
            partial_errors.append(_search_error(label, exc))
    if not raw_batches:
        raise RuntimeError("The research provider failed for all focused searches.")

    results: list[dict[str, str]] = []
    filtered_stale = 0
    filtered_irrelevant = 0
    seen_urls: set[str] = set()
    processed_urls: set[str] = set()
    for raw in (item for batch in raw_batches for item in batch):
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "").strip()
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            continue
        if url in processed_urls:
            continue
        processed_urls.add(url)
        published_raw = str(raw.get("date") or raw.get("published") or "").strip()
        try:
            published = _published_date(published_raw)
        except ValueError:
            filtered_stale += 1
            continue
        if published < cutoff or published > now.date():
            filtered_stale += 1
            continue
        title = _clean_external_text(raw.get("title"), limit=240)
        excerpt = _clean_external_text(raw.get("snippet") or raw.get("description"), limit=700)
        if not url or not title or url in seen_urls:
            continue
        match_reason = _match_reason(
            title,
            excerpt,
            subject=normalized_subject,
            city=normalized_city,
        )
        if match_reason is None:
            filtered_irrelevant += 1
            continue
        seen_urls.add(url)
        results.append(
            {
                "topic": _topic_for(title, excerpt),
                "title": title,
                "url": url,
                "source": _clean_external_text(raw.get("siteName"), limit=120)
                or parsed_url.hostname
                or "Web",
                "published_at": published.isoformat(),
                "excerpt": excerpt or "No excerpt was provided by the search source.",
                "match_reason": match_reason,
            }
        )
        if len(results) >= _MAX_ACCEPTED_RESULTS:
            break
    return {
        "provider": active_provider.name,
        "fetched_at": now.isoformat(),
        "filtered_stale_count": filtered_stale,
        "filtered_irrelevant_count": filtered_irrelevant,
        "partial_errors": partial_errors,
        "subject": normalized_subject,
        "results": results,
    }
