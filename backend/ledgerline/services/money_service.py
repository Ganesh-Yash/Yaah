"""Deposits, withdrawals and transfers with at-most-once semantics.

The idempotency contract (same shape as Stripe's):

1. Every money-moving request must carry ``Idempotency-Key``.
2. The key is scoped to ``(user, route)``. Two different requests that reuse a
   key are rejected with ``409 IDEMPOTENCY_KEY_REUSED`` -- reusing a key with a
   different body is a client bug, and silently paying out twice is not an
   acceptable way to handle it.
3. The idempotency record and the money movement are written in the **same**
   database transaction. A crash either loses both (safe retry) or keeps both
   (a replay returns the first result). There is no "money moved but the key
   was not saved" window, which is the bug that makes naive implementations
   double-pay.
4. Failed attempts (insufficient funds, frozen account, ...) roll back and are
   not cached: nothing moved, so a retry with the same key is a fresh attempt.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Callable

from .. import audit
from ..chain import canonical_json, sha256_hex
from ..config import Settings
from ..db import Database, utcnow_iso
from ..errors import AppError, conflict, not_found, validation_error
from ..ledger import Leg, get_account, get_account_by_number, post_transaction, system_account_id
from ..money import format_amount, parse_amount
from .accounts_service import Principal, serialize_account

IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
_DESCRIPTION_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_DESCRIPTION_LENGTH = 140


def validate_idempotency_key(raw: str | None) -> str:
    if not raw:
        raise AppError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "This endpoint requires an Idempotency-Key header so that retries cannot pay twice.",
            status_code=400,
        )
    if not IDEMPOTENCY_KEY_RE.match(raw):
        raise validation_error(
            "Idempotency-Key must be 8-128 characters of A-Z a-z 0-9 _ . : -"
        )
    return raw


def sanitize_description(raw: str | None) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise validation_error("description must be text.")
    cleaned = _DESCRIPTION_RE.sub("", raw).strip()
    if len(cleaned) > MAX_DESCRIPTION_LENGTH:
        raise validation_error(f"description must be at most {MAX_DESCRIPTION_LENGTH} characters.")
    return cleaned or None


def _validate_amount(raw: object, settings: Settings) -> int:
    amount_minor = parse_amount(raw)
    if amount_minor <= 0:
        raise validation_error("amount must be greater than zero.")
    if amount_minor > settings.max_amount_minor:
        raise validation_error(
            "amount exceeds the per-transaction limit.",
            {"max_amount": format_amount(settings.max_amount_minor)},
        )
    return amount_minor


def fingerprint(path: str, payload: dict[str, Any]) -> str:
    return sha256_hex(canonical_json({"path": path, "body": payload}))


def execute_idempotent(
    db: Database,
    *,
    principal: Principal,
    scope: str,
    key: str,
    request_hash: str,
    action: str,
    target_type: str,
    target_id: str,
    detail: dict[str, Any],
    operation: Callable[[sqlite3.Connection], dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """Run ``operation`` exactly once for ``(principal, scope, key)``.

    Returns ``(response_body, replayed)``.
    """
    result: dict[str, Any] | None = None
    replay: dict[str, Any] | None = None
    key_conflict: AppError | None = None

    with db.write() as conn:
        existing = conn.execute(
            "SELECT * FROM idempotency_keys WHERE user_id = ? AND scope = ? AND key = ?",
            (principal.id, scope, key),
        ).fetchone()

        if existing is not None:
            if existing["request_hash"] != request_hash:
                key_conflict = conflict(
                    "IDEMPOTENCY_KEY_REUSED",
                    "This Idempotency-Key was already used for a different request.",
                    {"original_transaction_id": existing["transaction_id"]},
                )
            else:
                replay = json.loads(existing["response_body"])
        else:
            result = operation(conn)
            transaction_id = result.get("transaction", {}).get("id")
            conn.execute(
                """
                INSERT INTO idempotency_keys
                    (id, user_id, scope, key, request_hash, response_status,
                     response_body, transaction_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sha256_hex(f"{principal.id}:{scope}:{key}")[:32],
                    principal.id,
                    scope,
                    key,
                    request_hash,
                    201,
                    canonical_json(result),
                    transaction_id,
                    utcnow_iso(),
                ),
            )
            audit.record_in(
                conn,
                action,
                actor=principal,
                target_type=target_type,
                target_id=target_id,
                detail={**detail, "transaction_id": transaction_id, "idempotency_key": key},
            )

    # Denials and replays are audited outside the transaction: the failure above
    # already rolled back, so there is nothing to keep atomic with.
    if key_conflict is not None:
        audit.record(
            db,
            "IDEMPOTENCY_KEY_REUSED",
            outcome="DENY",
            actor=principal,
            target_type=target_type,
            target_id=target_id,
            detail={"idempotency_key": key, "scope": scope},
        )
        raise key_conflict

    if replay is not None:
        audit.record(
            db,
            "IDEMPOTENT_REPLAY",
            actor=principal,
            target_type=target_type,
            target_id=target_id,
            detail={
                "idempotency_key": key,
                "scope": scope,
                "original_transaction_id": replay.get("transaction", {}).get("id"),
            },
        )
        return replay, True

    assert result is not None  # for type checkers; unreachable otherwise
    return result, False


def deposit(
    db: Database,
    settings: Settings,
    principal: Principal,
    *,
    account_id: str,
    raw_amount: object,
    description: str | None,
    idempotency_key: str,
) -> tuple[dict[str, Any], bool]:
    amount_minor = _validate_amount(raw_amount, settings)
    note = sanitize_description(description)

    def operation(conn: sqlite3.Connection) -> dict[str, Any]:
        account = get_account(conn, account_id)
        if account is None or account["owner_user_id"] != principal.id:
            raise not_found("Account")
        if account["status"] != "ACTIVE":
            raise conflict("ACCOUNT_NOT_ACTIVE", f"Account is {account['status'].lower()}.")
        currency = account["currency"]
        transaction = post_transaction(
            conn,
            tx_type="DEPOSIT",
            currency=currency,
            amount_minor=amount_minor,
            initiator_user_id=principal.id,
            legs=[
                Leg(account_id, amount_minor),
                Leg(system_account_id(currency), -amount_minor),
            ],
            description=note,
        )
        updated = get_account(conn, account_id)
        return {
            "transaction": transaction,
            "account": serialize_account(updated),
            "replayed": False,
        }

    return execute_idempotent(
        db,
        principal=principal,
        scope="POST /api/accounts/{account_id}/deposit",
        key=idempotency_key,
        request_hash=fingerprint(
            f"/api/accounts/{account_id}/deposit",
            {"amount": raw_amount, "description": note},
        ),
        action="DEPOSIT",
        target_type="account",
        target_id=account_id,
        detail={"amount": format_amount(amount_minor)},
        operation=operation,
    )


def withdraw(
    db: Database,
    settings: Settings,
    principal: Principal,
    *,
    account_id: str,
    raw_amount: object,
    description: str | None,
    idempotency_key: str,
) -> tuple[dict[str, Any], bool]:
    amount_minor = _validate_amount(raw_amount, settings)
    note = sanitize_description(description)

    def operation(conn: sqlite3.Connection) -> dict[str, Any]:
        account = get_account(conn, account_id)
        if account is None or account["owner_user_id"] != principal.id:
            raise not_found("Account")
        if account["status"] != "ACTIVE":
            raise conflict("ACCOUNT_NOT_ACTIVE", f"Account is {account['status'].lower()}.")
        currency = account["currency"]
        transaction = post_transaction(
            conn,
            tx_type="WITHDRAWAL",
            currency=currency,
            amount_minor=amount_minor,
            initiator_user_id=principal.id,
            legs=[
                Leg(account_id, -amount_minor),
                Leg(system_account_id(currency), amount_minor),
            ],
            description=note,
        )
        updated = get_account(conn, account_id)
        return {
            "transaction": transaction,
            "account": serialize_account(updated),
            "replayed": False,
        }

    return execute_idempotent(
        db,
        principal=principal,
        scope="POST /api/accounts/{account_id}/withdraw",
        key=idempotency_key,
        request_hash=fingerprint(
            f"/api/accounts/{account_id}/withdraw",
            {"amount": raw_amount, "description": note},
        ),
        action="WITHDRAWAL",
        target_type="account",
        target_id=account_id,
        detail={"amount": format_amount(amount_minor)},
        operation=operation,
    )


def transfer(
    db: Database,
    settings: Settings,
    principal: Principal,
    *,
    from_account_id: str,
    to_account_number: str,
    raw_amount: object,
    description: str | None,
    idempotency_key: str,
) -> tuple[dict[str, Any], bool]:
    amount_minor = _validate_amount(raw_amount, settings)
    note = sanitize_description(description)
    destination_number = (to_account_number or "").strip()
    if not destination_number:
        raise validation_error("to_account_number is required.")

    def operation(conn: sqlite3.Connection) -> dict[str, Any]:
        source = get_account(conn, from_account_id)
        if source is None or source["owner_user_id"] != principal.id:
            raise not_found("Account")
        if source["status"] != "ACTIVE":
            raise conflict("ACCOUNT_NOT_ACTIVE", f"Account is {source['status'].lower()}.")

        destination = get_account_by_number(conn, destination_number)
        if destination is None:
            raise not_found("Destination account")
        if destination["id"] == source["id"]:
            raise validation_error("You cannot transfer to the same account.")
        if destination["is_system"]:
            # Otherwise a customer could "pay" the clearing account and burn money.
            raise validation_error("Transfers must go to a customer account.")
        if destination["currency"] != source["currency"]:
            raise validation_error(
                "Source and destination accounts must use the same currency.",
                {"source_currency": source["currency"], "destination_currency": destination["currency"]},
            )
        if destination["status"] != "ACTIVE":
            raise conflict(
                "DESTINATION_NOT_ACTIVE",
                "The destination account cannot receive funds right now.",
            )

        transaction = post_transaction(
            conn,
            tx_type="TRANSFER",
            currency=source["currency"],
            amount_minor=amount_minor,
            initiator_user_id=principal.id,
            legs=[
                Leg(source["id"], -amount_minor),
                Leg(destination["id"], amount_minor),
            ],
            description=note,
        )
        updated_source = get_account(conn, source["id"])
        return {
            "transaction": transaction,
            "account": serialize_account(updated_source),
            "from_account": serialize_account(updated_source),
            "to_account": {
                "id": destination["id"],
                "account_number": destination["account_number"],
                "name": destination["name"],
                "currency": destination["currency"],
            },
            "replayed": False,
        }

    return execute_idempotent(
        db,
        principal=principal,
        scope="POST /api/transfers",
        key=idempotency_key,
        request_hash=fingerprint(
            "/api/transfers",
            {
                "from_account_id": from_account_id,
                "to_account_number": destination_number,
                "amount": raw_amount,
                "description": note,
            },
        ),
        action="TRANSFER",
        target_type="account",
        target_id=from_account_id,
        detail={
            "amount": format_amount(amount_minor),
            "to_account_number": destination_number,
        },
        operation=operation,
    )
