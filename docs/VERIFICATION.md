# Verification log

Everything the README claims as "verified" points here. This file records **what was actually
executed, on which machine, with what output** — and, at the bottom, what was *not* executed and is
therefore never claimed.

Last updated: 2026-11-08.

## Where things were run

| Environment | Used for | Runtime |
|---|---|---|
| Linux container (author's build environment) | full test suite, attack suite, HTTP matrix, frontend production build | Python 3.11.2, Node 22.22.3, SQLite 3.40.1 |
| Windows 11 (`10.0.26300`), `cmd.exe` | full test suite, attack suite, seed, live demo UI | project `.venv` (Python 3.10), Node 22 |
| GitHub Actions `ubuntu-latest` | tests + attack suite + frontend build + Docker image build, on every push | per `.github/workflows/ci.yml` |

## 1. Automated test suite

```
cd backend
python -m pytest -q
```

| Run | Result | Wall clock |
|---|---|---|
| Linux container | **155 passed** | 22.08 s |
| Windows 11, `cmd.exe` | **155 passed** | 41.35 s |

Breakdown (from `pytest --collect-only`):

| File | Tests | What it covers |
|---|---|---|
| `tests/test_money.py` | 53 | decimal handling, double-entry balance, deposits/withdrawals/transfers, negative-balance refusal, conservation |
| `tests/test_auth.py` | 31 | registration, login, lockout, JWT validation, refresh/logout, password change |
| `tests/test_rbac.py` | 16 | role matrix: customer / admin / auditor separation of duties, IDOR |
| `tests/test_integrity_tamper.py` | 15 | hash-chain verification, tamper detection, append-only enforcement |
| `tests/test_idempotency.py` | 13 | same key → one effect, key/body mismatch → 409, per-route scoping |
| `tests/test_http_hardening.py` | 12 | body-size cap, security headers, unknown-path 404 envelope, error shape |
| `tests/test_audit.py` | 9 | audit rows for allowed and denied actions, statement completeness |
| `tests/test_concurrency.py` | 6 | parallel withdrawals/transfers, one-writer serialisation, balance floor |

## 2. Attack suite against a running instance

```
python scripts/redteam.py --base-url http://127.0.0.1:8000
```

Result: **27/27 checks passed**, on both Linux and Windows 11. The checks that matter most:

```
[PASS] alg=none JWT rejected — HTTP 401
[PASS] other customer's account is 404 (not 403) — HTTP 404
[PASS] admin cannot deposit (separation of duties) — HTTP 403
[PASS] overdraw rejected — HTTP 409
[PASS] key reuse with a different body is a 409 — HTTP 409
[PASS] 20 racing withdrawals: 10 succeed, balance floors at 0.00 — success=10 balance=0.00
[PASS] 10 duplicate requests with one key create one transaction — transactions=1
[PASS] oversized body rejected — HTTP 413
[PASS] integrity report is green — failed=none
[PASS] customer funds are conserved and non-negative — customer_funds=2585.00
[PASS] denied attempts appear in the audit trail — denials=3
```

Note: the attack suite runs against a **live database** and deliberately creates users, accounts and
transactions in it. Reset with `scripts/seed.py --demo` on a fresh `data/ledgerline.db` before
recording a demo.

## 3. HTTP behaviour of the single-port deployment

Served with `LEDGERLINE_STATIC_DIR` pointing at the built `frontend/dist`:

| Request | Result |
|---|---|
| `GET /` | 200 `text/html` (React app) |
| `GET /audit` (client route) | 200 `text/html` (SPA fallback) |
| `GET /api/health` | 200 JSON |
| `GET /api/nope` | 404 JSON (`NOT_FOUND` envelope, never HTML) |
| `GET /favicon.svg` | 200 `image/svg+xml` |
| `GET /favicon.ico` | 404 JSON |
| `GET /assets/nope.js` | 404 JSON (a missing asset is not a page) |
| `GET /docs` | 200 (OpenAPI UI) |

## 4. Frontend build

```
cd frontend && npm ci && npm run build
```

`tsc --noEmit` clean; `vite build` → `dist/index.html`, `dist/assets/*.js` (170.86 kB, 54.10 kB gzip),
`dist/assets/*.css` (8.60 kB), `dist/favicon.svg`.

## 5. Continuous integration

| Run | Branch | Backend tests | Frontend build | Docker image builds |
|---|---|---|---|---|
| [42378862121](https://github.com/Ganesh-Yash/Yaah/actions/runs/42378862121) | `main` | success | success | success |
| [42492093608](https://github.com/Ganesh-Yash/Yaah/actions/runs/42492093608) | PR #2 | success | success | success |
| [42492314571](https://github.com/Ganesh-Yash/Yaah/actions/runs/42492314571) | `main` | success | success | success |

The "Backend tests" job runs the whole suite **and** starts the API to run `scripts/redteam.py`
against it, so a regression in the attack surface fails the build, not just a review.

## 6. What was *not* verified

Stated plainly, because the README does not claim it:

| Not verified | Why | What the repo claims instead |
|---|---|---|
| Running the Docker **container** | the development machine has no Docker daemon | the *image build* is verified by CI (`docker build -t ledgerline:ci .`, green) |
| PostgreSQL | no server available here | the port is documented in `docs/ARCHITECTURE.md` as a design, clearly marked untested |
| Multi-node / horizontal scaling | SQLite, single writer | listed as a known limitation, not a feature |
| Demo video | could not record one in this environment | listed as the one missing deliverable |
