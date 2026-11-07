"""FastAPI dependencies: settings, database, authentication, RBAC, throttles."""

from __future__ import annotations

from typing import Annotated, Callable

from fastapi import Depends, Header, Request

from . import audit, security
from .config import Settings
from .db import Database
from .errors import AppError, rate_limited, unauthenticated
from .services.accounts_service import Principal


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def client_ip(request: Request) -> str | None:
    settings: Settings = request.app.state.settings
    if settings.trusted_hosts and request.headers.get("x-forwarded-for"):
        # Only when the operator explicitly said a reverse proxy is in front.
        return request.headers["x-forwarded-for"].split(",")[0].strip()
    return request.client.host if request.client else None


def current_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Authenticate the bearer token and load the *current* user record.

    The role and status always come from the database. A token issued before a
    role change or a disable therefore cannot be used to keep old privileges.
    """
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db

    if not authorization:
        raise unauthenticated("Authentication required.")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise unauthenticated("Authorization header must be 'Bearer <token>'.")

    claims = security.decode_access_token(token.strip(), settings)
    with db.read() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (claims.get("sub"),)).fetchone()
        session = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (claims.get("sid"),)
        ).fetchone()

    if user is None:
        raise unauthenticated("This session is no longer valid.")
    if session is None or session["revoked_at"] is not None or session["user_id"] != user["id"]:
        # Logout, refresh-token reuse or a password change revokes the session,
        # which kills access tokens immediately rather than at expiry.
        raise unauthenticated("This session has been revoked; sign in again.")
    if user["status"] != "ACTIVE":
        raise AppError("ACCOUNT_DISABLED", "This account is not active.", status_code=403)
    if claims.get("pwd") != security.password_fingerprint(user["password_hash"]):
        # Password changed (or was reset) after this token was issued.
        raise unauthenticated("This session is no longer valid; sign in again.")

    return Principal(
        id=user["id"],
        email=user["email"],
        full_name=user["full_name"],
        role=user["role"],
        status=user["status"],
    )


def _record_denial(db: Database, principal: Principal, required_roles: list[str]) -> None:
    """Audit a refused authorisation check (best effort, never masks the 403)."""
    audit.record(
        db,
        "AUTHZ_DENIED",
        outcome="DENY",
        actor=principal,
        detail={"required_roles": required_roles, "actual_role": principal.role},
    )


def require_roles(*roles: str) -> Callable[..., Principal]:
    def dependency(
        request: Request, principal: Principal = Depends(current_principal)
    ) -> Principal:
        if principal.role not in roles:
            _record_denial(request.app.state.db, principal, list(roles))
            raise AppError(
                "FORBIDDEN",
                "Your role does not permit this action.",
                status_code=403,
                details={"required_roles": list(roles)},
            )
        return principal

    return dependency


def require_customer(
    request: Request, principal: Principal = Depends(current_principal)
) -> Principal:
    if principal.role != "CUSTOMER":
        _record_denial(request.app.state.db, principal, ["CUSTOMER"])
        raise AppError(
            "FORBIDDEN",
            "Only account holders can move money; administrators and auditors have read-only money access.",
            status_code=403,
        )
    return principal


def enforce_money_rate_limit(
    request: Request, principal: Principal = Depends(current_principal)
) -> None:
    limiter = request.app.state.money_limiter
    if not limiter.allow(principal.id):
        audit.record(
            request.app.state.db,
            "RATE_LIMITED",
            outcome="DENY",
            actor=principal,
            detail={"scope": "money_write"},
        )
        raise rate_limited("Too many money movements in a short time. Please slow down.")
