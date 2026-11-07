#!/usr/bin/env python3
"""Initialise the database: schema, system clearing accounts, optional demo data.

    python scripts/seed.py                       # schema + system accounts only
    python scripts/seed.py --demo                # + demo users and funded accounts
    python scripts/seed.py --demo --database data/other.db

Safe to re-run: every insert is conditional.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledgerline.config import Settings  # noqa: E402
from ledgerline.db import Database  # noqa: E402
from ledgerline.seed import DEMO_PASSWORD, bootstrap, seed_demo  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialise a LedgerLine database.")
    parser.add_argument("--demo", action="store_true", help="create demo users and accounts")
    parser.add_argument(
        "--database",
        default=os.environ.get("LEDGERLINE_DATABASE_PATH", "./data/ledgerline.db"),
        help="path to the SQLite database file",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("LEDGERLINE_DEMO_PASSWORD", DEMO_PASSWORD),
        help="password for the demo users",
    )
    args = parser.parse_args()

    settings = Settings(
        env="dev",
        database_path=args.database,
        jwt_secret="seed-script-" + "0" * 32,  # no tokens are issued here
        bcrypt_rounds=12,
    )
    db = Database(settings.database_path, settings.sqlite_busy_timeout_ms)
    db.initialize()
    bootstrap(db)
    print(f"[seed] schema and system accounts ready: {settings.database_path}")

    if args.demo:
        result = seed_demo(db, settings, args.password)
        if result["created"]:
            print(f"[seed] demo users/accounts created: {', '.join(result['created'])}")
            print(f"[seed] demo password: {args.password}")
        else:
            print("[seed] demo data already present; nothing to do")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
