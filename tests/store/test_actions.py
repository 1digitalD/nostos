from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nostos.store.actions import ActionRepo, ListingAction
from nostos.store.db import apply_migrations, connect
from nostos.store.repo import ListingRepo


def _seed_db(tmp_path: Path) -> tuple[Path, ActionRepo]:
    db_path = tmp_path / "nostos.db"
    with connect(db_path) as conn:
        apply_migrations(conn)
        listing_repo = ListingRepo(conn)
        listing_repo.ensure_listing("listing-1")
        listing_repo.ensure_listing("listing-2")
    return db_path, ActionRepo(connect(db_path))


def test_record_and_retrieve_actions(tmp_path: Path) -> None:
    _, repo = _seed_db(tmp_path)
    observed_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    star_id = repo.record_action(
        listing_id="listing-1",
        kind="star",
        created_at=observed_at,
    )
    assert isinstance(star_id, int)
    assert star_id > 0

    repo.record_action(
        listing_id="listing-1",
        kind="note",
        note="looks great, near skytrain",
        created_at=observed_at + timedelta(minutes=5),
    )

    actions = repo.get_actions(listing_id="listing-1")
    assert len(actions) == 2
    assert all(isinstance(item, ListingAction) for item in actions)
    # Newest first by created_at desc.
    assert actions[0].kind == "note"
    assert actions[0].note == "looks great, near skytrain"
    assert actions[1].kind == "star"
    assert actions[1].note is None


def test_get_actions_returns_empty_when_no_actions(tmp_path: Path) -> None:
    _, repo = _seed_db(tmp_path)
    assert repo.get_actions(listing_id="listing-1") == []


def test_actions_are_isolated_per_listing(tmp_path: Path) -> None:
    _, repo = _seed_db(tmp_path)
    observed_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    repo.record_action(listing_id="listing-1", kind="star", created_at=observed_at)
    repo.record_action(listing_id="listing-2", kind="dismiss", created_at=observed_at)

    listing_one = repo.get_actions(listing_id="listing-1")
    listing_two = repo.get_actions(listing_id="listing-2")
    assert [item.kind for item in listing_one] == ["star"]
    assert [item.kind for item in listing_two] == ["dismiss"]


def test_has_action_returns_true_when_present(tmp_path: Path) -> None:
    _, repo = _seed_db(tmp_path)
    observed_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    repo.record_action(listing_id="listing-1", kind="star", created_at=observed_at)

    assert repo.has_action(listing_id="listing-1", kind="star") is True
    assert repo.has_action(listing_id="listing-1", kind="dismiss") is False
    assert repo.has_action(listing_id="listing-2", kind="star") is False


def test_check_constraint_rejects_unknown_kind(tmp_path: Path) -> None:
    db_path, _ = _seed_db(tmp_path)
    with connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO listing_action(listing_id, kind, note, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    "listing-1",
                    "bogus_kind",
                    None,
                    datetime.now(tz=UTC).isoformat(),
                ),
            )


def test_multiple_actions_of_same_kind_are_allowed(tmp_path: Path) -> None:
    _, repo = _seed_db(tmp_path)
    base = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    for offset in range(3):
        repo.record_action(
            listing_id="listing-1",
            kind="star",
            created_at=base + timedelta(minutes=offset),
        )

    actions = repo.get_actions(listing_id="listing-1")
    assert len(actions) == 3
    assert all(item.kind == "star" for item in actions)
    # Newest first.
    assert actions[0].created_at > actions[-1].created_at
