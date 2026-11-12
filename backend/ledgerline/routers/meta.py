"""Health and metadata (unauthenticated, no sensitive data)."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..db import utcnow_iso
from ..money import format_amount, SUPPORTED_CURRENCIES

router = APIRouter(prefix="/api", tags=["meta"])


def _redis_state(request: Request) -> tuple[str, str]:
    """(state, detail) for the shared limiter backend."""
    client = getattr(request.app.state, "redis", None)
    if client is None:
        return "disabled", "in-process limiter (single worker)"
    try:
        client.ping()
        return "ok", "shared limiter"
    except Exception as exc:  # pragma: no cover - only when Redis is down
        return "unavailable", exc.__class__.__name__


@router.get("/health")
def health(request: Request) -> dict:
    """Liveness + dependency report. Never raises, never leaks a DSN.

    A load balancer should poll ``/api/ready``; this endpoint is for humans and
    for the deployment check in docs/DEPLOYMENT.md.
    """
    db = request.app.state.db
    database_ok, database_detail = db.ping()
    redis_state, redis_detail = _redis_state(request)
    try:
        schema_version = db.schema_version()
    except Exception:  # pragma: no cover - pre-migration database
        schema_version = None
    return {
        "status": "ok" if database_ok else "degraded",
        "service": "ledgerline-api",
        "version": __version__,
        "database": database_detail,
        "backend": getattr(db, "backend", "sqlite"),
        "time": utcnow_iso(),
        "checks": {
            "database": {"ok": database_ok, "detail": database_detail},
            "redis": {"ok": redis_state in {"ok", "disabled"}, "state": redis_state, "detail": redis_detail},
            "schema": {"version": schema_version},
        },
    }


@router.get("/ready", include_in_schema=False)
def ready(request: Request) -> JSONResponse:
    """Readiness probe: 503 until the database answers and the schema is present."""
    db = request.app.state.db
    database_ok, database_detail = db.ping()
    schema_version = db.schema_version() if database_ok else None
    ready_now = database_ok and schema_version is not None
    body = {
        "ready": ready_now,
        "database": {"ok": database_ok, "detail": database_detail},
        "schema_version": schema_version,
        "time": utcnow_iso(),
    }
    return JSONResponse(status_code=200 if ready_now else 503, content=body)


@router.get("/meta")
def meta(request: Request) -> dict:
    settings = request.app.state.settings
    return {
        "name": "LedgerLine",
        "version": __version__,
        "environment": settings.env,
        "supported_currencies": list(SUPPORTED_CURRENCIES),
        "max_amount": format_amount(settings.max_amount_minor),
        "daily_outflow_limit": (
            format_amount(settings.daily_outflow_limit_minor)
            if settings.daily_outflow_limit_minor > 0
            else "unlimited"
        ),
        "max_request_bytes": settings.max_request_bytes,
        "idempotency_header": "Idempotency-Key",
        "roles": ["ADMIN", "CUSTOMER", "AUDITOR"],
    }
