#!/usr/bin/env bash
# Local preview runner: PostgreSQL + the API + the built dashboard on one port.
#
# This is a development convenience, not a production artifact — production is
# deploy/docker-compose.yml. It exists because the preview the reviewer clicks is
# a live process, and a live process has to be reproducible after a restart.
#
# It is idempotent and repairs whatever is missing:
#   1. .venv and the Python dependencies
#   2. a PostgreSQL cluster at ./.pgdata  (inside the repo, so it is not in /tmp
#      and survives a snapshot)
#   3. the database, the two roles, the Prisma migration and demo data
#   4. frontend/dist, which is excluded from snapshots and may need rebuilding
#   5. the API on 0.0.0.0:$PORT, serving the SPA from the same origin
#
# Usage:  deploy/preview/start.sh            # port 8200
#         PORT=9000 deploy/preview/start.sh  # another port
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="$ROOT/.venv"
PGDATA="$ROOT/.pgdata"
LOG_DIR="$ROOT/.preview"
PORT="${PORT:-8200}"
DB_NAME="${DB_NAME:-balancevault}"

# Trust auth on the unix socket: pgserver's initdb default for a local cluster.
ADMIN_URL="postgresql://postgres:@/postgres?host=$PGDATA"
APP_URL="postgresql://balancevault_app:@/$DB_NAME?host=$PGDATA"
MIGRATOR_URL="postgresql://balancevault_migrator:@/$DB_NAME?host=$PGDATA"

say() { printf '[preview] %s\n' "$*"; }
mkdir -p "$LOG_DIR"

# ---------------------------------------------------------------- 1. Python ---
if [ ! -x "$VENV/bin/python" ]; then
    say "creating .venv and installing dependencies"
    python3 -m venv "$VENV"
    "$VENV/bin/python" -m pip install -q --upgrade pip
    "$VENV/bin/python" -m pip install -q \
        -r "$ROOT/backend/requirements.txt" \
        -r "$ROOT/backend/requirements-dev.txt" \
        pgserver redislite
fi

# ------------------------------------------------------------ 2. PostgreSQL ---
if [ ! -S "$PGDATA/.s.PGSQL.5432" ]; then
    say "starting PostgreSQL in .pgdata"
    mkdir -p "$PGDATA"
    nohup "$VENV/bin/python" - "$PGDATA" > "$LOG_DIR/postgres.log" 2>&1 <<'PY' &
import pathlib
import sys
import time

import pgserver

server = pgserver.get_server(pathlib.Path(sys.argv[1]))
print("URI=" + server.get_uri(), flush=True)
while True:                     # hold the cluster open for the API
    time.sleep(3600)
PY
    for _ in $(seq 60); do
        [ -S "$PGDATA/.s.PGSQL.5432" ] && break
        sleep 1
    done
fi
if [ ! -S "$PGDATA/.s.PGSQL.5432" ]; then
    say "PostgreSQL did not start — see $LOG_DIR/postgres.log"
    exit 1
fi

# ----------------------------------------- 3. database, roles, schema, seed ---
say "ensuring database '$DB_NAME' (roles, migration, grants)"
"$VENV/bin/python" - "$ADMIN_URL" "$MIGRATOR_URL" "$ROOT" "$DB_NAME" <<'PY'
import pathlib
import sys

import psycopg

admin_url, migrator_url, root, db_name = sys.argv[1:5]
root = pathlib.Path(root)
roles = root / "deploy" / "postgres" / "01-roles.sql"
grants = root / "deploy" / "postgres" / "02-grants.sql"
migrations = sorted((root / "prisma" / "migrations").glob("*/migration.sql"))

# CREATE DATABASE cannot run inside a transaction block.
with psycopg.connect(admin_url, autocommit=True) as admin:
    exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)).fetchone()
    if not exists:
        print(f"[preview] creating database {db_name}")
        admin.execute(f'CREATE DATABASE "{db_name}"')

db_url = admin_url.replace("/postgres?", f"/{db_name}?")
with psycopg.connect(db_url, autocommit=True) as conn:
    conn.execute(roles.read_text())          # idempotent; safe to re-run
    applied = conn.execute("SELECT to_regclass('public.users') AS t").fetchone()[0]
    if applied is None:
        print("[preview] applying the Prisma migration as the migrator role")
        conn.execute("SET ROLE balancevault_migrator")
        for migration in migrations:
            conn.execute(migration.read_text())
        conn.execute("RESET ROLE")
    conn.execute(grants.read_text())         # per-table grants, fail-closed
    version = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()[0]
    print(f"[preview] schema version {version}")
PY

export LEDGERLINE_DATABASE_URL="$APP_URL"
export LEDGERLINE_JWT_SECRET="${LEDGERLINE_JWT_SECRET:-preview-only-jwt-secret-not-for-production-000000}"

say "seeding demo data if absent (idempotent)"
# The seeder is idempotent and refuses to run when LEDGERLINE_ENV=prod.
(cd "$ROOT/backend" && "$VENV/bin/python" scripts/seed.py --demo >/dev/null) || {
    say "seeding failed — continuing; the API will report an empty database"
}

# --------------------------------------------------------------- 4. frontend ---
if [ ! -f "$ROOT/frontend/dist/index.html" ]; then
    say "frontend/dist is missing (it is excluded from snapshots) — building"
    (cd "$ROOT/frontend" && npm install --no-audit --no-fund && npm run build)
fi

# -------------------------------------------------------------------- 5. API ---
say "serving the API + dashboard on http://0.0.0.0:$PORT (PostgreSQL backend)"
export LEDGERLINE_ENV="${LEDGERLINE_ENV:-dev}"
export LEDGERLINE_STATIC_DIR="$ROOT/frontend/dist"
cd "$ROOT/backend"
exec "$VENV/bin/uvicorn" ledgerline.main:app --host 0.0.0.0 --port "$PORT"
