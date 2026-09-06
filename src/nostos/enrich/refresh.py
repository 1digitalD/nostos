"""Durable single-worker detail refresh queue."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeAlias, cast

from nostos.context import SearchContext
from nostos.enrich.review import (
    EXTRACTION_REVISION,
    apply_extraction_revision,
    preview_extraction_revision,
)
from nostos.model import SourceRecord
from nostos.model.source_record import JSONValue
from nostos.sources.base import Source
from nostos.store.db import apply_migrations, connect
from nostos.store.repo import ListingRepo

RefreshJob: TypeAlias = dict[str, JSONValue]
_MAX_ATTEMPTS = 3
_DEFAULT_CLAIM_TTL = timedelta(minutes=5)
_PUBLISHABLE_STATUSES = frozenset({"complete", "partial"})
_TERMINAL_STATUSES = frozenset({"blocked", "removed"})


class _TerminalRefreshError(RuntimeError):
    def __init__(self, outcome: str, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome


def enqueue_refresh(conn: sqlite3.Connection, *, listing_id: str) -> RefreshJob:
    """Queue a refresh, or join the listing's existing queued/running job."""

    apply_migrations(conn)
    with _immediate_transaction(conn):
        current = conn.execute(
            """
            SELECT * FROM detail_refresh_job
            WHERE listing_id=? AND state IN ('queued','running')
            ORDER BY requested_at DESC,id DESC LIMIT 1
            """,
            (listing_id,),
        ).fetchone()
        if current is not None:
            return _job_dict(current)

        source = conn.execute(
            """
            SELECT id,source,source_id,url,content_hash
            FROM source_record WHERE listing_id=? ORDER BY id DESC LIMIT 1
            """,
            (listing_id,),
        ).fetchone()
        if source is None:
            raise ValueError("Listing has no saved source record to refresh.")

        job_id = uuid.uuid4().hex
        requested_at = _utcnow().isoformat()
        conn.execute(
            """
            INSERT INTO detail_refresh_job(
                id,listing_id,requested_source_record_id,source,source_id,source_url,
                requested_content_hash,state,requested_at,next_attempt_at,
                extractor_revision,message
            ) VALUES (?,?,?,?,?,?,?,'queued',?,?,?,?)
            """,
            (
                job_id,
                listing_id,
                int(source["id"]),
                str(source["source"]),
                str(source["source_id"]),
                str(source["url"]),
                str(source["content_hash"]),
                requested_at,
                requested_at,
                EXTRACTION_REVISION,
                "Detail update queued.",
            ),
        )
    queued = conn.execute("SELECT * FROM detail_refresh_job WHERE id=?", (job_id,)).fetchone()
    assert queued is not None
    return _job_dict(queued)


def get_refresh_job(conn: sqlite3.Connection, *, listing_id: str) -> RefreshJob | None:
    """Return the latest refresh job for one listing."""

    row = conn.execute(
        """
        SELECT * FROM detail_refresh_job
        WHERE listing_id=? ORDER BY requested_at DESC,id DESC LIMIT 1
        """,
        (listing_id,),
    ).fetchone()
    return _job_dict(row) if row is not None else None


def run_refresh_once(
    db_path: str | Path,
    *,
    context: SearchContext,
    profile_id: str,
    sources: Mapping[str, Source],
    claim_ttl: timedelta = _DEFAULT_CLAIM_TTL,
    context_loader: Callable[[], SearchContext] | None = None,
) -> bool:
    """Claim and process one job, with network I/O outside SQLite transactions."""

    request_error: _TerminalRefreshError | None = None
    with connect(db_path) as conn:
        apply_migrations(conn)
        claimed = _claim_one(conn, claim_ttl=claim_ttl)
        if claimed is None:
            return False
        try:
            requested = _requested_record(conn, claimed)
        except _TerminalRefreshError as exc:
            requested = None
            request_error = exc

    job_id = str(claimed["id"])
    claim_token = str(claimed["claim_token"])
    if requested is None:
        assert request_error is not None
        _finish_failure(
            db_path,
            job_id=job_id,
            claim_token=claim_token,
            outcome=request_error.outcome,
            error=str(request_error),
            transient=False,
        )
        return True
    source = sources.get(requested.source)
    if source is None:
        _finish_failure(
            db_path,
            job_id=job_id,
            claim_token=claim_token,
            outcome="source_unavailable",
            error=f"Source {requested.source!r} is unavailable.",
            transient=False,
        )
        return True

    try:
        detailed = source.fetch_detail(requested)
    except Exception as exc:  # noqa: BLE001 - one source failure must not stop the worker
        _finish_failure(
            db_path,
            job_id=job_id,
            claim_token=claim_token,
            outcome="failed",
            error=_sanitize_error(exc),
            transient=True,
        )
        return True

    status = _detail_status(detailed)
    if status in _TERMINAL_STATUSES:
        _finish_failure(
            db_path,
            job_id=job_id,
            claim_token=claim_token,
            outcome=status,
            error=_detail_error(detailed),
            transient=False,
        )
        return True
    if status not in _PUBLISHABLE_STATUSES:
        _finish_failure(
            db_path,
            job_id=job_id,
            claim_token=claim_token,
            outcome="failed",
            error=_detail_error(detailed) or "The source did not return usable detail.",
            transient=True,
        )
        return True

    try:
        _publish_success(
            db_path,
            job_id=job_id,
            claim_token=claim_token,
            requested=requested,
            detailed=detailed,
            detail_status=status,
            context=context,
            context_loader=context_loader,
            profile_id=profile_id,
            sources=sources,
        )
    except _TerminalRefreshError as exc:
        _finish_failure(
            db_path,
            job_id=job_id,
            claim_token=claim_token,
            outcome=exc.outcome,
            error=str(exc),
            transient=False,
        )
    except Exception as exc:  # noqa: BLE001 - retain the previous good revision on publish failure
        _finish_failure(
            db_path,
            job_id=job_id,
            claim_token=claim_token,
            outcome="publish_failed",
            error=_sanitize_error(exc),
            transient=True,
        )
    return True


def _claim_one(
    conn: sqlite3.Connection, *, claim_ttl: timedelta
) -> sqlite3.Row | None:
    now = _utcnow()
    with conn:
        conn.execute(
            """
            UPDATE detail_refresh_job
            SET state='queued',claim_token=NULL,claim_expires_at=NULL,
                next_attempt_at=?,
                message='Previous worker claim expired; detail update queued again.'
            WHERE state='running' AND claim_expires_at<=? AND attempt_count<?
            """,
            (now.isoformat(), now.isoformat(), _MAX_ATTEMPTS),
        )
        conn.execute(
            """
            UPDATE detail_refresh_job
            SET state='failed',claim_token=NULL,claim_expires_at=NULL,completed_at=?,
                outcome='retries_exhausted',
                error=COALESCE(error,'Worker claim expired.'),
                message='Detail update failed after three attempts.'
            WHERE state='running' AND claim_expires_at<=? AND attempt_count>=?
            """,
            (now.isoformat(), now.isoformat(), _MAX_ATTEMPTS),
        )
        row = conn.execute(
            """
            SELECT * FROM detail_refresh_job
            WHERE state='queued' AND attempt_count<? AND next_attempt_at<=?
            ORDER BY requested_at,id LIMIT 1
            """,
            (_MAX_ATTEMPTS, now.isoformat()),
        ).fetchone()
        if row is None:
            return None
        claim_token = uuid.uuid4().hex
        expires = (now + claim_ttl).isoformat()
        updated = conn.execute(
            """
            UPDATE detail_refresh_job
            SET state='running',attempt_count=attempt_count+1,claim_token=?,
                claim_expires_at=?,started_at=COALESCE(started_at,?),
                message='Updating saved listing details.'
            WHERE id=? AND state='queued'
            """,
            (claim_token, expires, now.isoformat(), str(row["id"])),
        )
        if updated.rowcount != 1:
            return None
    claimed = conn.execute(
        "SELECT * FROM detail_refresh_job WHERE id=?", (row["id"],)
    ).fetchone()
    return cast(sqlite3.Row | None, claimed)


def _requested_record(conn: sqlite3.Connection, job: sqlite3.Row) -> SourceRecord:
    row = conn.execute(
        """
        SELECT source,source_id,url,payload,content_hash,fetched_at
        FROM source_record WHERE id=? AND listing_id=?
        """,
        (job["requested_source_record_id"], job["listing_id"]),
    ).fetchone()
    if row is None:
        raise _TerminalRefreshError("source_missing", "Requested source snapshot is unavailable.")
    import json

    return SourceRecord(
        source=str(row["source"]),
        source_id=str(row["source_id"]),
        url=str(row["url"]),
        payload=cast(JSONValue, json.loads(str(row["payload"]))),
        content_hash=str(row["content_hash"]),
        fetched_at=datetime.fromisoformat(str(row["fetched_at"])),
    )


def _publish_success(
    db_path: str | Path,
    *,
    job_id: str,
    claim_token: str,
    requested: SourceRecord,
    detailed: SourceRecord,
    detail_status: str,
    context: SearchContext,
    context_loader: Callable[[], SearchContext] | None,
    profile_id: str,
    sources: Mapping[str, Source],
) -> None:
    if (
        detailed.source != requested.source
        or detailed.source_id != requested.source_id
        or detailed.url != requested.url
    ):
        raise _TerminalRefreshError(
            "identity_mismatch", "The returned page did not match the requested listing identity."
        )

    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            """
            SELECT * FROM detail_refresh_job
            WHERE id=? AND state='running' AND claim_token=?
            """,
            (job_id, claim_token),
        ).fetchone()
        if job is None:
            conn.commit()
            return
        latest = conn.execute(
            """
            SELECT id,source,source_id,url,content_hash
            FROM source_record WHERE listing_id=? ORDER BY id DESC LIMIT 1
            """,
            (job["listing_id"],),
        ).fetchone()
        if latest is None or int(latest["id"]) != int(job["requested_source_record_id"]):
            raise _TerminalRefreshError(
                "stale_source", "Saved source content changed while this update was running."
            )
        if (
            str(latest["source"]) != str(job["source"])
            or str(latest["source_id"]) != str(job["source_id"])
            or str(latest["url"]) != str(job["source_url"])
            or str(latest["content_hash"]) != str(job["requested_content_hash"])
        ):
            raise _TerminalRefreshError(
                "stale_source", "Saved source identity changed while this update was running."
            )

        listing_id = str(job["listing_id"])
        publish_context = context_loader() if context_loader is not None else context
        result_source_record_id = ListingRepo(conn).add_source_record(
            listing_id=listing_id, record=detailed
        )
        preview = preview_extraction_revision(
            conn,
            listing_id=listing_id,
            context=publish_context,
            profile_id=profile_id,
            sources=sources,
        )
        apply_extraction_revision(
            conn,
            listing_id=listing_id,
            preview_token=preview.preview_token,
            context=context_loader() if context_loader is not None else publish_context,
        )
        conn.execute(
            """
            UPDATE detail_refresh_job
            SET state='succeeded',claim_token=NULL,claim_expires_at=NULL,completed_at=?,
                outcome=?,error=NULL,message=?,result_source_record_id=?
            WHERE id=? AND state='running' AND claim_token=?
            """,
            (
                _utcnow().isoformat(),
                detail_status,
                (
                    "Listing details updated from the saved source."
                    if detail_status == "complete"
                    else "Available listing details updated; some source evidence is incomplete."
                ),
                result_source_record_id,
                job_id,
                claim_token,
            ),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _finish_failure(
    db_path: str | Path,
    *,
    job_id: str,
    claim_token: str,
    outcome: str,
    error: str,
    transient: bool,
) -> None:
    clean_error = _sanitize_error(error)
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT attempt_count FROM detail_refresh_job
            WHERE id=? AND state='running' AND claim_token=?
            """,
            (job_id, claim_token),
        ).fetchone()
        if row is None:
            conn.commit()
            return
        attempts = int(row["attempt_count"])
        retry = transient and attempts < _MAX_ATTEMPTS
        state = "queued" if retry else "failed"
        completed_at = None if retry else _utcnow().isoformat()
        next_attempt_at = (
            (_utcnow() + timedelta(seconds=min(30, 2**attempts))).isoformat()
            if retry
            else _utcnow().isoformat()
        )
        if retry:
            message = f"Detail update will retry ({attempts} of {_MAX_ATTEMPTS} attempts used)."
        elif outcome == "blocked":
            message = "The source blocked detail access. Open the original listing for details."
        elif outcome == "removed":
            message = "The source reports that this listing is no longer available."
        else:
            message = "Detail update failed; the previous saved details remain available."
        conn.execute(
            """
            UPDATE detail_refresh_job
            SET state=?,claim_token=NULL,claim_expires_at=NULL,completed_at=?,
                next_attempt_at=?,outcome=?,error=?,message=?
            WHERE id=? AND state='running' AND claim_token=?
            """,
            (
                state,
                completed_at,
                next_attempt_at,
                outcome,
                clean_error,
                message,
                job_id,
                claim_token,
            ),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _detail_status(record: SourceRecord) -> str:
    if isinstance(record.payload, Mapping):
        value = record.payload.get("detail_status")
        if isinstance(value, str):
            return value.strip().lower()
        if record.payload.get("detail_error"):
            return "failed"
    return "complete"


def _detail_error(record: SourceRecord) -> str:
    if isinstance(record.payload, Mapping):
        value = record.payload.get("detail_error")
        if value:
            return _sanitize_error(str(value))
    return ""


def _sanitize_error(error: object) -> str:
    if isinstance(error, BaseException):
        return f"{type(error).__name__}: detail update failed"
    return " ".join(str(error).split())[:500]


def _job_dict(row: sqlite3.Row) -> RefreshJob:
    return {
        "id": str(row["id"]),
        "listing_id": str(row["listing_id"]),
        "state": str(row["state"]),
        "attempt_count": int(row["attempt_count"]),
        "requested_at": str(row["requested_at"]),
        "next_attempt_at": str(row["next_attempt_at"]),
        "started_at": str(row["started_at"]) if row["started_at"] is not None else None,
        "completed_at": (
            str(row["completed_at"]) if row["completed_at"] is not None else None
        ),
        "outcome": str(row["outcome"]) if row["outcome"] is not None else None,
        "error": str(row["error"]) if row["error"] is not None else None,
        "message": str(row["message"]),
    }


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


@contextmanager
def _immediate_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        savepoint = f"detail_refresh_{uuid.uuid4().hex}"
        conn.execute(f"SAVEPOINT {savepoint}")
        # A no-op write obtains a RESERVED lock before decisions are made.
        conn.execute(
            "UPDATE listing SET last_seen=last_seen "
            "WHERE id=(SELECT id FROM listing ORDER BY id LIMIT 1)"
        )
        try:
            yield
        except BaseException:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


__all__ = ["RefreshJob", "enqueue_refresh", "get_refresh_job", "run_refresh_once"]
