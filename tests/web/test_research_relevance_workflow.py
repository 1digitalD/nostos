from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from test_profile_page import _seed_listing, _seed_profile_and_citypack

from nostos.store.db import connect
from nostos.store.repo import ResearchRepo
from nostos.web.app import create_app


class EvidenceProvider:
    name = "fixture"

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("Search unavailable")
        return [
            {
                "title": "1234 West 4th Ave, Vancouver building",
                "snippet": "1234 West 4th Avenue, Vancouver: management contact details.",
                "url": "https://example.test/building",
                "date": datetime.now(UTC).date().isoformat(),
            },
            {
                "title": "Irrelevant Montreal apartment",
                "snippet": "999 Other St, Montreal",
                "url": "https://example.test/wrong",
                "date": datetime.now(UTC).date().isoformat(),
            },
        ]


def setup(tmp_path: Path) -> tuple[TestClient, Path, EvidenceProvider]:
    db, profile, pack = _seed_profile_and_citypack(tmp_path)
    provider = EvidenceProvider()
    app = create_app(
        db_path=db, profile_path=profile, citypack_path=pack, research_provider=provider
    )
    _seed_listing(db, profile.stem, "craigslist:test")
    return TestClient(app), db, provider


def test_real_compilation_persistence_cache_and_correction(tmp_path: Path) -> None:
    client, db, provider = setup(tmp_path)
    endpoint = "/listings/craigslist:test/web-research.json"
    page_url = "/listings/craigslist:test/research"
    compiled = client.post(endpoint)
    assert compiled.status_code == 200, compiled.text
    assert compiled.json()["count"] == 1
    assert provider.calls == 4
    page = client.get(page_url)
    assert "1234 West 4th Ave, Vancouver building" in page.text
    assert "Irrelevant Montreal apartment" not in page.text
    assert "matching street address and city" in page.text
    with connect(db) as conn:
        run, results = ResearchRepo(conn).get("craigslist:test")
        assert run and run["cache_key"] and run["filtered_irrelevant_count"]
        assert results[0]["match_reason"]
    assert client.post(endpoint).json()["status"] == "cached"
    assert provider.calls == 4
    changed = client.post(
        "/listings/craigslist:test/research-address",
        data={"address": "999 Other Street, Vancouver"},
    )
    assert changed.status_code == 200
    assert "1234 West 4th Ave, Vancouver building" not in changed.text
    assert client.post(endpoint).json()["count"] == 0
    assert provider.calls == 8
    assert "remain unknown" in client.get(page_url).text


def test_legacy_and_expired_results_hidden_and_missing_address_does_not_search(
    tmp_path: Path,
) -> None:
    client, db, provider = setup(tmp_path)
    with connect(db) as conn:
        ResearchRepo(conn).replace_results(
            listing_id="craigslist:test",
            subject="1234 West 4th Ave",
            provider="old",
            status="complete",
            error=None,
            fetched_at=datetime.now(UTC).isoformat(),
            filtered_stale_count=0,
            results=[
                {
                    "topic": "Safety",
                    "title": "Legacy unrelated result",
                    "url": "https://example.test/old",
                    "source": "Old",
                    "published_at": "2026-09-01",
                    "excerpt": "Never validated",
                }
            ],
        )
    assert "Legacy unrelated result" not in client.get("/listings/craigslist:test/research").text
    assert client.post("/listings/craigslist:test/web-research.json").json()["count"] == 1
    with connect(db) as conn:
        conn.execute(
            "UPDATE research_run SET fetched_at=?",
            ((datetime.now(UTC) - timedelta(days=2)).isoformat(),),
        )
        conn.commit()
    assert (
        "1234 West 4th Ave, Vancouver building"
        not in client.get("/listings/craigslist:test/research").text
    )
    with connect(db) as conn:
        row = conn.execute("SELECT id,payload FROM source_record LIMIT 1").fetchone()
        data = json.loads(row["payload"])
        data.update(address="Vancouver BC V6K 1A1", title="Spacious apartment")
        conn.execute("UPDATE source_record SET payload=? WHERE id=?", (json.dumps(data), row["id"]))
        conn.commit()
    calls = provider.calls
    page = client.get("/listings/craigslist:test/research")
    assert "Enter the building address" in page.text
    assert 'data-auto="0"' in page.text
    response = client.post("/listings/craigslist:test/web-research.json")
    assert response.status_code == 422
    assert provider.calls == calls
    invalid = client.post(
        "/listings/craigslist:test/research-address", data={"address": "Vancouver"}
    )
    assert "Enter a street number" in invalid.text


def test_provider_failure_does_not_masquerade_as_empty_success(tmp_path: Path) -> None:
    client, db, provider = setup(tmp_path)
    provider.fail = True
    response = client.post("/listings/craigslist:test/web-research.json")
    assert response.status_code == 502
    with connect(db) as conn:
        run, results = ResearchRepo(conn).get("craigslist:test")
        assert run is None and not results


def test_partial_search_failure_is_visible_with_accepted_evidence(tmp_path: Path) -> None:
    db, profile, pack = _seed_profile_and_citypack(tmp_path)

    class PartialProvider(EvidenceProvider):
        def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            if "safety" in query.lower():
                raise RuntimeError("Provider failed")
            return super().search(query, **kwargs)

    client = TestClient(
        create_app(
            db_path=db,
            profile_path=profile,
            citypack_path=pack,
            research_provider=PartialProvider(),
        )
    )
    _seed_listing(db, profile.stem, "craigslist:test")
    response = client.post("/listings/craigslist:test/web-research.json")
    assert response.status_code == 200
    page = client.get("/listings/craigslist:test/research")
    assert "Research is incomplete" in page.text
    assert "1234 West 4th Ave, Vancouver building" in page.text


def test_refresh_failure_retains_current_evidence(tmp_path: Path) -> None:
    client, db, provider = setup(tmp_path)
    endpoint = "/listings/craigslist:test/web-research.json"
    assert client.post(endpoint).status_code == 200
    provider.fail = True
    assert client.post(endpoint + "?force=1").status_code == 502
    with connect(db) as conn:
        run, results = ResearchRepo(conn).get("craigslist:test")
        assert run and len(results) == 1
