from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

MIGRATION_PATTERN = re.compile(r"^(?P<version>\d+)_.*\.sql$")


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    if db_path != Path(":memory:"):
        db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def apply_migrations(
    conn: sqlite3.Connection,
    *,
    migrations_dir: Path | None = None,
) -> list[int]:
    migrations_path = migrations_dir or Path(__file__).with_name("migrations")
    _ensure_schema_migration_table(conn)
    applied_versions = _load_applied_versions(conn)

    applied_now: list[int] = []
    for version, migration_file in _discover_migrations(migrations_path):
        if version in applied_versions:
            continue

        sql = migration_file.read_text(encoding="utf-8")
        with conn:
            conn.executescript(sql)
            conn.execute(
                """
                INSERT INTO schema_migration(version, applied_at)
                VALUES (?, ?)
                """,
                (version, datetime.now(tz=UTC).isoformat()),
            )
        applied_now.append(version)
        applied_versions.add(version)

    return applied_now


def _ensure_schema_migration_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migration (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """,
    )


def _load_applied_versions(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT version FROM schema_migration").fetchall()
    return {int(row[0]) for row in rows}


def _discover_migrations(migrations_dir: Path) -> list[tuple[int, Path]]:
    entries: list[tuple[int, Path]] = []
    seen_versions: set[int] = set()

    for candidate in sorted(migrations_dir.glob("*.sql")):
        match = MIGRATION_PATTERN.match(candidate.name)
        if match is None:
            continue
        version = int(match.group("version"))
        if version in seen_versions:
            msg = f"Duplicate migration version detected: {version}"
            raise ValueError(msg)
        seen_versions.add(version)
        entries.append((version, candidate))

    entries.sort(key=lambda item: item[0])
    return entries
