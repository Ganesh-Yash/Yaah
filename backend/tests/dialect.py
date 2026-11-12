"""Small helpers so one test can attack either engine.

The guarantees are identical; only the syntax for "disable the guard, then try
to rewrite history" differs. Keeping the difference here means the tamper tests
still express intent rather than engine trivia.
"""

from __future__ import annotations

import sqlite3

import pytest


def is_postgres(db: object) -> bool:
    return getattr(db, "backend", "sqlite") == "postgres"


def guard_errors() -> tuple[type[BaseException], ...]:
    """The exception family a blocked write raises on the current engine."""
    types: list[type[BaseException]] = [sqlite3.DatabaseError]
    try:
        import psycopg
    except ImportError:  # pragma: no cover
        return tuple(types)
    types.append(psycopg.Error)
    return tuple(types)


def drop_trigger(db: object, name: str) -> str:
    """SQL that removes an append-only guard (what an insider with SQL access does)."""
    if is_postgres(db):
        table = {
            "trg_ledger_no_update": "ledger_entries",
            "trg_ledger_no_delete": "ledger_entries",
            "trg_tx_no_update": "transactions",
            "trg_tx_no_delete": "transactions",
            "trg_audit_no_update": "audit_log",
            "trg_audit_no_delete": "audit_log",
        }.get(name)
        # The SQLite schema names the transaction triggers differently; map the
        # names the tests use onto whichever engine is live.
        if table is None:
            table = "ledger_entries" if "ledger" in name else "transactions" if "tx" in name else "audit_log"
        return f"DROP TRIGGER IF EXISTS {name} ON {table}"
    return f"DROP TRIGGER {name}"


def drop_non_negative_guard(db: object) -> str | None:
    """Remove the structural non-negative-balance guard, if the engine has one."""
    if is_postgres(db):
        return "ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_customer_non_negative"
    return None


def trigger_name_for(db: object, sqlite_name: str) -> str:
    """Map a SQLite trigger name to the live engine's name."""
    if is_postgres(db):
        return {
            "trg_ledger_no_update": "trg_ledger_no_update",
            "trg_ledger_no_delete": "trg_ledger_no_delete",
            "trg_audit_no_update": "trg_audit_no_update",
            "trg_audit_no_delete": "trg_audit_no_delete",
            "trg_transactions_no_update": "trg_tx_no_update",
            "trg_transactions_no_delete": "trg_tx_no_delete",
        }.get(sqlite_name, sqlite_name)
    return sqlite_name


__all__ = [
    "drop_non_negative_guard",
    "drop_trigger",
    "guard_errors",
    "is_postgres",
    "trigger_name_for",
]
