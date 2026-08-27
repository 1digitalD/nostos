from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from nostos.config.citypack import Citypack
from nostos.config.profile import Profile
from nostos.context import SearchContext
from nostos.model import Absence, Identity, Listing, Money, Observed, Origin, Place, SourceRecord
from nostos.sources.base import Capabilities, Liveness
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


class ScriptedSource:
    def __init__(
        self,
        *,
        name: str,
        records: tuple[SourceRecord, ...],
        discover_error: Exception | None = None,
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

    def discover(self, ctx: SearchContext) -> Iterator[SourceRecord]:
        del ctx
        if self._discover_error is not None:
            raise self._discover_error
        yield from self._records

    def fetch_detail(self, rec: SourceRecord) -> SourceRecord:
        return rec

    def check_liveness(self, rec: SourceRecord) -> Liveness:
        marker = rec.payload.get("liveness") if isinstance(rec.payload, dict) else None
        if marker in {"ok", "degraded", "failed"}:
            return Liveness(str(marker))
        return Liveness.OK

    def to_listing(self, rec: SourceRecord, ctx: SearchContext) -> Listing:
        payload = _payload_mapping(rec.payload)
        observed_at = rec.fetched_at
        rent_amount = Decimal(str(payload.get("rent", 2000)))
        return Listing(
            identity=Identity(
                listing_id=f"{self.name}:{rec.source_id}",
                source=self.name,
                source_id=rec.source_id,
                url=rec.url,
                signature=f"sig:{self.name}:{rec.source_id}",
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
) -> SourceRecord:
    timestamp = fetched_at or datetime(2026, 1, 2, 0, minutes, 0, tzinfo=UTC)
    return SourceRecord(
        source=source,
        source_id=f"{source}-{suffix}",
        url=f"https://example.test/{source}/{suffix}",
        content_hash=f"{source}-{suffix}",
        fetched_at=timestamp,
        payload={
            "title": f"{source} listing {suffix}",
            "address": f"{suffix} Example St",
            "rent": 2000 + suffix,
            "beds": 1,
            "baths": 1,
            "liveness": "ok",
        },
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


@dataclass(frozen=True)
class NotificationMessage:
    title: str
    body: str


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[NotificationMessage] = []

    def send(self, *, title: str, body: str) -> None:
        self.messages.append(NotificationMessage(title=title, body=body))

