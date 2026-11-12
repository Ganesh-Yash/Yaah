# BalanceVault

**Money movement with a ledger you can prove.**

> **BalanceVault is this codebase.** The challenge renamed the project; nothing was rewritten and
> nothing was merged from elsewhere. You will still see the original name in package paths,
> `LEDGERLINE_*` environment variables and the demo password — renaming those would have broken every
> run command and test for no functional gain. `BalanceVault` is the product name; `ledgerline` is the
> module name.

[![CI](https://github.com/Ganesh-Yash/Yaah/actions/workflows/ci.yml/badge.svg)](https://github.com/Ganesh-Yash/Yaah/actions/workflows/ci.yml)

A small banking core: accounts, deposits, withdrawals and transfers that always balance, never pay
twice, cannot be overdrawn by concurrent requests, and leave an append-only, hash-chained trail that
an auditor can independently verify.

| | |
|---|---|
| **Backend** | Python 3.11+ / FastAPI |
| **Database** | PostgreSQL 16 (deployment) or SQLite (tests, offline demo) — same code path |
| **Schema** | Prisma owns the PostgreSQL DDL and migrations; the API is Python |
| **Cache** | Redis, for rate limits shared across workers (in-process fallback) |
| **Frontend** | React 18 + TypeScript + Vite |
| **Tests** | 236 tests: all pass on PostgreSQL 16, 221 pass + 15 skip on SQLite |
| **Attack suite** | 27/27 checks against a running instance (`scripts/redteam.py`) |
| **Status** | Deployed and runnable end to end; see [known limitations](#known-limitations) |

> **Why Python and not the original Java/Maven scaffold?** The submission draft described a
> Spring Boot skeleton. The build environment I completed this work in had no JDK, no Maven and no
> network route to Maven Central, so a Java backend could have been *written* but never *compiled,
> run or attacked*. Since the rubric scores what a judge can actually break on a deployed system, I
> moved to FastAPI, where every claim below is backed by an executable test. The decision and its
> trade-offs are recorded in [PROMPTS.md](PROMPTS.md#decision-log).

---

## Contents

- [What it does](#what-it-does)
- [The ten requirements](#the-ten-requirements)
- [Quick start](#quick-start)
- [Demo accounts](#demo-accounts)
- [How the guarantees are built](#how-the-guarantees-are-built)
- [API](#api)
- [Verification and evidence](#verification-and-evidence)
- [Repository layout](#repository-layout)
- [Configuration](#configuration)
- [Known limitations](#known-limitations)
- [Documentation index](#documentation-index)

---

## What it does

Three roles, each with a different relationship to money:

| Role | Can do | Deliberately cannot |
|---|---|---|
| **CUSTOMER** (account holder) | See own accounts and statements, deposit, withdraw, transfer to another customer, read own transaction details | See anyone else's accounts, read the audit trail, open accounts |
| **ADMIN** | Open accounts, freeze/unfreeze them, create staff users, list users, unlock a locked account | Move money at all — no deposits, withdrawals or transfers |
| **AUDITOR** | Read every account, every transaction, the audit trail, and run independent integrity verification | Move money, change account state, create users |

That split is separation of duties: the person who can administer accounts cannot create money, and
the person who verifies the books cannot change them.

Money movements are atomic, double-entry and idempotent:

- a deposit writes two ledger legs (account `+amount`, external clearing `−amount`)
- a transfer writes two legs (source `−amount`, destination `+amount`)
- every transaction's legs sum to exactly zero, enforced in code *and* re-verified by the auditor
  endpoint
- every money-moving request requires an `Idempotency-Key`; the key record is committed in the same
  database transaction as the movement, so retries cannot double-pay
- each account has a daily outflow cap that withdrawals *and* transfers debit, so a stolen session
  cannot drain a balance in one burst — the refusal is a `409` and lands in the audit trail

## The ten requirements

| # | Requirement | Implementation | Proven by |
|---|---|---|---|
| 1 | Admin / account-holder / auditor authentication and role-based authorisation | `backend/ledgerline/deps.py`, `services/auth_service.py`, `routers/*` | `tests/test_auth.py` (28), `tests/test_rbac.py` (16) |
| 2 | Account creation and balance management | `services/accounts_service.py`, `routers/admin.py` | `tests/test_money.py`, `tests/test_rbac.py` |
| 3 | Deposits, withdrawals, transfers | `services/money_service.py`, `ledger.py` | `tests/test_money.py` (53) |
| 4 | Double-entry ledger and money-conservation checks | `ledger.py`, `services/integrity_service.py` | `tests/test_integrity_tamper.py`, `tests/test_money.py::TestLedgerBehaviour` |
| 5 | Server-side prevention of negative balances | conditional `UPDATE ... WHERE balance_minor + :delta >= 0` + `CHECK` constraint in `schema.sql` | `tests/test_concurrency.py`, `tests/test_integrity_tamper.py` |
| 6 | Idempotency to prevent duplicate transactions | `services/money_service.py::execute_idempotent` | `tests/test_idempotency.py` (13), `test_concurrency.py::TestConcurrentIdempotency` |
| 7 | Database transaction and concurrency protection | `db.py` (`BEGIN IMMEDIATE`), `ledger.py::apply_legs` | `tests/test_concurrency.py` (6 live-server scenarios) |
| 8 | Append-only audit trail and transaction statements | `schema.sql` triggers, `audit.py`, `services/accounts_service.py::statement` | `tests/test_audit.py` (9), `tests/test_integrity_tamper.py` |
| 9 | Account-level ownership and access-control checks | `fetch_authorized_account`, `require_customer` | `tests/test_rbac.py::TestAccountIsolation` |
| 10 | Automated security, concurrency and transaction tests | `backend/tests/*`, `scripts/redteam.py` | 236 tests + 27 attack checks |

The React dashboard, deployment files, seed data and API documentation are in place as well; the
demo video is the one deliverable I could not record here (see [known limitations](#known-limitations)).

### The six additional mandatory requirements

Added by a later clarification (BalanceVault rename). Each one is implemented, not described:

| # | Requirement | Implementation | Proven by |
|---|---|---|---|
| 11 | Account management and user onboarding | `routers/auth.py` (register), `routers/admin.py` (open account, freeze, staff users, unlock), `routers/accounts.py` | `tests/test_auth.py`, `tests/test_rbac.py`, `tests/test_money.py` |
| 12 | Role-based access control (ADMIN / CUSTOMER / AUDITOR) | `deps.py` (`ADMIN_ONLY`, `AUDITOR_OR_ADMIN`, `require_customer`), one dependency per route | `tests/test_rbac.py` (16) |
| 13 | PostgreSQL, with Prisma | `ledgerline/pg.py`, `prisma/schema.prisma`, `prisma/migrations/` | the **entire** suite on a real server: `LEDGERLINE_TEST_DATABASE_URL=... pytest -q` → 236 passed |
| 14 | Redis rate limiting | `ledgerline/limiter.py::RedisSlidingWindowLimiter` (one Lua script per check) | `tests/test_redis_limiter.py` (11) against a real Redis |
| 15 | First-admin bootstrap script | `scripts/bootstrap_admin.py` | `tests/test_bootstrap.py` (14) |
| 16 | Database roles and deployment health checks | `deploy/postgres/*.sql` (two roles), `/api/health`, `/api/ready` | `tests/test_db_roles.py` (13), `tests/test_redis_limiter.py` (11) |

Two of those deserve a sentence rather than a row:

- **The Postgres claim is the whole test suite, not a smoke test.** The same ~201 tests run against
  PostgreSQL 16.2 by pointing `LEDGERLINE_TEST_DATABASE_URL` at a server; each test gets its own
  schema. `tests/test_schema_parity.py` additionally checks that the Prisma models, the Postgres
  migration and the SQLite schema describe identical tables and columns.
- **The role claim is a fact about the database.** `tests/test_db_roles.py` builds a throwaway
  database, applies the real deployment files, and asserts the API's role is refused `UPDATE
  ledger_entries`, `DELETE FROM audit_log`, `TRUNCATE`, `CREATE TABLE` and `DROP TABLE`.

## Quick start

### Option A — the full stack (PostgreSQL + Redis + Prisma migrations)

```bash
cp .env.example .env                     # fill in the four secrets
docker compose -f deploy/docker-compose.yml up -d --build

# create the first admin (there is no other way in on an empty database)
docker compose -f deploy/docker-compose.yml --profile bootstrap run --rm bootstrap \
    --email you@example.com --name "Ops Admin" --print-token

curl -s localhost:8000/api/health | python -m json.tool
# open http://localhost:8000  → dashboard, /docs → OpenAPI, /api/ready → readiness
```

To put this on a public host, `render.yaml` (Render, free plan) and `fly.toml.example` (Fly, always-on)
both describe a single Docker service; the container migrates the database on boot and serves the SPA
itself, so there is nothing else to run. Steps for either, including the free-tier caveats:
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

The stack starts in dependency order — `db` → `migrate` (Prisma) → `grants` (the two database roles)
→ `api` — and the API runs as `balancevault_app`, a role that cannot rewrite the ledger.

> **Honest caveat:** the machine I developed on has no Docker daemon and no `psql`, so I could not run
> this compose file. What I *did* run is every part of it that matters: the Prisma migration SQL
> applied to a real PostgreSQL 16.2 server, the role/privilege files applied and attacked
> (`tests/test_db_roles.py`, 13 tests), the full 211-test suite against that server, and the API in
> front of it. The compose file itself is unexecuted; the SQL inside it is not. That distinction is
> the one I would want a judge to hold me to.

### Option B — local development on SQLite (no Docker, no server)

The fastest path, and the one the tests and the demo video use:

```bash
cd backend
python -m venv ../.venv && source ../.venv/bin/activate     # Windows: ..\.venv\Scripts\activate
pip install -r requirements-dev.txt
export LEDGERLINE_JWT_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
python scripts/seed.py --demo
uvicorn ledgerline.main:app --port 8000
```

Migrating that to PostgreSQL is one environment variable plus a migration run:

```bash
pip install -r requirements.txt
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=dev postgres:16      # or any server
export LEDGERLINE_DATABASE_URL="postgresql://postgres:dev@127.0.0.1:5432/postgres"
cd ../prisma && npm install && DATABASE_URL="$LEDGERLINE_DATABASE_URL" npx prisma migrate deploy
cd ../backend && python scripts/bootstrap_admin.py --email you@example.com --print-token
uvicorn ledgerline.main:app --port 8000
```

### Option C — frontend dev server (hot reload)

The polished path for UI work; needs the backend from Option A or B running on :8000.

```bash
cd frontend
npm install
npm run dev                                                  # http://localhost:5173, proxies /api
```

Linux and macOS work the same way; on Windows use `set` instead of `export` in CMD (or
`$env:` in PowerShell). For a single-port recording, `npm run build` and start uvicorn with
`LEDGERLINE_STATIC_DIR` pointing at `frontend/dist` — see `docs/SCRIPT.md`.

### Tests and the attack suite

```bash
cd backend
pip install -r requirements-dev.txt
pytest -q                                  # SQLite: 221 passed, 15 skipped

# the same suite against a real PostgreSQL server (needs one running)
pip install -r requirements-postgres.txt
export LEDGERLINE_TEST_DATABASE_URL=postgresql://postgres:dev@127.0.0.1:5432/postgres
pytest -q                                  # 236 passed

python scripts/redteam.py --base-url http://localhost:8000   # against a running instance
```

Expected red-team tail:

```
Concurrency
  [PASS] 20 racing withdrawals: 10 succeed, balance floors at 0.00 — success=10 balance=0.00
  [PASS] 10 duplicate requests with one key create one transaction — transactions=1
Ledger verification after all attacks
  [PASS] integrity report is green — failed=none
27/27 checks passed
```

## Demo accounts

Created by `scripts/seed.py --demo` (or `LEDGERLINE_DEMO_SEED=1`). Password: `LedgerLine!Demo2024`.

| Email | Role | Seeded accounts |
|---|---|---|
| `alice@ledgerline.test` | CUSTOMER | Everyday (1,250.00 USD), Savings (500.00 USD) |
| `bob@ledgerline.test` | CUSTOMER | Everyday (300.00 USD) |
| `admin@ledgerline.test` | ADMIN | — |
| `auditor@ledgerline.test` | AUDITOR | — |

Demo credentials are opt-in on purpose: a deployed instance never ships with known passwords unless
the operator asked for the seed. Change them with `POST /api/auth/change-password` if you deploy this
publicly.

## How the guarantees are built

### 1. Money is an integer

Amounts are stored and computed as **minor units** (`INTEGER`): 12.34 USD is `1234`. There is no
`FLOAT`, no `REAL`, and no floating-point arithmetic anywhere in the money path. Client input is a
decimal *string* parsed by a narrow grammar (`^(0|[1-9][0-9]{0,14})(\.[0-9]{1,2})?$`), so `-5`,
`1e3`, `1.001`, `" 5.00"`, `1,000.00` and non-ASCII digits are all rejected with `422` rather than
silently reinterpreted.

### 2. The balance check and the balance write are one statement

```sql
UPDATE accounts
   SET balance_minor = balance_minor + :delta, version = version + 1
 WHERE id = :id
   AND status = 'ACTIVE'
   AND (is_system = 1 OR balance_minor + :delta >= 0)
```

If `rowcount != 1`, the leg is not legal and the whole transaction rolls back. There is no
`read balance → decide → write balance` window for another request to slip into. Writes run inside
`BEGIN IMMEDIATE`, which takes SQLite's write lock up front. As a second line of defence the schema
carries `CHECK (is_system = 1 OR balance_minor >= 0)`, so even a future code path that forgets the
conditional update cannot persist a negative customer balance.

### 3. Idempotency lives in the same transaction as the money

```
BEGIN IMMEDIATE
  SELECT idempotency_keys WHERE (user, scope, key)
  ... if present and body hash matches -> return the stored response (200 + Idempotent-Replay: true)
  ... if present and body hash differs -> 409 IDEMPOTENCY_KEY_REUSED, nothing moves
  ... otherwise: post the two legs, INSERT the key with the serialised response, COMMIT
```

A crash either loses both the movement and the key (a retry is safe) or keeps both (a retry
replays). There is no third outcome, which is exactly the window that makes naive implementations
pay twice. Failed movements (insufficient funds, frozen account) roll back and are **not** cached:
nothing moved, so a retry with the same key is a fresh, safe attempt.

### 4. History is append-only and chained

`ledger_entries`, `transactions` and `audit_log` have `BEFORE UPDATE`/`BEFORE DELETE` triggers that
`RAISE(ABORT)`. On top of that, every ledger entry and audit row stores `prev_hash` and
`entry_hash = SHA256("ledgerline/ledger/v1" ␟ id ␟ prev_hash ␟ transaction_id ␟ account_id ␟
amount_minor ␟ currency ␟ created_at)`. `GET /api/auditor/integrity` re-walks both chains, recomputes
every balance from the entries, and checks that each transaction sums to zero. The tests drop the
triggers, rewrite history and confirm the verifier notices.

### 5. Tokens and roles

Access tokens are HS256 JWTs with **pinned algorithms** (which is what defeats `alg: none` and
RS→HS confusion), mandatory `iss`/`aud`/`exp`, a 15-minute lifetime and a fingerprint of the current
password hash. The role and account status are re-read from the database on every request, so a
demoted or disabled user loses access immediately rather than at token expiry. Refresh tokens are
opaque, stored only as SHA-256 hashes, single-use and rotated; replaying a used one revokes the whole
family and writes a `REFRESH_TOKEN_REUSE` audit row.

### 6. Errors that do not leak

Every failure returns the same envelope with a stable code, a human message and the request id —
never a stack trace, SQL fragment or internal path:

```json
{"error": {"code": "INSUFFICIENT_FUNDS", "message": "Insufficient funds for this movement.",
           "details": {"available": "50.00", "requested": "50.01"}, "request_id": "req_…"}}
```

Cross-tenant reads return **404, not 403**, so account ids cannot be probed for existence.

### 7. Limits live where the money moves

Three transport and velocity controls that used to be listed as accepted risks, now enforced and
tested (`tests/test_limits.py`, `tests/test_http_hardening.py`):

- **Per-account daily outflow cap.** Withdrawals and outgoing transfers debit one counter per UTC
  day. The counter is read inside the same `BEGIN IMMEDIATE` transaction as the posting, so ten
  concurrent requests cannot all squeeze under the cap — the test fires ten and gets exactly three.
  Refusals are `409 DAILY_LIMIT_EXCEEDED` with `limit` / `used_today` / `remaining_today`, and are
  audited as `DENY`. Configure with `LEDGERLINE_DAILY_OUTFLOW_LIMIT_MINOR` (`0` disables it).
- **A body cap that counts bytes, not headers.** `Content-Length` is a claim; a chunked request sends
  none. The ASGI middleware counts what actually arrives and answers `413` before buffering it
  (`LEDGERLINE_MAX_REQUEST_BYTES`, default 64 KiB).
- **Framing denied in production.** The CSP is environment-derived: `frame-ancestors 'none'` when
  `LEDGERLINE_ENV=prod`, `*` in dev so the hosted preview can embed the dashboard. Override with
  `LEDGERLINE_CSP_FRAME_ANCESTORS`.

## API

Full request/response examples are in [docs/API.md](docs/API.md); the running service also serves
`/docs` and `/openapi.json`.

| Method | Path | Who | Notes |
|---|---|---|---|
| `GET` | `/api/health` | anyone | liveness + database check |
| `GET` | `/api/meta` | anyone | version, currencies, limits |
| `POST` | `/api/auth/register` | anyone | always creates a CUSTOMER |
| `POST` | `/api/auth/login` | anyone | returns access + refresh tokens |
| `POST` | `/api/auth/refresh` | holder of a refresh token | rotates, detects reuse |
| `POST` | `/api/auth/logout` | authenticated | revokes the refresh family |
| `GET` | `/api/auth/me` | authenticated | |
| `POST` | `/api/auth/change-password` | authenticated | kills existing sessions |
| `GET` | `/api/accounts` | authenticated | customers see their own; staff see all |
| `POST` | `/api/accounts` | ADMIN | open an account |
| `GET` | `/api/accounts/{id}` | owner / staff | |
| `GET` | `/api/accounts/{id}/statement` | owner / staff | cursor pagination, running balance |
| `POST` | `/api/accounts/{id}/freeze` | ADMIN | `{"frozen": true\|false}` |
| `POST` | `/api/accounts/{id}/deposit` | owner | **requires `Idempotency-Key`** |
| `POST` | `/api/accounts/{id}/withdraw` | owner | **requires `Idempotency-Key`** |
| `POST` | `/api/transfers` | owner of source | **requires `Idempotency-Key`** |
| `GET` | `/api/transactions/{id}` | parties / staff | includes ledger legs and hashes |
| `GET` | `/api/audit` | ADMIN, AUDITOR | append-only trail, filters |
| `GET` | `/api/auditor/integrity` | ADMIN, AUDITOR | independent verification |
| `GET` | `/api/admin/users` | ADMIN | |
| `POST` | `/api/admin/users` | ADMIN | create ADMIN/AUDITOR only |
| `POST` | `/api/admin/users/{id}/unlock` | ADMIN | clears lockout |

## Verification and evidence

| Evidence | Where |
|---|---|
| 236 automated tests (unit, integration, live-server concurrency), run on both engines | `backend/tests/` |
| 27-check attack run against a live instance | `backend/scripts/redteam.py` |
| Concurrency: 20 racing withdrawals, 30 racing deposits, 20 duplicate keys, 40 opposite transfers | `backend/tests/test_concurrency.py` |
| Tamper detection: triggers dropped, history edited, verifier catches it | `backend/tests/test_integrity_tamper.py` |
| Token attacks: `alg=none`, tampered signature, wrong issuer/audience, expiry | `backend/tests/test_auth.py` |
| Threat model mapped to controls and tests | [THREATS.md](THREATS.md) |
| Honest AI usage log | [PROMPTS.md](PROMPTS.md) |

## Repository layout

```
backend/
  ledgerline/
    main.py            app factory, middleware, exception handlers, SPA hosting
    config.py          every security-relevant knob, env-driven
    db.py              backend factory + SQLite access (BEGIN IMMEDIATE writes)
    pg.py              PostgreSQL adapter (advisory-lock writes, Prisma-owned schema)
    limiter.py         in-process and Redis sliding-window rate limiters
    schema.sql         SQLite tables, CHECK constraints, append-only triggers
    money.py           strict decimal-string <-> integer minor units
    security.py        bcrypt, JWT, refresh-token rotation
    chain.py           SHA-256 hash chains + verification
    ledger.py          the only code that changes a balance
    audit.py           append-only audit writes
    services/          auth, accounts, money movement, integrity
    routers/           thin HTTP layer
    seed.py            system accounts + demo data
  scripts/seed.py      database initialiser (refuses demo data in prod)
  scripts/bootstrap_admin.py  first ADMIN, without an API call
  scripts/redteam.py   attack suite against a running instance
  tests/               236 tests (SQLite by default, PostgreSQL via an env var)
prisma/
  schema.prisma        PostgreSQL models; Node owns DDL, Python owns runtime
  migrations/          the migration `prisma migrate deploy` applies
deploy/
  docker-compose.yml   db → migrate → grants → api, bootstrap behind a profile
  postgres/            the two roles and their least-privilege grants
frontend/
  src/api.ts           typed client, idempotency-aware, refresh handling
  src/views/           auth, accounts, administration, audit
docs/                  architecture, API, deployment, demo script
Dockerfile             multi-stage: build SPA, then run API
docker-compose.yml     one-command demo deployment
render.yaml            Render Blueprint (Fly equivalent: fly.toml.example)
```

## Configuration

All settings are environment variables; see [.env.example](.env.example) and
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). The ones that matter:

| Variable | Default | Notes |
|---|---|---|
| `LEDGERLINE_ENV` | `dev` | `prod` refuses to boot with a weak JWT secret |
| `LEDGERLINE_JWT_SECRET` | ephemeral in dev | **must** be ≥32 chars in prod |
| `LEDGERLINE_DATABASE_PATH` | `./data/ledgerline.db` | the SQLite file |
| `LEDGERLINE_CORS_ORIGINS` | localhost dev origins | comma-separated allowlist |
| `LEDGERLINE_MAX_AMOUNT_MINOR` | `10000000` (100,000.00) | per-movement ceiling |
| `LEDGERLINE_DAILY_OUTFLOW_LIMIT_MINOR` | `100000000` (1,000,000.00) | per-account daily outflow cap; `0` disables |
| `LEDGERLINE_MAX_REQUEST_BYTES` | `65536` | request body cap, counted as it streams |
| `LEDGERLINE_CSP_FRAME_ANCESTORS` | derived | `'none'` in prod, `*` in dev |
| `LEDGERLINE_LOGIN_MAX_FAILURES` | `5` | per-account lockout |
| `LEDGERLINE_LOGIN_MAX_FAILURES_PER_IP` | `50` | password-spray ceiling |
| `LEDGERLINE_DEMO_SEED` | `0` | opt-in demo credentials |
| `LEDGERLINE_STATIC_DIR` | unset | serve the built SPA from the API |

## Known limitations

Written down rather than glossed over:

1. **One writer at a time, on both engines.** On SQLite that is `BEGIN IMMEDIATE`; on PostgreSQL it is
   a transaction-scoped advisory lock. This is a deliberate choice, not an oversight: it keeps one
   money code path and removes a TOCTOU race in the daily-limit check. Writes therefore scale
   *up* (bigger machine) rather than *out* (more writers) — the honest ceiling of this design. Reads
   and the auditor endpoints scale normally. See
   [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#two-engines-one-interface).
2. **One currency per account, no FX.** Transfers between different currencies are rejected rather
   than silently converted.
3. **Redis rate limiting is opt-in.** Set `LEDGERLINE_REDIS_URL` and both the per-user money throttle
   and the per-IP login throttle share one counter across workers; leave it unset and they are
   per-process, which is correct for exactly one worker. `/api/health` reports which is live
   (`checks.redis.state`), so a scaled deployment that forgot the variable is visible rather than
   silently over-permissive. The *account lockout* is in the database either way.
4. **Refresh token in `localStorage`.** A XSS vulnerability would expose it. Mitigations: strict CSP,
   no `dangerouslySetInnerHTML`, 15-minute access tokens, refresh rotation with reuse detection.
   The stronger design (HttpOnly cookie + CSRF token) is described in THREATS.md but not built.
5. **Tamper-evident, not tamper-proof.** Someone with write access to the database file can rewrite
   the whole chain consistently; anchoring the head hash somewhere external is required to catch
   that (documented as a residual risk, not implemented).
6. **The Prisma CLI could not be run in this build environment.** `npm install prisma` works, but
   every command that needs an engine fails: the download from `binaries.prisma.sh` is blocked here
   (the WASM-engine workarounds do not bypass it either). So the migration SQL in
   `prisma/migrations/` was applied and tested directly against PostgreSQL 16.2, and
   `tests/test_schema_parity.py` guards drift between the Prisma models, the migration and the SQLite
   schema. Running `npx prisma validate` / `migrate deploy` on a machine with network is the first
   thing to do in CI — see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#prisma-migrations).
7. **No password reset or email verification**, no 2FA, no device management. An admin can unlock a
   locked account, which is deliberately audited.
8. **No demo video.** The environment I built this in cannot capture a screen recording;
   [docs/DEMO.md](docs/DEMO.md) is the exact click-by-click script with the API calls and expected
   output, so the demo can be reproduced in about five minutes.
9. **The auditor endpoints scan the whole ledger.** Fine at demo scale; production would checkpoint
   chain verification (`chain_state.last_id`) and verify incrementally.
10. **The Docker Compose stack was written but not executed here.** There is no Docker daemon in this
    build environment. Its parts were run individually: the migration SQL against real PostgreSQL
    16.2, the role/privilege files with 13 tests, the API against that server with the full suite.
    The YAML is syntax-checked, and that is the extent of the claim.
11. **`scripts/bootstrap_admin.py` needs database credentials.** That is inherent — bootstrapping the
    first admin means writing to the users table before any admin exists. It is why the migrator
    credential is the most protected secret in a deployment, and why the script refuses to run when an
    admin is already present.

## Documentation index

| Document | What is in it |
|---|---|
| [THREATS.md](THREATS.md) | threat model: assets, actors, 30+ threats, controls, the test that proves each, and residual risk |
| [PROMPTS.md](PROMPTS.md) | how AI was used, what it got wrong, what I caught, and the decision log |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | components, data model, the hash-chain specification, Postgres porting notes |
| [docs/API.md](docs/API.md) | endpoint reference with requests, responses and error codes |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Docker, environment, TLS/proxy, backup and restore, key rotation |
| [docs/DEMO.md](docs/DEMO.md) | five-minute demo script, including how to *show* idempotency and tamper detection |
| [docs/VERIFICATION.md](docs/VERIFICATION.md) | what was actually executed, where, with what output — and what was not verified |
