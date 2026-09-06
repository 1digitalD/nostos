from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from test_profile_page import _seed_listing, _seed_profile_and_citypack

from nostos.store.db import connect
from nostos.store.repo import ResearchRepo
from nostos.web.app import create_app


class ReportProvider:
    name = "fixture"

    def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [{
            "title": "1234 West 4th Avenue, Vancouver building amenities",
            "snippet": "1234 West 4th Avenue, Vancouver has a fitness centre. "
                       "Building management is handled by Example Management.",
            "url": "https://example.test/building",
            "date": datetime.now(UTC).date().isoformat(),
        }]


def test_report_citations_exclusion_restore_and_address_change(tmp_path: Path) -> None:
    db, profile, pack = _seed_profile_and_citypack(tmp_path)
    client = TestClient(create_app(db_path=db, profile_path=profile, citypack_path=pack,
                                   research_provider=ReportProvider()))
    _seed_listing(db, profile.stem, "craigslist:test")
    endpoint = "/listings/craigslist:test"
    assert client.post(endpoint + "/web-research.json").status_code == 200
    page = client.get(endpoint + "/research")
    assert 'id="report-management"' in page.text
    assert 'id="report-gyms"' in page.text
    assert "Example Management" in page.text
    assert "Read cited evidence" in page.text
    with connect(db) as conn:
        run, _ = ResearchRepo(conn).get("craigslist:test")
        assert run is not None
        key = str(run["cache_key"])
        before = conn.execute("SELECT fields_json FROM listing").fetchall()
    data = {"url": "https://example.test/building", "cache_key": key, "excluded": "true"}
    excluded = client.post(endpoint + "/research-source", data=data)
    assert excluded.status_code == 200
    assert "Excluded from your report" in excluded.text
    assert 'id="citation-' not in excluded.text
    assert client.post(endpoint + "/web-research.json?force=1").status_code == 200
    assert 'id="citation-' not in client.get(endpoint + "/research").text
    data["excluded"] = "false"
    restored = client.post(endpoint + "/research-source", data=data)
    assert restored.status_code == 200
    assert 'id="citation-' in restored.text
    with connect(db) as conn:
        assert conn.execute("SELECT fields_json FROM listing").fetchall() == before
    client.post(endpoint + "/research-address", data={"address": "99 King Street"})
    assert client.post(endpoint + "/research-source", data=data).status_code == 409


def test_duplicate_research_does_not_repeat_provider_work(tmp_path: Path) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    started, release = threading.Event(), threading.Event()

    class WaitingProvider(ReportProvider):
        def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            started.set()
            assert release.wait(5)
            return super().search(query, **kwargs)

    db, profile, pack = _seed_profile_and_citypack(tmp_path)
    client = TestClient(create_app(db_path=db, profile_path=profile, citypack_path=pack,
                                   research_provider=WaitingProvider()))
    _seed_listing(db, profile.stem, "craigslist:test")
    endpoint = "/listings/craigslist:test/web-research.json"
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, endpoint)
        assert started.wait(5)
        try:
            assert client.post(endpoint).status_code == 429
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 200
    assert client.post(endpoint).json()["status"] == "cached"
