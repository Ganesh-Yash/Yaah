"""FastAPI application factory.

Middleware order matters and is deliberate:

    ServerErrorMiddleware
      -> request context (request id, ip, user agent, body size)
        -> security headers
          -> CORS
            -> routes / exception handlers

Every response carries ``X-Request-ID``; every ``/api`` response is
``Cache-Control: no-store`` so tokens and balances never sit in a shared proxy
cache.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import sys
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from . import __version__
from . import ctx as request_ctx
from .config import Settings
from .db import database_busy_types, make_database
from .errors import AppError
from .limiter import make_limiter, make_redis_client
from .routers import accounts, admin, audit_views, auth, meta, transfers
from .seed import bootstrap, seed_demo

MAX_BODY_BYTES = 64 * 1024
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

logger = logging.getLogger("ledgerline")


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or request_ctx.get_context().request_id


def _error_payload(code: str, message: str, request_id: str, details: dict[str, Any] | None = None) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": request_id,
        }
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = make_database(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db.initialize()
        bootstrap(db)
        if settings.demo_seed_enabled:
            seed_demo(db, settings)
        print(
            f"[ledgerline] v{__version__} ready env={settings.env} db={settings.database_path}",
            file=sys.stderr,
        )
        yield

    app = FastAPI(
        title="LedgerLine API",
        version=__version__,
        description=(
            "Money movement with a double-entry ledger, at-most-once transfers "
            "(Idempotency-Key), concurrency-safe balances and an append-only, "
            "hash-chained audit trail."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = db
    redis_client = make_redis_client(settings)
    app.state.redis = redis_client
    app.state.money_limiter = make_limiter(
        redis_client, limit=settings.money_writes_per_minute, window_seconds=60, prefix="ledgerline:money"
    )
    app.state.login_limiter = make_limiter(
        redis_client, limit=100, window_seconds=60, prefix="ledgerline:login"
    )

    # --- middleware -------------------------------------------------------

    # Added first, so it sits *below* the request-context middleware: the 413 it
    # produces still gets the request id and the security headers.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        incoming = request.headers.get("x-request-id")
        request_id = incoming if incoming and _REQUEST_ID_RE.match(incoming) else request_ctx.new_request_id()
        client = request.client.host if request.client else None
        if settings.trusted_hosts and request.headers.get("x-forwarded-for"):
            client = request.headers["x-forwarded-for"].split(",")[0].strip()
        request.state.request_id = request_id
        request_ctx.set_context(
            request_ctx.RequestContext(
                request_id=request_id,
                ip=client,
                user_agent=(request.headers.get("user-agent") or "")[:200] or None,
            )
        )
        try:
            length = request.headers.get("content-length")
            if length is not None and length.isdigit() and int(length) > settings.max_request_bytes:
                response: Response = JSONResponse(
                    status_code=413,
                    content=_error_payload(
                        "PAYLOAD_TOO_LARGE",
                        f"Request body must be at most {settings.max_request_bytes} bytes.",
                        request_id,
                    ),
                )
            else:
                response = await call_next(request)
        finally:
            request_ctx.clear_context()

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; "
            f"frame-ancestors {settings.frame_ancestors}"
        )
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        if settings.is_prod:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=False,  # bearer tokens only, no cookie-based auth
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
            expose_headers=["X-Request-ID", "Idempotent-Replay", "Location"],
            max_age=600,
        )

    if settings.trusted_hosts:
        from starlette.middleware.trustedhost import TrustedHostMiddleware

        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts))

    # --- exception handlers ----------------------------------------------

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        headers = {}
        if exc.status_code == 401:
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(exc.code, exc.message, _request_id(request), exc.details),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations and messages only: never echo the submitted value back
        # (it may be a password) and never expose internals.
        errors = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ()) if part != "body"),
                "message": error.get("msg", "invalid value"),
                "type": error.get("type", "invalid"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_error_payload(
                "VALIDATION_ERROR",
                "The request body is invalid.",
                _request_id(request),
                {"errors": errors},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 413: "PAYLOAD_TOO_LARGE"}.get(
            exc.status_code, "HTTP_ERROR"
        )
        message = {404: "Resource not found.", 405: "Method not allowed."}.get(
            exc.status_code, str(exc.detail)
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(code, message, _request_id(request)),
        )

    async def database_busy_handler(request: Request, exc: Exception) -> JSONResponse:
        # Same answer for "the writer lock timed out" on either engine: the
        # client should retry with the same Idempotency-Key, which is safe.
        logger.error("database busy: %s", exc)
        return JSONResponse(
            status_code=503,
            content=_error_payload(
                "SERVICE_BUSY",
                "The ledger is busy; retry the request.",
                _request_id(request),
            ),
            headers={"Retry-After": "1"},
        )

    for _exc_type in database_busy_types():
        app.add_exception_handler(_exc_type, database_busy_handler)

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = _request_id(request)
        logger.error("unhandled error [%s]: %s", request_id, traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content=_error_payload(
                "INTERNAL_ERROR",
                "An unexpected error occurred. Quote the request id when reporting it.",
                request_id,
            ),
        )

    # --- routes -----------------------------------------------------------

    app.include_router(meta.router)
    app.include_router(auth.router)
    app.include_router(accounts.router)
    app.include_router(transfers.router)
    app.include_router(admin.router)
    app.include_router(audit_views.router)

    # Any unmatched /api path must get the JSON error envelope, never the SPA.
    # Registered after the real routers (so they win) and before the static
    # mount (so it is not swallowed by the index.html fallback).
    @app.api_route(
        "/api/{unmatched:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        include_in_schema=False,
    )
    def api_route_not_found(unmatched: str) -> None:
        # Deliberately 404 for every method: an API client must never be handed
        # the SPA's index.html, and a consistent 404 is easier to reason about
        # than a 405 that only some deployments produce.
        raise AppError("NOT_FOUND", "Resource not found.", status_code=404)

    static_dir = Path(settings.static_dir) if settings.static_dir else None
    if static_dir and static_dir.is_dir():
        app.mount("/", SPAStaticFiles(directory=str(static_dir), html=True), name="spa")
    else:
        @app.get("/", include_in_schema=False)
        def service_root() -> dict:
            # API-only mode: say where the UI is instead of a cryptic 404.
            return {
                "service": "ledgerline-api",
                "version": __version__,
                "docs": "/docs",
                "health": "/api/health",
                "note": (
                    "The dashboard is not mounted in this process. In development it is "
                    "served by Vite on http://localhost:5173; set LEDGERLINE_STATIC_DIR to "
                    "serve it from this port."
                ),
            }

    return app


class BodySizeLimitMiddleware:
    """Refuse an oversized request body while it is still streaming.

    ``Content-Length`` is a claim, not a fact: a chunked request carries no
    length at all, so a header-only check is trivially bypassed. This middleware
    counts real bytes on the ASGI receive channel and answers ``413`` before the
    body is buffered, whatever encoding the client chose.
    """

    def __init__(self, app: Any, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        seen = 0
        exceeded = False
        response_started = False

        async def limited_receive() -> dict:
            nonlocal seen, exceeded
            message = await receive()
            if message["type"] == "http.request" and not exceeded:
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    exceeded = True
                    # Report end-of-body so the application cannot wait for more
                    # bytes; whatever it answers is swallowed by guarded_send.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def guarded_send(message: dict) -> None:
            nonlocal response_started
            if exceeded:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        await self.app(scope, limited_receive, guarded_send)

        if exceeded and not response_started:
            request_id = (scope.get("state") or {}).get("request_id") or "unavailable"
            response = JSONResponse(
                status_code=413,
                content=_error_payload(
                    "PAYLOAD_TOO_LARGE",
                    f"Request body must be at most {self.max_bytes} bytes.",
                    request_id,
                ),
            )
            await response(scope, receive, send)


class SPAStaticFiles(StaticFiles):
    """Serve the built SPA, falling back to index.html for client-side routes."""

    async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def]
        # Never answer an /api path with HTML: a missing endpoint must look like
        # a missing endpoint to an API client.
        if path == "api" or path.startswith("api/"):
            raise StarletteHTTPException(status_code=404)
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            return await self._spa_fallback(path, scope)
        if response.status_code != 404:
            return response
        return await self._spa_fallback(path, scope)

    async def _spa_fallback(self, path: str, scope):  # type: ignore[no-untyped-def]
        # Client-side routes (/audit, /accounts/42) get index.html; a missing
        # asset such as /favicon.ico or /assets/x.js stays an honest 404
        # instead of being rewritten into HTML with a 200.
        if "." in Path(path).name:
            raise StarletteHTTPException(status_code=404)
        return await super().get_response("index.html", scope)


app = create_app()
