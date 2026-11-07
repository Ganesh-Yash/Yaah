"""Append-only audit trail helpers.

Rules:

* Successes are recorded inside the same transaction as the action they
  describe, so "money moved" and "audit row written" cannot diverge.
* Denials are recorded *after* the failing transaction has rolled back, on a
  fresh connection, and are best-effort: a failure to write the audit row must
  never turn a clean 403 into a 500.
* Unauthenticated token failures are deliberately **not** audited: writing on
  every forged-token attempt turns the audit trail into an unauthenticated disk
  amplification vector. Those requests are still logged by the access log.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any

from . import ctx as request_ctx
from .chain import append_audit_entry, canonical_json
from .db import Database, utcnow_iso


def _insert(
    conn: sqlite3.Connection,
    *,
    action: str,
    outcome: str,
    actor_user_id: str | None,
    actor_role: str | None,
    subject: str | None,
    target_type: str | None,
    target_id: str | None,
    detail: dict[str, Any] | None,
) -> int:
    context = request_ctx.get_context()
    created_at = utcnow_iso()
    detail_json = canonical_json(detail) if detail is not None else None
    entry_id, prev_hash, entry_hash = append_audit_entry(
        conn,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        subject=subject,
        action=action,
        outcome=outcome,
        target_type=target_type,
        target_id=target_id,
        ip=context.ip,
        user_agent=context.user_agent,
        request_id=context.request_id,
        detail=detail_json,
        created_at=created_at,
    )
    conn.execute(
        """
        INSERT INTO audit_log (id, actor_user_id, actor_role, subject, action, outcome,
                               target_type, target_id, ip, user_agent, request_id, detail,
                               created_at, prev_hash, entry_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry_id,
            actor_user_id,
            actor_role,
            subject,
            action,
            outcome,
            target_type,
            target_id,
            context.ip,
            context.user_agent,
            context.request_id,
            detail_json,
            created_at,
            prev_hash,
            entry_hash,
        ),
    )
    return entry_id


def record_in(
    conn: sqlite3.Connection,
    action: str,
    *,
    outcome: str = "ALLOW",
    actor: Any | None = None,
    subject: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> int:
    """Append an audit row using an already-open write transaction."""
    return _insert(
        conn,
        action=action,
        outcome=outcome,
        actor_user_id=getattr(actor, "id", None),
        actor_role=getattr(actor, "role", None),
        subject=subject,
        target_type=target_type,
        target_id=target_id,
        detail=detail,
    )


def record(db: Database, action: str, *, outcome: str = "ALLOW", **kwargs: Any) -> None:
    """Append an audit row in its own short transaction (used for denials)."""
    try:
        with db.write() as conn:
            record_in(conn, action, outcome=outcome, **kwargs)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[ledgerline] audit write failed for {action}: {exc!r}", file=sys.stderr)


def safe_record(db: Database, action: str, *, outcome: str = "DENY", **kwargs: Any) -> None:
    record(db, action, outcome=outcome, **kwargs)


def parse_detail(row: sqlite3.Row | dict[str, Any]) -> Any:
    raw = row["detail"]
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:  # pragma: no cover
        return {"unparsed": raw}
