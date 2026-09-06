from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from nostos.web import research
from nostos.web.app import _research_subject


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "elements": [
                {"type": "node", "id": 1, "lat": 43.65, "lon": -79.39,
                 "tags": {"name": "Neighbourhood Fitness", "leisure": "fitness_centre"}},
                {"type": "node", "id": 2, "lat": 43.651, "lon": -79.391,
                 "tags": {"name": "Fresh Market", "shop": "supermarket"}},
                {"type": "way", "id": 3, "center": {"lat": 43.652, "lon": -79.392},
                 "tags": {"name": "Local Park", "leisure": "park"}},
            ]
        }


def test_research_subject_extracts_address_without_listing_facts() -> None:
    title = "55 BREMNER BLVD,2BEDS,2BATHS,PARKING,DOWNTOWN TORONTO - craigslist"

    assert _research_subject(None, title, "craigslist:unit") == "55 BREMNER BLVD"


def test_nearby_places_groups_and_distances_results(monkeypatch: Any) -> None:
    research.nearby_places.cache_clear()
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: _Response())

    result = research.nearby_places(43.649, -79.389)

    assert result["status"] == "ok"
    groups = result["groups"]
    assert groups["gyms"][0]["name"] == "Neighbourhood Fitness"
    assert groups["groceries"][0]["name"] == "Fresh Market"
    assert groups["parks"][0]["url"] == "https://www.openstreetmap.org/way/3"
    assert groups["gyms"][0]["distance_km"] > 0


class _ResearchProvider:
    name = "test-search"

    def __init__(self) -> None:
        self.request: dict[str, object] = {}

    def search(self, query: str, **kwargs: object) -> list[dict[str, Any]]:
        self.request = {"query": query, **kwargs}
        return [
            {
                "title": "<<<EXTERNAL_UNTRUSTED_CONTENT id=\"x\">>>Current management review",
                "url": "https://example.test/current",
                "snippet": "Source: Web Search --- Elevator maintenance improved.",
                "date": "2026-08-01",
                "siteName": "Local Review",
            },
            {
                "title": "Old building review",
                "url": "https://example.test/old",
                "snippet": "Old information.",
                "date": "2020-01-01",
                "siteName": "Archive",
            },
            {
                "title": "Undated review",
                "url": "https://example.test/undated",
                "snippet": "No date.",
                "siteName": "Unknown",
            },
            {
                "title": "Unsafe result",
                "url": "javascript:alert(1)",
                "snippet": "Should never be rendered.",
                "date": "2026-08-01",
            },
        ]


def test_perplexity_provider_uses_direct_structured_search_api(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    class SearchResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"results": [{"title": "Building", "url": "https://example.test"}]}

    def fake_post(url: str, **kwargs: Any) -> SearchResponse:
        captured.update({"url": url, **kwargs})
        return SearchResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    provider = research.PerplexityResearchProvider("test-key")

    results = provider.search(
        "55 Bremner", limit=10, date_after="2024-09-06", date_before="2026-09-06"
    )

    assert len(results) == 1
    assert captured["url"] == "https://api.perplexity.ai/search"
    assert captured["json"]["search_after_date_filter"] == "9/6/2024"
    assert captured["json"]["search_before_date_filter"] == "9/6/2026"


def test_compile_web_research_keeps_only_recent_dated_results() -> None:
    provider = _ResearchProvider()

    result = research.compile_web_research("55 Bremner Blvd", "toronto", provider=provider)

    assert result["provider"] == "test-search"
    assert result["filtered_stale_count"] == 2
    assert len(result["results"]) == 1
    finding = result["results"][0]
    assert finding["title"] == "Current management review"
    assert finding["topic"] == "Building management"
    assert provider.request["limit"] == 10
    assert provider.request["date_after"] == "2024-09-06"
    assert provider.request["date_before"] == "2026-09-06"
    assert provider.request["query"] == (
        '"55 Bremner Blvd" toronto property management building reviews safety incidents '
        "neighbourhood amenities gym grocery transit parks"
    )


def test_configured_provider_loads_standalone_env_file(
    tmp_path: Path, monkeypatch: Any
) -> None:
    env_path = tmp_path / "research.env"
    env_path.write_text("NOSTOS_PERPLEXITY_API_KEY=test-key\n", encoding="utf-8")
    monkeypatch.delenv("NOSTOS_PERPLEXITY_API_KEY", raising=False)
    monkeypatch.setenv("NOSTOS_RESEARCH_ENV_FILE", str(env_path))

    provider = research.configured_research_provider()

    assert isinstance(provider, research.PerplexityResearchProvider)
    assert provider.api_key == "test-key"
