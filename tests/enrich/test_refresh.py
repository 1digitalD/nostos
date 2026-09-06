from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nostos.config.citypack import Citypack
from nostos.config.profile import Profile
from nostos.context import SearchContext
from nostos.enrich.refresh import enqueue_refresh, get_refresh_job, run_refresh_once
from nostos.enrich.review import (
    apply_extraction_revision,
    load_current_extraction_revision,
    preview_extraction_revision,
)
from nostos.model import Listing, Origin, SourceRecord
from nostos.sources.base import Capabilities, Liveness
from nostos.sources.craigslist import CraigslistSource
from nostos.store.db import apply_migrations, connect
from nostos.store.repo import ListingRepo, ObservationRepo, ScoreRepo
from nostos.watch.runner import run_watch

NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)
LISTING_ID = "canonical-listing"


class FakeSource:
    name = "craigslist"
    capabilities = Capabilities(supports_detail_fetch=True)

    def __init__(
        self,
        fetcher: Callable[[SourceRecord], SourceRecord],
        *,
        records: tuple[SourceRecord, ...] = (),
    ) -> None:
        self._fetcher = fetcher
        self._records = records
        self._delegate = CraigslistSource()

    def discover(self, ctx: SearchContext) -> Iterator[SourceRecord]:
        del ctx
        return iter(self._records)

    def fetch_detail(self, rec: SourceRecord) -> SourceRecord:
        return self._fetcher(rec)

    def check_liveness(self, rec: SourceRecord) -> Liveness:
        del rec
        return Liveness.OK

    def to_listing(self, rec: SourceRecord, ctx: SearchContext) -> Listing:
        return self._delegate.to_listing(rec, ctx)


def _context(*, max_rent: float = 3_200) -> SearchContext:
    return SearchContext(
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
                        "key": "downtown",
                        "label": "Downtown",
                        "keywords": ["downtown"],
                        "bbox": [49.0, -124.0, 50.0, -123.0],
                    }
                ],
                "sources": {"craigslist": {"enabled": True, "load_bearing": False}},
                "address": {"directional": {}, "strip_tokens": [], "region_tokens": []},
            }
        ),
        profile=Profile.model_validate(
            {
                "city": "vancouver",
                "unknown_policy": "review",
                "hard": {"rent": {"max": max_rent, "currency": "CAD"}},
                "weights": {"laundry.in_suite": 5},
                "sources": {"craigslist": "on"},
                "schedule": "0 */6 * * *",
            }
        ),
    )


def _seed(db_path: Path) -> SourceRecord:
    record = SourceRecord(
        source="craigslist",
        source_id="abc123",
        url="https://vancouver.craigslist.org/van/apa/d/example/abc123.html",
        payload={"title": "2BR home", "price": 2_800, "beds": 2, "baths": 1},
        content_hash="discovery-v1",
        fetched_at=NOW,
    )
    with connect(db_path) as conn:
        apply_migrations(conn)
        repo = ListingRepo(conn)
        repo.add_source_record(listing_id=LISTING_ID, record=record)
        repo.upsert_listing_source(
            listing_id=LISTING_ID,
            source=record.source,
            source_id=record.source_id,
            signature="seed-signature",
            seen_at=record.fetched_at,
        )
    return record


def _detail(record: SourceRecord, *, status: str = "complete") -> SourceRecord:
    payload = dict(record.payload) if isinstance(record.payload, Mapping) else {}
    payload.update(
        {
            "description": "Bright home with private in-suite laundry.",
            "photos": ["https://images.example/one.jpg"],
            "detail_status": status,
        }
    )
    return record.model_copy(
        update={"payload": payload, "content_hash": f"detail-{status}", "fetched_at": NOW}
    )


def _job(db_path: Path) -> dict[str, Any]:
    with connect(db_path) as conn:
        job = get_refresh_job(conn, listing_id=LISTING_ID)
    assert job is not None
    return job


def test_enqueue_joins_active_job_and_success_publishes_revision(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    _seed(db_path)
    with connect(db_path) as conn:
        first = enqueue_refresh(conn, listing_id=LISTING_ID)
        second = enqueue_refresh(conn, listing_id=LISTING_ID)
    assert first["id"] == second["id"]

    source = FakeSource(_detail)
    assert run_refresh_once(
        db_path,
        context=_context(),
        profile_id="balanced",
        sources={source.name: source},
    )

    with connect(db_path) as conn:
        job = get_refresh_job(conn, listing_id=LISTING_ID)
        current = load_current_extraction_revision(conn, listing_id=LISTING_ID)
        source_count = conn.execute(
            "SELECT COUNT(*) FROM source_record WHERE listing_id=?", (LISTING_ID,)
        ).fetchone()[0]
        score = ScoreRepo(conn).get_score(LISTING_ID, "balanced")
    assert job is not None and job["state"] == "succeeded"
    assert job["outcome"] == "complete"
    assert current is not None
    laundry = current.machine_listing.attributes["in_suite_laundry"]
    assert laundry.value is True
    assert source_count == 2
    assert score is not None


def test_malformed_legacy_projection_does_not_block_valid_refresh(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    _seed(db_path)
    context = _context()
    source = FakeSource(_detail)
    with connect(db_path) as conn:
        preview = preview_extraction_revision(
            conn,
            listing_id=LISTING_ID,
            context=context,
            profile_id="balanced",
            sources={source.name: source},
        )
        apply_extraction_revision(
            conn,
            listing_id=LISTING_ID,
            preview_token=preview.preview_token,
            context=context,
        )
        projection = ListingRepo(conn).get_fields_projection(LISTING_ID)
        projection["rent"] = "amount=Decimal('3000') currency='CAD'"
        conn.execute(
            "UPDATE listing SET fields_json=? WHERE id=?",
            (json.dumps(projection), LISTING_ID),
        )
        enqueue_refresh(conn, listing_id=LISTING_ID)

    assert run_refresh_once(
        db_path,
        context=context,
        profile_id="balanced",
        sources={source.name: source},
    )
    with connect(db_path) as conn:
        job = get_refresh_job(conn, listing_id=LISTING_ID)
        current = load_current_extraction_revision(conn, listing_id=LISTING_ID)
        persisted = ListingRepo(conn).get_fields_projection(LISTING_ID)

    assert job is not None and job["state"] == "succeeded"
    assert current is not None
    assert isinstance(persisted["rent"], dict)


def test_failed_fetch_retries_three_times_without_replacing_source(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    _seed(db_path)
    with connect(db_path) as conn:
        enqueue_refresh(conn, listing_id=LISTING_ID)

    def fail(_record: SourceRecord) -> SourceRecord:
        raise RuntimeError("secret URL https://token.example/private")

    source = FakeSource(fail)
    for attempt in range(1, 4):
        assert run_refresh_once(
            db_path,
            context=_context(),
            profile_id="balanced",
            sources={source.name: source},
        )
        job = _job(db_path)
        assert job["attempt_count"] == attempt
        if attempt < 3:
            assert job["state"] == "queued"
            with connect(db_path) as conn:
                conn.execute(
                    "UPDATE detail_refresh_job SET next_attempt_at=? WHERE id=?",
                    ("2000-01-01T00:00:00+00:00", job["id"]),
                )

    job = _job(db_path)
    assert job["state"] == "failed"
    assert "token.example" not in str(job["error"])
    with connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_record").fetchone()[0] == 1
        assert load_current_extraction_revision(conn, listing_id=LISTING_ID) is None


def test_expired_claim_recovers_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    _seed(db_path)
    with connect(db_path) as conn:
        job = enqueue_refresh(conn, listing_id=LISTING_ID)
        conn.execute(
            """
            UPDATE detail_refresh_job
            SET state='running',attempt_count=1,claim_token='dead-worker',
                claim_expires_at='2000-01-01T00:00:00+00:00'
            WHERE id=?
            """,
            (job["id"],),
        )
    source = FakeSource(_detail)
    assert run_refresh_once(
        db_path,
        context=_context(),
        profile_id="balanced",
        sources={source.name: source},
    )
    recovered = _job(db_path)
    assert recovered["state"] == "succeeded"
    assert recovered["attempt_count"] == 2


def test_blocked_and_identity_mismatch_are_terminal(tmp_path: Path) -> None:
    blocked_db = tmp_path / "blocked.db"
    _seed(blocked_db)
    with connect(blocked_db) as conn:
        enqueue_refresh(conn, listing_id=LISTING_ID)
    blocked = FakeSource(lambda record: _detail(record, status="blocked"))
    run_refresh_once(
        blocked_db,
        context=_context(),
        profile_id="balanced",
        sources={blocked.name: blocked},
    )
    assert _job(blocked_db)["outcome"] == "blocked"

    mismatch_db = tmp_path / "mismatch.db"
    _seed(mismatch_db)
    with connect(mismatch_db) as conn:
        enqueue_refresh(conn, listing_id=LISTING_ID)

    def mismatch(record: SourceRecord) -> SourceRecord:
        return _detail(record).model_copy(update={"source_id": "another-unit"})

    mismatched = FakeSource(mismatch)
    run_refresh_once(
        mismatch_db,
        context=_context(),
        profile_id="balanced",
        sources={mismatched.name: mismatched},
    )
    assert _job(mismatch_db)["outcome"] == "identity_mismatch"


def test_correction_during_fetch_is_applied_to_published_revision(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    _seed(db_path)
    with connect(db_path) as conn:
        enqueue_refresh(conn, listing_id=LISTING_ID)

    def correct_then_fetch(record: SourceRecord) -> SourceRecord:
        with connect(db_path) as conn:
            ObservationRepo(conn).record_observation(
                listing_id=LISTING_ID,
                field="attributes.in_suite_laundry",
                value_json=False,
                origin=Origin.USER,
                confidence=1.0,
                evidence="user correction",
                observed_at=datetime(2026, 9, 6, 13, tzinfo=UTC),
            )
        return _detail(record)

    source = FakeSource(correct_then_fetch)
    run_refresh_once(
        db_path,
        context=_context(),
        profile_id="balanced",
        sources={source.name: source},
    )
    with connect(db_path) as conn:
        projection = ListingRepo(conn).get_fields_projection(LISTING_ID)
    laundry = projection["attributes.in_suite_laundry"]
    assert isinstance(laundry, dict)
    assert laundry["origin"] == "user"
    assert laundry["value"] is False


def test_stale_source_and_failed_publish_do_not_partially_publish(tmp_path: Path) -> None:
    stale_db = tmp_path / "stale.db"
    _seed(stale_db)
    with connect(stale_db) as conn:
        enqueue_refresh(conn, listing_id=LISTING_ID)

    def replace_source(record: SourceRecord) -> SourceRecord:
        with connect(stale_db) as conn:
            ListingRepo(conn).add_source_record(
                listing_id=LISTING_ID,
                record=record.model_copy(update={"content_hash": "newer-discovery"}),
            )
        return _detail(record)

    stale = FakeSource(replace_source)
    run_refresh_once(
        stale_db,
        context=_context(),
        profile_id="balanced",
        sources={stale.name: stale},
    )
    assert _job(stale_db)["outcome"] == "stale_source"
    with connect(stale_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM extraction_review").fetchone()[0] == 0

    rollback_db = tmp_path / "rollback.db"
    _seed(rollback_db)
    with connect(rollback_db) as conn:
        enqueue_refresh(conn, listing_id=LISTING_ID)
        conn.execute(
            """
            CREATE TRIGGER reject_refresh_score BEFORE INSERT ON score
            BEGIN SELECT RAISE(ABORT, 'score failed'); END
            """
        )
    source = FakeSource(_detail)
    run_refresh_once(
        rollback_db,
        context=_context(),
        profile_id="balanced",
        sources={source.name: source},
    )
    assert _job(rollback_db)["state"] == "queued"
    with connect(rollback_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_record").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM extraction_review").fetchone()[0] == 0


def test_profile_change_during_publish_rolls_back_and_retries(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    _seed(db_path)
    with connect(db_path) as conn:
        enqueue_refresh(conn, listing_id=LISTING_ID)
    contexts = iter((_context(), _context(max_rent=3_100)))
    source = FakeSource(_detail)
    run_refresh_once(
        db_path,
        context=_context(),
        profile_id="balanced",
        sources={source.name: source},
        context_loader=lambda: next(contexts),
    )
    job = _job(db_path)
    assert job["state"] == "queued"
    assert job["outcome"] == "publish_failed"
    with connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_record").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM extraction_review").fetchone()[0] == 0


def test_queue_details_preserves_known_detail_and_completes_new_listing(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    original = _seed(db_path)
    initial_source = FakeSource(_detail)
    with connect(db_path) as conn:
        enqueue_refresh(conn, listing_id=LISTING_ID)
    run_refresh_once(
        db_path,
        context=_context(),
        profile_id="balanced",
        sources={initial_source.name: initial_source},
    )

    known_discovery = original.model_copy(
        update={"payload": {"title": "Known skeleton", "price": 2_850}, "content_hash": "card"}
    )
    new_discovery = original.model_copy(
        update={
            "source_id": "new456",
            "url": "https://vancouver.craigslist.org/van/apa/d/example/new456.html",
            "payload": {"title": "New 2BR skeleton", "price": 2_900, "beds": 2},
            "content_hash": "new-card",
        }
    )
    fetches: list[str] = []

    def fetch(record: SourceRecord) -> SourceRecord:
        fetches.append(record.source_id)
        return _detail(record)

    source = FakeSource(fetch, records=(known_discovery, new_discovery))
    with connect(db_path) as conn:
        report = run_watch(
            conn=conn,
            context=_context(),
            sources=(source,),
            profile_id="balanced",
            run_id="queued-watch",
            queue_details=True,
        )
        known_records = conn.execute(
            "SELECT COUNT(*) FROM source_record WHERE listing_id=?", (LISTING_ID,)
        ).fetchone()[0]
        new_row = conn.execute(
            "SELECT listing_id FROM listing_source WHERE source_id='new456'"
        ).fetchone()
        assert new_row is not None
        new_listing_id = str(new_row["listing_id"])
        new_records = conn.execute(
            "SELECT COUNT(*) FROM source_record WHERE listing_id=?", (new_listing_id,)
        ).fetchone()[0]
        known_job = get_refresh_job(conn, listing_id=LISTING_ID)
        new_job = get_refresh_job(conn, listing_id=new_listing_id)
        assert known_job is not None and known_job["state"] == "queued"
        assert new_job is not None and new_job["state"] == "queued"

    assert report.source_reports["craigslist"].count == 2
    assert fetches == []
    assert known_records == 2
    assert new_records == 1

    assert run_refresh_once(
        db_path,
        context=_context(),
        profile_id="balanced",
        sources={source.name: source},
    )
    assert run_refresh_once(
        db_path,
        context=_context(),
        profile_id="balanced",
        sources={source.name: source},
    )
    with connect(db_path) as conn:
        assert load_current_extraction_revision(conn, listing_id=LISTING_ID) is not None
        assert load_current_extraction_revision(conn, listing_id=new_listing_id) is not None
        assert conn.execute(
            "SELECT COUNT(*) FROM source_record WHERE listing_id=?", (LISTING_ID,)
        ).fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM source_record WHERE listing_id=?", (new_listing_id,)
        ).fetchone()[0] == 2
