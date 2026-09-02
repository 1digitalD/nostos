from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from nostos.model.listing import Origin
from nostos.model.source_record import JSONValue
from nostos.store.db import apply_migrations, connect
from nostos.store.repo import ListingRepo, ObservationRepo, ScoreRepo


def test_migration_applies_to_empty_db_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    with connect(db_path) as conn:
        applied = apply_migrations(conn)
        assert applied == [1, 2, 3]
        assert apply_migrations(conn) == []

        table_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            ).fetchall()
        }

    assert {
        "schema_migration",
        "listing",
        "observation",
        "source_record",
        "listing_source",
        "score",
        "user_state",
        "run",
        "listing_action",
    }.issubset(table_names)


def test_listing_action_table_accepts_record_and_check_constraint(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    with connect(db_path) as conn:
        apply_migrations(conn)
        listing_repo = ListingRepo(conn)
        listing_repo.ensure_listing("listing-1")
        observed_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        conn.execute(
            """
            INSERT INTO listing_action(listing_id, kind, note, created_at)
            VALUES (?, ?, ?, ?)
            """,
            ("listing-1", "star", None, observed_at.isoformat()),
        )
        conn.execute(
            """
            INSERT INTO listing_action(listing_id, kind, note, created_at)
            VALUES (?, ?, ?, ?)
            """,
            ("listing-1", "note", "looked promising", observed_at.isoformat()),
        )

        rows = conn.execute(
            """
            SELECT listing_id, kind, note, created_at
            FROM listing_action
            ORDER BY id ASC
            """
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["kind"] == "star"
        assert rows[1]["kind"] == "note"
        assert rows[1]["note"] == "looked promising"

        # CHECK constraint should reject unknown kind values.
        import sqlite3 as _sqlite3

        with pytest.raises(_sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO listing_action(listing_id, kind, note, created_at)
                VALUES (?, ?, ?, ?)
                """,
                ("listing-1", "bogus", None, observed_at.isoformat()),
            )


def test_observation_projection_prefers_highest_precedence_origin(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    with connect(db_path) as conn:
        apply_migrations(conn)
        listing_repo = ListingRepo(conn)
        observation_repo = ObservationRepo(conn, listing_repo=listing_repo)

        listing_id = "listing-1"
        observed_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

        observation_repo.record_observation(
            listing_id=listing_id,
            field="beds",
            value_json=1.0,
            origin=Origin.TEXT_RULE,
            confidence=0.65,
            evidence="1 bed in description",
            observed_at=observed_at,
        )
        observation_repo.record_observation(
            listing_id=listing_id,
            field="beds",
            value_json=2.0,
            origin=Origin.SOURCE_FIELD,
            confidence=0.99,
            evidence="structured listing field",
            observed_at=observed_at,
        )

        projection = observation_repo.project_listing_fields(listing_id)
        projected_beds = cast(dict[str, JSONValue], projection["beds"])
        assert projected_beds["origin"] == Origin.SOURCE_FIELD.value
        assert projected_beds["value"] == 2.0

        projected_listing = listing_repo.get_fields_projection(listing_id)
        listing_projected_beds = cast(dict[str, JSONValue], projected_listing["beds"])
        assert listing_projected_beds["origin"] == Origin.SOURCE_FIELD.value
        assert listing_projected_beds["value"] == 2.0


def test_score_repo_keys_writes_by_profile_id(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    with connect(db_path) as conn:
        apply_migrations(conn)
        score_repo = ScoreRepo(conn)
        computed_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

        score_repo.upsert_score(
            listing_id="listing-1",
            profile_id="balanced",
            score=83.5,
            breakdown_json={"rent": 24.0},
            computed_at=computed_at,
        )
        score_repo.upsert_score(
            listing_id="listing-1",
            profile_id="aggressive",
            score=91.0,
            breakdown_json={"rent": 30.0},
            computed_at=computed_at,
        )

        balanced = score_repo.get_score("listing-1", "balanced")
        aggressive = score_repo.get_score("listing-1", "aggressive")

        assert balanced is not None
        assert aggressive is not None
        assert balanced.score == 83.5
        assert aggressive.score == 91.0

        rows = conn.execute(
            "SELECT profile_id, score, breakdown_json FROM score WHERE listing_id = ?",
            ("listing-1",),
        ).fetchall()
        stored = {
            (row[0], row[1], json.loads(row[2])["rent"])
            for row in rows
        }
        assert stored == {("balanced", 83.5, 24.0), ("aggressive", 91.0, 30.0)}
