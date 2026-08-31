from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

import nostos.watch.runner as runner_module
from nostos.config.citypack import Citypack
from nostos.config.profile import Profile
from nostos.context import SearchContext
from nostos.model import Absence, Identity, Listing, Money, Observed, Origin, Place, SourceRecord
from nostos.sources.base import Capabilities, Liveness
from nostos.sources.craigslist import CraigslistSource
from nostos.store.db import apply_migrations, connect
from nostos.store.repo import RunRepo
from nostos.watch.runner import run_watch


def test_full_watch_run_executes_against_temp_store(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    context = _build_context(
        source_flags={
            "stub": {"enabled": True, "load_bearing": False},
        }
    )
    source = ScriptedSource(
        name="stub",
        records=(
            _make_record("stub", 1, minutes=1),
            _make_record("stub", 2, minutes=2),
        ),
    )
    notifier = RecordingNotifier()

    with connect(db_path) as conn:
        apply_migrations(conn)
        report = run_watch(
            conn=conn,
            context=context,
            sources=(source,),
            profile_id="balanced",
            notifier=notifier,
            run_id="run-full",
            now=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )

        run_row = RunRepo(conn).get_run(report.run_id)
        assert run_row is not None
        source_counts = _source_counts(run_row.counts_json, "stub")
        assert source_counts["count"] == 2
        assert source_counts["status"] == "ok"

        score_rows = conn.execute("SELECT COUNT(*) FROM score").fetchone()
        assert score_rows is not None
        assert int(score_rows[0]) == 2


def test_one_source_failure_does_not_abort_other_sources(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    context = _build_context(
        source_flags={
            "good": {"enabled": True, "load_bearing": False},
            "bad": {"enabled": True, "load_bearing": False},
        }
    )
    good_source = ScriptedSource(
        name="good",
        records=(_make_record("good", 1, minutes=1),),
    )
    bad_source = ScriptedSource(
        name="bad",
        records=(),
        discover_error=RuntimeError("boom"),
    )

    with connect(db_path) as conn:
        apply_migrations(conn)
        report = run_watch(
            conn=conn,
            context=context,
            sources=(good_source, bad_source),
            profile_id="balanced",
            run_id="run-isolated",
            now=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )

        assert report.source_reports["good"].status == "ok"
        assert report.source_reports["good"].count == 1
        assert report.source_reports["bad"].status == "failed"
        assert report.source_reports["bad"].count == 0

        run_row = RunRepo(conn).get_run(report.run_id)
        assert run_row is not None
        assert _source_counts(run_row.counts_json, "good")["count"] == 1
        assert _source_counts(run_row.counts_json, "bad")["status"] == "failed"


def test_load_bearing_zero_triggers_alert_non_load_bearing_zero_does_not(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "nostos.db"
    context = _build_context(
        source_flags={
            "lb_zero": {"enabled": True, "load_bearing": True},
            "non_lb_zero": {"enabled": True, "load_bearing": False},
        }
    )
    notifier = RecordingNotifier()
    lb_source = ScriptedSource(name="lb_zero", records=())
    non_lb_source = ScriptedSource(name="non_lb_zero", records=())

    with connect(db_path) as conn:
        apply_migrations(conn)
        run_watch(
            conn=conn,
            context=context,
            sources=(lb_source, non_lb_source),
            profile_id="balanced",
            notifier=notifier,
            run_id="run-alerts",
            now=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )

    joined = "\n".join(message.body for message in notifier.messages)
    assert "lb_zero" in joined
    assert "load-bearing" in joined
    assert "non_lb_zero" not in joined


def test_check_liveness_exception_is_isolated_per_source(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    context = _build_context(
        source_flags={
            "good": {"enabled": True, "load_bearing": False},
            "liveness_broken": {"enabled": True, "load_bearing": False},
        }
    )
    good_source = ScriptedSource(
        name="good",
        records=(_make_record("good", 1, minutes=1),),
    )
    liveness_broken_source = ScriptedSource(
        name="liveness_broken",
        records=(_make_record("liveness_broken", 1, minutes=2),),
        check_liveness_error=RuntimeError("liveness exploded"),
    )

    with connect(db_path) as conn:
        apply_migrations(conn)
        report = run_watch(
            conn=conn,
            context=context,
            sources=(good_source, liveness_broken_source),
            profile_id="balanced",
            run_id="run-liveness-isolated",
            now=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )

        assert report.source_reports["good"].status == "ok"
        assert report.source_reports["good"].count == 1
        assert report.source_reports["liveness_broken"].status == "degraded"
        assert report.source_reports["liveness_broken"].count == 0

        run_row = RunRepo(conn).get_run(report.run_id)
        assert run_row is not None
        assert run_row.finished_at is not None
        assert _source_counts(run_row.counts_json, "good")["count"] == 1


def test_collect_sources_isolates_future_result_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "nostos.db"
    context = _build_context(
        source_flags={
            "good": {"enabled": True, "load_bearing": False},
            "bad": {"enabled": True, "load_bearing": False},
        }
    )
    good_source = ScriptedSource(
        name="good",
        records=(_make_record("good", 1, minutes=1),),
    )
    bad_source = ScriptedSource(
        name="bad",
        records=(_make_record("bad", 1, minutes=2),),
    )
    original = runner_module._collect_source_snapshot

    def crashing_collect(*, source: Any, context: Any) -> Any:
        if source.name == "bad":
            raise RuntimeError("thread snapshot crash")
        return original(source=source, context=context)

    monkeypatch.setattr(runner_module, "_collect_source_snapshot", crashing_collect)

    with connect(db_path) as conn:
        apply_migrations(conn)
        report = run_watch(
            conn=conn,
            context=context,
            sources=(good_source, bad_source),
            profile_id="balanced",
            run_id="run-future-isolated",
            now=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )

        assert report.source_reports["good"].status == "ok"
        assert report.source_reports["good"].count == 1
        assert report.source_reports["bad"].status == "failed"
        assert report.source_reports["bad"].count == 0


def test_notify_failure_does_not_leave_run_unfinished(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    context = _build_context(
        source_flags={
            "stub": {"enabled": True, "load_bearing": False},
        }
    )
    source = ScriptedSource(
        name="stub",
        records=(_make_record("stub", 1, minutes=1),),
    )

    class FailingNotifier:
        def send(self, *, title: str, body: str) -> None:
            del title, body
            raise RuntimeError("sink outage")

    with connect(db_path) as conn:
        apply_migrations(conn)
        report = run_watch(
            conn=conn,
            context=context,
            sources=(source,),
            profile_id="balanced",
            notifier=FailingNotifier(),
            run_id="run-notify-fails",
            now=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )

        run_row = RunRepo(conn).get_run(report.run_id)
        assert run_row is not None
        assert run_row.finished_at is not None


def test_per_source_counts_are_written_to_run_row(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    context = _build_context(
        source_flags={
            "alpha": {"enabled": True, "load_bearing": False},
            "beta": {"enabled": True, "load_bearing": False},
        }
    )
    alpha = ScriptedSource(name="alpha", records=(_make_record("alpha", 1, minutes=1),))
    beta = ScriptedSource(
        name="beta",
        records=(
            _make_record("beta", 1, minutes=1),
            _make_record("beta", 2, minutes=2),
            _make_record("beta", 3, minutes=3),
        ),
    )

    with connect(db_path) as conn:
        apply_migrations(conn)
        report = run_watch(
            conn=conn,
            context=context,
            sources=(alpha, beta),
            profile_id="balanced",
            run_id="run-counts",
            now=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )
        run_row = RunRepo(conn).get_run(report.run_id)
        assert run_row is not None
        assert _source_counts(run_row.counts_json, "alpha")["count"] == 1
        assert _source_counts(run_row.counts_json, "beta")["count"] == 3


def test_watermark_does_not_advance_when_count_is_far_below_baseline(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    source_name = "steady"
    context = _build_context(
        source_flags={
            source_name: {"enabled": True, "load_bearing": False},
        }
    )
    previous_watermark = "2026-01-02T00:10:00+00:00"

    with connect(db_path) as conn:
        apply_migrations(conn)
        run_repo = RunRepo(conn)
        for index in range(3):
            run_id = f"historical-{index}"
            started_at = datetime(2026, 1, 1, 0, 0, index, tzinfo=UTC)
            counts_json = cast(
                dict[str, Any],
                {
                "sources": {
                    source_name: {
                        "count": 10,
                        "status": "ok",
                        "watermark": {
                            "advanced": True,
                            "effective": previous_watermark,
                        },
                    }
                }
                },
            )
            run_repo.create_run(
                run_id=run_id,
                started_at=started_at,
                sources_json={"sources": [source_name]},
                counts_json=counts_json,
            )
            run_repo.finish_run(
                run_id=run_id,
                finished_at=started_at + timedelta(minutes=1),
                counts_json=counts_json,
            )

        source = ScriptedSource(
            name=source_name,
            records=(
                _make_record(
                    source_name,
                    1,
                    minutes=30,
                    fetched_at=datetime(2026, 1, 2, 1, 0, 0, tzinfo=UTC),
                ),
            ),
        )
        report = run_watch(
            conn=conn,
            context=context,
            sources=(source,),
            profile_id="balanced",
            run_id="run-watermark",
            now=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )

        source_report = report.source_reports[source_name]
        assert source_report.within_baseline_band is False
        assert source_report.watermark_advanced is False
        assert source_report.effective_watermark == previous_watermark

        run_row = RunRepo(conn).get_run(report.run_id)
        assert run_row is not None
        source_counts = _source_counts(run_row.counts_json, source_name)
        assert source_counts["watermark"]["advanced"] is False
        assert source_counts["watermark"]["effective"] == previous_watermark


def test_watermark_advances_when_within_band_and_candidate_is_newer(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    source_name = "steady"
    context = _build_context(
        source_flags={
            source_name: {"enabled": True, "load_bearing": False},
        }
    )
    previous_watermark = "2026-01-02T00:10:00+00:00"
    new_watermark = "2026-01-02T01:00:00+00:00"

    with connect(db_path) as conn:
        apply_migrations(conn)
        run_repo = RunRepo(conn)
        for index in range(3):
            run_id = f"historical-ok-{index}"
            started_at = datetime(2026, 1, 1, 0, 0, index, tzinfo=UTC)
            counts_json = cast(
                dict[str, Any],
                {
                "sources": {
                    source_name: {
                        "count": 10,
                        "status": "ok",
                        "watermark": {
                            "advanced": True,
                            "effective": previous_watermark,
                        },
                    }
                }
                },
            )
            run_repo.create_run(
                run_id=run_id,
                started_at=started_at,
                sources_json={"sources": [source_name]},
                counts_json=counts_json,
            )
            run_repo.finish_run(
                run_id=run_id,
                finished_at=started_at + timedelta(minutes=1),
                counts_json=counts_json,
            )

        source = ScriptedSource(
            name=source_name,
            records=(
                _make_record(
                    source_name,
                    1,
                    minutes=30,
                    fetched_at=datetime.fromisoformat(new_watermark),
                ),
                _make_record(
                    source_name,
                    2,
                    minutes=31,
                    fetched_at=datetime.fromisoformat(new_watermark),
                ),
                _make_record(
                    source_name,
                    3,
                    minutes=32,
                    fetched_at=datetime.fromisoformat(new_watermark),
                ),
                _make_record(
                    source_name,
                    4,
                    minutes=33,
                    fetched_at=datetime.fromisoformat(new_watermark),
                ),
                _make_record(
                    source_name,
                    5,
                    minutes=34,
                    fetched_at=datetime.fromisoformat(new_watermark),
                ),
                _make_record(
                    source_name,
                    6,
                    minutes=35,
                    fetched_at=datetime.fromisoformat(new_watermark),
                ),
                _make_record(
                    source_name,
                    7,
                    minutes=36,
                    fetched_at=datetime.fromisoformat(new_watermark),
                ),
                _make_record(
                    source_name,
                    8,
                    minutes=37,
                    fetched_at=datetime.fromisoformat(new_watermark),
                ),
                _make_record(
                    source_name,
                    9,
                    minutes=38,
                    fetched_at=datetime.fromisoformat(new_watermark),
                ),
                _make_record(
                    source_name,
                    10,
                    minutes=39,
                    fetched_at=datetime.fromisoformat(new_watermark),
                ),
            ),
        )
        report = run_watch(
            conn=conn,
            context=context,
            sources=(source,),
            profile_id="balanced",
            run_id="run-watermark-advance",
            now=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )

        source_report = report.source_reports[source_name]
        assert source_report.within_baseline_band is True
        assert source_report.watermark_advanced is True
        assert source_report.effective_watermark == new_watermark

        run_row = RunRepo(conn).get_run(report.run_id)
        assert run_row is not None
        source_counts = _source_counts(run_row.counts_json, source_name)
        assert source_counts["watermark"]["advanced"] is True
        assert source_counts["watermark"]["effective"] == new_watermark


def test_second_html_watch_skips_detail_for_seen_source_ids(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    context = _build_craigslist_context()
    fixture_dir = Path(__file__).resolve().parents[1] / "fixtures" / "craigslist"
    search_html = (fixture_dir / "search_results.html").read_text(encoding="utf-8")
    blocked_rss_html = (fixture_dir / "rss_blocked.html").read_text(encoding="utf-8")
    detail_html = _detail_html_with_price(
        (fixture_dir / "detail.html").read_text(encoding="utf-8"),
        "$2,950",
    )
    detailed_urls: list[str] = []

    def fetch_text(url: str) -> str:
        if "format=rss" in url:
            return blocked_rss_html
        if "/search/van/apa?" in url:
            return search_html
        if "/view/d/" in url:
            detailed_urls.append(url)
            return detail_html
        raise AssertionError(f"unexpected craigslist fixture URL: {url}")

    source = CraigslistSource(
        fetch_text=fetch_text,
        now=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )

    with connect(db_path) as conn:
        apply_migrations(conn)
        run_watch(
            conn=conn,
            context=context,
            sources=(source,),
            profile_id="balanced",
            run_id="run-craigslist-first",
            now=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )
        assert len(detailed_urls) == 2
        first_signature = _listing_source_signature(
            conn,
            source="craigslist",
            source_id="AbC123xYz9",
        )
        assert _projected_rent_amount(conn, listing_id="craigslist:AbC123xYz9") == Decimal("2950")
        assert (
            _count_observations(
                conn,
                listing_id="craigslist:AbC123xYz9",
                field="rent",
                origin=Origin.SOURCE_FIELD.value,
            )
            == 1
        )

        second_report = run_watch(
            conn=conn,
            context=context,
            sources=(source,),
            profile_id="balanced",
            run_id="run-craigslist-second",
            now=lambda: datetime(2026, 1, 2, 9, 4, 5, tzinfo=UTC),
        )
        assert len(detailed_urls) == 2
        assert second_report.source_reports["craigslist"].count == 2
        assert _projected_rent_amount(conn, listing_id="craigslist:AbC123xYz9") == Decimal("2950")
        assert (
            _count_observations(
                conn,
                listing_id="craigslist:AbC123xYz9",
                field="rent",
                origin=Origin.SOURCE_FIELD.value,
            )
            == 1
        )
        assert (
            _count_source_records(
                conn,
                source="craigslist",
                source_id="AbC123xYz9",
            )
            == 1
        )
        assert (
            _listing_source_signature(conn, source="craigslist", source_id="AbC123xYz9")
            == first_signature
        )


def test_second_html_watch_fetches_detail_for_unseen_ids(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    context = _build_craigslist_context()
    fixture_dir = Path(__file__).resolve().parents[1] / "fixtures" / "craigslist"
    search_html = (fixture_dir / "search_results.html").read_text(encoding="utf-8")
    search_html_run2 = search_html.replace(
        "</ul>",
        """
      <li class="cl-static-search-result" title="New 2BR only on run 2">
        <a href="https://www.craigslist.org/view/d/vancouver-new-2br/NewRun2xY1">
          <div class="title">New 2BR only on run 2</div>
          <div class="details">
            <div class="price">$2,600</div>
            <div class="location">Vancouver Downtown</div>
          </div>
        </a>
      </li>
    </ul>
        """,
    )
    blocked_rss_html = (fixture_dir / "rss_blocked.html").read_text(encoding="utf-8")
    detail_html = _detail_html_with_price(
        (fixture_dir / "detail.html").read_text(encoding="utf-8"),
        "$2,950",
    )
    detail_html_new_listing = _detail_html_with_price(
        (fixture_dir / "detail.html").read_text(encoding="utf-8"),
        "$3,100",
    )
    detailed_urls: list[str] = []
    search_requests = 0

    def fetch_text(url: str) -> str:
        nonlocal search_requests
        if "format=rss" in url:
            return blocked_rss_html
        if "/search/van/apa?" in url:
            search_requests += 1
            return search_html if search_requests == 1 else search_html_run2
        if "/view/d/" in url:
            detailed_urls.append(url)
            if url.endswith("/NewRun2xY1"):
                return detail_html_new_listing
            return detail_html
        raise AssertionError(f"unexpected craigslist fixture URL: {url}")

    source = CraigslistSource(
        fetch_text=fetch_text,
        now=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )

    with connect(db_path) as conn:
        apply_migrations(conn)
        run_watch(
            conn=conn,
            context=context,
            sources=(source,),
            profile_id="balanced",
            run_id="run-craigslist-first",
            now=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )
        assert len(detailed_urls) == 2

        second_report = run_watch(
            conn=conn,
            context=context,
            sources=(source,),
            profile_id="balanced",
            run_id="run-craigslist-second",
            now=lambda: datetime(2026, 1, 2, 9, 4, 5, tzinfo=UTC),
        )
        assert len(detailed_urls) == 3
        assert detailed_urls[-1].endswith("/NewRun2xY1")
        assert second_report.source_reports["craigslist"].count == 3
        assert _projected_rent_amount(conn, listing_id="craigslist:NewRun2xY1") == Decimal("3100")
        assert _count_source_records(conn, source="craigslist", source_id="NewRun2xY1") == 1


def test_seen_ids_with_posted_timestamp_still_fetch_detail(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    source_name = "scripted"
    context = _build_context(
        source_flags={
            source_name: {"enabled": True, "load_bearing": False},
        }
    )
    source = ScriptedSource(
        name=source_name,
        records=(
            _make_record(
                source_name,
                1,
                minutes=1,
                posted="2026-01-02T00:01:00Z",
            ),
        ),
    )

    with connect(db_path) as conn:
        apply_migrations(conn)
        run_watch(
            conn=conn,
            context=context,
            sources=(source,),
            profile_id="balanced",
            run_id="run-scripted-first",
            now=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )
        run_watch(
            conn=conn,
            context=context,
            sources=(source,),
            profile_id="balanced",
            run_id="run-scripted-second",
            now=lambda: datetime(2026, 1, 2, 9, 4, 5, tzinfo=UTC),
        )

        assert source.fetch_detail_calls == 2


def test_cross_source_duplicate_collapses_to_one_listing_row(tmp_path: Path) -> None:
    """Same physical unit posted on two sites: one listing, two listing_source rows."""
    db_path = tmp_path / "nostos.db"
    context = _build_context(
        source_flags={
            "alpha": {"enabled": True, "load_bearing": False},
            "beta": {"enabled": True, "load_bearing": False},
        }
    )
    shared_signature = "1234 west 10th|2950"
    alpha = ScriptedSource(
        name="alpha",
        records=(_make_record("alpha", 1, minutes=1, signature=shared_signature),),
    )
    beta = ScriptedSource(
        name="beta",
        records=(_make_record("beta", 1, minutes=2, signature=shared_signature),),
    )
    notifier = RecordingNotifier()

    with connect(db_path) as conn:
        apply_migrations(conn)
        run_watch(
            conn=conn,
            context=context,
            sources=(alpha, beta),
            profile_id="balanced",
            notifier=notifier,
            run_id="run-cross-source",
            now=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )

        listing_rows = conn.execute("SELECT id FROM listing").fetchall()
        assert len(listing_rows) == 1
        canonical_id = str(listing_rows[0]["id"])

        source_rows = conn.execute(
            "SELECT source, source_id FROM listing_source WHERE listing_id = ? ORDER BY source",
            (canonical_id,),
        ).fetchall()
        assert [(str(row["source"]), str(row["source_id"])) for row in source_rows] == [
            ("alpha", "alpha-1"),
            ("beta", "beta-1"),
        ]

        assert int(conn.execute("SELECT COUNT(*) FROM score").fetchone()[0]) == 1

        # Both sources' rent observations are retained (provenance preserved),
        # not one overwriting the other.
        rent_origins = conn.execute(
            "SELECT origin FROM observation WHERE listing_id = ? AND field = 'rent' ORDER BY id",
            (canonical_id,),
        ).fetchall()
        assert len(rent_origins) == 2

        # The merged pair produces exactly one "new listing" notification line,
        # not one per source.
        new_listing_lines = [
            line
            for message in notifier.messages
            if message.title == "Nostos new listings"
            for line in message.body.splitlines()
            if line.startswith("- ")
        ]
        assert len(new_listing_lines) == 1
        assert canonical_id in new_listing_lines[0]


def test_distinct_signatures_stay_separate(tmp_path: Path) -> None:
    """Two genuinely different units, even from different sources, do not merge."""
    db_path = tmp_path / "nostos.db"
    context = _build_context(
        source_flags={
            "alpha": {"enabled": True, "load_bearing": False},
            "beta": {"enabled": True, "load_bearing": False},
        }
    )
    alpha = ScriptedSource(
        name="alpha",
        records=(_make_record("alpha", 1, minutes=1, signature="1234 west 10th|2950"),),
    )
    beta = ScriptedSource(
        name="beta",
        records=(_make_record("beta", 1, minutes=2, signature="5678 east 5th|1800"),),
    )

    with connect(db_path) as conn:
        apply_migrations(conn)
        run_watch(
            conn=conn,
            context=context,
            sources=(alpha, beta),
            profile_id="balanced",
            run_id="run-distinct",
            now=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )

        listing_rows = conn.execute("SELECT id FROM listing ORDER BY id").fetchall()
        assert [str(row["id"]) for row in listing_rows] == ["alpha:alpha-1", "beta:beta-1"]
        assert int(conn.execute("SELECT COUNT(*) FROM score").fetchone()[0]) == 2


def test_cross_source_dedupe_holds_across_two_runs(tmp_path: Path) -> None:
    """A duplicate arriving from a second source in a later run adopts the
    canonical id already recorded for that signature, rather than creating a
    second listing row."""
    db_path = tmp_path / "nostos.db"
    context = _build_context(
        source_flags={
            "alpha": {"enabled": True, "load_bearing": False},
            "beta": {"enabled": True, "load_bearing": False},
        }
    )
    shared_signature = "1234 west 10th|2950"
    alpha = ScriptedSource(
        name="alpha",
        records=(_make_record("alpha", 1, minutes=1, signature=shared_signature),),
    )
    beta = ScriptedSource(
        name="beta",
        records=(_make_record("beta", 1, minutes=2, signature=shared_signature),),
    )

    with connect(db_path) as conn:
        apply_migrations(conn)
        run_watch(
            conn=conn,
            context=context,
            sources=(alpha,),
            profile_id="balanced",
            run_id="run-first-source-only",
            now=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )
        run_watch(
            conn=conn,
            context=context,
            sources=(beta,),
            profile_id="balanced",
            run_id="run-second-source-arrives",
            now=lambda: datetime(2026, 1, 3, 3, 4, 5, tzinfo=UTC),
        )

        listing_rows = conn.execute("SELECT id FROM listing").fetchall()
        assert len(listing_rows) == 1
        canonical_id = str(listing_rows[0]["id"])
        assert canonical_id == "alpha:alpha-1"

        source_rows = conn.execute(
            "SELECT source FROM listing_source WHERE listing_id = ? ORDER BY source",
            (canonical_id,),
        ).fetchall()
        assert [str(row["source"]) for row in source_rows] == ["alpha", "beta"]


def test_same_source_signature_collision_does_not_merge(tmp_path: Path) -> None:
    """Known limitation, made explicit: the signature is address tokens plus a
    coarse price bucket, so two different units in the same building at the
    same rent (e.g. #305 and #410, both $2950) hash identically. Merging is
    restricted to cross-source matches (see `_canonicalize_listings`), so two
    listings from the *same* source that collide on signature are kept apart
    rather than being silently folded into one — the safe failure mode, even
    though it means a genuine same-source repost under a new source_id also
    will not be merged by this mechanism."""
    db_path = tmp_path / "nostos.db"
    context = _build_context(
        source_flags={
            "alpha": {"enabled": True, "load_bearing": False},
        }
    )
    shared_signature = "1234 west 10th|2950"
    alpha = ScriptedSource(
        name="alpha",
        records=(
            _make_record("alpha", 1, minutes=1, signature=shared_signature),
            _make_record("alpha", 2, minutes=2, signature=shared_signature),
        ),
    )

    with connect(db_path) as conn:
        apply_migrations(conn)
        run_watch(
            conn=conn,
            context=context,
            sources=(alpha,),
            profile_id="balanced",
            run_id="run-same-source-collision",
            now=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )

        listing_rows = conn.execute("SELECT id FROM listing ORDER BY id").fetchall()
        assert [str(row["id"]) for row in listing_rows] == ["alpha:alpha-1", "alpha:alpha-2"]


class ScriptedSource:
    def __init__(
        self,
        *,
        name: str,
        records: tuple[SourceRecord, ...],
        discover_error: Exception | None = None,
        check_liveness_error: Exception | None = None,
    ) -> None:
        self.name = name
        self.capabilities = Capabilities(
            requires_credentials=False,
            supports_detail_fetch=False,
            requires_browser=False,
            rate_limit_per_minute=60.0,
        )
        self._records = records
        self._discover_error = discover_error
        self._check_liveness_error = check_liveness_error
        self.fetch_detail_calls = 0

    def discover(self, ctx: SearchContext) -> Iterator[SourceRecord]:
        del ctx
        if self._discover_error is not None:
            raise self._discover_error
        yield from self._records

    def fetch_detail(self, rec: SourceRecord) -> SourceRecord:
        self.fetch_detail_calls += 1
        return rec

    def check_liveness(self, rec: SourceRecord) -> Liveness:
        if self._check_liveness_error is not None:
            raise self._check_liveness_error
        marker = rec.payload.get("liveness") if isinstance(rec.payload, dict) else None
        if marker in {"ok", "degraded", "failed"}:
            return Liveness(str(marker))
        return Liveness.OK

    def to_listing(self, rec: SourceRecord, ctx: SearchContext) -> Listing:
        payload = _payload_mapping(rec.payload)
        observed_at = rec.fetched_at
        rent_amount = Decimal(str(payload.get("rent", 2000)))
        signature = str(payload.get("signature") or f"sig:{self.name}:{rec.source_id}")
        return Listing(
            identity=Identity(
                listing_id=f"{self.name}:{rec.source_id}",
                source=self.name,
                source_id=rec.source_id,
                url=rec.url,
                signature=signature,
            ),
            place=Place(
                raw_address=str(payload.get("address", "123 Example St")),
                structured=None,
                point=None,
                area_key=None,
            ),
            rent=Observed(
                value=Money(
                    amount=rent_amount,
                    currency=ctx.citypack.locale.currency,
                    period="month",
                ),
                origin=Origin.SOURCE_FIELD,
                confidence=1.0,
                evidence="stub rent",
                observed_at=observed_at,
            ),
            beds=Observed(
                value=float(payload.get("beds", 1.0)),
                origin=Origin.SOURCE_FIELD,
                confidence=1.0,
                evidence="stub beds",
                observed_at=observed_at,
            ),
            baths=Observed(
                value=float(payload.get("baths", 1.0)),
                origin=Origin.SOURCE_FIELD,
                confidence=1.0,
                evidence="stub baths",
                observed_at=observed_at,
            ),
            area=Absence.NOT_STATED,
            floor=Absence.NOT_STATED,
            parking=Absence.NOT_STATED,
            furnishing=Absence.NOT_STATED,
            photos=[],
            attributes={},
            raw_ref=rec.to_ref(),
            schema_version=1,
        )


def _make_record(
    source: str,
    suffix: int,
    *,
    minutes: int,
    fetched_at: datetime | None = None,
    posted: str | None = None,
    signature: str | None = None,
) -> SourceRecord:
    timestamp = fetched_at or datetime(2026, 1, 2, 0, minutes, 0, tzinfo=UTC)
    payload: dict[str, Any] = {
        "title": f"{source} listing {suffix}",
        "address": f"{suffix} Example St",
        "rent": 2000 + suffix,
        "beds": 1,
        "baths": 1,
        "liveness": "ok",
    }
    if posted is not None:
        payload["posted"] = posted
    if signature is not None:
        payload["signature"] = signature
    return SourceRecord(
        source=source,
        source_id=f"{source}-{suffix}",
        url=f"https://example.test/{source}/{suffix}",
        content_hash=f"{source}-{suffix}",
        fetched_at=timestamp,
        payload=payload,
    )


def _build_context(
    *,
    source_flags: dict[str, dict[str, bool]],
) -> SearchContext:
    citypack_sources = {
        source_name: {
            "enabled": bool(flags["enabled"]),
            "load_bearing": bool(flags["load_bearing"]),
        }
        for source_name, flags in source_flags.items()
    }
    profile_sources = {source_name: "on" for source_name in source_flags}
    citypack = Citypack.model_validate(
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
                    "label": "Kitsilano",
                    "keywords": ["kits"],
                    "bbox": [49.262, -123.19, 49.278, -123.145],
                }
            ],
            "sources": citypack_sources,
            "address": {
                "directional": {"w": "west"},
                "strip_tokens": ["vancouver"],
                "region_tokens": ["bc"],
            },
        }
    )
    profile = Profile.model_validate(
        {
            "city": "vancouver",
            "hard": {"exclude": []},
            "weights": {},
            "sources": profile_sources,
            "notify": [],
            "schedule": "0 */6 * * *",
        }
    )
    return SearchContext(citypack=citypack, profile=profile)


def _build_craigslist_context() -> SearchContext:
    citypack = Citypack.model_validate(
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
                    "label": "Kitsilano",
                    "keywords": ["kits beach", "kitsilano"],
                    "bbox": [49.262, -123.190, 49.278, -123.145],
                }
            ],
            "sources": {
                "craigslist": {
                    "enabled": True,
                    "load_bearing": True,
                    "base_url": "https://vancouver.craigslist.org",
                    "areas": ["van"],
                }
            },
            "address": {
                "directional": {"w": "west"},
                "strip_tokens": ["vancouver"],
                "region_tokens": ["bc"],
            },
        }
    )
    profile = Profile.model_validate(
        {
            "city": "vancouver",
            "hard": {
                "rent": {"max": 3600, "currency": "CAD"},
                "beds": {"eq": 2},
                "area": {"min": 700, "unit": "sqft"},
                "exclude": [],
            },
            "weights": {},
            "sources": {"craigslist": "on"},
            "notify": [],
            "schedule": "0 */6 * * *",
        }
    )
    return SearchContext(citypack=citypack, profile=profile)


def _payload_mapping(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    raise ValueError("stub payload must be a mapping")


def _source_counts(counts_json: dict[str, Any], source_name: str) -> dict[str, Any]:
    sources = counts_json.get("sources")
    if not isinstance(sources, dict):
        raise AssertionError("run.counts_json.sources is missing or malformed")
    payload = sources.get(source_name)
    if not isinstance(payload, dict):
        raise AssertionError(f"run.counts_json.sources.{source_name} is missing or malformed")
    return payload


def _detail_html_with_price(html: str, price: str) -> str:
    return html.replace("</body>", f'<span class="price">{price}</span></body>')


def _projected_rent_amount(conn: sqlite3.Connection, *, listing_id: str) -> Decimal:
    row = conn.execute(
        "SELECT fields_json FROM listing WHERE id = ?",
        (listing_id,),
    ).fetchone()
    if row is None:
        raise AssertionError(f"missing listing row for {listing_id}")
    fields_json = json.loads(str(row["fields_json"]))
    if not isinstance(fields_json, dict):
        raise AssertionError("listing.fields_json must be a JSON object")
    rent_payload = fields_json.get("rent")
    if not isinstance(rent_payload, dict):
        raise AssertionError("listing.fields_json.rent must be present and be a JSON object")
    value_payload = rent_payload.get("value")
    if not isinstance(value_payload, dict):
        raise AssertionError("listing.fields_json.rent.value must be a JSON object")
    amount = value_payload.get("amount")
    if not isinstance(amount, str):
        raise AssertionError("listing.fields_json.rent.value.amount must be a string")
    return Decimal(amount)


def _count_observations(
    conn: sqlite3.Connection,
    *,
    listing_id: str,
    field: str,
    origin: str | None = None,
) -> int:
    if origin is None:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM observation
            WHERE listing_id = ? AND field = ?
            """,
            (listing_id, field),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM observation
            WHERE listing_id = ? AND field = ? AND origin = ?
            """,
            (listing_id, field, origin),
        ).fetchone()
    if row is None:
        return 0
    return int(row[0])


def _count_source_records(conn: sqlite3.Connection, *, source: str, source_id: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM source_record
        WHERE source = ? AND source_id = ?
        """,
        (source, source_id),
    ).fetchone()
    if row is None:
        return 0
    return int(row[0])


def _listing_source_signature(conn: sqlite3.Connection, *, source: str, source_id: str) -> str:
    row = conn.execute(
        """
        SELECT signature
        FROM listing_source
        WHERE source = ? AND source_id = ?
        """,
        (source, source_id),
    ).fetchone()
    if row is None:
        raise AssertionError(f"missing listing_source row for {source}:{source_id}")
    return str(row["signature"])


@dataclass(frozen=True)
class NotificationMessage:
    title: str
    body: str


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[NotificationMessage] = []

    def send(self, *, title: str, body: str) -> None:
        self.messages.append(NotificationMessage(title=title, body=body))

