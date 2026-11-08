# Five-minute demo script

A live walkthrough that shows the four claims in the tagline — balances, at-most-once, concurrency,
provability — rather than describing them. Times are cumulative.

**Setup:** `docker compose up --build` (or the two-terminal local setup), then open
`http://localhost:8000`. Demo password for every seeded user: `LedgerLine!Demo2024`.


> Recording it? **[SCRIPT.md](SCRIPT.md)** has the word-for-word narration, second by second,
> plus a 12-slide deck ([LedgerLine-deck.pptx](LedgerLine-deck.pptx)) whose speaker notes carry
> the same timings.
---

## 0:00 — Sign in as an account holder

Email `alice@ledgerline.test`, password `LedgerLine!Demo2024` (the "account holder" chip on the login
screen fills it in).

Point out:
- two accounts, balances in USD, account numbers on the cards;
- the role badge in the header — this user can move money, and only sees her own accounts.

## 0:30 — A transfer that balances

On "Alice — Everyday" → **Transfer**. Destination: copy the account number from Bob's card — sign in
as `bob@ledgerline.test` in a second browser profile to read it, or use the seeded number shown in
the accounts table as admin. Amount: `100.50`. Description: `rent`.

The receipt shows:
- a transaction id,
- the new balance,
- **both ledger legs** (`+100.50` / `−100.50`),
- the idempotency key that was used.

Click **Statement**: every row is a ledger entry with a running balance, newest first.

## 1:15 — The same request cannot pay twice

Repeat the transfer with the *same* form (the key is displayed above the button). Instead of a second
movement, the dialog returns the original receipt with an **idempotent replay** badge and the balance
is unchanged.

The API version, if you prefer a terminal:

```bash
BASE=http://localhost:8000
T=$(curl -s -X POST $BASE/api/auth/login -H 'Content-Type: application/json' \
     -d '{"email":"alice@ledgerline.test","password":"LedgerLine!Demo2024"}' | jq -r .access_token)
A=$(curl -s $BASE/api/accounts -H "Authorization: Bearer $T" | jq -r '.accounts[0].id')
K=$(uuidgen)

for attempt in 1 2; do
  curl -s -o /dev/null -D - -X POST $BASE/api/accounts/$A/deposit \
    -H "Authorization: Bearer $T" -H "Idempotency-Key: $K" \
    -H 'Content-Type: application/json' -d '{"amount":"250.00"}' \
  | grep -Ei 'HTTP/|idempotent-replay'
done
# HTTP/1.1 201 Created        Idempotent-Replay: false
# HTTP/1.1 200 OK             Idempotent-Replay: true
```

Then show the key being *abused*:

```bash
curl -s -X POST $BASE/api/accounts/$A/deposit -H "Authorization: Bearer $T" \
  -H "Idempotency-Key: $K" -H 'Content-Type: application/json' -d '{"amount":"9999.00"}' | jq
# {"error":{"code":"IDEMPOTENCY_KEY_REUSED", …}}   and the balance did not move
```

## 2:00 — Concurrency: 20 withdrawals, 10 payments' worth of money

This is the one that usually breaks in student projects. Start with a 100.00 balance and fire 20
withdrawals of 10.00 at once:

```bash
A2=$(curl -s $BASE/api/accounts -H "Authorization: Bearer $T" | jq -r '.accounts[1].id')
for i in $(seq 20); do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/api/accounts/$A2/withdraw \
    -H "Authorization: Bearer $T" -H "Idempotency-Key: $(uuidgen)" \
    -H 'Content-Type: application/json' -d '{"amount":"10.00"}' &
done | sort | uniq -c
wait
# 10 × 201 Created
# 10 × 409 Conflict        (INSUFFICIENT_FUNDS)
```

The balance floors at exactly `0.00` and never goes negative. The formal version of this is
`tests/test_concurrency.py`, which asserts the same thing on a real server.

## 2:45 — Separation of duties

Sign in as `admin@ledgerline.test`:
- open an account for a new customer (Administration tab),
- freeze and unfreeze one,
- create an auditor,
- try to deposit into an account → `403`: administrators cannot move money.

Sign in as `auditor@ledgerline.test`:
- every account, read-only;
- the audit trail, including **denied** attempts (`AUTHZ_DENIED`) with request ids;
- no money buttons.

## 3:30 — Prove the books

As the auditor, open **Audit & integrity**. Every check is listed with what it proves:
balances recomputed from entries, every transaction summing to zero, no negative customer balances,
money conserved across all accounts, and both SHA-256 chains walking end to end. The head hashes are
shown so they can be recorded externally.

Now attack the database directly and re-run it:

```bash
DB=/var/lib/ledgerline/ledgerline.db        # or ./data/ledgerline.db
sqlite3 $DB "DROP TRIGGER trg_ledger_no_update;
             UPDATE ledger_entries SET amount_minor = 1 WHERE id = 1;"
```

Click **Re-run verification** → red, `ledger_hash_chain` fails with `first_bad_id: 1`, and
`balances_match_entries` flags the account. That is the difference between *storing* a ledger and
being able to *prove* one.

(Restore the database or re-seed afterwards.)

## 4:15 — The attack suite

```bash
cd backend && python scripts/redteam.py --base-url http://localhost:8000
```

27 checks: forged and `alg=none` tokens, role injection, IDOR on accounts and statements, auditor and
admin attempting to move money, overdraw, negative/whitespace/three-decimal amounts, missing and
reused idempotency keys, transfer to the clearing account, 20 racing withdrawals, 10 duplicate
requests, SQL metacharacters, oversized bodies — then a final integrity verification.

## 4:45 — Close

> Every movement writes two ledger legs that sum to zero and commits them together with its
> idempotency record. Balances are protected by a conditional `UPDATE` inside `BEGIN IMMEDIATE`, so
> concurrency cannot overdraw them. History is append-only and hash-chained, and the auditor endpoint
> recomputes it from scratch. The limits are written down in README → *Known limitations* and
> THREATS.md → *Accepted risks*.

---

## Backup talking points if something goes wrong live

| Symptom | What to say / do |
|---|---|
| `503 SERVICE_BUSY` | Writer contention under load; retry with the same idempotency key. Raise `LEDGERLINE_SQLITE_BUSY_TIMEOUT_MS` or move to Postgres. |
| Locked out from too many logins | Sign in as admin → Administration → the account shows `locked`; `POST /api/admin/users/{id}/unlock`, or wait 15 minutes. |
| "Why not Postgres?" | Answer with `docs/ARCHITECTURE.md#porting-to-postgres`: the conditional UPDATE and idempotency-in-one-transaction pattern is unchanged; only the locking primitive differs. |
| "What breaks first at scale?" | Single-writer throughput, then the in-memory rate limiter, then the full-ledger integrity scan. All three are in README → *Known limitations* with the fix. |
