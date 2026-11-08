"""Health and metadata (unauthenticated, no sensitive data)."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Request

from .. import __version__
from ..db import utcnow_iso
from ..money import format_amount, SUPPORTED_CURRENCIES

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/health")
def health(request: Request) -> dict:
    db = request.app.state.db
    database_ok = True
    detail = "ok"
    try:
        with db.read() as conn:
            conn.execute("SELECT 1").fetchone()
    except sqlite3.Error as exc:  # pragma: no cover - only on a broken volume
        database_ok = False
        detail = exc.__class__.__name__
    return {
        "status": "ok" if database_ok else "degraded",
        "service": "ledgerline-api",
        "version": __version__,
        "database": detail,
        "time": utcnow_iso(),
    }


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
