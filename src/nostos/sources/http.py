"""Shared HTTP fetching with robots.txt checks and per-source rate limits."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from protego import Protego

from nostos.sources.base import Capabilities

_logger = logging.getLogger(__name__)

HttpGet = Callable[..., httpx.Response]
FetchRobotsText = Callable[[str], str]
Clock = Callable[[], float]
Sleep = Callable[[float], None]


class RobotsDisallowedError(Exception):
    """Raised when robots.txt disallows fetching a URL."""

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"robots.txt disallows {url}: {reason}")


@dataclass
class TokenBucket:
    """Token bucket limiting requests to ``rate_per_minute`` over time."""

    rate_per_minute: float
    clock: Clock = field(default=time.monotonic)
    sleep: Sleep = field(default=time.sleep)
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)

    def __post_init__(self) -> None:
        if self.rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        self._tokens = self.rate_per_minute
        self._last_refill = self.clock()

    def acquire(self) -> None:
        refill_rate = self.rate_per_minute / 60.0
        while True:
            now = self.clock()
            elapsed = now - self._last_refill
            if elapsed > 0:
                self._tokens = min(
                    self.rate_per_minute,
                    self._tokens + elapsed * refill_rate,
                )
                self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            wait_seconds = (1.0 - self._tokens) / refill_rate
            self.sleep(wait_seconds)


@dataclass
class RobotsCache:
    """Cache robots.txt parsers per host for the lifetime of one run."""

    user_agent: str
    fetch_robots_text: FetchRobotsText
    _parsers: dict[str, Protego | None] = field(default_factory=dict)

    def allowed(self, url: str) -> bool:
        parser = self._parser_for(url)
        if parser is None:
            return True
        return bool(parser.can_fetch(url, self.user_agent))

    def disallow_reason(self, url: str) -> str:
        parser = self._parser_for(url)
        if parser is None:
            return "robots.txt unavailable; allowed by default"
        if parser.can_fetch(url, self.user_agent):
            return "allowed"
        return f"disallowed for user-agent {self.user_agent!r}"

    def _parser_for(self, url: str) -> Protego | None:
        host = _host_key(url)
        if host not in self._parsers:
            robots_url = urljoin(f"{_scheme(url)}://{host}", "/robots.txt")
            try:
                body = self.fetch_robots_text(robots_url)
            except Exception as exc:
                _logger.info(
                    "robots.txt fetch failed for %s: %s; allowing by default",
                    host,
                    exc,
                )
                self._parsers[host] = None
            else:
                self._parsers[host] = Protego.parse(body)
        return self._parsers[host]


@dataclass
class SourceHttpClient:
    """HTTP client enforcing robots.txt and a per-source token bucket."""

    user_agent: str
    rate_limit_per_minute: float | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    clock: Clock = field(default=time.monotonic)
    sleep: Sleep = field(default=time.sleep)
    http_get: HttpGet = field(default=httpx.get)
    fetch_robots_text: FetchRobotsText | None = None
    _robots: RobotsCache = field(init=False)
    _bucket: TokenBucket | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        request_headers = {"User-Agent": self.user_agent, **dict(self.headers)}

        def robots_fetch(robots_url: str) -> str:
            if self.rate_limit_per_minute is not None:
                self._throttle()
            response = self.http_get(
                robots_url,
                headers=request_headers,
                timeout=self.timeout,
                follow_redirects=True,
            )
            response.raise_for_status()
            return response.text

        self._robots = RobotsCache(
            user_agent=self.user_agent,
            fetch_robots_text=self.fetch_robots_text or robots_fetch,
        )
        if self.rate_limit_per_minute is not None:
            self._bucket = TokenBucket(
                rate_per_minute=self.rate_limit_per_minute,
                clock=self.clock,
                sleep=self.sleep,
            )

    def fetch_text(self, url: str) -> str:
        if not self._robots.allowed(url):
            reason = self._robots.disallow_reason(url)
            _logger.info("Skipping fetch disallowed by robots.txt: %s (%s)", url, reason)
            raise RobotsDisallowedError(url, reason)
        self._throttle()
        response = self.http_get(
            url,
            headers={"User-Agent": self.user_agent, **dict(self.headers)},
            timeout=self.timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.text

    def _throttle(self) -> None:
        if self._bucket is not None:
            self._bucket.acquire()


def build_fetch_text(
    capabilities: Capabilities,
    *,
    user_agent: str,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    clock: Clock | None = None,
    sleep: Sleep | None = None,
    http_get: HttpGet | None = None,
    fetch_robots_text: FetchRobotsText | None = None,
) -> Callable[[str], str]:
    """Create a fetch function wired to shared robots and rate-limit behavior."""

    client_kwargs: dict[str, Any] = {
        "user_agent": user_agent,
        "rate_limit_per_minute": capabilities.rate_limit_per_minute,
        "headers": headers or {},
        "timeout": timeout,
    }
    if clock is not None:
        client_kwargs["clock"] = clock
    if sleep is not None:
        client_kwargs["sleep"] = sleep
    if http_get is not None:
        client_kwargs["http_get"] = http_get
    if fetch_robots_text is not None:
        client_kwargs["fetch_robots_text"] = fetch_robots_text

    client = SourceHttpClient(**client_kwargs)
    return client.fetch_text


def _scheme(url: str) -> str:
    parsed = urlparse(url)
    return parsed.scheme or "https"


def _host_key(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.netloc:
        msg = f"URL missing host: {url}"
        raise ValueError(msg)
    return parsed.netloc.lower()
