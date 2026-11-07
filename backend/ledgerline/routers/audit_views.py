"""Auditor endpoints: the audit trail and independent integrity verification."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..db import Database
from ..deps import get_db, require_roles
from ..services import integrity_service
from ..services.accounts_service import Principal

router = APIRouter(prefix="/api", tags=["audit"])

STAFF_READ = Depends(require_roles("ADMIN", "AUDITOR"))


@router.get("/audit")
def list_audit(
    limit: int = Query(default=100, ge=1, le=500),
    before_id: int | None = Query(default=None, ge=1),
    action: str | None = Query(default=None, max_length=64),
    outcome: str | None = Query(default=None, max_length=16),
    db: Database = Depends(get_db),
    principal: Principal = STAFF_READ,
) -> dict:
    clauses = []
    params: list[object] = []
    if before_id is not None:
        clauses.append("id < ?")
        params.append(before_id)
    if action:
        clauses.append("action = ?")
        params.append(action)
    if outcome:
        clauses.append("outcome = ?")
        params.append(outcome)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with db.read() as conn:
        rows = conn.execute(
            f"""
            SELECT id, actor_user_id, actor_role, subject, action, outcome, target_type,
                   target_id, ip, user_agent, request_id, detail, created_at, entry_hash
              FROM audit_log {where}
             ORDER BY id DESC
             LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    return {
        "entries": [
            {
                "id": int(row["id"]),
                "actor_user_id": row["actor_user_id"],
                "actor_role": row["actor_role"],
                "subject": row["subject"],
                "action": row["action"],
                "outcome": row["outcome"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "ip": row["ip"],
                "user_agent": row["user_agent"],
                "request_id": row["request_id"],
                "detail": row["detail"],
                "created_at": row["created_at"],
                "entry_hash": row["entry_hash"],
            }
            for row in rows
        ],
        "next_cursor": int(rows[-1]["id"]) if len(rows) == limit else None,
    }


@router.get("/auditor/integrity")
def integrity(
    db: Database = Depends(get_db),
    principal: Principal = STAFF_READ,
) -> dict:
    return integrity_service.verify(db)
