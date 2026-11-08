# Deployment

The application is one process: FastAPI serves the JSON API **and** the built React app from the same
origin, so there is no CORS surface and no second host to secure.

## Option 1 — Docker Compose

> **Status:** this is the option CI verifies. The development machine had no Docker daemon, so I
> never started the container myself, but the "Docker image builds" job runs
> `docker build -t ledgerline:ci .` on every push and is green on `main` — the multi-stage build
> itself is known-good, not assumed. Option 2 is the path exercised end to end here (seeded, served,
> attacked) and is backed by 155 tests plus a 27-check live attack suite.

```bash
cp .env.example .env
# Edit .env: set LEDGERLINE_JWT_SECRET to 32+ random characters
python -c 'import secrets; print(secrets.token_urlsafe(48))'   # paste this in

docker compose up --build -d
docker compose logs -f app
# http://localhost:8000  → dashboard,  /api/health → JSON,  /docs → OpenAPI UI
```

The image is multi-stage: Node builds the SPA, then a slim Python image runs the API with the built
assets. The container runs as an unprivileged user and keeps the database on a named volume.

Seeding happens on first boot when `LEDGERLINE_DEMO_SEED=1` (the compose default). Set it to `0` for a
clean deployment and create users through the API or `python scripts/seed.py`.

## Option 2 — bare metal / a VM

```bash
python3 -m venv /opt/ledgerline/venv
/opt/ledgerline/venv/bin/pip install -r backend/requirements.txt
( cd frontend && npm ci && npm run build )

export LEDGERLINE_ENV=prod
export LEDGERLINE_JWT_SECRET="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
export LEDGERLINE_DATABASE_PATH=/var/lib/ledgerline/ledgerline.db
export LEDGERLINE_STATIC_DIR=/opt/ledgerline/frontend/dist
export LEDGERLINE_DEMO_SEED=0

cd backend
/opt/ledgerline/venv/bin/uvicorn ledgerline.main:app --host 127.0.0.1 --port 8000 --workers 1
```

> **Workers must stay at 1** with SQLite: multiple processes writing the same file serialise on the
> file lock and one will eventually return `503`. Scale by moving to Postgres (see below), not by
> adding workers.

### systemd unit

```ini
[Unit]
Description=LedgerLine API
After=network.target

[Service]
User=ledgerline
Group=ledgerline
WorkingDirectory=/opt/ledgerline/backend
EnvironmentFile=/etc/ledgerline/env
ExecStart=/opt/ledgerline/venv/bin/uvicorn ledgerline.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/ledgerline

[Install]
WantedBy=multi-user.target
```

## Reverse proxy and TLS

Terminate TLS at the proxy and forward to the app on loopback. The app already sends HSTS when
`LEDGERLINE_ENV=prod`, `nosniff`, `no-referrer`, a CSP and `Cache-Control: no-store` on `/api`.

```nginx
server {
    listen 443 ssl http2;
    server_name ledger.example.com;
    # ssl_certificate ... ;

    client_max_body_size 128k;          # mirrors the app's 64 KiB limit

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```

Set `LEDGERLINE_TRUSTED_HOSTS=ledger.example.com` (a comma-separated allowlist) if the app is exposed
directly, and only then does it honour `X-Forwarded-For` for client IPs. If you leave it unset, the
app uses the socket peer address — which is the *proxy* address, so all users share one rate-limit
bucket. Behind a proxy, set it.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `LEDGERLINE_ENV` | `dev` | `prod` refuses to boot without a ≥32-char JWT secret and enables HSTS |
| `LEDGERLINE_JWT_SECRET` | ephemeral in dev | signing key; rotating it logs everyone out |
| `LEDGERLINE_DATABASE_PATH` | `./data/ledgerline.db` | SQLite file (directory is created) |
| `LEDGERLINE_STATIC_DIR` | unset | serve the built SPA from this directory |
| `LEDGERLINE_CORS_ORIGINS` | localhost origins | comma-separated allowlist; empty = no CORS middleware (same-origin only) |
| `LEDGERLINE_TRUSTED_HOSTS` | empty | TrustedHost allowlist; also enables `X-Forwarded-For` |
| `LEDGERLINE_ACCESS_TTL_SECONDS` | `900` | access-token lifetime |
| `LEDGERLINE_REFRESH_TTL_SECONDS` | `2592000` | refresh-token lifetime |
| `LEDGERLINE_MAX_AMOUNT_MINOR` | `10000000` | per-movement ceiling (100,000.00) |
| `LEDGERLINE_MONEY_WRITES_PER_MINUTE` | `30` | per-user money throttle |
| `LEDGERLINE_LOGIN_MAX_FAILURES` | `5` | per-account lockout threshold |
| `LEDGERLINE_LOGIN_MAX_FAILURES_PER_IP` | `50` | password-spray ceiling |
| `LEDGERLINE_LOGIN_LOCKOUT_SECONDS` | `900` | lockout window |
| `LEDGERLINE_BCRYPT_ROUNDS` | `12` | password hash cost |
| `LEDGERLINE_SQLITE_BUSY_TIMEOUT_MS` | `10000` | writer wait before `503` |
| `LEDGERLINE_DEMO_SEED` | `0` | create demo users/accounts on boot |

## First-run checklist

1. `LEDGERLINE_JWT_SECRET` is random, ≥32 characters and stored outside the repository.
2. `LEDGERLINE_ENV=prod`.
3. `LEDGERLINE_DEMO_SEED=0`, or the demo password changed immediately.
4. HTTPS in front, `LEDGERLINE_TRUSTED_HOSTS` set to the public hostname.
5. The data directory is on a persistent volume and the file is `0600`, owned by the service user.
6. `curl -fsS https://host/api/health` returns `{"status":"ok","database":"ok"}`.
7. Log in as auditor and check `GET /api/auditor/integrity` → `ok: true`.
8. `python scripts/redteam.py --base-url https://host` → 27/27.

## Backup and restore

The database is a single file (plus `-wal`/`-shm`). Use SQLite's online backup so the copy is
consistent while the app runs:

```bash
# backup (safe while the app is running)
sqlite3 /var/lib/ledgerline/ledgerline.db ".backup '/backups/ledgerline-$(date +%F-%H%M).db'"

# verify a backup before trusting it
sqlite3 /backups/ledgerline-2026-11-07-1200.db "PRAGMA integrity_check;"
sqlite3 /backups/ledgerline-2026-11-07-1200.db \
  "SELECT name, last_hash FROM chain_state; SELECT COUNT(*) FROM ledger_entries;"

# restore
systemctl stop ledgerline
cp /backups/ledgerline-2026-11-07-1200.db /var/lib/ledgerline/ledgerline.db
chown ledgerline:ledgerline /var/lib/ledgerline/ledgerline.db && chmod 600 /var/lib/ledgerline/ledgerline.db
systemctl start ledgerline
curl -fsS localhost:8000/api/health
```

Record `chain_state.last_hash` after every backup somewhere the database owner cannot edit. Restoring
an old backup is otherwise undetectable — the chain is internally consistent by then (see THREATS.md
R1).

## Key rotation

Rotating `LEDGERLINE_JWT_SECRET` invalidates every access and refresh token: all users sign in again.
Refresh tokens are stored hashed and are not secret-derived, so they survive a rotation but the
sessions they belong to still require a new access token — acceptable for a planned window.

```bash
# set the new secret in /etc/ledgerline/env, then
systemctl restart ledgerline
```

## Health, monitoring and logs

- Liveness/readiness: `GET /api/health` (checks the database with `SELECT 1`).
- Structured-ish logs on stderr with one line per request from uvicorn, plus `[ledgerline]` boot lines
  and `unhandled error [req_…]` entries that pair with the `request_id` a user quotes.
- Business events are **not** in the logs, they are in the audit table (`GET /api/audit`), which is
  the system of record.
- Useful alerts: `5xx` rate, `503 SERVICE_BUSY` (writer contention), `429` spikes (brute force),
  `/api/auditor/integrity` returning `ok: false` (run it on a schedule and alert).
- Disk: the WAL file can grow under sustained write load; checkpointing happens automatically, but
  monitor free space because a full disk fails commits (`500`, rolled back, no partial state).

## Scheduled integrity check

```bash
# cron: every 15 minutes, alert if the books no longer verify
TOKEN=$(curl -s -X POST https://host/api/auth/login -H 'Content-Type: application/json' \
  -d "{\"email\":\"$AUDITOR_EMAIL\",\"password\":\"$AUDITOR_PASSWORD\"}" | jq -r .access_token)
curl -s https://host/api/auditor/integrity -H "Authorization: Bearer $TOKEN" \
  | jq -e '.ok == true' > /dev/null || echo "LEDGER INTEGRITY FAILURE" | mail -s alert ops@example.com
```

## Moving to Postgres

Not shipped, but the port is small and deliberate:

1. Replace `db.py` with a psycopg connection pool; `BEGIN IMMEDIATE` becomes `BEGIN` plus
   `SELECT … FROM accounts WHERE id = ANY(%s) ORDER BY id FOR UPDATE`.
2. `INTEGER` → `BIGINT`, ISO strings → `TIMESTAMPTZ` (hash the same canonical strings).
3. `AUTOINCREMENT`-style ids come from a sequence instead of `chain_state.last_id`.
4. Idempotency insert becomes `INSERT … ON CONFLICT (user_id, scope, key) DO NOTHING` followed by
   `SELECT … FOR UPDATE` of the winning row.
5. Triggers become `RAISE EXCEPTION`; also `REVOKE UPDATE, DELETE` on the history tables from the
   application role, so the guarantee survives a bug in the app.

`docs/ARCHITECTURE.md#porting-to-postgres` has the SQL notes, and the concurrency tests are written
against HTTP, so they carry over unchanged.
