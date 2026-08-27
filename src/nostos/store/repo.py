from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from nostos.model.listing import Origin
from nostos.model.source_record import JSONValue, SourceRecord


def _isoformat(value: datetime | None = None) -> str:
    timestamp = value or datetime.now(tz=UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC).isoformat()


def _json_dumps(value: JSONValue) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _json_loads(value: str) -> JSONValue:
    return cast(JSONValue, json.loads(value))


def _as_json_object(value: JSONValue) -> dict[str, JSONValue]:
    if isinstance(value, dict):
        return {str(key): cast(JSONValue, item) for key, item in value.items()}
    msg = f"Expected JSON object, got {type(value)!r}"
    raise TypeError(msg)


class ListingRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def ensure_listing(
        self,
        listing_id: str,
        *,
        seen_at: datetime | None = None,
        status: str = "active",
        schema_version: int = 1,
    ) -> None:
        seen_iso = _isoformat(seen_at)
        self._conn.execute(
            """
            INSERT INTO listing(id, first_seen, last_seen, status, schema_version, fields_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_seen = excluded.last_seen,
                status = excluded.status,
                schema_version = excluded.schema_version
            """,
            (listing_id, seen_iso, seen_iso, status, schema_version, "{}"),
        )

    def replace_fields_projection(
        self,
        listing_id: str,
        fields: dict[str, JSONValue],
        *,
        seen_at: datetime | None = None,
        status: str = "active",
        schema_version: int = 1,
    ) -> None:
        self.ensure_listing(
            listing_id,
            seen_at=seen_at,
            status=status,
            schema_version=schema_version,
        )
        self._conn.execute(
            """
            UPDATE listing
            SET fields_json = ?, last_seen = ?, status = ?, schema_version = ?
            WHERE id = ?
            """,
            (
                _json_dumps(cast(JSONValue, fields)),
                _isoformat(seen_at),
                status,
                schema_version,
                listing_id,
            ),
        )

    def get_fields_projection(self, listing_id: str) -> dict[str, JSONValue]:
        row = self._conn.execute(
            "SELECT fields_json FROM listing WHERE id = ?",
            (listing_id,),
        ).fetchone()
        if row is None:
            return {}
        return _as_json_object(_json_loads(str(row["fields_json"])))

    def upsert_listing_source(
        self,
        *,
        listing_id: str,
        source: str,
        source_id: str,
        signature: str,
        seen_at: datetime | None = None,
    ) -> None:
        self.ensure_listing(listing_id, seen_at=seen_at)
        self._conn.execute(
            """
            INSERT INTO listing_source(listing_id, source, source_id, signature)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(listing_id, source, source_id) DO UPDATE SET
                signature = excluded.signature
            """,
            (listing_id, source, source_id, signature),
        )

    def add_source_record(self, *, listing_id: str, record: SourceRecord) -> int:
        self.ensure_listing(listing_id, seen_at=record.fetched_at)
        cursor = self._conn.execute(
            """
            INSERT INTO source_record(
                listing_id,
                source,
                source_id,
                url,
                payload,
                content_hash,
                fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing_id,
                record.source,
                record.source_id,
                record.url,
                _json_dumps(record.payload),
                record.content_hash,
                _isoformat(record.fetched_at),
            ),
        )
        inserted_id = cursor.lastrowid
        if inserted_id is None:
            msg = "source_record insert did not return a row id"
            raise RuntimeError(msg)
        return int(inserted_id)


class ObservationRepo:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        listing_repo: ListingRepo | None = None,
    ) -> None:
        self._conn = conn
        self._listing_repo = listing_repo or ListingRepo(conn)

        precedence_pairs = " ".join(
            f"WHEN '{origin.value}' THEN {origin.precedence}" for origin in Origin
        )
        self._origin_precedence_case_sql = f"CASE origin {precedence_pairs} ELSE 0 END"

    def record_observation(
        self,
        *,
        listing_id: str,
        field: str,
        value_json: JSONValue,
        origin: Origin,
        confidence: float,
        evidence: str | None,
        observed_at: datetime,
        status: str = "active",
        schema_version: int = 1,
    ) -> None:
        self._listing_repo.ensure_listing(
            listing_id,
            seen_at=observed_at,
            status=status,
            schema_version=schema_version,
        )
        self._conn.execute(
            """
            INSERT INTO observation(
                listing_id,
                field,
                value_json,
                origin,
                confidence,
                evidence,
                observed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing_id,
                field,
                _json_dumps(value_json),
                origin.value,
                confidence,
                evidence,
                _isoformat(observed_at),
            ),
        )

        projected_fields = self.project_listing_fields(listing_id)
        self._listing_repo.replace_fields_projection(
            listing_id,
            projected_fields,
            seen_at=observed_at,
            status=status,
            schema_version=schema_version,
        )

    def project_listing_fields(self, listing_id: str) -> dict[str, JSONValue]:
        query = f"""
            WITH ranked AS (
                SELECT
                    field,
                    value_json,
                    origin,
                    confidence,
                    evidence,
                    observed_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY field
                        ORDER BY
                            {self._origin_precedence_case_sql} DESC,
                            observed_at DESC,
                            id DESC
                    ) AS row_number
                FROM observation
                WHERE listing_id = ?
            )
            SELECT field, value_json, origin, confidence, evidence, observed_at
            FROM ranked
            WHERE row_number = 1
            ORDER BY field
        """

        rows = self._conn.execute(query, (listing_id,)).fetchall()
        projected: dict[str, JSONValue] = {}

        for row in rows:
            observed_payload: dict[str, JSONValue] = {
                "value": _json_loads(str(row["value_json"])),
                "origin": str(row["origin"]),
                "confidence": float(row["confidence"]),
                "evidence": cast(JSONValue, row["evidence"]),
                "observed_at": str(row["observed_at"]),
                "detail": cast(JSONValue, {}),
            }
            projected[str(row["field"])] = cast(JSONValue, observed_payload)

        return projected


@dataclass(frozen=True)
class ScoreRow:
    listing_id: str
    profile_id: str
    score: float
    breakdown_json: dict[str, JSONValue]
    computed_at: datetime


class ScoreRepo:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        listing_repo: ListingRepo | None = None,
    ) -> None:
        self._conn = conn
        self._listing_repo = listing_repo or ListingRepo(conn)

    def upsert_score(
        self,
        *,
        listing_id: str,
        profile_id: str,
        score: float,
        breakdown_json: dict[str, JSONValue],
        computed_at: datetime,
    ) -> None:
        self._listing_repo.ensure_listing(listing_id, seen_at=computed_at)
        self._conn.execute(
            """
            INSERT INTO score(listing_id, profile_id, score, breakdown_json, computed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(listing_id, profile_id) DO UPDATE SET
                score = excluded.score,
                breakdown_json = excluded.breakdown_json,
                computed_at = excluded.computed_at
            """,
            (
                listing_id,
                profile_id,
                score,
                _json_dumps(cast(JSONValue, breakdown_json)),
                _isoformat(computed_at),
            ),
        )

    def get_score(self, listing_id: str, profile_id: str) -> ScoreRow | None:
        row = self._conn.execute(
            """
            SELECT listing_id, profile_id, score, breakdown_json, computed_at
            FROM score
            WHERE listing_id = ? AND profile_id = ?
            """,
            (listing_id, profile_id),
        ).fetchone()
        return _row_to_score(row)


def _row_to_score(row: sqlite3.Row | None) -> ScoreRow | None:
    if row is None:
        return None
    return ScoreRow(
        listing_id=str(row["listing_id"]),
        profile_id=str(row["profile_id"]),
        score=float(row["score"]),
        breakdown_json=_as_json_object(_json_loads(str(row["breakdown_json"]))),
        computed_at=datetime.fromisoformat(str(row["computed_at"])),
    )


@dataclass(frozen=True)
class RunRow:
    id: str
    started_at: datetime
    finished_at: datetime | None
    sources_json: dict[str, JSONValue]
    counts_json: dict[str, JSONValue]
    notes: str | None


class RunRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create_run(
        self,
        *,
        run_id: str,
        started_at: datetime,
        sources_json: dict[str, JSONValue],
        counts_json: dict[str, JSONValue] | None = None,
        notes: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO run(id, started_at, finished_at, sources_json, counts_json, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                _isoformat(started_at),
                None,
                _json_dumps(cast(JSONValue, sources_json)),
                _json_dumps(cast(JSONValue, counts_json or {})),
                notes,
            ),
        )

    def finish_run(
        self,
        *,
        run_id: str,
        finished_at: datetime,
        counts_json: dict[str, JSONValue],
        notes: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE run
            SET finished_at = ?, counts_json = ?, notes = ?
            WHERE id = ?
            """,
            (
                _isoformat(finished_at),
                _json_dumps(cast(JSONValue, counts_json)),
                notes,
                run_id,
            ),
        )

    def get_run(self, run_id: str) -> RunRow | None:
        row = self._conn.execute(
            """
            SELECT id, started_at, finished_at, sources_json, counts_json, notes
            FROM run
            WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        finished_value = row["finished_at"]
        return RunRow(
            id=str(row["id"]),
            started_at=datetime.fromisoformat(str(row["started_at"])),
            finished_at=(
                datetime.fromisoformat(str(finished_value))
                if isinstance(finished_value, str)
                else None
            ),
            sources_json=_as_json_object(_json_loads(str(row["sources_json"]))),
            counts_json=_as_json_object(_json_loads(str(row["counts_json"]))),
            notes=cast(str | None, row["notes"]),
        )


@dataclass(frozen=True)
class UserStateRow:
    listing_id: str
    profile_id: str
    shortlisted: bool
    excluded: bool
    contact_status: str | None
    notes: str | None
    viewing_at: datetime | None
    viewing_done: bool


class UserStateRepo:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        listing_repo: ListingRepo | None = None,
    ) -> None:
        self._conn = conn
        self._listing_repo = listing_repo or ListingRepo(conn)

    def upsert_state(
        self,
        *,
        listing_id: str,
        profile_id: str,
        shortlisted: bool = False,
        excluded: bool = False,
        contact_status: str | None = None,
        notes: str | None = None,
        viewing_at: datetime | None = None,
        viewing_done: bool = False,
    ) -> None:
        self._listing_repo.ensure_listing(listing_id)
        self._conn.execute(
            """
            INSERT INTO user_state(
                listing_id,
                profile_id,
                shortlisted,
                excluded,
                contact_status,
                notes,
                viewing_at,
                viewing_done
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(listing_id, profile_id) DO UPDATE SET
                shortlisted = excluded.shortlisted,
                excluded = excluded.excluded,
                contact_status = excluded.contact_status,
                notes = excluded.notes,
                viewing_at = excluded.viewing_at,
                viewing_done = excluded.viewing_done
            """,
            (
                listing_id,
                profile_id,
                int(shortlisted),
                int(excluded),
                contact_status,
                notes,
                _isoformat(viewing_at) if viewing_at else None,
                int(viewing_done),
            ),
        )

    def get_state(self, listing_id: str, profile_id: str) -> UserStateRow | None:
        row = self._conn.execute(
            """
            SELECT
                listing_id,
                profile_id,
                shortlisted,
                excluded,
                contact_status,
                notes,
                viewing_at,
                viewing_done
            FROM user_state
            WHERE listing_id = ? AND profile_id = ?
            """,
            (listing_id, profile_id),
        ).fetchone()
        if row is None:
            return None
        viewing_value = row["viewing_at"]
        return UserStateRow(
            listing_id=str(row["listing_id"]),
            profile_id=str(row["profile_id"]),
            shortlisted=bool(row["shortlisted"]),
            excluded=bool(row["excluded"]),
            contact_status=cast(str | None, row["contact_status"]),
            notes=cast(str | None, row["notes"]),
            viewing_at=(
                datetime.fromisoformat(str(viewing_value))
                if isinstance(viewing_value, str)
                else None
            ),
            viewing_done=bool(row["viewing_done"]),
        )
