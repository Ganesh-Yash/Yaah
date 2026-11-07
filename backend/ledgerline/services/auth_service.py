"""Registration, login, refresh-token rotation and logout."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import audit, security
from ..config import Settings
from ..db import Database, iso_add_seconds, utcnow_iso
from ..errors import AppError, conflict, invalid_credentials, rate_limited, unauthenticated
from ..security import TokenPair


def serialize_user(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "full_name": row["full_name"],
        "role": row["role"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


def _create_refresh_token(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    settings: Settings,
    family_id: str | None = None,
    previous_id: str | None = None,
    ip: str | None = None,
) -> tuple[str, str]:
    plaintext, token_hash = security.new_refresh_token()
    token_id = str(uuid.uuid4())
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO refresh_tokens
            (id, user_id, family_id, token_hash, issued_at, expires_at, created_ip)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            token_id,
            user_id,
            family_id or str(uuid.uuid4()),
            token_hash,
            now,
            iso_add_seconds(settings.refresh_ttl_seconds),
            ip,
        ),
    )
    if previous_id is not None:
        conn.execute(
            "UPDATE refresh_tokens SET used_at = ?, replaced_by_id = ? WHERE id = ?",
            (now, token_id, previous_id),
        )
    return plaintext, token_id


def _issue_pair(
    conn: sqlite3.Connection,
    *,
    user: sqlite3.Row,
    settings: Settings,
    family_id: str | None = None,
    previous_id: str | None = None,
    ip: str | None = None,
) -> TokenPair:
    access, expires_in = security.create_access_token(
        user_id=user["id"], password_hash=user["password_hash"], settings=settings
    )
    refresh, _ = _create_refresh_token(
        conn,
        user_id=user["id"],
        settings=settings,
        family_id=family_id,
        previous_id=previous_id,
        ip=ip,
    )
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        access_expires_in=expires_in,
        refresh_expires_in=settings.refresh_ttl_seconds,
    )


def register(
    db: Database, settings: Settings, *, email: str, password: str, full_name: str
) -> dict[str, Any]:
    email = security.normalize_email(email)
    security.validate_password(password)
    name = (full_name or "").strip()
    if not 2 <= len(name) <= 120:
        raise AppError("VALIDATION_ERROR", "full_name must be 2-120 characters.", status_code=422)

    # Public registration can only ever create a CUSTOMER. The role is not part
    # of this function's inputs at all, so there is no field to tamper with.
    password_hash = security.hash_password(password, settings.bcrypt_rounds)
    now = utcnow_iso()
    user_id = str(uuid.uuid4())
    try:
        with db.write() as conn:
            conn.execute(
                """
                INSERT INTO users (id, email, full_name, password_hash, role, status,
                                   failed_login_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'CUSTOMER', 'ACTIVE', 0, ?, ?)
                """,
                (user_id, email, name, password_hash, now, now),
            )
            audit.record_in(conn, "USER_REGISTERED", subject=email, target_type="user", target_id=user_id)
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    except sqlite3.IntegrityError as exc:
        # Registration necessarily reveals whether an address is taken; this is
        # a documented, accepted trade-off (see THREATS.md).
        raise conflict("EMAIL_ALREADY_REGISTERED", "An account with this email already exists.") from exc
    return serialize_user(row)


def _window_cutoff(window_seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _consecutive_subject_failures(db: Database, *, subject: str, window_seconds: int) -> int:
    """Failures since the last success or admin unlock for this address.

    Counting from the audit log (rather than only from a counter column) means
    an admin unlock is a first-class reset event, and it also survives a
    restart.
    """
    with db.read() as conn:
        reset_id = conn.execute(
            """
            SELECT COALESCE(MAX(id), 0) FROM audit_log
             WHERE subject = ? AND action IN ('LOGIN_SUCCESS', 'USER_UNLOCKED')
            """,
            (subject,),
        ).fetchone()[0]
        return conn.execute(
            """
            SELECT COUNT(*) FROM audit_log
             WHERE action = 'LOGIN_FAILURE' AND subject = ? AND id > ? AND created_at >= ?
            """,
            (subject, int(reset_id), _window_cutoff(window_seconds)),
        ).fetchone()[0]


def _ip_failures(db: Database, *, ip: str | None, window_seconds: int) -> int:
    """A generous per-address ceiling: stops password spraying, not one user."""
    if not ip:
        return 0
    with db.read() as conn:
        return conn.execute(
            """
            SELECT COUNT(*) FROM audit_log
             WHERE action = 'LOGIN_FAILURE' AND ip = ? AND created_at >= ?
            """,
            (ip, _window_cutoff(window_seconds)),
        ).fetchone()[0]


def login(
    db: Database,
    settings: Settings,
    *,
    email: str,
    password: str,
    ip: str | None = None,
) -> tuple[TokenPair, dict[str, Any]]:
    normalized = security.normalize_email(email)
    if len(password) > security.PASSWORD_MAX_BYTES * 2:
        # Cheap rejection before bcrypt, keeps oversized bodies from burning CPU.
        raise invalid_credentials()

    failures = _consecutive_subject_failures(
        db, subject=normalized, window_seconds=settings.login_lockout_seconds
    )
    ip_failures = _ip_failures(db, ip=ip, window_seconds=settings.login_lockout_seconds)
    if failures >= settings.login_max_failures or ip_failures >= settings.login_max_failures_per_ip:
        audit.record(
            db,
            "LOGIN_BLOCKED",
            outcome="DENY",
            subject=normalized,
            detail={"reason": "too_many_failures", "window_seconds": settings.login_lockout_seconds},
        )
        raise rate_limited(
            "Too many failed sign-in attempts. Try again later."
        )

    with db.read() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = ?", (normalized,)).fetchone()

    if user is None:
        # Burn comparable CPU so timing does not reveal whether the email exists.
        security.verify_password(password, "", dummy_hash=security._DUMMY_HASH)
        audit.record(
            db, "LOGIN_FAILURE", outcome="DENY", subject=normalized, detail={"reason": "unknown_email"}
        )
        raise invalid_credentials()

    if user["status"] == "DISABLED":
        audit.record(
            db, "LOGIN_FAILURE", outcome="DENY", subject=normalized, detail={"reason": "disabled"}
        )
        raise unauthenticated("This account is disabled.")

    locked_until = user["locked_until"]
    if locked_until and locked_until > utcnow_iso():
        audit.record(
            db,
            "LOGIN_BLOCKED",
            outcome="DENY",
            subject=normalized,
            actor=None,
            detail={"reason": "locked_until", "locked_until": locked_until},
        )
        raise rate_limited("This account is temporarily locked after repeated failures.")

    if not security.verify_password(password, user["password_hash"]):
        with db.write() as conn:
            conn.execute(
                """
                UPDATE users
                   SET failed_login_count = failed_login_count + 1,
                       locked_until = CASE WHEN failed_login_count + 1 >= ? THEN ? ELSE locked_until END,
                       updated_at = ?
                 WHERE id = ?
                """,
                (
                    settings.login_max_failures,
                    iso_add_seconds(settings.login_lockout_seconds),
                    utcnow_iso(),
                    user["id"],
                ),
            )
            audit.record_in(
                conn,
                "LOGIN_FAILURE",
                outcome="DENY",
                subject=normalized,
                target_type="user",
                target_id=user["id"],
                detail={"reason": "bad_password", "failed_count": int(user["failed_login_count"]) + 1},
            )
        raise invalid_credentials()

    with db.write() as conn:
        conn.execute(
            "UPDATE users SET failed_login_count = 0, locked_until = NULL, updated_at = ? WHERE id = ?",
            (utcnow_iso(), user["id"]),
        )
        pair = _issue_pair(conn, user=user, settings=settings, ip=ip)
        audit.record_in(
            conn, "LOGIN_SUCCESS", actor=None, subject=normalized, target_type="user", target_id=user["id"]
        )
    return pair, serialize_user(user)


def refresh(db: Database, settings: Settings, *, refresh_token: str, ip: str | None = None) -> TokenPair:
    token_hash = security.hash_refresh_token(refresh_token)
    with db.read() as conn:
        row = conn.execute(
            "SELECT * FROM refresh_tokens WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        user = (
            conn.execute("SELECT * FROM users WHERE id = ?", (row["user_id"],)).fetchone()
            if row is not None
            else None
        )

    if row is None:
        raise unauthenticated("Refresh token is invalid or has expired.")

    if row["used_at"] is not None:
        # Reuse of a rotated token means either theft or a replayed backup.
        # Revoke the whole family and record it.
        with db.write() as conn:
            conn.execute(
                "UPDATE refresh_tokens SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL",
                (utcnow_iso(), row["family_id"]),
            )
            audit.record_in(
                conn,
                "REFRESH_TOKEN_REUSE",
                outcome="DENY",
                target_type="user",
                target_id=row["user_id"],
                detail={"family_id": row["family_id"]},
            )
        raise unauthenticated("Refresh token has already been used; the session was revoked.")

    if row["revoked_at"] is not None or row["expires_at"] <= utcnow_iso():
        raise unauthenticated("Refresh token is invalid or has expired.")

    if user is None or user["status"] != "ACTIVE":
        raise unauthenticated("This account can no longer sign in.")

    with db.write() as conn:
        pair = _issue_pair(
            conn,
            user=user,
            settings=settings,
            family_id=row["family_id"],
            previous_id=row["id"],
            ip=ip,
        )
        conn.execute(
            "DELETE FROM refresh_tokens WHERE expires_at < ? AND used_at IS NOT NULL",
            (utcnow_iso(),),
        )
    return pair


def logout(db: Database, *, refresh_token: str, actor: Any | None = None) -> None:
    token_hash = security.hash_refresh_token(refresh_token)
    with db.write() as conn:
        row = conn.execute(
            "SELECT * FROM refresh_tokens WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        if row is None:
            return  # idempotent: logging out twice is not an error
        conn.execute(
            "UPDATE refresh_tokens SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL",
            (utcnow_iso(), row["family_id"]),
        )
        audit.record_in(
            conn,
            "LOGOUT",
            actor=actor,
            target_type="user",
            target_id=row["user_id"],
            detail={"family_id": row["family_id"]},
        )


def change_password(
    db: Database, settings: Settings, *, user_id: str, current_password: str, new_password: str
) -> None:
    with db.read() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        raise unauthenticated()
    if not security.verify_password(current_password, user["password_hash"]):
        raise invalid_credentials()
    security.validate_password(new_password)
    new_hash = security.hash_password(new_password, settings.bcrypt_rounds)
    with db.write() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (new_hash, utcnow_iso(), user_id),
        )
        # Every existing refresh token dies with the old password; access tokens
        # die immediately too because of the 'pwd' claim fingerprint.
        conn.execute(
            "UPDATE refresh_tokens SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (utcnow_iso(), user_id),
        )
        audit.record_in(conn, "PASSWORD_CHANGED", target_type="user", target_id=user_id)
