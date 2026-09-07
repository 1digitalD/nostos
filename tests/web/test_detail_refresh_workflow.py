"""End-to-end coverage for explicit saved-listing detail refreshes."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nostos.model.source_record import SourceRecord
from nostos.sources.craigslist import CraigslistSource
from nostos.store.db import apply_migrations, connect
from nostos.store.repo import ListingRepo
from nostos.web import create_app
from nostos.workflows import correct_listing_fact
from tests.web.test_profile_page import _seed_profile_and_citypack

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "craigslist"
LISTING_ID = "craigslist:detail-refresh-1"
SOURCE_ID = "ttyaU3MwTGwwdBcafMuZiN"
SOURCE_URL = (
    "https://www.craigslist.org/view/d/vancouver-private-yard-ground-level-bed/"
    f"{SOURCE_ID}"
)
FIXED_NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _source_fixture_fetcher(
    detail_html: str,
    *,
    before_return: Callable[[], None] | None = None,
    fail: bool = False,
) -> Callable[[str], str]:
    called = False

    def fetch(url: str) -> str:
        nonlocal called
        assert url == SOURCE_URL
        if fail:
            raise RuntimeError("fixture source unavailable")
        if not called and before_return is not None:
            called = True
            before_return()
        return detail_html

    return fetch


def _seed_listing(
    db_path: Path,
    *,
    description: str,
    content_hash: str = "discovery-v1",
    price: int = 2800,
) -> None:
    with connect(db_path) as conn:
        ListingRepo(conn).ensure_listing(LISTING_ID, seen_at=FIXED_NOW)
        ListingRepo(conn).add_source_record(
            listing_id=LISTING_ID,
            record=SourceRecord(
                source="craigslist",
                source_id=SOURCE_ID,
                url=SOURCE_URL,
                payload={
                    "id": SOURCE_ID,
                    "source": "craigslist",
                    "url": SOURCE_URL,
                    "title": "Discovery title",
                    "location": "Vancouver",
                    "price": price,
                    "description": description,
                },
                content_hash=content_hash,
                fetched_at=FIXED_NOW,
            ),
        )
        conn.commit()


def _client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fetcher: Callable[[str], str],
) -> tuple[TestClient, Path, str, CraigslistSource]:
    db_path, profile_path, citypack_path = _seed_profile_and_citypack(tmp_path)
    with connect(db_path) as conn:
        apply_migrations(conn)
    source = CraigslistSource(fetch_text=fetcher, now=lambda: FIXED_NOW)
    # AppState resolves sources on construction. Replace only the Craigslist
    # constructor; all refresh work still goes through the real source parser.
    monkeypatch.setattr("nostos.web.app.CraigslistSource", lambda: source)
    app = create_app(
        db_path=db_path,
        profile_path=profile_path,
        citypack_path=citypack_path,
    )
    return TestClient(app), db_path, profile_path.stem, source


def _run_worker(
    client: TestClient, db_path: Path, profile_id: str, source: CraigslistSource
) -> bool:
    from nostos.enrich.refresh import run_refresh_once

    state = client.app.state.nostos  # type: ignore[attr-defined]
    return run_refresh_once(
        db_path,
        context=state.context,
        profile_id=profile_id,
        sources={"craigslist": source},
    )


def _latest_payload(db_path: Path) -> dict[str, object]:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT payload FROM source_record WHERE listing_id=? ORDER BY id DESC LIMIT 1",
            (LISTING_ID,),
        ).fetchone()
    assert row is not None
    value = json.loads(str(row["payload"]))
    assert isinstance(value, dict)
    return value


def _make_retry_due(db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE detail_refresh_job SET next_attempt_at=?",
            ("1970-01-01T00:00:00+00:00",),
        )
        conn.commit()


def test_enqueue_worker_fetches_real_parser_and_renders_tail_and_gallery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    detail_html = (FIXTURE_DIR / "detail.html").read_text(encoding="utf-8")
    client, db_path, profile_id, source = _client(
        tmp_path,
        monkeypatch,
        fetcher=_source_fixture_fetcher(detail_html),
    )
    _seed_listing(db_path, description="Discovery text only.")

    first = client.post(f"/listings/{LISTING_ID}/detail-refresh")
    assert first.status_code == 202, first.text
    job = first.json()
    assert job["state"] == "queued"
    job_id = job["id"]

    # Repeated clicks join the durable active job.
    duplicate = client.post(f"/listings/{LISTING_ID}/detail-refresh")
    assert duplicate.status_code == 202
    assert duplicate.json()["id"] == job_id
    assert client.get(f"/listings/{LISTING_ID}/detail-refresh").json()["state"] == "queued"

    assert _run_worker(client, db_path, profile_id, source) is True
    status = client.get(f"/listings/{LISTING_ID}/detail-refresh")
    assert status.status_code == 200
    assert status.json()["state"] == "succeeded"

    payload = _latest_payload(db_path)
    assert "Western Rental - Sutton 1st West." in str(payload["description"])
    photos = payload.get("photos") or payload.get("images") or []
    assert isinstance(photos, list)
    assert len(photos) >= 2
    assert any("29gpTFMBsM3" in str(photo) for photo in photos)

    detail = client.get(f"/listings/{LISTING_ID}")
    assert detail.status_code == 200
    assert "Western Rental - Sutton 1st West." in detail.text
    assert "00303_5cDkQaVN8lX_0CI0pO_600x450.jpg" in detail.text
    assert re.search(r"detail-score-block.*?<strong>\d+</strong>", detail.text, re.S)


def test_failed_refresh_keeps_previous_source_evidence_and_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_description = "Previously captured description that remains available."
    client, db_path, profile_id, source = _client(
        tmp_path,
        monkeypatch,
        fetcher=_source_fixture_fetcher("unused", fail=True),
    )
    _seed_listing(db_path, description=old_description)
    before = _latest_payload(db_path)

    queued = client.post(f"/listings/{LISTING_ID}/detail-refresh")
    assert queued.status_code == 202
    assert _run_worker(client, db_path, profile_id, source) is True
    for _ in range(2):
        _make_retry_due(db_path)
        assert _run_worker(client, db_path, profile_id, source) is True

    status = client.get(f"/listings/{LISTING_ID}/detail-refresh").json()
    assert status["state"] == "failed"
    assert status["error"] == "detail fetch failed (RuntimeError)"
    assert _latest_payload(db_path) == before
    detail = client.get(f"/listings/{LISTING_ID}")
    assert old_description in detail.text
    assert "Western Rental - Sutton 1st West." not in detail.text


def test_refresh_reapplies_correction_made_during_source_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    detail_html = (FIXTURE_DIR / "detail.html").read_text(encoding="utf-8")
    db_holder: dict[str, Path] = {}

    def correct_during_fetch() -> None:
        with connect(db_holder["db"]) as conn:
            correct_listing_fact(
                conn,
                listing_id=LISTING_ID,
                field="rent",
                value="2600",
                currency="CAD",
                area_unit="sqft",
            )

    fetcher = _source_fixture_fetcher(detail_html, before_return=correct_during_fetch)
    client, db_path, profile_id, source = _client(
        tmp_path,
        monkeypatch,
        fetcher=fetcher,
    )
    db_holder["db"] = db_path
    _seed_listing(db_path, description="Discovery text only.")

    assert client.post(f"/listings/{LISTING_ID}/detail-refresh").status_code == 202
    assert _run_worker(client, db_path, profile_id, source) is True
    assert client.get(f"/listings/{LISTING_ID}/detail-refresh").json()["state"] == "succeeded"

    # The fetched record is preserved, but the correction remains authoritative
    # when the normal details view reconstructs the listing.
    payload = _latest_payload(db_path)
    assert payload["price"] == 2700
    detail = client.get(f"/listings/{LISTING_ID}")
    assert re.search(r"<span>Monthly rent</span><strong>2600\.00 CAD</strong>", detail.text)
