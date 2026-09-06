from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from nostos.web import research


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


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        (
            "55 Bremner Boulevard, Unit 2201, 2 beds, 2 baths, parking, Toronto",
            "55 Bremner Boulevard",
        ),
        ("55 BREMNER BLVD #2201, Toronto - craigslist", "55 Bremner Boulevard"),
    ],
)
def test_normalize_research_subject_removes_unit_and_listing_facts(
    subject: str, expected: str
) -> None:
    assert research.normalize_research_subject(subject, "Toronto") == expected


def test_locality_only_subject_is_rejected_before_provider_call() -> None:
    provider = _ResearchProvider([])

    assert research.normalize_research_subject("Downtown Toronto condo", "Toronto") is None
    with pytest.raises(ValueError, match="street address"):
        research.compile_web_research("Downtown Toronto condo", "Toronto", provider=provider)
    assert provider.calls == []


def test_subject_that_names_another_supported_city_is_rejected_before_provider_call() -> None:
    provider = _ResearchProvider([])

    assert research.normalize_research_subject("55 Bremner Blvd, Vancouver", "Toronto") is None
    with pytest.raises(ValueError, match="street address and city"):
        research.compile_web_research("55 Bremner Blvd, Vancouver", "Toronto", provider=provider)
    assert provider.calls == []


@pytest.mark.parametrize("subject", ["55-57 Bremner Blvd", "55 / 57 Bremner Blvd"])
def test_ambiguous_address_numbers_are_rejected(subject: str) -> None:
    assert research.normalize_research_subject(subject, "Toronto") is None


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

    def __init__(
        self,
        results: list[dict[str, Any]],
        *,
        failure_indexes: set[int] | None = None,
        failure_message: str = "search failed",
    ) -> None:
        self.results = results
        self.failure_indexes = failure_indexes or set()
        self.failure_message = failure_message
        self.calls: list[dict[str, object]] = []

    def search(self, query: str, **kwargs: object) -> list[dict[str, Any]]:
        self.calls.append({"query": query, **kwargs})
        if len(self.calls) - 1 in self.failure_indexes:
            raise RuntimeError(self.failure_message)
        return self.results


def _finding(
    url: str,
    *,
    title: str,
    snippet: str = "",
    published: str | None = None,
) -> dict[str, Any]:
    finding: dict[str, Any] = {"title": title, "url": url, "snippet": snippet}
    if published is not None:
        finding["date"] = published
    return finding


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


def test_compile_web_research_runs_three_bounded_targeted_searches_with_dynamic_dates() -> None:
    today = datetime.now(UTC).date()
    provider = _ResearchProvider(
        [
            _finding(
                "https://example.test/current",
                title="55 Bremner Blvd building management review, Toronto",
                published=today.isoformat(),
            )
        ]
    )

    result = research.compile_web_research("55 Bremner Blvd", "Toronto", provider=provider)

    queries = [str(call["query"]) for call in provider.calls]
    assert len(queries) == 3
    assert len(set(queries)) == 3
    normalized = research.normalize_research_subject("55 Bremner Blvd", "Toronto")
    assert normalized is not None
    assert all(f'"{normalized}"' in query for query in queries)
    assert all("Toronto" in query for query in queries)
    assert all("rent" not in query.casefold() for query in queries)
    assert all("bed" not in query.casefold() for query in queries)
    query_text = " ".join(queries).casefold()
    assert "management" in query_text
    assert "review" in query_text
    assert "safety" in query_text
    limits = [call["limit"] for call in provider.calls]
    assert all(isinstance(limit, int) and 0 < limit <= 10 for limit in limits)
    assert all(
        call["date_after"] == (today - timedelta(days=730)).isoformat()
        for call in provider.calls
    )
    assert all(call["date_before"] == today.isoformat() for call in provider.calls)
    assert result["fetched_at"].startswith(today.isoformat())


def test_compile_web_research_accepts_valid_address_evidence_and_title_only_fallback() -> None:
    today = date.today().isoformat()
    provider = _ResearchProvider(
        [
            _finding(
                "https://example.test/management",
                title='<<<EXTERNAL_UNTRUSTED_CONTENT id="x">>>55 Bremner Blvd management, Toronto',
                snippet="Source: Web Search --- Toronto residents report elevator repairs.",
                published=today,
            ),
            _finding(
                "https://example.test/title-only",
                title="55 Bremner Boulevard, Toronto building review",
                published=today,
            ),
            _finding(
                "https://example.test/55-bremner-blvd-toronto",
                title="Unrelated neighbourhood article",
                published=today,
            ),
        ]
    )

    result = research.compile_web_research(
        "55 Bremner Blvd Unit 2201", "Toronto", provider=provider
    )

    assert {item["url"] for item in result["results"]} == {
        "https://example.test/management",
        "https://example.test/title-only",
    }
    assert all(item["match_reason"] for item in result["results"])
    assert result["results"][0]["title"] == "55 Bremner Blvd management, Toronto"
    assert result["filtered_irrelevant_count"] >= 1


def test_compile_web_research_rejects_wrong_number_street_city_and_direction() -> None:
    today = date.today().isoformat()
    provider = _ResearchProvider(
        [
            _finding(
                "https://example.test/wrong-number",
                title="56 King St W building review, Toronto",
                published=today,
            ),
            _finding(
                "https://example.test/wrong-street",
                title="55 Queen St W building review, Toronto",
                published=today,
            ),
            _finding(
                "https://example.test/wrong-city",
                title="55 King St W building review, Vancouver",
                snippet="Toronto rental alternatives.",
                published=today,
            ),
            _finding(
                "https://example.test/wrong-direction",
                title="55 King St E building review, Toronto",
                published=today,
            ),
            _finding(
                "https://example.test/valid",
                title="55 King Street West building review, Toronto",
                published=today,
            ),
        ]
    )

    result = research.compile_web_research("55 King St W", "Toronto", provider=provider)

    assert [item["url"] for item in result["results"]] == ["https://example.test/valid"]
    assert result["filtered_irrelevant_count"] == 4
    rejected_urls = {
        "https://example.test/wrong-number",
        "https://example.test/wrong-street",
        "https://example.test/wrong-city",
        "https://example.test/wrong-direction",
    }
    assert all(item["url"] not in rejected_urls for item in result["results"])


def test_compile_web_research_deduplicates_same_url_across_searches() -> None:
    today = date.today().isoformat()
    provider = _ResearchProvider(
        [
            _finding(
                "https://example.test/same",
                title="55 Bremner Blvd building review, Toronto",
                published=today,
            ),
            _finding(
                "https://example.test/other",
                title="55 Bremner Blvd safety report, Toronto",
                published=today,
            ),
        ]
    )

    result = research.compile_web_research("55 Bremner Blvd", "Toronto", provider=provider)

    assert {item["url"] for item in result["results"]} == {
        "https://example.test/same",
        "https://example.test/other",
    }
    assert len(result["results"]) == 2


def test_compile_web_research_rejects_result_with_multiple_distinct_addresses() -> None:
    today = date.today().isoformat()
    provider = _ResearchProvider(
        [
            _finding(
                "https://example.test/multiple-addresses",
                title="55 Bremner Blvd and 65 Bremner Blvd building review, Toronto",
                published=today,
            )
        ]
    )

    result = research.compile_web_research("55 Bremner Blvd", "Toronto", provider=provider)

    assert result["results"] == []
    assert result["filtered_irrelevant_count"] == 1


def test_compile_web_research_filters_stale_future_and_undated_results() -> None:
    today = date.today()
    provider = _ResearchProvider(
        [
            _finding(
                "https://example.test/current",
                title="55 Bremner Blvd review, Toronto",
                published=today.isoformat(),
            ),
            _finding(
                "https://example.test/old",
                title="55 Bremner Blvd old review, Toronto",
                published=(today - timedelta(days=731)).isoformat(),
            ),
            _finding(
                "https://example.test/future",
                title="55 Bremner Blvd future review, Toronto",
                published=(today + timedelta(days=1)).isoformat(),
            ),
            _finding(
                "https://example.test/undated",
                title="55 Bremner Blvd undated review, Toronto",
            ),
            _finding(
                "https://example.test/invalid-date",
                title="55 Bremner Blvd invalid date review, Toronto",
                published="not-a-date",
            ),
        ]
    )

    result = research.compile_web_research("55 Bremner Blvd", "Toronto", provider=provider)

    assert {item["url"] for item in result["results"]} == {
        "https://example.test/current",
    }
    assert result["filtered_stale_count"] == 4


def test_compile_web_research_reports_partial_provider_failure() -> None:
    today = date.today().isoformat()
    provider = _ResearchProvider(
        [
            _finding(
                "https://example.test/current",
                title="55 Bremner Blvd, Toronto",
                published=today,
            )
        ],
        failure_indexes={1},
        failure_message="reviews unavailable; api_token=SECRET-123",
    )

    result = research.compile_web_research("55 Bremner Blvd", "Toronto", provider=provider)

    assert len(provider.calls) == 3
    assert result["results"]
    assert len(result["partial_errors"]) == 1
    assert "Management and reviews" in str(result["partial_errors"][0])
    assert "SECRET-123" not in str(result["partial_errors"][0])


def test_compile_web_research_reports_all_provider_failures() -> None:
    provider = _ResearchProvider(
        [], failure_indexes={0, 1, 2}, failure_message="provider unavailable"
    )

    with pytest.raises(RuntimeError, match="all focused searches"):
        research.compile_web_research("55 Bremner Blvd", "Toronto", provider=provider)
    assert len(provider.calls) == 3


def test_research_cache_key_uses_normalized_address_and_city() -> None:
    canonical = research.research_cache_key("55 Bremner Boulevard Unit 2201", "Toronto")
    equivalent = research.research_cache_key("55 Bremner Blvd #2201", "toronto")
    other_city = research.research_cache_key("55 Bremner Blvd", "Vancouver")

    assert canonical
    assert canonical == equivalent
    assert canonical != other_city
    assert canonical.startswith(f"{research.RESEARCH_RELEVANCE_VERSION}|")
    assert research.RESEARCH_RELEVANCE_VERSION == "address-v2"


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
