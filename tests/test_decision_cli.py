from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

import nostos.cli as cli_module
from nostos.model import Origin
from nostos.store.db import apply_migrations, connect
from nostos.store.repo import ListingRepo, ObservationRepo
from tests.test_cli import StubSource, _record, _write_citypack, _write_profile


def _saved_listing_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    source = StubSource(records=(_record("decision", in_suite_laundry=True),))
    monkeypatch.setattr(cli_module, "SOURCE_FACTORIES", {"stub": lambda: source})
    citypack_path = _write_citypack(tmp_path / "citypack.yaml", source_name="stub")
    profile_path = _write_profile(
        tmp_path / "profile.yaml", laundry_weight=1.0, source_name="stub"
    )
    db_path = tmp_path / "nostos.db"
    record = _record("decision", in_suite_laundry=True)
    with connect(db_path) as conn:
        apply_migrations(conn)
        ListingRepo(conn).add_source_record(listing_id="stub:decision", record=record)
    with connect(db_path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return db_path, profile_path, citypack_path


def test_decision_json_reads_saved_listing_read_only_without_migrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, profile_path, citypack_path = _saved_listing_workspace(tmp_path, monkeypatch)
    before = db_path.read_bytes()
    monkeypatch.setattr(
        cli_module,
        "apply_migrations",
        lambda *_args, **_kwargs: pytest.fail("decision must not run migrations"),
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "decision",
            "stub:decision",
            "--profile",
            str(profile_path),
            "--db",
            str(db_path),
            "--citypack",
            str(citypack_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "unverified"
    assert payload["headline"] == "Confirm essentials first"
    assert payload["stale"] is True
    assert payload["captured_at"] == "2026-01-02 03:04:05+00:00"
    assert payload["checks"]
    assert db_path.read_bytes() == before

    with cli_module._connect_read_only(db_path) as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO listing(id) VALUES ('should-not-write')")


def test_decision_json_uses_corrected_current_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, profile_path, citypack_path = _saved_listing_workspace(tmp_path, monkeypatch)
    with connect(db_path) as conn:
        ObservationRepo(conn).record_observation(
            listing_id="stub:decision",
            field="rent",
            value_json={"amount": "5000", "currency": "CAD", "period": "month"},
            origin=Origin.USER,
            confidence=1.0,
            evidence="corrected by renter",
            observed_at=datetime(2026, 1, 3, tzinfo=UTC),
        )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "decision",
            "stub:decision",
            "--profile",
            str(profile_path),
            "--db",
            str(db_path),
            "--citypack",
            str(citypack_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "miss"
    assert payload["blockers"] == ["rent $5,000 > max $3,200"]
