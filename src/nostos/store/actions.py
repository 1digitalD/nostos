"""Listing action log: star, dismiss, contacted, note.

Append-only history of human-driven actions taken from the local web UI.
Stored in the `listing_action` table introduced by migration 0002.

The web UI is the only writer in v0.2.0; the MCP server continues to expose
the same ranked listings via the existing `list` and `explain` tools.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

ActionKind = Literal["star", "dismiss", "contacted", "note"]

_KIND_VALUES: tuple[str, ...] = ("star", "dismiss", "contacted", "note")


@dataclass(frozen=True, slots=True)
class ListingAction:
    id: int
    listing_id: str
    kind: str
    note: str | None
    created_at: datetime


class ActionRepo:
    """Repository for `listing_action` rows."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # Non-note kinds are state flags, not a history. Re-recording the same
    # (listing_id, kind) is a no-op so the UI can submit idempotently without
    # producing duplicate rows. Notes remain append-only.
    _FLAG_KINDS: frozenset[str] = frozenset({"star", "dismiss", "contacted"})

    def record_action(
        self,
        *,
        listing_id: str,
        kind: ActionKind,
        note: str | None = None,
        created_at: datetime | None = None,
    ) -> int:
        if kind not in _KIND_VALUES:
            msg = f"unknown listing_action kind: {kind!r}"
            raise ValueError(msg)
        if kind in self._FLAG_KINDS:
            existing = self._existing_id(listing_id=listing_id, kind=kind)
            if existing is not None:
                return existing
        timestamp = created_at or datetime.now(tz=UTC)
        cursor = self._conn.execute(
            """
            INSERT INTO listing_action(listing_id, kind, note, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (listing_id, kind, note, timestamp.isoformat()),
        )
        inserted_id = cursor.lastrowid
        if inserted_id is None:
            msg = "listing_action insert did not return a row id"
            raise RuntimeError(msg)
        return int(inserted_id)

    def _existing_id(self, *, listing_id: str, kind: str) -> int | None:
        row = self._conn.execute(
            """
            SELECT id FROM listing_action
            WHERE listing_id = ? AND kind = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (listing_id, kind),
        ).fetchone()
        return int(row["id"]) if row is not None else None

    def get_actions(self, *, listing_id: str) -> list[ListingAction]:
        rows = self._conn.execute(
            """
            SELECT id, listing_id, kind, note, created_at
            FROM listing_action
            WHERE listing_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (listing_id,),
        ).fetchall()
        return [_row_to_action(row) for row in rows]

    def has_action(self, *, listing_id: str, kind: str) -> bool:
        if kind not in _KIND_VALUES:
            return False
        row = self._conn.execute(
            """
            SELECT 1
            FROM listing_action
            WHERE listing_id = ? AND kind = ?
            LIMIT 1
            """,
            (listing_id, kind),
        ).fetchone()
        return row is not None

    def action_states_for(
        self, *, listing_ids: tuple[str, ...]
    ) -> dict[str, dict[str, bool]]:
        """Return per-listing flag-kind state for many listings in one query."""

        if not listing_ids:
            return {}
        placeholders = ",".join("?" for _ in listing_ids)
        rows = self._conn.execute(
            f"""
            SELECT listing_id, kind
            FROM listing_action
            WHERE listing_id IN ({placeholders})
              AND kind IN ('star', 'dismiss', 'contacted')
            """,
            listing_ids,
        ).fetchall()
        states: dict[str, dict[str, bool]] = {}
        for row in rows:
            states.setdefault(str(row["listing_id"]), {})[str(row["kind"])] = True
        return states


def _row_to_action(row: sqlite3.Row) -> ListingAction:
    raw_created = row["created_at"]
    created_at = (
        datetime.fromisoformat(str(raw_created))
        if isinstance(raw_created, str)
        else datetime.now(tz=UTC)
    )
    raw_note = row["note"]
    note_value: str | None
    if isinstance(raw_note, str):
        stripped = raw_note.strip()
        note_value = stripped if stripped else None
    else:
        note_value = None
    return ListingAction(
        id=int(row["id"]),
        listing_id=str(row["listing_id"]),
        kind=str(row["kind"]),
        note=note_value,
        created_at=created_at,
    )
