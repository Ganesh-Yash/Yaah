"""Create the first ADMIN without an API call.

Why this exists: the API can only create a staff user on behalf of an existing
ADMIN (see routers/admin.py). On a brand new deployment there is no admin, so
there is no way in — the bootstrap step closes that gap and nothing else.

Design rules this script follows:

* It is not an endpoint. There is no unauthenticated "create first admin" route
  to attack, and the script needs the same database credentials as a migration.
* The role is never taken from a request body; it is hard-coded to ADMIN here.
* It refuses to run when an admin already exists, unless --force is given, so a
  second accidental run cannot silently add a backdoor account.
* The password is read from a prompt or an environment variable, never from
  argv, because argv lands in shell history and in process listings.
* The creation is written to the audit log with actor_role=SYSTEM, so the hash
  chain records the bootstrap as a real event.

Usage::

    LEDGERLINE_DATABASE_URL=postgresql://... python -m scripts.bootstrap_admin \
        --email admin@example.com --name "Ops Admin" --print-token

In development the command prints a one-time password when --print-token is
passed; in production pass LEDGERLINE_BOOTSTRAP_PASSWORD or type it at the
prompt.
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import string
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledgerline import audit, security  # noqa: E402
from ledgerline.config import Settings  # noqa: E402
from ledgerline.db import is_unique_violation, make_database, utcnow_iso  # noqa: E402

ADMIN_ROLE = "ADMIN"


def _generate_password(length: int = 24) -> str:
    """Generate a password that satisfies the same policy the API enforces."""
    alphabet = string.ascii_letters + string.digits
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        # Guarantee the character classes the validator wants, without relying
        # on a lucky draw.
        candidate = candidate[:-3] + "@#A"
        try:
            security.validate_password(candidate)
            return candidate
        except Exception:  # pragma: no cover - extremely unlikely
            continue


def _read_password(interactive: bool) -> str | None:
    env_password = os.environ.get("LEDGERLINE_BOOTSTRAP_PASSWORD")
    if env_password:
        return env_password
    if not interactive:
        return None
    first = getpass.getpass("Password for the first admin: ")
    if not first:
        return None
    second = getpass.getpass("Repeat password: ")
    if first != second:
        print("Passwords did not match.", file=sys.stderr)
        raise SystemExit(2)
    return first


def count_admins(db) -> int:
    with db.read() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users WHERE role = ?", (ADMIN_ROLE,)).fetchone()[0])


def bootstrap_admin(
    db,
    settings,
    *,
    email: str,
    full_name: str,
    password: str,
    force: bool = False,
) -> dict:
    email = security.normalize_email(email)
    security.validate_password(password)
    full_name = full_name.strip()
    if not 2 <= len(full_name) <= 120:
        raise SystemExit("full_name must be 2-120 characters.")

    existing = count_admins(db)
    if existing and not force:
        raise SystemExit(
            f"Refusing to run: {existing} admin account(s) already exist. "
            "Use the admin API, or re-run with --force if you are intentionally "
            "adding another."
        )

    user_id = str(uuid.uuid4())
    now = utcnow_iso()
    password_hash = security.hash_password(password, settings.bcrypt_rounds)
    try:
        with db.write() as conn:
            conn.execute(
                """
                INSERT INTO users (id, email, full_name, password_hash, role, status,
                                   failed_login_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'ACTIVE', 0, ?, ?)
                """,
                (user_id, email, full_name, password_hash, ADMIN_ROLE, now, now),
            )
            audit.record_in(
                conn,
                "ADMIN_BOOTSTRAPPED",
                actor=None,
                subject=email,
                target_type="user",
                target_id=user_id,
                detail={"role": ADMIN_ROLE, "actor_role": "SYSTEM", "forced": force},
            )
    except Exception as exc:
        if is_unique_violation(exc):
            raise SystemExit(f"An account with {email} already exists.") from exc
        raise

    return {"id": user_id, "email": email, "full_name": full_name, "role": ADMIN_ROLE, "created_at": now}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the first ADMIN account (no API needed).")
    parser.add_argument("--email", required=True, help="Admin email address")
    parser.add_argument("--name", default="System Administrator", help="Display name")
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the password from stdin (use in containers: echo $PASS | ... --password-stdin)",
    )
    parser.add_argument("--print-token", action="store_true", help="Generate a password and print it once")
    parser.add_argument("--force", action="store_true", help="Create even if an admin already exists")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    db = make_database(settings)
    db.initialize()

    password = None
    generated = False
    if args.print_token:
        password = _generate_password()
        generated = True
    elif args.password_stdin:
        password = sys.stdin.readline().strip()
    else:
        password = _read_password(interactive=sys.stdin.isatty())
    if not password:
        print(
            "No password supplied. Use --print-token, --password-stdin, "
            "LEDGERLINE_BOOTSTRAP_PASSWORD, or run interactively.",
            file=sys.stderr,
        )
        return 2

    created = bootstrap_admin(
        db, settings, email=args.email, full_name=args.name, password=password, force=args.force
    )

    backend = getattr(db, "backend", "sqlite")
    print(f"Created admin {created['email']} ({created['id']}) on {backend}.")
    if generated:
        # Printed once, on purpose: this is the only time it is available.
        print(f"One-time password: {password}")
        print("Store it in your password manager now; it cannot be recovered.")
    print("Next: log in with that password and change it, then create customer accounts via the API.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
