"""PostgreSQL access layer.

The service speaks to two engines behind one interface: SQLite for local tests
and the offline demo, PostgreSQL for anything deployed. Both expose
``read()``/``write()``/``initialize()``/``scalar()`` and hand out a connection
whose ``execute(sql, params)`` behaves the same way, so the money path has no
"if postgres" branches in it.

Two details are worth reading before the code:

**Why ``pg_advisory_xact_lock`` on writes.** SQLite has one writer, so
``BEGIN IMMEDIATE`` serialises everything. Postgres does not, so the same
correctness argument has to be re-established explicitly. Taking a single
transaction-scoped advisory lock at the start of every write transaction
reproduces "one writer at a time" exactly, which is what the balance
conditional-UPDATE trick depends on. It is deliberately *not* row-level locking:
the balance check and the write must not be separated by another writer's
commit. Throughput is fine for this workload and the cost is honest.

**Why timestamps are TEXT.** They are generated in Python as fixed-width
``2026-01-01T00:00:00.000000Z`` strings. Fixed width means lexicographic order
is chronological order, so the same ``WHERE created_at >= ?`` comparisons work on
both engines and a row read from either is byte-identical.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

ADVISORY_LOCK_KEY = 4_172_911  # one writer at a time, same as BEGIN IMMEDIATE
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "prisma" / "migrations"

_PLACEHOLDER = re.compile(r"\?")


def to_pg_sql(sql: str) -> str:
    """Translate the portable ``?`` placeholders to psycopg's ``%s``.

    Queries are written once, with ``?``, and used on both engines. There is no
    ``?`` inside a string literal anywhere in this code base -- if that ever
    changes, this is the function to revisit.
    """
    return _PLACEHOLDER.sub("%s", sql)


def _is_missing_relation(exc: BaseException) -> bool:
    """True for "that table does not exist yet" (an unmigrated database)."""
    try:
        import psycopg
    except ImportError:  # pragma: no cover
        return False
    return isinstance(exc, psycopg.errors.UndefinedTable)


class PgRow(dict):
    """A row that behaves like ``sqlite3.Row``: name *and* position access.

    The money path indexes rows positionally in a few places (``row[0]`` for
    scalar aggregates). Rather than rewriting those call sites — and risking a
    silent column-order bug — the adapter makes the Postgres rows support the
    same two access patterns SQLite does.
    """

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _row_factory(cursor: Any) -> Any:
    names = [column.name for column in cursor.description] if cursor.description else []

    def make_row(values: Any) -> PgRow:
        return PgRow(zip(names, values))

    return make_row


class PgConnection:
    """Thin adapter so call sites look identical to the SQLite ones."""

    def __init__(self, raw: Any) -> None:
        self.raw = raw

    def execute(self, sql: str, params: Any = ()) -> Any:
        return self.raw.execute(to_pg_sql(sql), params or None)

    def close(self) -> None:
        self.raw.close()


class PostgresDatabase:
    backend = "postgres"

    def __init__(
        self,
        url: str,
        *,
        busy_timeout_ms: int = 10_000,
        migrations_dir: Path | None = None,
        schema: str | None = None,
    ) -> None:
        self.url = url
        self.busy_timeout_ms = busy_timeout_ms
        self.migrations_dir = migrations_dir or MIGRATIONS_DIR
        # Each test gets its own schema, so the suite can run against a real
        # server without 167 CREATE DATABASE calls.
        self.schema = schema

    # -- connections -------------------------------------------------------

    def connect(self) -> PgConnection:
        import psycopg
        seconds = max(1, int(self.busy_timeout_ms / 1000))
        raw = psycopg.connect(
            self.url,
            autocommit=True,  # explicit transaction control only, as with sqlite
            row_factory=_row_factory,
            options=(
                f"-c lock_timeout={seconds * 1000} "
                f"-c statement_timeout={max(30, seconds * 6) * 1000}"
                + (f" -c search_path={self.schema}" if self.schema else "")
            ),
        )
        return PgConnection(raw)

    @contextmanager
    def read(self) -> Iterator[PgConnection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # pragma: no cover - connection already gone
                pass
            raise
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[PgConnection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            # Serialise writers for the whole transaction: this is the Postgres
            # equivalent of SQLite's BEGIN IMMEDIATE.
            conn.execute("SELECT pg_advisory_xact_lock(?)", (ADVISORY_LOCK_KEY,))
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # pragma: no cover
                pass
            raise
        finally:
            conn.close()

    # -- schema ------------------------------------------------------------

    def is_initialised(self) -> bool:
        with self.read() as conn:
            row = conn.execute("SELECT to_regclass('users') AS reg").fetchone()
        return bool(row and row["reg"])

    def initialize(self) -> None:
        """Apply the SQL migrations if the database is empty.

        In a deployed environment ``prisma migrate deploy`` owns this and the
        call is a no-op (it sees ``users`` and returns). For tests and the
        single-container demo the same migration files are applied here, so
        both paths execute identical SQL.
        """
        if self.is_initialised():
            return
        files = sorted(self.migrations_dir.glob("*/migration.sql"))
        if not files:
            raise RuntimeError(f"no migrations found in {self.migrations_dir}")
        with self.write() as conn:
            for path in files:
                conn.execute(path.read_text(encoding="utf-8"))

    def schema_version(self) -> str | None:
        """The applied schema version, or None when the database is still empty.

        An empty database is a normal state — it is what a release step is for — so
        a missing table must read as "no version yet", not as an error. The SQLite
        adapter has always behaved this way; this keeps the two engines honest with
        each other, and keeps /api/ready answerable before the first migration.
        """
        try:
            with self.read() as conn:
                row = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        except Exception as exc:  # noqa: BLE001 - narrow check below
            if _is_missing_relation(exc):
                return None
            raise
        return None if row is None else str(row["value"])

    # -- helpers -----------------------------------------------------------

    def ping(self) -> tuple[bool, str]:
        try:
            with self.read() as conn:
                conn.execute("SELECT 1").fetchone()
            return True, "ok"
        except Exception as exc:  # pragma: no cover - only on a broken server
            return False, exc.__class__.__name__

    def scalar(self, sql: str, params: tuple = ()) -> object:
        with self.read() as conn:
            row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return next(iter(row.values()))
