"""SQLite storage: schema, connection factory and unidata-version guard.

The database records the ``unicodedata.unidata_version`` of the interpreter
that created it. Every connection compares the stored value with the running
interpreter's version and refuses to read or write on mismatch -- the
normalization rules must never silently change underneath a directory.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import ToolError, UnidataVersionMismatch
from .normalization import unidata_version

SCHEMA_VERSION = "1"
BUSY_TIMEOUT_SECONDS = 30.0


class DatabaseNotInitialized(ToolError):
    error_code = "DATABASE_NOT_INITIALIZED"


def _connect_raw(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(path),
        timeout=BUSY_TIMEOUT_SECONDS,
        isolation_level=None,  # autocommit; transactions are explicit
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_SECONDS * 1000)}")
    conn.execute("PRAGMA foreign_keys=ON")
    if str(path) != ":memory:":
        # WAL lets concurrent worker processes snapshot a consistent revision
        # while a writer is mid-migration.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(path: str | Path) -> None:
    """Create a fresh database with the current interpreter's unidata version."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = _connect_raw(path)
    try:
        # executescript() implicitly commits any open transaction, so run the
        # idempotent DDL first and only afterwards take an explicit write
        # transaction for the metadata insert.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tenants (
                tenant_id TEXT PRIMARY KEY,
                policy    TEXT NOT NULL CHECK (policy IN ('V1','V2')),
                revision  INTEGER NOT NULL CHECK (revision >= 0)
            );
            CREATE TABLE IF NOT EXISTS accounts (
                tenant_id  TEXT NOT NULL,
                account_id TEXT NOT NULL,
                raw_login  TEXT NOT NULL,
                login_name TEXT NOT NULL,
                PRIMARY KEY (tenant_id, account_id),
                UNIQUE (tenant_id, login_name)
            );
            CREATE TABLE IF NOT EXISTS plans (
                plan_id          TEXT PRIMARY KEY,
                tenant_id        TEXT NOT NULL,
                digest           TEXT NOT NULL,
                status           TEXT NOT NULL
                                 CHECK (status IN ('committed','rejected')),
                result_json      TEXT NOT NULL,
                source_revision  INTEGER NOT NULL
            );
            """
        )
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT value FROM meta WHERE key='unidata_version'"
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('unidata_version', ?)",
                (unidata_version(),),
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
        elif existing["value"] != unidata_version():
            raise UnidataVersionMismatch(
                "database was created with Unicode data "
                f"{existing['value']!r}, running interpreter has "
                f"{unidata_version()!r}",
                details={
                    "stored_version": existing["value"],
                    "runtime_version": unidata_version(),
                },
            )
        conn.execute("COMMIT")
    except Exception:
        # executescript may already have committed; ROLLBACK is then a no-op
        # style guard that itself must not mask the original error.
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.close()


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a guarded connection. Refuses uninitialized or version-mismatched DBs."""
    conn = _connect_raw(path)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='unidata_version'"
        ).fetchone()
    except sqlite3.OperationalError as exc:  # no such table -> not initialized
        conn.close()
        raise DatabaseNotInitialized(
            f"database at {path!s} is not initialized; run init first"
        ) from exc
    if row is None:
        conn.close()
        raise DatabaseNotInitialized(
            f"database at {path!s} is not initialized; run init first"
        )
    if row["value"] != unidata_version():
        conn.close()
        raise UnidataVersionMismatch(
            "stored Unicode data version "
            f"{row['value']!r} differs from runtime {unidata_version()!r}",
            details={
                "stored_version": row["value"],
                "runtime_version": unidata_version(),
            },
        )
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` transaction with explicit commit/rollback."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
