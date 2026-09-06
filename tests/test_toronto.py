"""Toronto user workflow with synthetic source responses; no network required."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import nostos.cli as cli
from nostos.config.profile import load_profile
from nostos.context import load_search_context
from nostos.sources.craigslist import CraigslistSource
from nostos.sources.kijiji import KijijiSource
from nostos.web.app import create_app


def test_toronto_init_watch_rank_browse_and_correct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOSTOS_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli.app, [
        "init", "--non-interactive", "--city", "toronto", "--max-rent", "3200",
        "--beds", "2", "--laundry", "nice-to-have", "--source", "craigslist",
    ])
    assert result.exit_code == 0, result.output
    profile = tmp_path / "profile.yaml"
    assert load_profile(profile).city == "toronto"
    pack = cli._resolve_citypack_path(None, profile_path=profile)
    db = cli._resolve_db_path(None, profile_path=profile)
    assert db == tmp_path / "toronto" / "nostos.db"
    assert not (tmp_path / "nostos.db").exists()
    urls: list[str] = []

    def fetch(url: str) -> str:
        urls.append(url)
        assert urlparse(url).hostname == "toronto.craigslist.org"
        if "/search/" in url:
            assert "/search/tor/apa" in url
            return '''<?xml version="1.0"?><rss version="2.0"><channel><item>
              <title>Liberty Village two bedroom</title>
              <link>https://toronto.craigslist.org/tor/apa/d/liberty-village/1234567890</link>
              <description>$2800 / 2br - 850ft2</description>
              <pubDate>Fri, 04 Sep 2026 12:00:00 GMT</pubDate>
            </item></channel></rss>'''
        return '''<html><head><script type="application/ld+json" id="ld_posting_data">
          {"@context":"http://schema.org","@type":"House",
           "name":"Liberty Village two bedroom", "numberOfBedrooms":2,
           "numberOfBathroomsTotal":1,
           "address":{"streetAddress":"100 Liberty Street","addressLocality":"Toronto"}}
          </script></head><body><span id="titletextonly">Liberty Village two bedroom</span>
          <span class="price">$2800</span><section id="postingbody">
          Unfurnished 2BR apartment with 1 bath and in-suite laundry. 850 sqft.
          </section></body></html>'''

    monkeypatch.setitem(cli.SOURCE_FACTORIES, "craigslist", lambda: CraigslistSource(
        fetch_text=fetch, now=lambda: datetime(2026, 9, 4, 13, tzinfo=UTC)
    ))
    watched = runner.invoke(cli.app, ["watch", "--yes"])
    assert watched.exit_code == 0, watched.output
    assert urls
    ranked = runner.invoke(cli.app, ["rank"])
    assert ranked.exit_code == 0, ranked.output
    listed = runner.invoke(cli.app, ["list"])
    assert listed.exit_code == 0, listed.output
    assert "Liberty Village" in listed.output, watched.output + ranked.output + listed.output
    with TestClient(create_app(db_path=db, profile_path=profile, citypack_path=pack)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Liberty Village" in page.text
        assert client.get("/profile").status_code == 200
        assert client.get("/", params={"rent_max": 1000}).status_code == 200
    # Relaxing an edited profile replays the stored response without fetching again.
    before = len(urls)
    text = profile.read_text().replace("3200", "2000")
    profile.write_text(text)
    assert runner.invoke(cli.app, ["rank"]).exit_code == 0
    assert "listed_count=0" in runner.invoke(cli.app, ["list"]).output
    profile.write_text(text.replace("2000", "3200"))
    assert runner.invoke(cli.app, ["rank"]).exit_code == 0
    assert "Liberty Village" in runner.invoke(cli.app, ["list"]).output
    assert len(urls) == before


def test_toronto_kijiji_scope_and_unknown_city(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOSTOS_HOME", str(tmp_path))
    runner = CliRunner()
    common = ["--non-interactive", "--max-rent", "3200", "--beds", "2",
              "--laundry", "nice-to-have", "--source", "kijiji"]
    bad = runner.invoke(cli.app, ["init", "--city", "unknown-city", *common])
    assert bad.exit_code == 2
    assert not (tmp_path / "profile.yaml").exists()
    assert runner.invoke(cli.app, ["init", "--city", "toronto", *common]).exit_code == 0
    profile = tmp_path / "profile.yaml"
    context = load_search_context(
        citypack_path=cli._resolve_citypack_path(None, profile_path=profile), profile_path=profile
    )
    urls: list[str] = []

    def fetch(url: str) -> str:
        urls.append(url)
        return ('<script type="application/ld+json">'
                '{"@type":"ItemList","itemListElement":[]}</script>')

    assert list(KijijiSource(fetcher=fetch).discover(context)) == []
    assert urls == [
        "https://www.kijiji.ca/b-apartments-condos/city-of-toronto/apartments/k0c37l1700273"
    ]
    assert cli._resolve_db_path(None) == tmp_path / "nostos.db"
    explicit = tmp_path / "custom.db"
    assert cli._resolve_db_path(explicit, profile_path=profile) == explicit
