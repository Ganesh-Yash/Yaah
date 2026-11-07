# Architecture

```
                        ┌─────────────────────────────────────────────┐
  browser ──────────────►  React SPA (Vite build, served by FastAPI)   │
                        │  api.ts: tokens, refresh, Idempotency-Key    │
                        └──────────────────┬──────────────────────────┘
                                           │ same origin (or Vite proxy)
                        ┌──────────────────▼──────────────────────────┐
                        │              FastAPI application             │
                        │  middleware: request id · CSP · CORS · 64KiB │
                        │  routers: auth · accounts · transfers ·      │
                        │           admin · audit_views · meta         │
                        └──────────────────┬──────────────────────────┘
                                           │
        ┌──────────────────────────────────┼──────────────────────────────────┐
        │                                  │                                  │
┌───────▼────────┐              ┌──────────▼───────────┐            ┌─────────▼─────────┐
│ auth_service   │              │ money_service        │            │ integrity_service │
│ bcrypt, JWT,   │              │ idempotency + limits │            │ balance recompute │
│ sessions,      │              │                      │            │ chain verification│
│ refresh rotate │              └──────────┬───────────┘            └─────────┬─────────┘
└───────┬────────┘                         │                                  │
        │                        ┌─────────▼──────────┐                       │
        │                        │ ledger.py (only     │                       │
        │                        │ code that mutates a │                       │
        │                        │ balance)            │                       │
        │                        └─────────┬──────────┘                       │
        └──────────────────────────────────┼──────────────────────────────────┘
                                           │
                        ┌──────────────────▼──────────────────────────┐
                        │  db.py: SQLite, WAL, synchronous=FULL,       │
                        │  BEGIN IMMEDIATE for every write             │
                        └──────────────────┬──────────────────────────┘
                                           │
                        ┌──────────────────▼──────────────────────────┐
                        │ schema.sql: CHECK constraints + append-only  │
                        │ triggers + chain_state head pointers         │
                        └─────────────────────────────────────────────┘
```

## Layers and why they exist

| Layer | Files | Rule |
|---|---|---|
| Transport | `main.py`, `routers/` | No business logic. Validates shapes, calls a service, sets headers. |
| Service | `services/` | Owns the transaction boundary and all invariants. |
| Posting engine | `ledger.py` | The **only** module allowed to change `accounts.balance_minor` or write `ledger_entries`. |
| Storage | `db.py`, `schema.sql` | Enforces what code might forget: non-negative balances, append-only history. |
| Cross-cutting | `chain.py`, `audit.py`, `security.py`, `ctx.py` | Hash chains, audit writes, crypto, request context. |

The dependency direction is one-way: routers → services → ledger → db. Nothing imports upwards.

## Data model

| Table | Purpose | Notes |
|---|---|---|
| `users` | identities | role ∈ ADMIN/CUSTOMER/AUDITOR; `failed_login_count`, `locked_until` |
| `sessions` | one sign-in each | access tokens carry `sid`; revoking a session kills its tokens immediately |
| `refresh_tokens` | opaque, hashed, single-use | `family_id` == session id; `used_at` drives reuse detection |
| `accounts` | balances | `balance_minor` is a **cache** of `SUM(ledger_entries)`; `is_system` marks the clearing accounts |
| `transactions` | movement headers | immutable; type + amount + initiator |
| `ledger_entries` | the truth | signed minor units; `id` contiguous; `prev_hash`/`entry_hash` |
| `idempotency_keys` | request dedupe | unique `(user_id, scope, key)` + request fingerprint + stored response |
| `audit_log` | who did what | append-only, hash-chained, includes denials |
| `chain_state` | chain heads | O(1) appends without a `MAX(id)` scan |
| `schema_meta` | schema version | placeholder for a real migration tool |

### The five invariants

| Invariant | Enforced by | Proven by |
|---|---|---|
| Every transaction's legs sum to exactly zero | `ledger.apply_legs` refuses to post otherwise; re-verified by the auditor endpoint | `test_integrity_tamper.py::TestLedgerShape`, `test_money.py` |
| A customer balance is never negative | conditional `UPDATE … AND balance_minor + :delta >= 0` **and** `CHECK (is_system = 1 OR balance_minor >= 0)` | `test_concurrency.py`, `test_integrity_tamper.py::test_a_negative_customer_balance_is_detected` |
| `accounts.balance_minor == SUM(entries.amount_minor)` | the posting engine writes both in one transaction; `/api/auditor/integrity` recomputes | `test_integrity_tamper.py::test_integrity_notices_a_tampered_balance_cache` |
| History is append-only | `BEFORE UPDATE`/`BEFORE DELETE` triggers on `ledger_entries`, `transactions`, `audit_log` | `test_integrity_tamper.py::TestAppendOnly` |
| The same request cannot move money twice | idempotency row committed in the movement's transaction | `test_idempotency.py`, `test_concurrency.py::TestConcurrentIdempotency` |

## The hash chain, precisely

So that an auditor can verify a database copy without running this application:

```
domain      = "ledgerline/ledger/v1"      (or "ledgerline/audit/v1")
fields      = id, prev_hash, <domain-specific fields in fixed order>
canon(x)    = "" if x is None else str(x)
entry_hash  = SHA256( domain \x1f id \x1f prev_hash \x1f field_1 \x1f … \x1f field_n )
```

- `\x1f` is the ASCII unit separator, which cannot appear in any field we store.
- Ledger fields: `transaction_id, account_id, amount_minor, currency, created_at`.
- Audit fields: `actor_user_id, actor_role, subject, action, outcome, target_type, target_id, ip,
  user_agent, request_id, detail, created_at`. `detail` is stored as canonical JSON
  (`sort_keys=True, separators=(",", ":")`) and hashed exactly as stored.
- The first entry's `prev_hash` is 64 zeros, ids start at 1 and are contiguous, and
  `chain_state.last_hash` must equal the last row's `entry_hash`.

`GET /api/auditor/integrity` returns the head hashes, so a verifier can compare them with an
externally anchored value.

## Sequence: a transfer with a retry

```
client                         API                              SQLite
  │ POST /transfers               │                                  │
  │ Idempotency-Key: K           │                                  │
  ├──────────────────────────────►                                  │
  │                              │ BEGIN IMMEDIATE ────────────────►│  write lock taken
  │                              │ SELECT idempotency_keys(user,scope,K)
  │                              │   → none                          │
  │                              │ UPDATE accounts SET balance=… WHERE id=src
  │                              │        AND balance + (-amt) >= 0  │  rowcount 1
  │                              │ UPDATE accounts SET balance=… WHERE id=dst
  │                              │ INSERT transactions …             │
  │                              │ INSERT ledger_entries ×2 (hashed) │
  │                              │ INSERT idempotency_keys(K, hash, response)
  │                              │ COMMIT ─────────────────────────►│
  │◄── 201 + Location ───────────┤                                  │
  │                              │                                  │
  │ (network dies before the client sees the response)               │
  │ POST /transfers  same K      │                                  │
  ├──────────────────────────────►                                  │
  │                              │ BEGIN IMMEDIATE ────────────────►│
  │                              │ SELECT idempotency_keys … → found │
  │                              │ fingerprint matches → replay      │
  │◄── 200 Idempotent-Replay: true + the original body ────────────┤
```

The second request never reaches the posting engine. Note that the replay response is the *original*
body, including the balances as they were at the time — that is what makes the replay trustworthy to
an automated client.

## Concurrency model

SQLite allows one writer at a time. The design leans into that instead of fighting it:

1. Every write enters through `db.write()`, which issues `BEGIN IMMEDIATE` — the write lock is taken
   before the first read, so no transaction can be half-upgraded and no two can interleave.
2. Balance mutation is a single conditional `UPDATE`, so even if the isolation model changed, the
   check-and-write could not be split.
3. `busy_timeout = 10s` means a concurrent writer waits rather than failing; a genuine timeout becomes
   `503` + `Retry-After`.
4. `synchronous = FULL` + WAL: a committed movement survives power loss; readers never block behind
   the writer.
5. Legs are applied in ascending account-id order, which is the deadlock-avoidance discipline that
   carries over to Postgres.

Measured behaviour (`tests/test_concurrency.py`, real uvicorn, real sockets, 2 vCPU sandbox): 20
racing withdrawals finish with exactly 10 successes and a `0.00` balance; 40 opposite transfers
conserve the total exactly; 20 duplicate requests with one key produce one transaction.

## Failure modes and what happens

| Failure | Result |
|---|---|
| Process dies mid-transaction | SQLite rolls back on next open; either both the movement and the idempotency key exist, or neither |
| Process dies after commit, before responding | Client retries with the same key → replay, no second movement |
| Insufficient funds discovered mid-transaction | Whole transaction rolls back; the denial is audited afterwards on a separate connection |
| Database locked beyond timeout | `503 SERVICE_BUSY` with `Retry-After: 1`; the client retries with the same idempotency key |
| Disk full during commit | Commit fails, transaction rolled back, `500`; no partial state |
| Tampering with the database file | Balance recomputation or the hash chain fails; `/api/auditor/integrity` reports `ok: false` with the first bad id |

## Porting to Postgres

The write path is deliberately shaped so the port is mechanical:

| Concern | SQLite (now) | Postgres |
|---|---|---|
| Write lock | `BEGIN IMMEDIATE` | `BEGIN` then `SELECT ... FOR UPDATE` on the account ids **in ascending order** (prevents deadlocks) |
| Ledger ids | `chain_state.last_id + 1` | a real `SEQUENCE` (`nextval`), or keep `chain_state` with row locking |
| Integers | `INTEGER` (64-bit) | `BIGINT` |
| Timestamps | `TEXT` ISO-8601 UTC | `TIMESTAMPTZ` (hash the same canonical string form) |
| Append-only | triggers with `RAISE(ABORT)` | a `BEFORE UPDATE OR DELETE` trigger with `RAISE EXCEPTION`, plus `REVOKE UPDATE, DELETE` from the app role |
| Idempotency race | serialised by the single writer | `INSERT ... ON CONFLICT (user_id, scope, key) DO NOTHING`, then `SELECT ... FOR UPDATE` on the winner's row |
| Conditional balance update | unchanged | unchanged — same SQL |

The important part is that the conditional `UPDATE` and the idempotency-in-one-transaction pattern do
not change; only the locking primitive does.

## Deliberate non-goals

No FX, no interest, no scheduled payments, no reversals, no multi-tenant partitioning, no event
streaming. Each would be a project of its own; the ones that affect integrity are listed as known
limitations in the README instead of being half-built.
