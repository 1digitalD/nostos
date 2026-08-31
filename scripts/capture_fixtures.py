"""Record live source pages as test fixtures, with contact data stripped.

Fixtures in `tests/fixtures/` must be *recorded* pages. A fixture hand-written to
match a parser cannot detect that parser drifting from the site, which is the one
thing fixtures exist to catch.

This fetches through the project's own `SourceHttpClient`, so robots.txt and the
per-source rate limit apply exactly as they do in a real run — and it reports the
robots.txt verdict for every URL, which is also how you find out whether a source
can be scraped at all.

Contact data is redacted before anything is written. Scraped landlord names, phone
numbers and email addresses must never enter this repository. Redaction is
mechanical and therefore incomplete: read every file before you commit it.

Usage:
    uv run python scripts/capture_fixtures.py robots https://vancouver.craigslist.org
    uv run python scripts/capture_fixtures.py craigslist \
        --base-url https://vancouver.craigslist.org --area van --out tests/fixtures/craigslist
    uv run python scripts/capture_fixtures.py url https://example.com/page --out /tmp/page.html
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nostos.sources.base import Capabilities  # noqa: E402
from nostos.sources.craigslist import CL_USER_AGENT  # noqa: E402
from nostos.sources.http import (  # noqa: E402
    RobotsDisallowedError,
    SourceHttpClient,
    build_fetch_text,
)

# Mechanical contact-data patterns. Deliberately over-broad in text: a false positive
# costs a mangled fixture, a false negative puts someone's phone number in a public repo.
#
# URLs are handled separately and deliberately. A craigslist post id is ten digits
# (`.../d/title/7712345678.html`) and a kijiji id is nine or ten, so a bare ten-digit
# run is ambiguous: it is both a phone-number shape and a listing id. Rewriting ids
# would destroy the very URLs the parsers extract, so URLs are masked before the
# text patterns run, after the URL-shaped contact patterns have had their turn.

# Applied to URLs, before masking.
_URL_REDACTIONS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("tel-link", re.compile(r"tel:[+0-9().\-\s]{7,}"), "tel:redacted"),
    ("mailto-link", re.compile(r"mailto:[^\s\"'<>]+"), "mailto:redacted@example.invalid"),
    (
        "reply-link",
        re.compile(r"https?://[^\s\"'<>]*/(?:reply|contact)/[^\s\"'<>]*"),
        "https://example.invalid/reply/redacted",
    ),
)

# Applied to everything outside a URL.
_TEXT_REDACTIONS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "email",
        re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
        "redacted@example.invalid",
    ),
    (
        "phone",
        re.compile(
            r"(?<![A-Za-z0-9])(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}"
            r"(?![A-Za-z0-9])"
        ),
        "555-555-5555",
    ),
    (
        "contact-field",
        re.compile(
            r'("(?:telephone|email|contactPoint|faxNumber|agentName|sellerName)"\s*:\s*)"[^"]*"'
        ),
        r'\1"redacted"',
    ),
)

_URL_RE = re.compile(r"(?:https?|ftp)://[^\s\"'<>]+")
_MASK = "\x00u{}\x00"


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Strip contact data. Returns the cleaned text and a per-pattern hit count.

    Incomplete by construction: it matches patterns, so it cannot remove personal
    names, and a phone number embedded in some other URL's query string survives
    masking. Every captured file must still be read before it is committed.
    """
    counts: dict[str, int] = {}

    for label, pattern, replacement in _URL_REDACTIONS:
        text, n = pattern.subn(replacement, text)
        if n:
            counts[label] = counts.get(label, 0) + n

    # Mask surviving URLs so listing ids are not mistaken for phone numbers.
    urls: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        urls.append(match.group(0))
        return _MASK.format(len(urls) - 1)

    text = _URL_RE.sub(_stash, text)

    for label, pattern, replacement in _TEXT_REDACTIONS:
        text, n = pattern.subn(replacement, text)
        if n:
            counts[label] = counts.get(label, 0) + n

    for index, url in enumerate(urls):
        text = text.replace(_MASK.format(index), url)

    return text, counts


def _client(*, user_agent: str, rate_limit: float | None) -> SourceHttpClient:
    return SourceHttpClient(
        user_agent=user_agent,
        rate_limit_per_minute=rate_limit,
        headers={"Accept-Language": "en-CA,en;q=0.9"},
        timeout=30.0,
    )


def _write(out: Path, body: str) -> None:
    cleaned, counts = redact(body)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(cleaned, encoding="utf-8")
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
    print(f"  wrote {out} ({len(cleaned)} bytes; redacted: {summary})")


def _fetch(fetch_text: Callable[[str], str], url: str) -> str | None:
    print(f"  GET {url}")
    try:
        return fetch_text(url)
    except RobotsDisallowedError as exc:
        print(f"  ROBOTS-BLOCKED: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 - a capture tool reports, it does not raise
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        return None


def _review_warning() -> None:
    print("\nREVIEW EVERY FILE BEFORE COMMITTING.")
    print("Redaction matches patterns, so it cannot catch everything. In particular it")
    print("does NOT remove personal NAMES — no pattern identifies those. Read each file")
    print("and check visible text, JSON-LD blocks, meta tags and inline scripts.")


def cmd_robots(args: argparse.Namespace) -> int:
    """Print a host's robots.txt and the verdict for the URLs a source would fetch."""
    origin = args.origin.rstrip("/")
    host = urlparse(origin).netloc
    client = _client(user_agent=args.user_agent, rate_limit=None)

    robots_url = urljoin(origin, "/robots.txt")
    print(f"robots.txt for {host}:")
    try:
        import httpx

        response = httpx.get(
            robots_url,
            headers={"User-Agent": args.user_agent},
            timeout=30.0,
            follow_redirects=True,
        )
        response.raise_for_status()
        print("-" * 60)
        print(response.text.strip())
        print("-" * 60)
    except Exception as exc:  # noqa: BLE001
        print(f"  could not fetch {robots_url}: {exc}")
        return 1

    print(f"\nVerdicts for user-agent {args.user_agent!r}:")
    probes = args.probe or [
        "/search/van/apa?format=rss&hasImage=1",
        "/search/van/apa?hasImage=1",
        "/van/apa/d/example/0000000000.html",
    ]
    for probe in probes:
        url = urljoin(origin + "/", probe.lstrip("/"))
        allowed = client._robots.allowed(url)  # noqa: SLF001 - deliberate: this is the question
        print(f"  {'ALLOW ' if allowed else 'BLOCK '} {url}")
    return 0


def cmd_craigslist(args: argparse.Namespace) -> int:
    base = args.base_url.rstrip("/")
    out = Path(args.out)
    fetch_text = build_fetch_text(
        Capabilities(supports_detail_fetch=True, rate_limit_per_minute=args.rate_limit),
        user_agent=CL_USER_AGENT,
        headers={"Accept-Language": "en-CA,en;q=0.9"},
        timeout=30.0,
    )

    query = {"hasImage": "1"}
    if args.max_price:
        query["max_price"] = str(args.max_price)

    print(f"craigslist: {base} area={args.area}")
    rss_query = urlencode({**query, "format": "rss"})
    rss = _fetch(fetch_text, f"{base}/search/{args.area}/apa?{rss_query}")
    if rss is not None:
        _write(out / "rss.xml", rss)

    html = _fetch(fetch_text, f"{base}/search/{args.area}/apa?{urlencode(query)}")
    if html is not None:
        _write(out / "search_results.html", html)

    detail_url = args.detail_url
    if detail_url is None and rss is not None:
        match = re.search(r"<link>\s*(https?://[^\s<]+)\s*</link>", rss[rss.find("<item>") :])
        detail_url = match.group(1) if match else None
    if detail_url is None and html is not None:
        match = re.search(r'href="(https?://[^"]*/d/[^"]+)"', html)
        detail_url = match.group(1) if match else None

    if detail_url is None:
        print("  no detail URL found; pass --detail-url to capture one")
    else:
        detail = _fetch(fetch_text, detail_url)
        if detail is not None:
            _write(out / "detail.html", detail)

    _review_warning()
    return 0


def cmd_url(args: argparse.Namespace) -> int:
    fetch_text = build_fetch_text(
        Capabilities(rate_limit_per_minute=args.rate_limit),
        user_agent=args.user_agent,
        timeout=30.0,
    )
    body = _fetch(fetch_text, args.url)
    if body is None:
        return 1
    _write(Path(args.out), body)
    _review_warning()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--user-agent", default=CL_USER_AGENT, help="User-Agent to send and to match robots rules."
    )
    parser.add_argument(
        "--rate-limit", type=float, default=20.0, help="Requests per minute (default: 20)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_robots = sub.add_parser("robots", help="Print robots.txt and per-URL verdicts.")
    p_robots.add_argument("origin", help="Scheme + host, e.g. https://vancouver.craigslist.org")
    p_robots.add_argument("--probe", action="append", help="Extra path to test (repeatable).")
    p_robots.set_defaults(func=cmd_robots)

    p_cl = sub.add_parser("craigslist", help="Capture craigslist search + detail fixtures.")
    p_cl.add_argument("--base-url", required=True)
    p_cl.add_argument("--area", default="van")
    p_cl.add_argument("--max-price", type=int, default=None)
    p_cl.add_argument("--detail-url", default=None)
    p_cl.add_argument("--out", default="tests/fixtures/craigslist")
    p_cl.set_defaults(func=cmd_craigslist)

    p_url = sub.add_parser("url", help="Capture one URL to one file.")
    p_url.add_argument("url")
    p_url.add_argument("--out", required=True)
    p_url.set_defaults(func=cmd_url)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
