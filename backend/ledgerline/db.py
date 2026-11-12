"""SQLite access layer.

Why ``BEGIN IMMEDIATE`` for writes
----------------------------------
SQLite allows one writer at a time. A deferred transaction that starts by
reading and only later upgrades to a write can fail with ``SQLITE_BUSY`` and,
more importantly, produces the classic read-then-write race:

    read balance -> 100, decide 80 is allowed, ... another request spends 80 ...

Starting every write transaction with ``BEGIN IMMEDIATE`` takes the write lock
up front, so the balance read and the balance write happen inside one
serialised critical section. Combined with the conditional ``UPDATE ...
WHERE balance_minor >= :amount`` in :mod:`ledgerline.ledger`, a balance can
never go negative no matter how many requests arrive at once.

The same code shape maps to Postgres: ``BEGIN`` + ``SELECT ... FOR UPDATE`` on
the accounts in id order (see docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def utcnow_iso() -> str:
    """Fixed-width UTC timestamp: lexicographic order == chronological order."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def iso_add_seconds(seconds: int) -> str:
    from datetime import timedelta

    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


class Database:
    def __init__(self, path: str, busy_timeout_ms: int = 10_000) -> None:
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms

    # -- connections -------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,  # explicit transaction control only
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        # FULL gives us durability of committed money movements even on power
        # loss; WAL keeps readers from blocking behind the single writer.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN DEFERRED")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    # -- schema ------------------------------------------------------------

    def initialize(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        finally:
            conn.close()

    def scalar(self, sql: str, params: tuple = ()) -> object:
        with self.read() as conn:
            row = conn.execute(sql, params).fetchone()
        return None if row is None else row[0]


    def ping(self) -> tuple[bool, str]:
        try:
            with self.read() as conn:
                conn.execute("SELECT 1").fetchone()
            return True, "ok"
        except Exception as exc:  # pragma: no cover - only on a broken volume
            return False, exc.__class__.__name__

    def schema_version(self) -> str | None:
        try:
            value = self.scalar("SELECT value FROM schema_meta WHERE key = 'version'")
        except sqlite3.Error:  # pragma: no cover - pre-migration database
            return None
        return None if value is None else str(value)


def make_database(settings: object):
    """Pick the backend from the settings, not from scattered flags.

    ``LEDGERLINE_DATABASE_URL`` present -> PostgreSQL. Absent -> SQLite, which
    is what the test suite and the offline demo use.
    """
    url = getattr(settings, "database_url", None)
    if url:
        from .pg import PostgresDatabase

        return PostgresDatabase(
            str(url),
            busy_timeout_ms=getattr(settings, "sqlite_busy_timeout_ms", 10_000),
            schema=getattr(settings, "database_schema", None),
        )
    return Database(
        str(getattr(settings, "database_path")),
        int(getattr(settings, "sqlite_busy_timeout_ms", 10_000)),
    )


def is_unique_violation(exc: BaseException) -> bool:
    """True for a UNIQUE/PRIMARY KEY collision on either engine."""
    if isinstance(exc, sqlite3.IntegrityError):
        return "UNIQUE" in str(exc).upper()
    try:  # psycopg is an optional import for SQLite-only checkouts
        import psycopg
    except ImportError:  # pragma: no cover
        return False
    return isinstance(exc, psycopg.errors.UniqueViolation)


def database_busy_types() -> tuple[type[BaseException], ...]:
    """Exception classes that mean "the write lock did not let us in"."""
    types: list[type[BaseException]] = [sqlite3.OperationalError]
    try:
        import psycopg
    except ImportError:  # pragma: no cover
        return tuple(types)
    types.extend(
        [
            psycopg.errors.LockNotAvailable,
            psycopg.errors.QueryCanceled,
            psycopg.errors.OperationalError,
        ]
    )
    return tuple(types)
