# API reference

Base URL: `http://localhost:8000` (the SPA is served from the same origin).
Interactive docs: `/docs` · OpenAPI: `/openapi.json`.

## Conventions

**Authentication.** `Authorization: Bearer <access_token>`. Access tokens live 15 minutes; refresh
tokens 30 days and rotate on every use.

**Money.** Amounts are **decimal strings** with at most two fraction digits: `"25"`, `"25.5"`,
`"25.50"`. Signs, exponents, thousands separators and whitespace are rejected with `422`. Responses
carry both a formatted string (`amount: "25.50"`) and integer minor units (`amount_minor: 2550`).

**Idempotency.** `POST` endpoints that move money require `Idempotency-Key` (8–128 characters of
`A-Z a-z 0-9 _ . : -`), unique per user per endpoint. Replaying a key with the same body returns the
stored response (`200`, `Idempotent-Replay: true`). Replaying it with a different body is
`409 IDEMPOTENCY_KEY_REUSED`.

**Errors.** Every failure:

```json
{
  "error": {
    "code": "INSUFFICIENT_FUNDS",
    "message": "Insufficient funds for this movement.",
    "details": {"available": "50.00", "requested": "50.01"},
    "request_id": "req_1f0c…"
  }
}
```

| Code | HTTP | Meaning |
|---|---|---|
| `VALIDATION_ERROR` | 422 | body/fields invalid (never echoes values) |
| `UNAUTHENTICATED` | 401 | missing/expired/revoked token |
| `INVALID_CREDENTIALS` | 401 | bad email or password (indistinguishable by design) |
| `FORBIDDEN` | 403 | wrong role |
| `NOT_FOUND` | 404 | unknown **or not yours** — deliberately identical |
| `INSUFFICIENT_FUNDS` | 409 | `details.available`, `details.requested` |
| `ACCOUNT_NOT_ACTIVE` | 409 | account frozen |
| `DESTINATION_NOT_ACTIVE` | 409 | destination frozen |
| `IDEMPOTENCY_KEY_REQUIRED` | 400 | header missing |
| `IDEMPOTENCY_KEY_REUSED` | 409 | same key, different body |
| `EMAIL_ALREADY_REGISTERED` | 409 | |
| `RATE_LIMITED` | 429 | throttle or lockout |
| `PAYLOAD_TOO_LARGE` | 413 | body > 64 KiB |
| `SERVICE_BUSY` | 503 | database write lock timeout; retry (same key) |

---

## Meta

### `GET /api/health`
```json
{"status": "ok", "service": "ledgerline-api", "version": "1.0.0", "database": "ok",
 "time": "2026-11-07T13:43:51.717436Z"}
```

### `GET /api/meta`
```json
{"name": "LedgerLine", "version": "1.0.0", "environment": "dev",
 "supported_currencies": ["USD","EUR","GBP","INR"], "max_amount": "100000.00",
 "idempotency_header": "Idempotency-Key", "roles": ["ADMIN","CUSTOMER","AUDITOR"]}
```

## Authentication

### `POST /api/auth/register`
```json
{"email": "new@example.com", "password": "at-least-12-chars", "full_name": "New User"}
```
`201` → `{"user": {…, "role": "CUSTOMER"}}`. Always creates a CUSTOMER; extra fields (e.g. `role`) are
rejected with `422`.

### `POST /api/auth/login`
```json
{"email": "alice@ledgerline.test", "password": "LedgerLine!Demo2024"}
```
`200`:
```json
{"access_token": "eyJ…", "refresh_token": "9tF…", "token_type": "bearer",
 "expires_in": 900, "refresh_expires_in": 2592000,
 "user": {"id": "…", "email": "…", "full_name": "…", "role": "CUSTOMER", "status": "ACTIVE"}}
```
`401 INVALID_CREDENTIALS` for wrong password **and** unknown email. `429` after 5 consecutive
failures for that address, or beyond the per-IP ceiling.

### `POST /api/auth/refresh`
```json
{"refresh_token": "9tF…"}
```
`200` with a new pair. The old token is consumed; replaying it revokes the entire family (session) and
returns `401`.

### `POST /api/auth/logout`
```json
{"refresh_token": "9tF…"}
```
`204`. Revokes the session: the access token stops working immediately.

### `GET /api/auth/me`
`200` → `{"user": {…}}`

### `POST /api/auth/change-password`
```json
{"current_password": "…", "new_password": "…"}
```
`204`. Revokes all sessions and refresh tokens for the user.

## Accounts

### `GET /api/accounts`
`200` → `{"accounts": [Account, …]}`. Customers see their own; admin/auditor see every account
including system accounts.

```json
{"id": "2a4b…", "account_number": "400955529947", "name": "Alice — Everyday",
 "currency": "USD", "balance": "1250.00", "balance_minor": 125000,
 "status": "ACTIVE", "is_system": false, "version": 1,
 "created_at": "2026-11-07T13:42:03.118Z", "owner_email": "alice@ledgerline.test"}
```

### `POST /api/accounts` — ADMIN
```json
{"owner_email": "alice@ledgerline.test", "currency": "USD", "name": "Everyday"}
```
`201` → `{"account": {…}}`. Owner must be an existing CUSTOMER.

### `GET /api/accounts/{account_id}`
`200` → `{"account": {…}}`. Owner, or any staff. Foreign account → `404`.

### `GET /api/accounts/{account_id}/statement?limit=50&before_id=123`
`200`:
```json
{"account": {…},
 "entries": [{"entry_id": 12, "transaction_id": "…", "type": "TRANSFER",
              "direction": "CREDIT", "amount": "100.50", "amount_minor": 10050,
              "currency": "USD", "balance_after": "1399.50",
              "description": "rent", "created_at": "2026-11-07T13:44:02.001Z"}],
 "next_cursor": 12, "limit": 50}
```
Newest first; `balance_after` is the running balance over the whole account (not just this page).

### `POST /api/accounts/{id}/freeze` — ADMIN
```json
{"frozen": true}
```
`200` → `{"account": {…}}`. System accounts cannot be frozen (`422`).

### `POST /api/accounts/{id}/deposit` — owner only, `Idempotency-Key` required
```json
{"amount": "250.00", "description": "salary"}
```
`201` (`200` on replay):
```json
{"transaction": {"id": "…", "type": "DEPOSIT", "currency": "USD", "amount": "250.00",
                 "amount_minor": 25000, "description": "salary", "created_at": "…",
                 "entries": [
                   {"id": 1, "account_id": "…", "direction": "CREDIT", "amount": "250.00",
                    "amount_minor": 25000, "signed_amount_minor": 25000, "currency": "USD"},
                   {"id": 2, "account_id": "sys-cash-usd", "direction": "DEBIT",
                    "amount": "250.00", "amount_minor": 25000,
                    "signed_amount_minor": -25000, "currency": "USD"}]},
 "account": {…post-movement snapshot…},
 "replayed": false}
```
Headers: `Location: /api/transactions/{id}` and `Idempotent-Replay: true|false`.

### `POST /api/accounts/{id}/withdraw` — owner only, `Idempotency-Key` required
Same shape as deposit. `409 INSUFFICIENT_FUNDS` with `available`/`requested` when the balance is
short; nothing is written.

### `POST /api/transfers` — owner of the source, `Idempotency-Key` required
```json
{"from_account_id": "…", "to_account_number": "406185141308",
 "amount": "100.50", "description": "rent"}
```
`201`:
```json
{"transaction": {… two legs: source DEBIT, destination CREDIT …},
 "account": {…source snapshot…},
 "from_account": {…},
 "to_account": {"id": "…", "account_number": "406185141308",
                "name": "Bob — Everyday", "currency": "USD"},
 "replayed": false}
```
Rejected: self-transfer (`422`), different currencies (`422`), system account destination (`422`)
source not yours (`404`), destination missing (`404`), destination frozen (`409`), insufficient funds
(`409`).

### `GET /api/transactions/{id}`
`200` → `{"transaction": {… entries include prev_hash/entry_hash …}}`. Visible to the initiator, both
parties, and staff; `404` for everyone else.

## Audit and integrity — ADMIN, AUDITOR

### `GET /api/audit?limit=100&before_id=&action=&outcome=`
```json
{"entries": [{"id": 42, "actor_user_id": "…", "actor_role": "CUSTOMER",
              "subject": null, "action": "DEPOSIT", "outcome": "ALLOW",
              "target_type": "account", "target_id": "…", "ip": "127.0.0.1",
              "user_agent": "Mozilla/5.0", "request_id": "req_…",
              "detail": "{\"amount\":\"10.00\",\"transaction_id\":\"…\"}",
              "created_at": "…", "entry_hash": "…"}],
 "next_cursor": 42}
```

### `GET /api/auditor/integrity`
```json
{"ok": true,
 "generated_at": "…",
 "checks": [{"name": "balances_match_entries",
             "description": "Each stored balance equals the sum of that account's ledger entries.",
             "ok": true, "detail": {"mismatches": []}}, …],
 "totals": {"accounts": 7, "customer_funds": "2300.00", "ledger_entries": 10,
            "transactions": 5, "audit_rows": 14,
            "ledger_head_hash": "d3b1…", "audit_head_hash": "9859…"}}
```
Checks: `balances_match_entries`, `no_negative_customer_balances`, `transactions_balanced`,
`transactions_have_entries`, `entries_match_account_currency`, `no_orphan_entries`,
`money_is_conserved`, `ledger_hash_chain`, `audit_hash_chain`.

## Administration — ADMIN

| Method | Path | Body | Result |
|---|---|---|---|
| `GET` | `/api/admin/users` | — | `{"users": [{id, email, full_name, role, status, failed_login_count, locked_until, created_at}]}` |
| `POST` | `/api/admin/users` | `{"email","password","full_name","role":"ADMIN"\|"AUDITOR"}` | `201`; `CUSTOMER` is rejected |
| `POST` | `/api/admin/users/{id}/unlock` | — | `200`; clears the failure counter and lockout, audited as `USER_UNLOCKED` |

## Worked example: deposit with a retry

```bash
BASE=http://localhost:8000
TOKEN=$(curl -s -X POST $BASE/api/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"alice@ledgerline.test","password":"LedgerLine!Demo2024"}' \
  | python -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')
ACCOUNT=$(curl -s $BASE/api/accounts -H "Authorization: Bearer $TOKEN" \
  | python -c 'import json,sys;print(json.load(sys.stdin)["accounts"][0]["id"])')
KEY=$(python -c 'import uuid;print(uuid.uuid4())')

# first attempt -> 201
curl -i -s -X POST $BASE/api/accounts/$ACCOUNT/deposit \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: $KEY" \
  -H 'Content-Type: application/json' -d '{"amount":"250.00"}' | head -20

# identical retry -> 200 + Idempotent-Replay: true, same transaction id, no second movement
curl -i -s -X POST $BASE/api/accounts/$ACCOUNT/deposit \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: $KEY" \
  -H 'Content-Type: application/json' -d '{"amount":"250.00"}' | head -20

# same key, different amount -> 409 IDEMPOTENCY_KEY_REUSED, balance unchanged
curl -s -X POST $BASE/api/accounts/$ACCOUNT/deposit \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: $KEY" \
  -H 'Content-Type: application/json' -d '{"amount":"999.00"}'
```
