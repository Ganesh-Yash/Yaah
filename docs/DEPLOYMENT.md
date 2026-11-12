# Deployment

Two shapes, same API:

- **SQLite, one container** — the shape the original demo used. FastAPI serves the JSON API **and** the
  built React app from the same origin, so there is no CORS surface and no second host to secure.
- **PostgreSQL + Redis, four containers** — the shape the brief asks for: Prisma owns the schema, the
  API runs as a least-privilege database role, and rate-limit counters are shared.

## The full stack (PostgreSQL + Redis + Prisma)

> **Status:** the compose file was written and syntax-checked here but **not executed** — the build
> environment has no Docker daemon. Every component was exercised individually instead: the Prisma
> migration applied to PostgreSQL 16.2, the role/privilege files applied and attacked
> (`backend/tests/test_db_roles.py`, 13 tests), the full API test suite against that server
> (235 tests), and the Redis limiter against a real Redis (`backend/tests/test_redis_limiter.py`, 11
> tests). The YAML is the only unexecuted part.

```bash
cp .env.example .env
# Fill in POSTGRES_PASSWORD, MIGRATOR_PASSWORD, APP_PASSWORD, JWT_SECRET
python -c 'import secrets; print(secrets.token_urlsafe(48))'   # for each secret

docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs -f api
```

Startup order is encoded with `depends_on`, and it is not decoration:

| Step | Container | What it does | Why it must come first |
|---|---|---|---|
| 1 | `db`, `redis` | PostgreSQL 16, Redis 7 (AOF on) | nothing can start without them |
| 2 | `migrate` | `prisma migrate deploy` as `balancevault_migrator` | the tables must exist |
| 3 | `grants` | apply `01-roles.sql` then `02-grants.sql` | per-table grants need the tables |
| 4 | `api` | uvicorn as `balancevault_app` | it must not start before it can read |

### First admin

There is deliberately no unauthenticated "create the first admin" endpoint. The one-time step is a
script:

```bash
docker compose -f deploy/docker-compose.yml --profile bootstrap run --rm bootstrap \
    --email you@example.com --name "Ops Admin" --print-token
```

It prints a generated one-time password (the only time it is available), writes
`ADMIN_BOOTSTRAPPED` into the hash-chained audit log, and **refuses to run again** once any admin
exists. `--force` is available for a deliberate second admin; `--password-stdin` and
`LEDGERLINE_BOOTSTRAP_PASSWORD` avoid the prompt in automation. The password is never taken from
argv, because argv shows up in `ps` and shell history.

Then:

```bash
curl -s localhost:8000/api/health | python -m json.tool    # dependency report
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/api/ready   # 200 when ready
```

## Prisma migrations

`prisma/` owns the PostgreSQL DDL; the API never issues DDL. Standard workflow on a machine with
network access (the build environment for this submission could not download Prisma's engine
binaries — see PROMPTS.md, G25):

```bash
cd prisma
npm install                       # installs the prisma CLI
export DATABASE_URL="postgresql://balancevault_migrator:...@host:5432/balancevault"
npx prisma migrate status         # what is applied vs what is pending
npx prisma migrate deploy         # apply pending migrations (production-safe)
npx prisma migrate dev --name X   # author a new migration (development only)
npx prisma validate               # schema sanity check
```

Two rules that keep this honest:

1. **Never edit an applied migration.** Add a new one; `migrate deploy` compares checksums.
2. **Only the migrator role runs these.** The API's role has no `CREATE`, so a bug in the API cannot
   change the schema even if it wanted to.

`backend/tests/test_schema_parity.py` fails if `schema.prisma`, the migration SQL and the SQLite
`schema.sql` disagree about tables or columns — that is the offline guard against drift.

### Migrations on deploy

The API runs as a role with no DDL rights, so the schema is applied by a **release step**, not by the
app on startup:

```bash
cd backend && python -m scripts.migrate          # or: make migrate
```

| Platform | Where the command goes |
|---|---|
| Fly.io | `[deploy] release_command = "python -m scripts.migrate"` (see `fly.toml.example`) |
| Render | nothing to configure: `scripts/start.sh` migrates on boot. A paid instance may use
`preDeployCommand` instead, with `LEDGERLINE_SKIP_STARTUP_MIGRATION=1` |
| Docker Compose | the `migrate` service already does this (`prisma migrate deploy`) |

Behaviour, and why it is safe to run on every deploy:

- **Empty database** → applies `prisma/migrations/*/migration.sql` and confirms `schema_meta.version`.
- **Schema already present** → changes nothing and prints the version (idempotent).
- **No target configured** → exits `1`, so the platform stops the deploy instead of starting the API
  against a database it cannot use.

`LEDGERLINE_MIGRATION_DATABASE_URL` (optional) lets the release step connect with the **migrator**
credential while the API keeps `LEDGERLINE_DATABASE_URL` as the **application** role. Unset, both use
the same URL.

> **What this script does not do:** it does not maintain its own migration ledger. Prisma owns
> `_prisma_migrations`, and a private hand-rolled table would make a later `prisma migrate deploy`
> believe nothing had been applied and re-run the initial migration. So `scripts.migrate` bootstraps an
> empty database (the fresh-deploy case); once a deployment is live, schema changes go through
> `prisma migrate deploy` — the workflow in the section above.

The runtime image ships `prisma/migrations` for exactly this reason (`COPY prisma/ ./prisma/` in the
root `Dockerfile`), because `pg.py` resolves the directory at `/app/prisma/migrations`.

## Deploying on Render

`render.yaml` in the repository root is a Blueprint: **New + → Blueprint → pick this repository → Apply**.
It declares one Docker web service — the API serves the built SPA itself, so there is no second service
and no CORS surface.

Three Render behaviours would break this image if nothing was done about them, so the repo handles them:

| Render behaviour | What the repo does |
|---|---|
| The port is assigned dynamically in `$PORT` | `scripts/start.sh` runs uvicorn on `${PORT:-8000}`, and the Dockerfile health check reads the same variable. A hardcoded 8000 gives a 502 from the load balancer while the process log looks healthy |
| Not every plan has a pre-deploy command | the entrypoint runs `python -m scripts.migrate` before uvicorn, under `set -eu`, so a failed migration aborts the boot instead of serving against a missing schema |
| The only stable hostname is `RENDER_EXTERNAL_HOSTNAME` | it is used as the default `LEDGERLINE_TRUSTED_HOSTS`, which also enables reading `X-Forwarded-For` for the per-IP login throttle |

**Which branch is deployed.** `branch:` inside `render.yaml` — not the branch picker in
the dashboard — is what Render builds. This has already bitten once: a blueprint created
from the right branch still deployed `main`, because the file said `branch: main`, so the
service came up with every setting correct and none of the code those settings were for.
Change that line when the target moves (to `main` after a merge, for example), then
**Blueprints → Sync** and redeploy. Recognising the symptom takes one glance: a build from
`main` starts its log with `INFO: Started server process`, while a build of this branch
starts with `[migrate] target: ...`.

**Cost.** The blueprint is the zero-cost configuration — Render `free` + Neon `free` + Upstash `free`,
no card needed on any of the three. Three consequences follow, and they are stated here rather than
discovered during a demo:

- **Cold start.** A free web service spins down after 15 minutes without traffic, and the next request
  spends about a minute waking it (Render shows its own loading page meanwhile). Open the URL once before
  a demo, or switch the service to `starter` (~$7/month) in the dashboard and the sleep disappears. The
  750 instance-hours per month only run out if something keeps the service permanently awake.
- **No Shell and no one-off jobs.** So `bootstrap_admin` cannot be run from inside Render. There are two
  ways in; both are below.
- **No free Render Postgres.** It expires 30 days after creation and is deleted 14 days later, taking the
  ledger with it. Use **Neon** `free` (`aws-ap-southeast-1`, same region, `?sslmode=require`) for
  PostgreSQL and **Upstash** `free` (`ap-southeast-1`, `rediss://`) for Redis. Neither expires.

```bash
# 1. Neon (console.neon.tech): project in AWS ap-southeast-1. Copy the DIRECT
#    connection string (not the -pooler host) and append ?sslmode=require
# 2. Upstash (console.upstash.com): Redis in ap-southeast-1, TLS on; copy rediss://
# 3. Render: New + → Blueprint → this repository
#      branch:  the branch you want live — Render deploys git, not a working dir
#      region:  singapore
#    Enter LEDGERLINE_DATABASE_URL and LEDGERLINE_REDIS_URL when prompted.
```

Then open `https://<name>.onrender.com/api/health`: `backend: postgres` and
`checks.schema.version: "2"` mean the release step did its job.

### First admin when there is no Shell

`bootstrap_admin` is a script, not an endpoint (decision D14), and a free instance offers nowhere to run
it. Two options — the first leaves no known credentials behind:

**From your own machine**, after the first successful deploy (that is what creates the schema):

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
LEDGERLINE_DATABASE_URL="postgresql://...neon.tech/balancevault?sslmode=require" \
  .venv/bin/python -m scripts.bootstrap_admin --email you@example.com --print-token
```

It prints a one-time password; the script refuses to run again once an admin exists, and the creation is
written into the audit chain as a `SYSTEM` event.

**Or seed the documented demo users**: set `LEDGERLINE_DEMO_SEED=1`, redeploy, log in as
`admin@ledgerline.test` / `LedgerLine!Demo2024`, change that password from the dashboard, then set the
variable back to `0` and redeploy. Those credentials are in this repository, so an instance left in that
state has a published admin account — it is a way to get in, not a way to stay.

### First-deploy failures, and what they actually mean

All three of these happened on the first real Render deploy of this repository:

| Log line | Cause | Fix |
|---|---|---|
| `LEDGERLINE_JWT_SECRET must be set to at least 32 characters in prod` | the service was created by hand instead of from `render.yaml`, so nothing generated the secret | add `LEDGERLINE_JWT_SECRET` yourself, 32+ characters |
| `ValueError: Redis URL must specify one of the following schemes (redis://, rediss://, unix://)` | the value was pasted **with its surrounding quotes** (`"rediss://..."`), so the scheme read `"rediss` | quotes and stray whitespace are now stripped on load, and a URL that still does not parse degrades the limiter instead of stopping the app (G34) |
| Deploy log has no `[migrate]` line and no `COPY prisma/ ./prisma/` step | the build came from a branch without the release step — `main` | set the service's branch to the one carrying this work, then redeploy |

The third one is worth recognising instantly: once the container starts through
`scripts/start.sh`, `[migrate] target: ...` is the *first* thing in the log. If the log
begins with `INFO: Started server process`, the image is not this image.

**Neon free and the health check.** A free Neon database suspends after ~5 idle minutes, and the monthly
allowance is 100 compute-hours. On a free Render service nothing polls it while the service sleeps, so
`healthCheckPath: /api/ready` is safe here: the database idles exactly when the site does, and the
allowance is never threatened. This becomes a trap only if you add something that keeps the site warm —
an uptime monitor or a keep-alive cron. At 0.25 CU for 730 hours that is ≈182 CU-h, so Neon would hit its
cap near day 16 and suspend the database mid-judging. If you do add one, point the health check at
`/api/health` (liveness, no query) or move the database to Neon Launch.

Platform difference worth knowing: `fly deploy` ships your working tree, while Render deploys whatever
is on the selected branch. Pick the branch in the dashboard, or merge first.

## Database roles

`deploy/postgres/01-roles.sql` creates the two roles; `02-grants.sql` grants per table. The split is
the point:

| Role | Used by | Rights |
|---|---|---|
| `balancevault_migrator` | `prisma migrate deploy`, the admin bootstrap | owns `public`; full DDL |
| `balancevault_app` | the running API | `SELECT` on all tables; `INSERT`/`UPDATE` on `users`, `accounts`, `sessions`, `refresh_tokens`, `chain_state`; `INSERT` only on `transactions`, `ledger_entries`, `audit_log`, `idempotency_keys`; `DELETE` only on `refresh_tokens` |

Consequence, and the reason for the split: SQL injection that reaches the database as the app role
**cannot** `UPDATE ledger_entries`, `DELETE FROM audit_log`, `TRUNCATE` or `DROP TABLE`. That is not a
claim in a document — `backend/tests/test_db_roles.py` asserts each of those refusals against a real
server.

Re-running `02-grants.sql` after a migration is safe and is the recommended habit: new tables start
readable-but-not-writable for the app (`ALTER DEFAULT PRIVILEGES`), so a forgotten grant fails closed.

## Redis

Set `LEDGERLINE_REDIS_URL` (e.g. `redis://redis:6379/0`) and both throttles — per-user money writes
and per-IP login attempts — move from process memory into Redis, where they are shared by every
worker. The check and the increment happen in one Lua script, so two workers cannot both see
"29 used" and both allow the thirtieth write.

If Redis is unreachable the limiter **fails open** and logs once; `/api/health` reports
`checks.redis.state = "unavailable"`. Reasoning is in THREATS.md §8: the money invariants live in the
database, so a cache outage must not stop the bank. If your risk appetite prefers fail-closed, that is
a one-line change in `RedisSlidingWindowLimiter.allow` — and it should be a conscious choice, which is
why it is written down here.

Redis holds only timestamps. There is nothing in it worth backing up; AOF is enabled so a restart does
not reset everyone's budget.

## Local preview (development only)

`deploy/preview/start.sh` runs the full PostgreSQL stack without Docker: a cluster in `./.pgdata`
(inside the repository, so a sandbox or laptop restart does not strand it), the two roles, the Prisma
migration, demo data, the built dashboard and the API, all on one port.

```bash
deploy/preview/start.sh            # http://localhost:8200
PORT=9000 deploy/preview/start.sh  # another port
```

It is idempotent and repairs only what is missing — virtualenv, cluster, database, `frontend/dist`
(which is gitignored and excluded from snapshots), then starts the API in the foreground. Re-running
it is the recovery procedure after a restart, and it is what the reviewer-facing preview runs on.

**Not a production path**, by construction: trust authentication on a local socket, a fixed
development JWT secret, and demo credentials. Production is the compose stack above.

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
| `LEDGERLINE_DATABASE_PATH` | `./data/ledgerline.db` | SQLite file (directory is created); ignored when a URL is set |
| `LEDGERLINE_DATABASE_URL` | unset | `postgresql://…` selects PostgreSQL. Unset = SQLite |
| `LEDGERLINE_DATABASE_SCHEMA` | unset | Postgres schema for the connection (per-test isolation uses this) |
| `LEDGERLINE_MIGRATION_DATABASE_URL` | unset | credential the release step uses for DDL; defaults to `LEDGERLINE_DATABASE_URL` |
| `LEDGERLINE_REDIS_URL` | unset | `redis://…` shares rate-limit counters across workers |
| `LEDGERLINE_BOOTSTRAP_PASSWORD` | unset | password for `scripts/bootstrap_admin.py` in automation |
| `LEDGERLINE_DAILY_OUTFLOW_LIMIT_MINOR` | `100000000` (1,000,000.00) | per-account ceiling on withdrawals + outgoing transfers per UTC day; `0` disables |
| `LEDGERLINE_MAX_REQUEST_BYTES` | `65536` | request body cap, enforced on the byte stream (chunked uploads included) |
| `LEDGERLINE_CSP_FRAME_ANCESTORS` | `'none'` in prod, `*` in dev | set to `'self'` if you need to embed the dashboard yourself |
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

- **Liveness:** `GET /api/health` — never raises, reports database reachability, the live backend
  (`sqlite`/`postgres`), the limiter backend (`redis`/`in-process`) and the applied schema version.
- **Readiness:** `GET /api/ready` — `200` only when the database answers *and* the schema is present;
  `503` otherwise. This is the probe a load balancer should use, and what the container
  `HEALTHCHECK` calls: a pod that cannot reach its database must not receive traffic.
- **Redis is deliberately not a readiness condition.** A Redis outage degrades rate limiting (the
  limiter fails open) but the API still serves correctly, so marking the service unready would remove
  healthy capacity during a cache incident. It is reported by `/api/health` instead — monitor
  `checks.redis.state` and alert on `unavailable`.
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

## Moving from SQLite to PostgreSQL

The migration is a configuration change plus one DDL run; no application code changes.

```bash
# 1. provision PostgreSQL and create the roles
psql "$ADMIN_URL" -f deploy/postgres/01-roles.sql

# 2. apply the schema as the migrator
cd prisma && DATABASE_URL="$MIGRATOR_URL" npx prisma migrate deploy

# 3. grant the app role its per-table privileges
psql "$ADMIN_URL" -f deploy/postgres/02-grants.sql

# 4. create the first admin (SQLite demo data does not travel)
cd ../backend && LEDGERLINE_DATABASE_URL="$MIGRATOR_URL" \
    python -m scripts.bootstrap_admin --email you@example.com --print-token

# 5. point the API at the app role
export LEDGERLINE_DATABASE_URL="postgresql://balancevault_app:...@host/balancevault"
uvicorn ledgerline.main:app --port 8000
```

Existing rows are not migrated by these files: this deployment starts empty. If you do need to move
existing ledgers, copy `users`, `accounts`, `transactions`, `ledger_entries`, `audit_log` and
`chain_state`, keep `id` values, then re-run the auditor (`GET /api/auditor/integrity`) — the hash
chain is computed over canonical strings and verifies identically on both engines, which is exactly
why timestamps are stored as `TEXT` rather than `timestamptz`.

Rollout advice: run the full suite against the target server before switching —

```bash
LEDGERLINE_TEST_DATABASE_URL="$APP_URL" pytest -q     # 235 passed
```
