"""Administrator endpoints: account opening, freeze/unfreeze, staff users."""

from __future__ import annotations

import sqlite3
import uuid

from fastapi import APIRouter, Depends, Query, status

from .. import audit, security
from ..config import Settings
from ..db import Database, utcnow_iso
from ..deps import get_db, get_settings, require_roles
from ..errors import conflict, not_found, validation_error
from ..models import CreateAccountRequest, CreateStaffUserRequest, FreezeRequest
from ..services import accounts_service
from ..services.accounts_service import Principal

router = APIRouter(prefix="/api", tags=["admin"])

ADMIN_ONLY = Depends(require_roles("ADMIN"))


@router.post("/accounts", status_code=status.HTTP_201_CREATED)
def create_account(
    payload: CreateAccountRequest,
    db: Database = Depends(get_db),
    actor: Principal = ADMIN_ONLY,
) -> dict:
    account = accounts_service.create_account(
        db, actor, owner_email=payload.owner_email, currency=payload.currency, name=payload.name
    )
    return {"account": account}


@router.post("/accounts/{account_id}/freeze")
def freeze_account(
    account_id: str,
    payload: FreezeRequest,
    db: Database = Depends(get_db),
    actor: Principal = ADMIN_ONLY,
) -> dict:
    account = accounts_service.set_frozen(
        db, actor, account_id=account_id, frozen=payload.frozen
    )
    return {"account": account}


@router.get("/admin/users")
def list_users(
    db: Database = Depends(get_db),
    actor: Principal = ADMIN_ONLY,
) -> dict:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT id, email, full_name, role, status, failed_login_count, locked_until, created_at"
            " FROM users ORDER BY created_at ASC"
        ).fetchall()
    return {"users": [dict(row) for row in rows]}


@router.post("/admin/users", status_code=status.HTTP_201_CREATED)
def create_staff_user(
    payload: CreateStaffUserRequest,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = ADMIN_ONLY,
) -> dict:
    email = security.normalize_email(payload.email)
    security.validate_password(payload.password)
    name = payload.full_name.strip()
    if not 2 <= len(name) <= 120:
        raise validation_error("full_name must be 2-120 characters.")
    user_id = str(uuid.uuid4())
    now = utcnow_iso()
    password_hash = security.hash_password(payload.password, settings.bcrypt_rounds)
    try:
        with db.write() as conn:
            conn.execute(
                """
                INSERT INTO users (id, email, full_name, password_hash, role, status,
                                   failed_login_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'ACTIVE', 0, ?, ?)
                """,
                (user_id, email, name, password_hash, payload.role, now, now),
            )
            audit.record_in(
                conn,
                "STAFF_USER_CREATED",
                actor=actor,
                target_type="user",
                target_id=user_id,
                detail={"email": email, "role": payload.role},
            )
    except sqlite3.IntegrityError as exc:
        raise conflict("EMAIL_ALREADY_REGISTERED", "An account with this email already exists.") from exc
    return {
        "user": {
            "id": user_id,
            "email": email,
            "full_name": name,
            "role": payload.role,
            "status": "ACTIVE",
            "created_at": now,
        }
    }


@router.post("/admin/users/{user_id}/unlock")
def unlock_user(
    user_id: str,
    db: Database = Depends(get_db),
    actor: Principal = ADMIN_ONLY,
) -> dict:
    with db.write() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise not_found("User")
        conn.execute(
            "UPDATE users SET failed_login_count = 0, locked_until = NULL, updated_at = ? WHERE id = ?",
            (utcnow_iso(), user_id),
        )
        audit.record_in(
            conn,
            "USER_UNLOCKED",
            actor=actor,
            subject=row["email"],
            target_type="user",
            target_id=user_id,
        )
    return {"unlocked": True, "user_id": user_id}
