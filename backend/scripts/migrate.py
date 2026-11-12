"""Release step: bring a database up to the schema this build expects.

This exists because a deployment needs the schema applied *before* the API starts,
and the API's role has no DDL rights by design (see deploy/postgres/02-grants.sql).
It is the command a platform release hook should run:

    python -m scripts.migrate            # Fly: release_command, Railway: pre-deploy

What it does, in order:

1. Resolves the target database. ``LEDGERLINE_MIGRATION_DATABASE_URL`` wins if it
   is set, so a deployment can keep the migrator credential separate from the
   credential the API runs with. Otherwise it uses ``LEDGERLINE_DATABASE_URL``.
2. If the schema is absent, applies the Prisma migration SQL shipped in
   ``prisma/migrations/`` (the image now includes that directory).
3. If the schema is present, leaves it alone and reports its version. This is what
   makes the step idempotent: running it on every deploy is safe.

What it deliberately does **not** do: invent its own migration ledger. Prisma owns
that bookkeeping (``_prisma_migrations``). Recording hand-applied files in a
private table would make a later ``prisma migrate deploy`` believe nothing had been
applied and re-run the initial migration. So this script bootstraps an empty
database — the fresh-deploy case — and afterwards schema changes go through
``prisma migrate deploy``. That boundary is written down in docs/DEPLOYMENT.md.

Exit codes: 0 = the database is at the expected schema, 1 = it could not be
brought there. A release hook should fail the deploy on 1, not start the API
against a database it cannot use.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledgerline.config import Settings  # noqa: E402
from ledgerline.db import make_database  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    settings = Settings.from_env()

    migration_url = os.environ.get("LEDGERLINE_MIGRATION_DATABASE_URL") or settings.database_url
    if not migration_url and settings.is_prod:
        # Without a URL this would migrate the default SQLite path — in a container
        # that is an ephemeral file, so the API would come up "healthy" against a
        # database that disappears on the next deploy. Fail the release instead.
        print(
            "[migrate] LEDGERLINE_DATABASE_URL is not set. A production release "
            "must target a real database; refusing to create a throwaway SQLite "
            "file. Set LEDGERLINE_DATABASE_URL (or LEDGERLINE_MIGRATION_DATABASE_URL).",
            file=sys.stderr,
        )
        return 1

    if migration_url and migration_url != settings.database_url:
        # Use the migrator credential for DDL, but never log it.
        settings = dataclasses.replace(settings, database_url=migration_url)

    try:
        return _apply(settings)
    except Exception as exc:  # noqa: BLE001
        # A release hook is read by a human in a deploy log. Give them one line
        # that names the problem instead of a traceback that buries it.
        detail = str(exc).splitlines()[0] if str(exc) else ""
        print(f"[migrate] failed: {exc.__class__.__name__}: {detail}", file=sys.stderr)
        return 1


def _apply(settings: Settings) -> int:
    db = make_database(settings)
    backend = getattr(db, "backend", "sqlite")
    print(f"[migrate] target: {backend}")

    before = db.schema_version()
    if before is not None:
        print(f"[migrate] schema already present (version {before}); nothing to do.")
        ok, detail = db.ping()
        if not ok:
            print(f"[migrate] database is not reachable: {detail}", file=sys.stderr)
            return 1
        return 0

    print("[migrate] schema absent — applying prisma/migrations")
    try:
        db.initialize()
    except Exception as exc:  # noqa: BLE001 - the release step must report, not trace
        print(
            f"[migrate] failed to apply the schema: {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    after = db.schema_version()
    if after is None:
        print(
            "[migrate] the migration ran but schema_meta has no version row; "
            "refusing to report success.",
            file=sys.stderr,
        )
        return 1
    print(f"[migrate] done — schema version {after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
