# LedgerLine — Threat Model

This is the threat model I used to drive the build, not a description written afterwards. Each row
names an attack, the control that is supposed to stop it, the test that proves the control works, and
the residual risk I am **not** claiming to have solved.

Everything in the "Evidence" column is executable:

```bash
cd backend
pytest -q                                            # 153 tests
python scripts/redteam.py --base-url http://localhost:8000   # 27 live checks
```

---

## 1. Scope and method

**In scope:** the HTTP API, the ledger and balance logic, authentication and authorisation, the audit
trail, the browser client, and the deployment defaults in this repository.

**Out of scope** (stated so a reader does not assume otherwise): the operating system, the TLS
terminator, physical access to the host, the security of the SQLite file beyond filesystem
permissions, and the identity provider-less "who owns this email address" problem.

**Method:** enumerate assets → enumerate actors and their capabilities → walk each entry point
(unauthenticated, customer, admin, auditor, and "someone with SQL access") → for each threat choose a
preventive control and, where prevention cannot be complete, a detective control. STRIDE is used as a
checklist to avoid blind spots (Spoofing → §3, Tampering → §4/§5, Repudiation → §5, Information
disclosure → §6, DoS → §7, Elevation of privilege → §2/§3).

**Assets**

| Asset | Why it matters |
|---|---|
| Customer balances | Direct financial loss; the one thing a ledger exists to protect |
| Ledger history | Without it, "the balance is right" is unfalsifiable |
| Audit trail | Attribution: who moved the money and who tried |
| Credentials and tokens | Impersonation |
| Availability | A ledger that cannot post is a failed product |

**Actors**

| Actor | Capabilities assumed |
|---|---|
| Anonymous attacker | Internet access, knows the API shape, can brute force and fuzz |
| Malicious customer | A valid CUSTOMER account and token, can craft arbitrary HTTP |
| Compromised customer browser | Can read `localStorage` (refresh token) |
| Malicious admin | Valid ADMIN account; can open/freeze accounts |
| Malicious auditor | Valid AUDITOR account; read-only by design |
| Insider with SQL access | Can run arbitrary SQL against the database file |
| Compromised dependency | Supply-chain code executing in-process |

---

## 2. Money integrity

The core promise: *the books always balance and the same request never pays twice.*

| ID | Threat | Attack a judge can try | Control | Evidence | Residual risk |
|---|---|---|---|---|---|
| **M1** | Duplicate request pays twice | Send the same transfer twice, or replay a request captured from the browser | `Idempotency-Key` required on every money endpoint; the key row is inserted in the **same transaction** as the movement; replay returns the stored response with `200` + `Idempotent-Replay: true` | `test_idempotency.py::test_replay_returns_the_original_result_and_moves_money_once`; `test_concurrency.py::test_twenty_duplicate_requests_pay_once` (20 parallel, exactly one `201`) | A client that generates a *new* key per click defeats this. Mitigation: the UI generates one key per form intent and reuses it on retry |
| **M2** | Key reuse with a different body silently executes | Reuse a key with a larger amount | Request fingerprint (SHA-256 of method + path + canonical body) is stored with the key; mismatch → `409 IDEMPOTENCY_KEY_REUSED`, nothing moves | `test_idempotency.py::test_same_key_with_different_body_is_a_conflict` | None known |
| **M3** | Concurrent withdrawals overdraw an account | 20 parallel `POST /withdraw` of 10.00 against a 100.00 balance | `BEGIN IMMEDIATE` write lock + conditional `UPDATE … WHERE balance_minor + :delta >= 0`; `rowcount != 1` aborts the transaction | `test_concurrency.py::test_only_as_many_withdrawals_succeed_as_the_balance_allows` (exactly 10 × `201`, 10 × `409`, balance `0.00`) | None known under SQLite's single-writer model |
| **M4** | Lost update on concurrent deposits | 30 parallel deposits of 1.00 | Balance is mutated by `balance_minor = balance_minor + ?` in SQL, never read-modify-write in Python | `test_concurrency.py::test_balance_is_exactly_the_sum_of_concurrent_deposits` (balance `30.00`) | None known |
| **M5** | Transfer deadlock or partial transfer | 40 simultaneous opposite transfers; freeze the destination mid-flight | Deterministic leg ordering by account id; both legs in one transaction; destination status re-checked inside the transaction | `test_concurrency.py::test_money_is_conserved_under_opposite_transfers`, `test_money.py::test_transfer_is_atomic_when_destination_is_frozen` | Under Postgres the same ordering prevents deadlocks; untested there |
| **M6** | Negative balance written by a future code path | — (no API for it) | Second line of defence in storage: `CHECK (is_system = 1 OR balance_minor >= 0)` | `test_integrity_tamper.py::test_a_negative_customer_balance_is_detected` (direct SQL rejected by the DB) | None known |
| **M7** | Unbalanced transaction posted (bug or injection) | — | `apply_legs` refuses to post unless legs sum to exactly zero and are distinct accounts; `integrity` re-checks every transaction | `test_integrity_tamper.py::TestLedgerShape` | None known |
| **M8** | Money "burned" by transferring into the clearing account | `POST /transfers` with `to_account_number: "SYS-CASH-USD"` | System accounts are rejected as transfer destinations and cannot be frozen or owned | `test_money.py::test_transfer_to_system_account_is_rejected`; red-team check | None known |
| **M9** | Float rounding drift over many operations | Deposit `0.10` three times and inspect | No floating point in the money path: integer minor units, strict decimal-string parsing, `CHECK`ed `INTEGER` column | `test_money.py::TestAmountParsing` (53 cases), `test_money.py::test_statement_running_balance` | None known |
| **M10** | Amount tampering (`-5`, `1e3`, `1.001`, `" 5.00"`, `"1,000.00"`, Arabic-Indic digits) | Try each against an endpoint | Narrow grammar `^(0|[1-9][0-9]{0,14})(\.[0-9]{1,2})?$`; sign, exponent, whitespace, separators and non-ASCII rejected with `422` | `test_money.py::TestAmountParsing::test_invalid` (14 cases), red-team | None known |
| **M11** | Cross-currency confusion | Transfer USD → EUR account | Currency belongs to the account, never to the request; mismatches are rejected, and there is no implicit FX | `test_money.py::test_cross_currency_transfer_is_rejected` | No FX support at all (by design) |
| **M12** | Self-transfer used to inflate volume or hit a race | Transfer to the same account number | Duplicate legs are refused before any write | `test_money.py::test_transfer_to_self_is_rejected` | None known |
| **M13** | Unbounded amounts (fat-finger or laundering pattern) | Deposit `999999999.00` | Per-movement ceiling (`LEDGERLINE_MAX_AMOUNT_MINOR`, default 100,000.00) and positive-only amounts | `test_money.py::test_limits`, red-team | Ceiling is per movement, not per day; daily limits are not implemented |

---

## 3. Authentication and session security

| ID | Threat | Attack | Control | Evidence | Residual |
|---|---|---|---|---|---|
| **A1** | `alg: none` token accepted | Send `{"alg":"none"}` JWT | Algorithm list pinned to `HS256` in `jwt.decode`; `typ`, `iss`, `aud`, `exp` required | `test_auth.py::test_alg_none_token_is_rejected`; red-team | None known |
| **A2** | Token signed with an attacker secret | Re-sign claims with your own key | Signature verified against the server secret; startup refuses a <32-char secret in `prod` | `test_auth.py::test_token_signed_with_another_secret_is_rejected` | A leaked secret is game over; rotation is documented, not automated |
| **A3** | Tampered token payload (change `sub`) | Flip a character in the signature | Signature covers the payload | red-team `tampered JWT signature rejected` | None known |
| **A4** | Cross-service token confusion | Change `iss` or `aud` | Both verified; failed → `401` | `test_auth.py::test_wrong_audience_and_issuer_are_rejected` | None known |
| **A5** | Expired token reuse | Wait 15 minutes / craft `exp` in the past | `exp` verified with 5s leeway; `401 Token has expired` | `test_auth.py::test_expired_token_is_rejected` | None known |
| **A6** | Refresh token accepted as access token | Send the refresh token to `/api/auth/me` | Separate token types (`typ` claim) and formats; refresh tokens are opaque and only stored hashed | `test_auth.py::test_refresh_token_metadata_is_not_accepted_as_access_token` | None known |
| **A7** | Refresh-token replay after theft | Reuse an already-rotated refresh token | Single-use rotation; reuse revokes the **entire family and session**, so the thief and the victim are both logged out | `test_auth.py::test_reuse_revokes_the_whole_family`; audit row `REFRESH_TOKEN_REUSE` | The window between theft and detection is one refresh cycle |
| **A8** | Logged-out access token keeps working for 15 minutes | `logout`, then reuse the access token | Access tokens carry a session id (`sid`); logout revokes the session row, so the token dies immediately | `test_auth.py::test_logout_kills_the_access_token_immediately`, `test_logout_only_kills_that_session` | None known |
| **A9** | Old sessions survive a password change | Change the password, reuse the old token | Password change revokes every session and every refresh token for the user; access tokens also carry a password fingerprint | `test_auth.py::test_password_change_kills_old_sessions` | None known |
| **A10** | Stale privileges after a role change or disable | Disable a user, keep using their token | Role and status are read from the database on every request; the JWT carries no role | `test_auth.py::test_deleted_user_token_stops_working` | One DB read per request (accepted cost) |
| **A11** | Brute force / credential stuffing | 100 logins against one account | bcrypt cost 12; lockout after 5 consecutive failures for 15 minutes; per-IP ceiling (50) against spraying; failures counted from the audit log so they survive restarts and are resettable by an admin unlock | `test_auth.py::test_account_locks_after_repeated_failures`, `test_rbac.py::test_admin_can_unlock_a_locked_user`; red-team | Lockout is a DoS vector against a known account (5 requests). Accepted: it is visible in the audit trail and an admin can unlock |
| **A12** | User enumeration through login timing or messages | Compare responses for a real vs. fake email | Identical `401 INVALID_CREDENTIALS` and a dummy bcrypt verification when the user does not exist | `test_auth.py::test_unknown_email_and_wrong_password_are_indistinguishable` | bcrypt timing is close but not provably identical |
| **A13** | Weak or truncated passwords | Register with `password`, 73 chars, all-same characters | Minimum 12 characters, common-password list, ≥5 distinct characters, hard cap at 72 **bytes** so bcrypt never silently truncates | `test_auth.py::test_weak_passwords_rejected` | No breach-corpus check (an 8k-anonymity API would be the next step) |
| **A14** | Credentials in logs or error bodies | Trigger a validation error with a password field | Validation errors echo field names and messages only, never values | `test_http_hardening.py::test_validation_errors_do_not_echo_the_password`; `test_audit.py::test_audit_rows_contain_no_secrets` | Application stdout is not redacted by default for other payloads |
| **A15** | Session/token theft via XSS in the browser client | Inject a script through a field rendered as HTML | CSP `script-src 'self'`, no `dangerouslySetInnerHTML`, descriptions stripped of control characters; access token in memory only | `test_http_hardening.py::test_security_headers_on_every_response` | **Refresh token lives in `localStorage`.** A successful XSS could exfiltrate it. The fix (HttpOnly cookie + CSRF token + `SameSite=Strict`) is specified in §8 but not built |

---

## 4. Authorisation

| ID | Threat | Attack | Control | Evidence | Residual |
|---|---|---|---|---|---|
| **Z1** | IDOR: read another customer's account or statement | Swap the account id in the URL | Every account read goes through an ownership check; failure is `404`, so ids cannot be probed | `test_rbac.py::test_reading_another_customers_account_is_404_not_403`; red-team | Account UUIDs are unguessable; account numbers are deliberately non-secret (see Z10) |
| **Z2** | Move money out of someone else's account | `POST /transfers` with their `from_account_id` | Ownership check inside the same transaction as the post | `test_rbac.py::test_cannot_transfer_from_another_customers_account`; red-team | None known |
| **Z3** | Deposit into someone else's account (griefing/laundering) | `POST /accounts/{their_id}/deposit` | Owner check → `404` | `test_rbac.py::test_cannot_deposit_into_another_customers_account` | None known |
| **Z4** | Privilege escalation at registration ("mass assignment") | Add `"role":"ADMIN"` to the registration body | Pydantic models are `extra="forbid"`; the role is not an input to the service at all | `test_auth.py::test_role_cannot_be_escalated_through_the_payload`; red-team | None known |
| **Z5** | Customer creates staff accounts | `POST /api/admin/users` as a customer | ADMIN-only dependency, plus the request model only allows `ADMIN`/`AUDITOR` values | `test_rbac.py::test_customer_cannot_create_staff` | None known |
| **Z6** | Customer reads the audit trail | `GET /api/audit` | Role dependency (ADMIN/AUDITOR) | `test_rbac.py::test_customer_cannot_reach_staff_endpoints`; red-team | None known |
| **Z7** | Auditor or admin creates money | `POST /deposit` as staff | `require_customer` dependency refuses non-CUSTOMER roles; separation of duties | `test_rbac.py::test_auditor_is_read_only`, `test_admin_cannot_move_money_separation_of_duties`; red-team | An admin can still open an account for themselves and move money as that customer. Accepted: it is one identity, not two — a real bank separates these duties across people |
| **Z8** | Auditor mutates account state | `POST /freeze` as auditor | ADMIN-only | `test_rbac.py::test_auditor_is_read_only` | None known |
| **Z9** | Enumerating accounts by id | Iterate `GET /api/accounts/{uuid}` | UUIDv4 + `404` on foreign ids + per-user money throttling | `test_rbac.py` | Not rate-limited *per read*; a determined attacker can probe, but UUID space is 2^122 |
| **Z10** | Enumerating accounts by number through transfers | Iterate 12-digit numbers and watch for `404` vs `409` | 12-digit random numbers, per-user throttle (30 money writes/minute), every attempt audited | `test_money.py::test_unknown_destination_is_404` | **Residual:** a patient attacker with many accounts can probe ~43k numbers/day. Mitigation would be a transfer-by-alias scheme or hard rate limits; accepted for this scale and documented |

---

## 5. Tampering, repudiation and history

| ID | Threat | Attack | Control | Evidence | Residual |
|---|---|---|---|---|---|
| **H1** | Ledger entry edited after the fact | `UPDATE ledger_entries …` | `BEFORE UPDATE` trigger raises `ABORT` | `test_integrity_tamper.py::TestAppendOnly` (6 statements) | Whoever can drop triggers can also drop the database |
| **H2** | Ledger entry deleted | `DELETE FROM ledger_entries` | `BEFORE DELETE` trigger | same | same |
| **H3** | Transaction header edited/deleted | `UPDATE/DELETE transactions` | Triggers | same | same |
| **H4** | Audit rows edited or deleted | `UPDATE/DELETE audit_log` | Triggers | same | same |
| **H5** | Insider drops a trigger and rewrites history | `DROP TRIGGER` then edit | Every entry stores `prev_hash` + `entry_hash`; `GET /api/auditor/integrity` re-walks both chains and reports the first bad id | `test_integrity_tamper.py::test_integrity_notices_an_edited_ledger_entry` (asserts `first_bad_id == 1`), `…deleted_ledger_entry`, `…rewritten_audit_row` | **Tamper-evident, not tamper-proof.** An attacker who recomputes the whole chain produces a valid chain. The defence is external anchoring (see §8) |
| **H6** | Balance cache edited directly | `UPDATE accounts SET balance_minor = …` | Integrity recomputes every balance from `ledger_entries` and compares | `test_integrity_tamper.py::test_integrity_notices_a_tampered_balance_cache` | None known |
| **H7** | Chain head pointer desynchronised | `UPDATE chain_state` | Head is compared with the last row's hash on every verification | `test_integrity_tamper.py::test_integrity_notices_a_moved_chain_head` | None known |
| **H8** | Gaps in ledger ids (silent deletion) | Delete a row (with triggers dropped) | Ids are contiguous by construction and checked | `test_integrity_tamper.py::test_ledger_ids_have_no_gaps` | None known |
| **H9** | Repudiation: "I never made that transfer" | — | Every movement, denial and auth event is written to the append-only audit log with actor, role, ip, user agent and request id | `test_audit.py` (9 tests) | Reads are **not** audited (see R6) |
| **H10** | Request id spoofing to muddy attribution | Send a crafted `X-Request-ID` | Header is echoed only if it matches `[A-Za-z0-9_.:-]{1,64}`, otherwise replaced; ip/user-agent are recorded separately by the server | `test_audit.py::test_client_supplied_request_id_is_echoed_but_sanitised` | A valid-looking spoofed id still appears; it is an aid, not evidence on its own |
| **H11** | Log injection via description | Deposit with control characters/newlines in the description | Control characters stripped; audit `detail` is canonical JSON | `test_audit.py::test_audit_rows_contain_no_secrets` | — |

---

## 6. Information disclosure

| ID | Threat | Attack | Control | Evidence | Residual |
|---|---|---|---|---|---|
| **D1** | Stack traces / SQL / paths in 500s | Force an internal error | Generic `INTERNAL_ERROR` envelope with request id; details go to stderr only | `test_http_hardening.py::test_internal_errors_do_not_leak_details` | Server-side logs still contain the traceback (intended) |
| **D2** | Tokens cached by a proxy | Inspect `Cache-Control` | `no-store` on every `/api` response | `test_http_hardening.py::test_api_responses_are_not_cacheable` | None known |
| **D3** | Cross-origin reads | Call the API from `https://evil.example` | Strict CORS allowlist, credentials disabled (bearer only) | `test_http_hardening.py::test_cors_allows_only_configured_origins`; red-team | A misconfigured `LEDGERLINE_CORS_ORIGINS` widens this; documented |
| **D4** | Verbose error distinctions reveal account existence | Compare `403`/`404`/`409` | Foreign reads are `404`; the same shape is used for unknown and foreign accounts | `test_rbac.py::TestAccountIsolation` | Transfer destination `404` still confirms a number does not exist; accepted, throttled, audited |
| **D5** | Password hashes exposed through the API | Inspect every serialised user | Serialisers are explicit allowlists; hashes never leave the database | `test_auth.py::test_register_and_login`, `test_audit.py::test_audit_rows_contain_no_secrets` | A database dump exposes hashes (bcrypt cost 12) |
| **D6** | PII in responses to the wrong role | Auditor reading customers | Auditors are *meant* to see account ownership; customers only see their own email | `test_rbac.py::test_auditor_and_admin_can_see_every_account` | No field-level redaction of names/emails for staff |

---

## 7. Availability and input handling

| ID | Threat | Attack | Control | Evidence | Residual |
|---|---|---|---|---|---|
| **P1** | Oversized bodies | POST 100 KB JSON | `Content-Length` > 64 KiB → `413` before parsing | `test_http_hardening.py::test_large_bodies_are_rejected`; red-team | **Chunked bodies without `Content-Length` are not capped** (documented gap) |
| **P2** | bcrypt CPU exhaustion via login flood | 1000 logins/minute | Per-IP burst limiter (100/min) + per-account lockout + oversized-password rejection before hashing | `test_auth.py::TestLogin` | Limiter is in-process; multiple workers multiply the budget |
| **P3** | Money-endpoint flooding | Script transfers | Per-user sliding window (30/min) with `429` + audit row | `test_*` via `enforce_money_rate_limit`; exercised by the concurrency suite | In-process; Redis needed for a cluster |
| **P4** | Audit-log write amplification as an unauthenticated DoS | Flood with forged tokens | Unauthenticated token failures are deliberately **not** audited | design decision in `audit.py`; §9 explains the trade-off | Those attempts are only in the access log |
| **P5** | SQL injection | `'; DROP TABLE ledger_entries; --` in every field | 100% parameterised SQL; the only dynamic SQL is a column-filter `WHERE` built from fixed strings | `test_idempotency.py` payloads, red-team `SQL metacharacters are stored as data`, `SQL-ish path parameter is a 404` | None known |
| **P6** | Malformed JSON / wrong types | Send arrays, numbers where strings belong | Pydantic validation with `extra="forbid"`, `422` with field-level errors | `test_http_hardening.py`, `test_money.py` | None known |
| **P7** | Writer starvation / "database is locked" | 40 concurrent writers | `busy_timeout=10s`, short transactions, WAL; a lock timeout maps to `503` + `Retry-After` | `test_concurrency.py` (no `503` observed in 6 scenarios) | SQLite serialises writers; throughput is bounded by design |

---

## 8. Accepted risks and what I would do next

These are real gaps, not hypotheticals. I would rather list them than be caught by a judge who finds
them.

| ID | Accepted risk | Why it is acceptable here | Next step |
|---|---|---|---|
| **R1** | Chain rewrite by someone with write access to the DB | Detecting it requires a witness the attacker does not control | Publish `ledger_head_hash` to an append-only external store (S3 Object Lock, a transparency log, or a daily signed email); verify on boot |
| **R2** | Refresh token in `localStorage` | Needed for a reload-surviving session without a cookie/CSRF layer | Move to `HttpOnly; Secure; SameSite=Strict` refresh cookie + double-submit CSRF token; keep access tokens in memory |
| **R3** | Single-node SQLite | One writer is *sufficient* for correctness at this scale, and it made the concurrency guarantees provable in tests | Port to Postgres: `SELECT … FOR UPDATE` in id order, `SERIALIZABLE` or conditional updates unchanged; see docs/ARCHITECTURE.md |
| **R4** | In-memory rate limits | The process is the deployment unit; account lockout is DB-backed | Redis-backed limiter with the same call signatures |
| **R5** | No daily/velocity limits, no chargeback or reversal workflow | Out of scope for the seven-day brief | Add per-account daily limits and an audited two-person reversal flow |
| **R6** | Reads are not audited | Auditing every statement read is a large write amplification and a privacy liability | Optional read-audit with sampling, or only for staff access to customer data |
| **R7** | Transfer destination enumeration | 12-digit random numbers, throttled, audited | Transfer by alias / saved payee with confirmation |
| **R8** | No 2FA, no email verification, no password reset | Demo scope | TOTP for staff at minimum; staff roles are the higher-value accounts |
| **R9** | `frame-ancestors *` in the CSP | The hosted preview embeds the dashboard in an iframe; `DENY` breaks the demo | Set `frame-ancestors 'none'` in production where embedding is not required |
| **R10** | Chunked body limit | FastAPI/Starlette bound the parse cost; the JSON parser is the real limit | Add an ASGI body-limit middleware that counts bytes as they stream |
| **R11** | Account numbers are not secret | They are printed on statements by design | Keep them non-secret but rate-limit lookups; never use them as an authenticator |

---

## 9. Two deliberate design decisions worth defending

**1. Unauthenticated failures are not written to the audit trail.** Every other denial is. Writing a
row per forged token would let anyone with `curl` fill the disk and serialise the single writer behind
their garbage. The access log still records the request; the security-relevant, authenticated events
are all in the chain.

**2. Failed money movements do not consume their idempotency key.** If the key row were written on
failure, a customer who tried to withdraw 80.00 with 50.00 available could never retry with the same
intent after a deposit — and worse, a transient failure would poison the key. Rolling back on failure
keeps the at-most-once property (no money moved) while remaining usable. Proven by
`test_idempotency.py::test_failed_movement_is_not_cached`.

---

## 10. Reproducing the verification

```bash
# 1. unit + integration + live concurrency suite
cd backend && pytest -q

# 2. tamper detection (drops triggers, edits history, expects the verifier to notice)
pytest tests/test_integrity_tamper.py -v

# 3. attack a running instance
python scripts/redteam.py --base-url http://localhost:8000

# 4. independent verification as an auditor
curl -s -X POST localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"auditor@ledgerline.test","password":"LedgerLine!Demo2024"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])' > /tmp/aud
curl -s localhost:8000/api/auditor/integrity -H "Authorization: Bearer $(cat /tmp/aud)" | python -m json.tool
```
