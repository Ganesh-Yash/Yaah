"""Database seeding: system accounts and an optional demo dataset.

Demo credentials are only created when ``LEDGERLINE_DEMO_SEED=1`` (or the seed
script is run explicitly), so a deployment cannot silently ship with known
passwords.
"""

from __future__ import annotations

import sys
from typing import Any

from . import audit
from .config import Settings
from .db import Database, utcnow_iso
from .ledger import Leg, ensure_system_accounts, post_transaction, system_account_id
from .money import SUPPORTED_CURRENCIES
from .security import hash_password, normalize_email

DEMO_PASSWORD = "LedgerLine!Demo2024"

DEMO_USERS: tuple[tuple[str, str, str], ...] = (
    ("admin@ledgerline.test", "Ada Administrator", "ADMIN"),
    ("auditor@ledgerline.test", "Avery Auditor", "AUDITOR"),
    ("alice@ledgerline.test", "Alice Account", "CUSTOMER"),
    ("bob@ledgerline.test", "Bob Balance", "CUSTOMER"),
)

# (owner email, account name, currency, opening deposit in minor units)
DEMO_ACCOUNTS: tuple[tuple[str, str, str, int], ...] = (
    ("alice@ledgerline.test", "Alice — Everyday", "USD", 125_000),
    ("alice@ledgerline.test", "Alice — Savings", "USD", 50_000),
    ("bob@ledgerline.test", "Bob — Everyday", "USD", 30_000),
)


def bootstrap(db: Database) -> None:
    """Idempotent: make sure the external-cash clearing accounts exist."""
    with db.write() as conn:
        ensure_system_accounts(conn, SUPPORTED_CURRENCIES)


def seed_demo(db: Database, settings: Settings, password: str = DEMO_PASSWORD) -> dict[str, Any]:
    bootstrap(db)
    created: list[str] = []
    for email, full_name, role in DEMO_USERS:
        normalized = normalize_email(email)
        with db.read() as conn:
            exists = conn.execute("SELECT 1 FROM users WHERE email = ?", (normalized,)).fetchone()
        if exists:
            continue
        import uuid

        user_id = str(uuid.uuid4())
        now = utcnow_iso()
        with db.write() as conn:
            conn.execute(
                """
                INSERT INTO users (id, email, full_name, password_hash, role, status,
                                   failed_login_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'ACTIVE', 0, ?, ?)
                """,
                (
                    user_id,
                    normalized,
                    full_name,
                    hash_password(password, settings.bcrypt_rounds),
                    role,
                    now,
                    now,
                ),
            )
            audit.record_in(
                conn,
                "DEMO_USER_SEEDED",
                target_type="user",
                target_id=user_id,
                detail={"email": normalized, "role": role},
            )
        created.append(normalized)

    import secrets
    import string
    import uuid

    for owner_email, name, currency, opening in DEMO_ACCOUNTS:
        normalized = normalize_email(owner_email)
        with db.read() as conn:
            owner = conn.execute("SELECT * FROM users WHERE email = ?", (normalized,)).fetchone()
            if owner is None:
                continue
            existing = conn.execute(
                "SELECT 1 FROM accounts WHERE owner_user_id = ? AND name = ?",
                (owner["id"], name),
            ).fetchone()
        if existing:
            continue
        account_id = str(uuid.uuid4())
        number = "40" + "".join(secrets.choice(string.digits) for _ in range(10))
        with db.write() as conn:
            conn.execute(
                """
                INSERT INTO accounts (id, account_number, owner_user_id, name, currency,
                                      balance_minor, is_system, status, version, created_at)
                VALUES (?, ?, ?, ?, ?, 0, 0, 'ACTIVE', 0, ?)
                """,
                (account_id, number, owner["id"], name, currency, utcnow_iso()),
            )
            post_transaction(
                conn,
                tx_type="DEPOSIT",
                currency=currency,
                amount_minor=opening,
                initiator_user_id=None,
                legs=[
                    Leg(account_id, opening),
                    Leg(system_account_id(currency), -opening),
                ],
                description="Opening deposit (demo seed)",
            )
            audit.record_in(
                conn,
                "DEMO_ACCOUNT_SEEDED",
                target_type="account",
                target_id=account_id,
                detail={"owner": normalized, "currency": currency, "opening_minor": opening},
            )
        created.append(number)

    if created:
        print(
            f"[ledgerline] demo seed created: {', '.join(created)} (password: {password})",
            file=sys.stderr,
        )
    return {"created": created, "password": password if created else None}
