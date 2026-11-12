"""Account lifecycle, ownership checks and statements."""

from __future__ import annotations

import secrets
import sqlite3
import string
import uuid
from typing import Any

from .. import audit
from ..db import Database, utcnow_iso
from ..errors import AppError, conflict, not_found, validation_error
from ..money import format_amount, validate_currency

ACCOUNT_NUMBER_PREFIX = "40"
ACCOUNT_NUMBER_LENGTH = 12
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


class Principal:
    """The authenticated caller. Role/status come from the database, not the JWT."""

    __slots__ = ("id", "email", "full_name", "role", "status")

    def __init__(self, *, id: str, email: str, full_name: str, role: str, status: str) -> None:
        self.id = id
        self.email = email
        self.full_name = full_name
        self.role = role
        self.status = status

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Principal(id={self.id!r}, role={self.role!r})"


def serialize_account(row: sqlite3.Row, *, include_owner_email: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": row["id"],
        "account_number": row["account_number"],
        "name": row["name"],
        "currency": row["currency"],
        "balance": format_amount(int(row["balance_minor"])),
        "balance_minor": int(row["balance_minor"]),
        "status": row["status"],
        "is_system": bool(row["is_system"]),
        "version": int(row["version"]),
        "created_at": row["created_at"],
    }
    if include_owner_email is not None:
        payload["owner_email"] = include_owner_email
    return payload


def _generate_account_number() -> str:
    # 12 digits, cryptographically random, fixed prefix so support can spot them.
    suffix = "".join(secrets.choice(string.digits) for _ in range(ACCOUNT_NUMBER_LENGTH - 2))
    return ACCOUNT_NUMBER_PREFIX + suffix


def create_account(
    db: Database,
    actor: Principal,
    *,
    owner_email: str,
    currency: str,
    name: str,
) -> dict[str, Any]:
    currency = validate_currency(currency)
    label = (name or "").strip()
    if not 2 <= len(label) <= 80:
        raise validation_error("name must be 2-80 characters.")
    email = (owner_email or "").strip().lower()

    with db.write() as conn:
        owner = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if owner is None:
            raise not_found("Account owner")
        if owner["role"] != "CUSTOMER":
            raise validation_error("Accounts can only be opened for CUSTOMER users.")

        account_id = str(uuid.uuid4())
        now = utcnow_iso()
        for _ in range(5):
            try:
                conn.execute(
                    """
                    INSERT INTO accounts
                        (id, account_number, owner_user_id, name, currency, balance_minor,
                         is_system, status, version, created_at)
                    VALUES (?, ?, ?, ?, ?, 0, 0, 'ACTIVE', 0, ?)
                    """,
                    (account_id, _generate_account_number(), owner["id"], label, currency, now),
                )
                break
            except sqlite3.IntegrityError:
                continue
        else:  # pragma: no cover - 10^10 space
            raise AppError("INTERNAL_ERROR", "Could not allocate an account number.", status_code=500)

        audit.record_in(
            conn,
            "ACCOUNT_CREATED",
            actor=actor,
            target_type="account",
            target_id=account_id,
            detail={"owner": owner["email"], "currency": currency},
        )
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return serialize_account(row, include_owner_email=owner["email"])


def list_accounts(db: Database, principal: Principal) -> list[dict[str, Any]]:
    with db.read() as conn:
        if principal.role == "CUSTOMER":
            rows = conn.execute(
                "SELECT * FROM accounts WHERE owner_user_id = ? ORDER BY created_at ASC",
                (principal.id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT a.*, u.email AS owner_email
                  FROM accounts a LEFT JOIN users u ON u.id = a.owner_user_id
                 ORDER BY a.created_at ASC
                """
            ).fetchall()
            return [
                serialize_account(row, include_owner_email=row["owner_email"]) for row in rows
            ]
    return [serialize_account(row, include_owner_email=principal.email) for row in rows]


def fetch_authorized_account(
    conn: sqlite3.Connection, principal: Principal, account_id: str
) -> sqlite3.Row:
    """Load an account the caller may see.

    A customer asking for somebody else's account gets ``404`` -- not ``403`` --
    so the endpoint cannot be used to confirm that an account id exists.
    """
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise not_found("Account")
    if principal.role == "CUSTOMER" and row["owner_user_id"] != principal.id:
        raise not_found("Account")
    return row


def get_account(db: Database, principal: Principal, account_id: str) -> dict[str, Any]:
    with db.read() as conn:
        row = fetch_authorized_account(conn, principal, account_id)
        owner_email = None
        if principal.role != "CUSTOMER":
            owner_email = conn.execute(
                "SELECT email FROM users WHERE id = ?", (row["owner_user_id"],)
            ).fetchone()
            owner_email = owner_email["email"] if owner_email else None
    return serialize_account(row, include_owner_email=owner_email)


def set_frozen(
    db: Database, actor: Principal, *, account_id: str, frozen: bool
) -> dict[str, Any]:
    with db.write() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise not_found("Account")
        if row["is_system"]:
            raise validation_error("System accounts cannot be frozen or unfrozen.")
        if row["status"] == "CLOSED":
            raise conflict("ACCOUNT_CLOSED", "A closed account cannot change state.")
        new_status = "FROZEN" if frozen else "ACTIVE"
        if row["status"] == new_status:
            return serialize_account(row)
        conn.execute(
            "UPDATE accounts SET status = ?, version = version + 1 WHERE id = ?",
            (new_status, account_id),
        )
        audit.record_in(
            conn,
            "ACCOUNT_FROZEN" if frozen else "ACCOUNT_UNFROZEN",
            actor=actor,
            target_type="account",
            target_id=account_id,
            detail={"from": row["status"], "to": new_status},
        )
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return serialize_account(row)


def statement(
    db: Database,
    principal: Principal,
    *,
    account_id: str,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: int | None = None,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), MAX_PAGE_SIZE))
    with db.read() as conn:
        account = fetch_authorized_account(conn, principal, account_id)
        # The cursor predicate is built in Python rather than written as
        # ``(? IS NULL OR r.id < ?)``: Postgres cannot infer the type of an
        # untyped NULL parameter, and building the clause keeps one query text
        # that runs unchanged on both engines.
        params: list[Any] = [account_id]
        cursor_clause = ""
        if before_id is not None:
            cursor_clause = "WHERE r.id < ?"
            params.append(before_id)
        params.append(limit)
        rows = conn.execute(
            f"""
            WITH running AS (
                SELECT e.id, e.transaction_id, e.amount_minor, e.currency, e.created_at,
                       SUM(e.amount_minor) OVER (ORDER BY e.id) AS running_balance_minor
                  FROM ledger_entries e
                 WHERE e.account_id = ?
            )
            SELECT r.*, t.type AS tx_type, t.description
              FROM running r JOIN transactions t ON t.id = r.transaction_id
             {cursor_clause}
             ORDER BY r.id DESC
             LIMIT ?
            """,
            tuple(params),
        ).fetchall()

    entries = [
        {
            "entry_id": int(row["id"]),
            "transaction_id": row["transaction_id"],
            "type": row["tx_type"],
            "direction": "CREDIT" if row["amount_minor"] > 0 else "DEBIT",
            "amount": format_amount(abs(int(row["amount_minor"]))),
            "amount_minor": abs(int(row["amount_minor"])),
            "currency": row["currency"],
            "balance_after": format_amount(int(row["running_balance_minor"])),
            "description": row["description"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
    next_cursor = entries[-1]["entry_id"] if len(entries) == limit else None
    return {
        "account": serialize_account(account),
        "entries": entries,
        "next_cursor": next_cursor,
        "limit": limit,
    }


def transaction_detail(db: Database, principal: Principal, transaction_id: str) -> dict[str, Any]:
    with db.read() as conn:
        tx = conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (transaction_id,)
        ).fetchone()
        if tx is None:
            raise not_found("Transaction")
        entries = conn.execute(
            """
            SELECT e.*, a.account_number, a.owner_user_id, a.name AS account_name
              FROM ledger_entries e JOIN accounts a ON a.id = e.account_id
             WHERE e.transaction_id = ?
             ORDER BY e.id ASC
            """,
            (transaction_id,),
        ).fetchall()
        if principal.role == "CUSTOMER":
            owns = any(row["owner_user_id"] == principal.id for row in entries)
            initiated = tx["initiator_user_id"] == principal.id
            if not (owns or initiated):
                raise not_found("Transaction")
    return {
        "id": tx["id"],
        "type": tx["type"],
        "currency": tx["currency"],
        "amount": format_amount(int(tx["amount_minor"])),
        "amount_minor": int(tx["amount_minor"]),
        "description": tx["description"],
        "created_at": tx["created_at"],
        "entries": [
            {
                "id": int(row["id"]),
                "account_id": row["account_id"],
                "account_number": row["account_number"],
                "account_name": row["account_name"],
                "direction": "CREDIT" if row["amount_minor"] > 0 else "DEBIT",
                "amount": format_amount(abs(int(row["amount_minor"]))),
                "amount_minor": abs(int(row["amount_minor"])),
                "prev_hash": row["prev_hash"],
                "entry_hash": row["entry_hash"],
            }
            for row in entries
        ],
    }
