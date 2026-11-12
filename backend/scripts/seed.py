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
from ledgerline.db import make_database  # noqa: E402
from ledgerline.seed import DEMO_PASSWORD, bootstrap, seed_demo  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialise a LedgerLine database.")
    parser.add_argument("--demo", action="store_true", help="create demo users and accounts")
    parser.add_argument(
        "--database",
        default=None,
        help="SQLite file to use (ignored when LEDGERLINE_DATABASE_URL is set)",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("LEDGERLINE_DEMO_PASSWORD", DEMO_PASSWORD),
        help="password for the demo users",
    )
    args = parser.parse_args()

    # Honour the same configuration the application does: this used to construct
    # a SQLite Database directly, so on a PostgreSQL deployment it silently
    # seeded a local file and the API came up empty.
    settings = Settings.from_env()
    if args.database:
        import dataclasses

        settings = dataclasses.replace(settings, database_path=args.database)

    db = make_database(settings)
    db.initialize()
    bootstrap(db)
    target = getattr(db, "backend", "sqlite")
    print(
        "[seed] schema and system accounts ready: "
        f"{target} / {settings.database_url or settings.database_path}"
    )

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
