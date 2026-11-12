"""Application error type and the JSON error envelope.

Every client-visible failure is an :class:`AppError` with a stable machine
code. Handlers never leak stack traces, SQL or internal identifiers; the
``request_id`` is the only bridge between what a user sees and what the audit
log recorded.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        # Response headers the error needs in order to be actionable, e.g.
        # Retry-After on a 429. The handler forwards these verbatim.
        self.headers = headers or {}


# --- Authentication / authorisation -----------------------------------------


def invalid_credentials() -> AppError:
    # Deliberately identical for "no such user" and "wrong password" so the
    # endpoint cannot be used to enumerate accounts.
    return AppError("INVALID_CREDENTIALS", "Email or password is incorrect.", status_code=401)


def unauthenticated(message: str = "Authentication required.") -> AppError:
    return AppError("UNAUTHENTICATED", message, status_code=401)


def forbidden(message: str = "You do not have access to this resource.") -> AppError:
    return AppError("FORBIDDEN", message, status_code=403)


def not_found(resource: str = "Resource") -> AppError:
    # Used for cross-tenant access too: a customer probing another customer's
    # account gets 404, not 403, so existence is not disclosed.
    return AppError("NOT_FOUND", f"{resource} not found.", status_code=404)


def conflict(code: str, message: str, details: dict[str, Any] | None = None) -> AppError:
    return AppError(code, message, status_code=409, details=details)


def validation_error(message: str, details: dict[str, Any] | None = None) -> AppError:
    return AppError("VALIDATION_ERROR", message, status_code=422, details=details)


def rate_limited(
    message: str = "Too many requests. Please slow down.", retry_after_seconds: int = 60
) -> AppError:
    # A 429 without Retry-After tells a client to back off but not for how long,
    # and clients that have to guess tend to retry immediately. One minute is the
    # window every limiter in this service enforces.
    return AppError(
        "RATE_LIMITED",
        message,
        status_code=429,
        headers={"Retry-After": str(retry_after_seconds)},
    )
