from __future__ import annotations

import httpx
import pytest

from nostos.sources.base import Capabilities
from nostos.sources.http import (
    RobotsDisallowedError,
    SourceHttpClient,
    TokenBucket,
    build_fetch_text,
)

ROBOTS_BODY = "\n".join(
    [
        "User-agent: *",
        "Disallow: /blocked/",
        "Allow: /allowed/",
        "",
    ]
)


def _response(url: str, *, status_code: int = 200, text: str = "") -> httpx.Response:
    request = httpx.Request("GET", url)
    return httpx.Response(status_code, text=text, request=request)


def test_robots_disallowed_url_is_not_fetched() -> None:
    fetched_urls: list[str] = []

    def http_get(url: str, **kwargs: object) -> httpx.Response:
        fetched_urls.append(url)
        if url.endswith("/robots.txt"):
            return _response(url, text=ROBOTS_BODY)
        return _response(url, text="page-body")

    client = SourceHttpClient(
        user_agent="nostos-test",
        rate_limit_per_minute=None,
        http_get=http_get,
    )

    with pytest.raises(RobotsDisallowedError, match="robots.txt disallows"):
        client.fetch_text("https://example.com/blocked/listing")

    assert "https://example.com/blocked/listing" not in fetched_urls
    assert fetched_urls == ["https://example.com/robots.txt"]

    assert client.fetch_text("https://example.com/allowed/listing") == "page-body"
    assert "https://example.com/allowed/listing" in fetched_urls


def test_robots_txt_is_cached_per_host_per_run() -> None:
    robots_fetches = 0

    def http_get(url: str, **kwargs: object) -> httpx.Response:
        nonlocal robots_fetches
        if url.endswith("/robots.txt"):
            robots_fetches += 1
            return _response(url, text=ROBOTS_BODY)
        return _response(url, text="page-body")

    client = SourceHttpClient(
        user_agent="nostos-test",
        rate_limit_per_minute=None,
        http_get=http_get,
    )

    with pytest.raises(RobotsDisallowedError):
        client.fetch_text("https://example.com/blocked/one")
    with pytest.raises(RobotsDisallowedError):
        client.fetch_text("https://example.com/blocked/two")

    assert robots_fetches == 1


def test_token_bucket_never_exceeds_declared_rate_with_fake_clock() -> None:
    current = [0.0]
    sleeps: list[float] = []

    def clock() -> float:
        return current[0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        current[0] += seconds

    bucket = TokenBucket(rate_per_minute=6.0, clock=clock, sleep=sleep)

    for _ in range(6):
        bucket.acquire()
    assert sleeps == []

    bucket.acquire()
    assert pytest.approx(sum(sleeps)) == 10.0

    request_times: list[float] = []
    for _ in range(5):
        bucket.acquire()
        request_times.append(current[0])

    assert request_times == sorted(request_times)
    max_requests_per_minute = 6.0
    for index, request_time in enumerate(request_times):
        window_start = max(0.0, request_time - 60.0)
        allowed = max_requests_per_minute * ((request_time - window_start) / 60.0)
        requests_in_window = 1 + sum(
            1 for earlier in request_times[:index] if earlier >= window_start
        )
        assert requests_in_window <= allowed + 1e-9


def test_build_fetch_text_uses_capabilities_rate_limit() -> None:
    current = [0.0]
    sleeps: list[float] = []

    def clock() -> float:
        return current[0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        current[0] += seconds

    def http_get(url: str, **kwargs: object) -> httpx.Response:
        if url.endswith("/robots.txt"):
            return _response(url, text="User-agent: *\n")
        return _response(url, text="ok")

    fetch_text = build_fetch_text(
        Capabilities(rate_limit_per_minute=6.0),
        user_agent="nostos-test",
        clock=clock,
        sleep=sleep,
        http_get=http_get,
        fetch_robots_text=lambda _url: "User-agent: *\n",
    )

    for _ in range(6):
        assert fetch_text("https://example.com/page") == "ok"
    assert sleeps == []

    assert fetch_text("https://example.com/page-seven") == "ok"
    assert pytest.approx(sum(sleeps)) == 10.0


def test_robots_disallowed_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")

    def http_get(url: str, **kwargs: object) -> httpx.Response:
        if url.endswith("/robots.txt"):
            return _response(url, text=ROBOTS_BODY)
        return _response(url, text="page-body")

    client = SourceHttpClient(
        user_agent="nostos-test",
        rate_limit_per_minute=None,
        http_get=http_get,
    )

    with pytest.raises(RobotsDisallowedError):
        client.fetch_text("https://example.com/blocked/listing")

    assert any(
        "Skipping fetch disallowed by robots.txt" in record.message
        for record in caplog.records
    )


def test_kijiji_skips_robots_disallowed_search_url() -> None:
    from nostos.config.citypack import Citypack
    from nostos.config.profile import Profile
    from nostos.context import SearchContext
    from nostos.sources.http import RobotsDisallowedError
    from nostos.sources.kijiji import KijijiSource

    calls: list[str] = []

    def fetcher(url: str) -> str:
        calls.append(url)
        if "/blocked/" in url:
            raise RobotsDisallowedError(url, "test disallow")
        return "<html></html>"

    source = KijijiSource(fetcher=fetcher)
    context = SearchContext(
        citypack=Citypack.model_validate(
            {
                "name": "vancouver",
                "locale": {
                    "language": "en-CA",
                    "timezone": "America/Vancouver",
                    "currency": "CAD",
                    "area_unit": "sqft",
                },
                "areas": [
                    {
                        "key": "kits_beach",
                        "label": "Kits",
                        "keywords": ["kits"],
                        "bbox": [49.262, -123.190, 49.278, -123.145],
                    }
                ],
                "sources": {
                    "kijiji": {
                        "enabled": True,
                        "load_bearing": False,
                        "regions": [{"path": "blocked/vancouver", "id": "c37l1700287"}],
                    }
                },
                "address": {
                    "directional": {"w": "west"},
                    "strip_tokens": ["vancouver"],
                    "region_tokens": ["bc"],
                },
            }
        ),
        profile=Profile.model_validate(
            {"city": "vancouver", "weights": {}, "schedule": "0 */6 * * *"}
        ),
    )

    records = list(source.discover(context))

    assert records == []
    assert calls == [
        "https://www.kijiji.ca/b-apartments-condos/blocked/vancouver/apartments/k0c37l1700287"
    ]
