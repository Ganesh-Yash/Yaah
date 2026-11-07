"""Password hashing, JWT access tokens and refresh-token rotation.

Threats this module is built around:

* **Token forgery** -- HS256 with a pinned algorithm list and mandatory
  ``iss``/``aud``/``exp`` claims. Pinning the algorithm is what stops the
  classic ``alg: none`` and RS256->HS256 confusion attacks.
* **Stale privileges** -- the access token carries only the user id plus a
  fingerprint of the current password hash. The role and status are re-read
  from the database on every request, so demoting or disabling a user takes
  effect immediately instead of at token expiry.
* **Refresh-token theft** -- refresh tokens are opaque random strings, stored
  only as SHA-256 hashes, single-use, and rotated. Presenting an already-used
  token revokes the entire token family (the standard reuse-detection
  response), which is logged as a security event.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from . import chain
from .errors import AppError, validation_error

PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_BYTES = 72  # bcrypt silently truncates beyond this
MAX_EMAIL_LENGTH = 254

# A very small "obviously bad" list. A real deployment would check a breach
# corpus (k-anonymity API) instead; see THREATS.md.
_COMMON_PASSWORDS = {
    "password",
    "password123",
    "password1234",
    "123456789012",
    "qwertyuiop12",
    "letmein12345",
    "administrator",
    "changeme1234",
    "ledgerline12",
    "iloveyou1234",
}


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    access_expires_in: int
    refresh_expires_in: int


# --- passwords --------------------------------------------------------------


def validate_password(password: object) -> str:
    if not isinstance(password, str):
        raise validation_error("password must be a string.")
    if len(password) < PASSWORD_MIN_LENGTH:
        raise validation_error(f"password must be at least {PASSWORD_MIN_LENGTH} characters.")
    encoded = password.encode("utf-8")
    if len(encoded) > PASSWORD_MAX_BYTES:
        # Rejecting is the only honest option: bcrypt would truncate and the
        # user would believe the tail of their password mattered.
        raise validation_error("password must be at most 72 bytes when UTF-8 encoded.")
    if password.lower() in _COMMON_PASSWORDS:
        raise validation_error("password is too common; choose something unique.")
    if len(set(password)) < 5:
        raise validation_error("password is too repetitive; use more distinct characters.")
    return password


def hash_password(password: str, rounds: int) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds)).decode("ascii")


def verify_password(password: str, password_hash: str, *, dummy_hash: str | None = None) -> bool:
    """Constant-ish-time verification.

    When the user does not exist we still run bcrypt against a dummy hash so
    that "unknown email" and "wrong password" take the same time.
    """
    target = password_hash if password_hash else (dummy_hash or _DUMMY_HASH)
    try:
        ok = bcrypt.checkpw(password.encode("utf-8")[:PASSWORD_MAX_BYTES], target.encode("ascii"))
    except (ValueError, TypeError):
        return False
    return ok and bool(password_hash)


# A fixed bcrypt hash of a random string, used only to burn comparable CPU.
_DUMMY_HASH = "$2b$12$.xsoA7bVADb8kVm.2ULu5uR4qOAy/eD7XFup7wg5fkdPSO8/KK9fW"


def normalize_email(email: object) -> str:
    if not isinstance(email, str):
        raise validation_error("email must be a string.")
    value = email.strip().lower()
    if len(value) > MAX_EMAIL_LENGTH or not _looks_like_email(value):
        raise validation_error("email address is not valid.")
    return value


def _looks_like_email(value: str) -> bool:
    if value.count("@") != 1:
        return False
    local, _, domain = value.partition("@")
    if not local or not domain or len(local) > 64:
        return False
    if domain.startswith(".") or domain.endswith(".") or ".." in domain or "." not in domain:
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789!#$%&'*+/=?^_`{|}~.-")
    return all(ch in allowed for ch in local) and all(
        ch.isalnum() or ch in "-." for ch in domain
    )


# --- JWT --------------------------------------------------------------------


def password_fingerprint(password_hash: str) -> str:
    return chain.sha256_hex(password_hash)[:16]


def create_access_token(
    *,
    user_id: str,
    session_id: str,
    password_hash: str,
    settings: Any,
    now: datetime | None = None,
) -> tuple[str, int]:
    issued = now or datetime.now(timezone.utc)
    expires = issued + timedelta(seconds=settings.access_ttl_seconds)
    payload = {
        "sub": user_id,
        "sid": session_id,
        "typ": "access",
        "jti": uuid.uuid4().hex,
        "pwd": password_fingerprint(password_hash),
        "iat": int(issued.timestamp()),
        "nbf": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    return token, settings.access_ttl_seconds


class TokenError(AppError):
    def __init__(self, message: str = "Access token is missing, invalid or expired.") -> None:
        super().__init__("UNAUTHENTICATED", message, status_code=401)


def decode_access_token(token: str, settings: Any) -> dict[str, Any]:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],  # pinned: rejects alg=none / confusion attacks
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={
                "require": ["exp", "iat", "sub", "sid", "iss", "aud", "typ"],
                "verify_aud": True,
                "verify_iss": True,
                "verify_exp": True,
                "verify_signature": True,
            },
            leeway=5,
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Access token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError() from exc
    if claims.get("typ") != "access":
        raise TokenError()
    return claims


# --- refresh tokens ---------------------------------------------------------


def new_refresh_token() -> tuple[str, str]:
    """Return ``(plaintext, sha256 hash)``. Only the hash is persisted."""
    plaintext = secrets.token_urlsafe(48)
    return plaintext, hash_refresh_token(plaintext)


def hash_refresh_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
