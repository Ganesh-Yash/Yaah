"""Independent verification of the books.

``GET /api/auditor/integrity`` recomputes everything from the ledger and
compares it with what the system claims. It is intentionally boring and
read-only: no cached flags, no "we think this is fine" booleans.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .. import chain
from ..db import Database, utcnow_iso
from ..money import format_amount


def _check(name: str, description: str, ok: bool, **detail: Any) -> dict[str, Any]:
    return {"name": name, "description": description, "ok": ok, "detail": detail}


def verify(db: Database) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    with db.read() as conn:
        checks.append(_balances_match_entries(conn))
        checks.append(_no_negative_customer_balances(conn))
        checks.append(_transactions_balanced(conn))
        checks.append(_transactions_have_entries(conn))
        checks.append(_entries_match_account_currency(conn))
        checks.append(_no_orphan_entries(conn))
        checks.append(_money_is_conserved(conn))
        ledger_chain = chain.verify_ledger_chain(conn)
        audit_chain = chain.verify_audit_chain(conn)
        checks.append(
            _check(
                "ledger_hash_chain",
                "Every ledger entry links to its predecessor by SHA-256.",
                ledger_chain["ok"],
                rows=ledger_chain["rows"],
                head_hash=ledger_chain["head_hash"],
                first_bad_id=ledger_chain["first_bad_id"],
                problems=ledger_chain["problems"],
            )
        )
        checks.append(
            _check(
                "audit_hash_chain",
                "Every audit row links to its predecessor by SHA-256.",
                audit_chain["ok"],
                rows=audit_chain["rows"],
                head_hash=audit_chain["head_hash"],
                first_bad_id=audit_chain["first_bad_id"],
                problems=audit_chain["problems"],
            )
        )
        totals = _totals(conn)

    return {
        "ok": all(check["ok"] for check in checks),
        "generated_at": utcnow_iso(),
        "checks": checks,
        "totals": totals,
    }


def _balances_match_entries(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT a.id, a.account_number, a.balance_minor,
               COALESCE(SUM(e.amount_minor), 0) AS computed_minor
          FROM accounts a
          LEFT JOIN ledger_entries e ON e.account_id = a.id
         GROUP BY a.id
        HAVING a.balance_minor <> COALESCE(SUM(e.amount_minor), 0)
        """
    ).fetchall()
    return _check(
        "balances_match_entries",
        "Each stored balance equals the sum of that account's ledger entries.",
        len(rows) == 0,
        mismatches=[
            {
                "account_number": row["account_number"],
                "stored": format_amount(int(row["balance_minor"])),
                "computed": format_amount(int(row["computed_minor"])),
            }
            for row in rows[:20]
        ],
    )


def _no_negative_customer_balances(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT account_number, balance_minor FROM accounts WHERE is_system = 0 AND balance_minor < 0"
    ).fetchall()
    return _check(
        "no_negative_customer_balances",
        "No customer account has a negative balance.",
        len(rows) == 0,
        offenders=[row["account_number"] for row in rows[:20]],
    )


def _transactions_balanced(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT transaction_id, SUM(amount_minor) AS total, COUNT(*) AS legs
          FROM ledger_entries
         GROUP BY transaction_id
        HAVING SUM(amount_minor) <> 0 OR COUNT(*) < 2
        """
    ).fetchall()
    return _check(
        "transactions_balanced",
        "Every transaction has at least two legs and sums to zero.",
        len(rows) == 0,
        offenders=[
            {"transaction_id": row["transaction_id"], "sum_minor": row["total"], "legs": row["legs"]}
            for row in rows[:20]
        ],
    )


def _transactions_have_entries(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT t.id FROM transactions t
         WHERE NOT EXISTS (SELECT 1 FROM ledger_entries e WHERE e.transaction_id = t.id)
        """
    ).fetchall()
    return _check(
        "transactions_have_entries",
        "No transaction header exists without ledger entries.",
        len(rows) == 0,
        offenders=[row["id"] for row in rows[:20]],
    )


def _entries_match_account_currency(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT e.id FROM ledger_entries e JOIN accounts a ON a.id = e.account_id
         WHERE e.currency <> a.currency
        """
    ).fetchall()
    return _check(
        "entries_match_account_currency",
        "No ledger entry is booked in a currency other than its account's.",
        len(rows) == 0,
        offenders=[row["id"] for row in rows[:20]],
    )


def _no_orphan_entries(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT e.id FROM ledger_entries e
         WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.id = e.transaction_id)
        """
    ).fetchall()
    return _check(
        "no_orphan_entries",
        "Every ledger entry belongs to an existing transaction.",
        len(rows) == 0,
        offenders=[row["id"] for row in rows[:20]],
    )


def _money_is_conserved(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT COALESCE(SUM(balance_minor), 0) AS total FROM accounts").fetchone()
    total = int(row["total"])
    return _check(
        "money_is_conserved",
        "Across every account (including the external clearing account) the sum is zero.",
        total == 0,
        total=format_amount(total),
    )


def _totals(conn: sqlite3.Connection) -> dict[str, Any]:
    accounts = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN is_system = 0 THEN balance_minor END), 0) AS customer FROM accounts"
    ).fetchone()
    entries = conn.execute("SELECT COUNT(*) AS n FROM ledger_entries").fetchone()
    transactions = conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()
    audit_rows = conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()
    ledger_head = conn.execute(
        "SELECT last_hash FROM chain_state WHERE name = 'ledger'"
    ).fetchone()
    audit_head = conn.execute("SELECT last_hash FROM chain_state WHERE name = 'audit'").fetchone()
    return {
        "accounts": int(accounts["n"]),
        "customer_funds": format_amount(int(accounts["customer"])),
        "ledger_entries": int(entries["n"]),
        "transactions": int(transactions["n"]),
        "audit_rows": int(audit_rows["n"]),
        "ledger_head_hash": ledger_head["last_hash"] if ledger_head else None,
        "audit_head_hash": audit_head["last_hash"] if audit_head else None,
    }
