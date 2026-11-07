"""The posting engine: the only code allowed to change a balance.

Every money movement becomes one ``transactions`` row plus two or more
``ledger_entries`` rows whose signed amounts sum to exactly zero. Balances are
updated with a *conditional* UPDATE inside the same transaction:

    UPDATE accounts
       SET balance_minor = balance_minor + :delta, version = version + 1
     WHERE id = :id
       AND status = 'ACTIVE'
       AND (is_system = 1 OR balance_minor + :delta >= 0)

``rowcount == 0`` means "this leg is not legal" and the whole transaction is
rolled back. There is no read-then-write window in which two concurrent
withdrawals can both see the same balance: the balance check and the balance
write are a single SQL statement, executed under the write lock taken by
``BEGIN IMMEDIATE``.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from . import chain
from .db import utcnow_iso
from .errors import AppError, conflict, not_found, validation_error
from .money import format_amount

DIRECTION_CREDIT = "CREDIT"
DIRECTION_DEBIT = "DEBIT"


@dataclass(frozen=True)
class Leg:
    """One side of a double entry. Positive = credit, negative = debit."""

    account_id: str
    amount_minor: int


def get_account(conn: sqlite3.Connection, account_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()


def get_account_by_number(conn: sqlite3.Connection, account_number: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM accounts WHERE account_number = ?", (account_number,)
    ).fetchone()


def system_account_id(currency: str) -> str:
    return f"sys-cash-{currency.lower()}"


def system_account_number(currency: str) -> str:
    return f"SYS-CASH-{currency}"


def ensure_system_accounts(conn: sqlite3.Connection, currencies: Iterable[str]) -> None:
    """Create the external-cash clearing account used by deposits/withdrawals.

    It is the "world" counterparty, so its balance is normally negative (cash
    left the outside world and entered a customer account). It is the only
    account allowed to go negative, and it cannot be owned, frozen or used as a
    transfer destination.
    """
    for currency in currencies:
        conn.execute(
            """
            INSERT OR IGNORE INTO accounts
                (id, account_number, owner_user_id, name, currency, balance_minor,
                 is_system, status, version, created_at)
            VALUES (?, ?, NULL, ?, ?, 0, 1, 'ACTIVE', 0, ?)
            """,
            (
                system_account_id(currency),
                system_account_number(currency),
                f"External cash clearing ({currency})",
                currency,
                utcnow_iso(),
            ),
        )


def _explain_failed_leg(conn: sqlite3.Connection, leg: Leg) -> AppError:
    account = get_account(conn, leg.account_id)
    if account is None:
        return not_found("Account")
    if account["status"] != "ACTIVE":
        return conflict(
            "ACCOUNT_NOT_ACTIVE",
            f"Account {account['account_number']} is {account['status'].lower()}.",
            {"account_status": account["status"]},
        )
    if leg.amount_minor < 0:
        available = int(account["balance_minor"])
        return conflict(
            "INSUFFICIENT_FUNDS",
            "Insufficient funds for this movement.",
            {
                "available": format_amount(available),
                "requested": format_amount(-leg.amount_minor),
            },
        )
    # Should be unreachable: a credit against an ACTIVE account cannot fail.
    raise AppError("INTERNAL_ERROR", "The movement could not be applied.", status_code=500)


def apply_legs(conn: sqlite3.Connection, legs: list[Leg]) -> list[dict[str, Any]]:
    """Validate conservation, then apply each leg with a conditional UPDATE."""
    if len(legs) < 2:
        raise AppError("INTERNAL_ERROR", "A movement needs at least two legs.", status_code=500)
    if len({leg.account_id for leg in legs}) != len(legs):
        # from == to would otherwise debit and credit the same account.
        raise validation_error("Source and destination accounts must be different.")
    total = sum(leg.amount_minor for leg in legs)
    if total != 0:  # invariant guard: never post an unbalanced transaction
        raise AppError(
            "INTERNAL_ERROR",
            "Refusing to post an unbalanced transaction.",
            status_code=500,
            details={"sum_minor": total},
        )

    applied: list[Leg] = []
    # Deterministic order: on Postgres this is the deadlock-avoidance lock order.
    for leg in sorted(legs, key=lambda item: item.account_id):
        cursor = conn.execute(
            """
            UPDATE accounts
               SET balance_minor = balance_minor + ?,
                   version = version + 1
             WHERE id = ?
               AND status = 'ACTIVE'
               AND (is_system = 1 OR balance_minor + ? >= 0)
            """,
            (leg.amount_minor, leg.account_id, leg.amount_minor),
        )
        if cursor.rowcount != 1:
            raise _explain_failed_leg(conn, leg)
        applied.append(leg)
    return [
        {"account_id": leg.account_id, "amount_minor": leg.amount_minor}
        for leg in applied
    ]


def post_transaction(
    conn: sqlite3.Connection,
    *,
    tx_type: str,
    currency: str,
    amount_minor: int,
    initiator_user_id: str | None,
    legs: list[Leg],
    description: str | None = None,
) -> dict[str, Any]:
    """Write the transaction header, the legs and the hash-chained entries."""
    if amount_minor <= 0:
        raise validation_error("Amount must be greater than zero.")
    if tx_type not in {"DEPOSIT", "WITHDRAWAL", "TRANSFER"}:
        raise AppError("INTERNAL_ERROR", "Unknown transaction type.", status_code=500)

    applied = apply_legs(conn, legs)

    transaction_id = str(uuid.uuid4())
    created_at = utcnow_iso()
    conn.execute(
        """
        INSERT INTO transactions
            (id, type, currency, amount_minor, initiator_user_id, description, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (transaction_id, tx_type, currency, amount_minor, initiator_user_id, description, created_at),
    )

    entries: list[dict[str, Any]] = []
    for leg in applied:
        entry_id, prev_hash, entry_hash = chain.append_ledger_entry(
            conn,
            transaction_id=transaction_id,
            account_id=leg["account_id"],
            amount_minor=leg["amount_minor"],
            currency=currency,
            created_at=created_at,
        )
        conn.execute(
            """
            INSERT INTO ledger_entries
                (id, transaction_id, account_id, amount_minor, currency, created_at,
                 prev_hash, entry_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry_id,
                transaction_id,
                leg["account_id"],
                leg["amount_minor"],
                currency,
                created_at,
                prev_hash,
                entry_hash,
            ),
        )
        entries.append(
            {
                "id": entry_id,
                "account_id": leg["account_id"],
                "direction": DIRECTION_CREDIT if leg["amount_minor"] > 0 else DIRECTION_DEBIT,
                "amount": format_amount(abs(leg["amount_minor"])),
                "amount_minor": abs(leg["amount_minor"]),
                "signed_amount_minor": leg["amount_minor"],
                "currency": currency,
            }
        )

    return {
        "id": transaction_id,
        "type": tx_type,
        "currency": currency,
        "amount": format_amount(amount_minor),
        "amount_minor": amount_minor,
        "description": description,
        "initiator_user_id": initiator_user_id,
        "created_at": created_at,
        "entries": entries,
    }
