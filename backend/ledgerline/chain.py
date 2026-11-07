"""Append-only hash chains for the ledger and the audit log.

Each appended row stores ``prev_hash`` (the previous row's hash) and
``entry_hash = SHA256(domain || id || prev_hash || canonical fields...)``.

Why: a database backup, a rogue DBA or a compromised app cannot rewrite
history without breaking every subsequent hash. ``GET /api/auditor/integrity``
re-walks both chains and reports the first divergent row. Because the chain is
keyed only by a public hash function, anyone holding a copy of the database can
verify it independently -- no secret needed.

This is *tamper-evident*, not tamper-proof: an attacker with write access can
recompute the whole chain. Detecting that requires anchoring the head hash
somewhere they do not control (see THREATS.md, "residual risks").
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

GENESIS_HASH = "0" * 64
LEDGER_DOMAIN = "ledgerline/ledger/v1"
AUDIT_DOMAIN = "ledgerline/audit/v1"
SEPARATOR = "\x1f"  # unit separator: cannot appear in our canonical fields


def canonical(value: Any) -> str:
    """Deterministic string form used inside hash payloads.

    ``None`` becomes the empty string so that a NULL and a vanished column
    cannot produce the same hash as another distinct value.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(domain: str, row_id: int, prev_hash: str, fields: tuple[Any, ...]) -> str:
    payload = SEPARATOR.join([domain, canonical(row_id), prev_hash, *map(canonical, fields)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _head(conn: sqlite3.Connection, chain: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT last_id, last_hash FROM chain_state WHERE name = ?", (chain,)
    ).fetchone()
    if row is None:  # pragma: no cover - schema always seeds both rows
        raise RuntimeError(f"chain_state row missing for {chain}")
    return row


def append_ledger_entry(
    conn: sqlite3.Connection,
    *,
    transaction_id: str,
    account_id: str,
    amount_minor: int,
    currency: str,
    created_at: str,
) -> tuple[int, str, str]:
    """Reserve the next ledger id and hash, and persist the chain head.

    Must be called inside a ``BEGIN IMMEDIATE`` transaction so that the head is
    read and advanced atomically.
    """
    head = _head(conn, "ledger")
    entry_id = int(head["last_id"]) + 1
    prev_hash = str(head["last_hash"])
    fields = (transaction_id, account_id, amount_minor, currency, created_at)
    entry_hash = compute_hash(LEDGER_DOMAIN, entry_id, prev_hash, fields)
    conn.execute(
        "UPDATE chain_state SET last_id = ?, last_hash = ? WHERE name = 'ledger'",
        (entry_id, entry_hash),
    )
    return entry_id, prev_hash, entry_hash


def append_audit_entry(
    conn: sqlite3.Connection,
    *,
    actor_user_id: str | None,
    actor_role: str | None,
    subject: str | None,
    action: str,
    outcome: str,
    target_type: str | None,
    target_id: str | None,
    ip: str | None,
    user_agent: str | None,
    request_id: str | None,
    detail: str | None,
    created_at: str,
) -> tuple[int, str, str]:
    head = _head(conn, "audit")
    entry_id = int(head["last_id"]) + 1
    prev_hash = str(head["last_hash"])
    fields = (
        actor_user_id,
        actor_role,
        subject,
        action,
        outcome,
        target_type,
        target_id,
        ip,
        user_agent,
        request_id,
        detail,
        created_at,
    )
    entry_hash = compute_hash(AUDIT_DOMAIN, entry_id, prev_hash, fields)
    conn.execute(
        "UPDATE chain_state SET last_id = ?, last_hash = ? WHERE name = 'audit'",
        (entry_id, entry_hash),
    )
    return entry_id, prev_hash, entry_hash


def verify_ledger_chain(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT id, transaction_id, account_id, amount_minor, currency, created_at,
               prev_hash, entry_hash
          FROM ledger_entries
         ORDER BY id ASC
        """
    ).fetchall()
    return _verify(rows, LEDGER_DOMAIN, "ledger", conn)


def verify_audit_chain(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT id, actor_user_id, actor_role, subject, action, outcome, target_type,
               target_id, ip, user_agent, request_id, detail, created_at,
               prev_hash, entry_hash
          FROM audit_log
         ORDER BY id ASC
        """
    ).fetchall()
    return _verify(rows, AUDIT_DOMAIN, "audit", conn)


def _verify(rows: list[sqlite3.Row], domain: str, chain: str, conn: sqlite3.Connection) -> dict[str, Any]:
    expected_prev = GENESIS_HASH
    expected_id = 1
    last_hash = GENESIS_HASH
    for row in rows:
        fields_map = {
            "ledger": (
                "transaction_id",
                "account_id",
                "amount_minor",
                "currency",
                "created_at",
            ),
            "audit": (
                "actor_user_id",
                "actor_role",
                "subject",
                "action",
                "outcome",
                "target_type",
                "target_id",
                "ip",
                "user_agent",
                "request_id",
                "detail",
                "created_at",
            ),
        }[chain]
        recomputed = compute_hash(
            domain, row["id"], row["prev_hash"], tuple(row[name] for name in fields_map)
        )
        problems: list[str] = []
        if int(row["id"]) != expected_id:
            problems.append(f"id gap: expected {expected_id}, found {row['id']}")
        if row["prev_hash"] != expected_prev:
            problems.append(f"prev_hash mismatch at id {row['id']}")
        if recomputed != row["entry_hash"]:
            problems.append(f"hash mismatch at id {row['id']}")
        if problems:
            return {
                "ok": False,
                "chain": chain,
                "rows": len(rows),
                "first_bad_id": int(row["id"]),
                "problems": problems,
                "head_hash": last_hash,
            }
        expected_prev = str(row["entry_hash"])
        last_hash = expected_prev
        expected_id += 1

    head = _head(conn, chain)
    head_ok = int(head["last_id"]) == len(rows) and str(head["last_hash"]) == last_hash
    return {
        "ok": head_ok,
        "chain": chain,
        "rows": len(rows),
        "first_bad_id": None,
        "problems": [] if head_ok else ["chain_state head does not match the last row"],
        "head_hash": last_hash,
    }
