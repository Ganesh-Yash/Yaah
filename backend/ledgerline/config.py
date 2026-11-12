"""Runtime configuration, loaded once from the environment.

Every security-relevant knob is explicit here so that it can be audited in one
place (and so tests can construct a fully isolated :class:`Settings`).
"""

from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass, field, replace

DEV_JWT_SECRET_PREFIX = "dev-only-"
MIN_SECRET_LENGTH = 32


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - operator error
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _platform_public_host() -> tuple[str, ...]:
    """Default Host allowlist, from the platform when it will tell us.

    Render injects ``RENDER_EXTERNAL_HOSTNAME`` (e.g. ``balancevault.onrender.com``)
    into every service. Without that default a fresh deploy rejects every request
    with "Invalid host header" unless the operator guesses the value exactly, and
    the allowlist is also what switches on reading ``X-Forwarded-For`` for the
    per-IP login throttle — so getting it wrong is both a 400 and a security
    regression. Explicit ``LEDGERLINE_TRUSTED_HOSTS`` still wins.
    """
    host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip().lower()
    return (host,) if host else ()


@dataclass(frozen=True)
class Settings:
    """Immutable application settings.

    ``env`` is ``dev``, ``test`` or ``prod``. In ``prod`` the service refuses to
    boot with a missing or weak JWT secret rather than silently running with a
    guessable one.
    """

    env: str = "dev"
    database_path: str = "./data/ledgerline.db"
    # When set (postgresql://...), PostgreSQL is used and the SQLite path is
    # ignored. Unset means SQLite: tests and the offline demo.
    database_url: str | None = None
    # Optional Postgres schema (least-privilege deployments and per-test isolation).
    database_schema: str | None = None
    # Shared rate limiting. Unset -> in-process (single worker only).
    redis_url: str | None = None

    jwt_secret: str = ""
    jwt_issuer: str = "ledgerline"
    jwt_audience: str = "ledgerline-api"
    access_ttl_seconds: int = 15 * 60
    refresh_ttl_seconds: int = 30 * 24 * 60 * 60

    cors_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    )

    # Money rail limits. Amounts are integer minor units (cents/paise).
    max_amount_minor: int = 100_000_00  # 100,000.00 per single movement
    money_writes_per_minute: int = 30
    # Total value that may *leave* one account in a UTC day (withdrawals plus
    # outgoing transfers). 0 disables the check. This is a velocity limit, not a
    # balance limit: it bounds the blast radius of a stolen session.
    daily_outflow_limit_minor: int = 1_000_000_00  # 1,000,000.00 per account per day
    # Hard ceiling on a request body, enforced while the bytes stream in.
    max_request_bytes: int = 64 * 1024

    # Brute-force protection.
    login_max_failures: int = 5
    login_max_failures_per_ip: int = 50
    login_lockout_seconds: int = 15 * 60

    bcrypt_rounds: int = 12
    sqlite_busy_timeout_ms: int = 10_000

    # Serve the built SPA from the API process (single-container deployment).
    static_dir: str | None = None

    # Demo seeding is opt-in so a deployed instance never silently ships with
    # known credentials unless the operator asked for it.
    demo_seed_enabled: bool = False

    trusted_hosts: tuple[str, ...] = ()

    # ``frame-ancestors`` source list for the CSP. ``None`` means "derive it":
    # ``'none'`` in prod, ``*`` elsewhere (the hosted preview embeds the
    # dashboard in an iframe, and ``'none'`` would break that demo).
    csp_frame_ancestors: str | None = None

    extra: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.env not in {"dev", "test", "prod"}:
            raise SystemExit(f"unknown LEDGERLINE_ENV {self.env!r}")
        secret = self.jwt_secret
        if len(secret) < MIN_SECRET_LENGTH:
            if self.env == "prod":
                raise SystemExit(
                    "LEDGERLINE_JWT_SECRET must be set to at least "
                    f"{MIN_SECRET_LENGTH} characters in prod"
                )
            # Ephemeral, loudly-marked dev secret: tokens do not survive a
            # restart, which is exactly what you want in local development.
            secret = DEV_JWT_SECRET_PREFIX + secrets.token_urlsafe(36)
            object.__setattr__(self, "jwt_secret", secret)

    @property
    def uses_postgres(self) -> bool:
        return bool(self.database_url)

    @property
    def is_dev_secret(self) -> bool:
        return self.jwt_secret.startswith(DEV_JWT_SECRET_PREFIX)

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def frame_ancestors(self) -> str:
        """Resolved CSP ``frame-ancestors`` value.

        Production refuses to be framed unless an operator opts in explicitly;
        development keeps the permissive value so the hosted preview (and any
        other iframe embed) still renders.
        """
        if self.csp_frame_ancestors:
            return self.csp_frame_ancestors
        return "'none'" if self.is_prod else "*"

    @classmethod
    def from_env(cls) -> "Settings":
        settings = cls(
            env=os.environ.get("LEDGERLINE_ENV", "dev").strip().lower(),
            database_path=os.environ.get("LEDGERLINE_DATABASE_PATH", "./data/ledgerline.db"),
            database_url=os.environ.get("LEDGERLINE_DATABASE_URL") or None,
            database_schema=os.environ.get("LEDGERLINE_DATABASE_SCHEMA") or None,
            redis_url=os.environ.get("LEDGERLINE_REDIS_URL") or None,
            jwt_secret=os.environ.get("LEDGERLINE_JWT_SECRET", ""),
            jwt_issuer=os.environ.get("LEDGERLINE_JWT_ISSUER", "ledgerline"),
            jwt_audience=os.environ.get("LEDGERLINE_JWT_AUDIENCE", "ledgerline-api"),
            access_ttl_seconds=_env_int("LEDGERLINE_ACCESS_TTL_SECONDS", 15 * 60),
            refresh_ttl_seconds=_env_int("LEDGERLINE_REFRESH_TTL_SECONDS", 30 * 24 * 60 * 60),
            cors_origins=_env_list(
                "LEDGERLINE_CORS_ORIGINS",
                (
                    "http://localhost:5173",
                    "http://127.0.0.1:5173",
                    "http://localhost:4173",
                    "http://127.0.0.1:4173",
                ),
            ),
            max_amount_minor=_env_int("LEDGERLINE_MAX_AMOUNT_MINOR", 100_000_00),
            money_writes_per_minute=_env_int("LEDGERLINE_MONEY_WRITES_PER_MINUTE", 30),
            daily_outflow_limit_minor=_env_int(
                "LEDGERLINE_DAILY_OUTFLOW_LIMIT_MINOR", 1_000_000_00
            ),
            max_request_bytes=_env_int("LEDGERLINE_MAX_REQUEST_BYTES", 64 * 1024),
            login_max_failures=_env_int("LEDGERLINE_LOGIN_MAX_FAILURES", 5),
            login_max_failures_per_ip=_env_int("LEDGERLINE_LOGIN_MAX_FAILURES_PER_IP", 50),
            login_lockout_seconds=_env_int("LEDGERLINE_LOGIN_LOCKOUT_SECONDS", 15 * 60),
            bcrypt_rounds=_env_int("LEDGERLINE_BCRYPT_ROUNDS", 12),
            sqlite_busy_timeout_ms=_env_int("LEDGERLINE_SQLITE_BUSY_TIMEOUT_MS", 10_000),
            static_dir=os.environ.get("LEDGERLINE_STATIC_DIR") or None,
            demo_seed_enabled=_env_bool("LEDGERLINE_DEMO_SEED", False),
            trusted_hosts=_env_list("LEDGERLINE_TRUSTED_HOSTS", _platform_public_host()),
            csp_frame_ancestors=os.environ.get("LEDGERLINE_CSP_FRAME_ANCESTORS") or None,
        )
        if settings.is_dev_secret:
            print(
                "[ledgerline] WARNING: using an ephemeral development JWT secret. "
                "Set LEDGERLINE_JWT_SECRET before deploying.",
                file=sys.stderr,
            )
        return settings

    def with_overrides(self, **kwargs: object) -> "Settings":
        """Return a copy with overrides applied (used by tests)."""
        return replace(self, **kwargs)
