# How I built this with AI — and what I refused to let it decide

I used an AI assistant the way I would use a fast junior engineer who never gets tired: it wrote a
large share of the first draft of the code, and I decided what to build, what "correct" means, what to
reject, and what I was willing to sign my name to.

That distinction is the point of this file. It is not a log of prompts — it is the evidence that the
engineering judgement was mine:

- I defined the invariants (conservation, non-negativity, at-most-once, append-only) and chose where
  each one is enforced: code, transaction, or database constraint.
- Every row in the decision log below is a proposal I did **not** accept; what I did accept is
  listed separately, under "What I kept from it".
- I found **32 specific defects** in generated code. Each row records how I found it and what I
  changed, because "the AI wrote it" is not an excuse a judge would accept.
- Every claim in the README is traced to a test or a script in this repository, and every gap is
  written down in "Known limitations".

I was slower than the assistant on some tasks. I was also the only participant who could be wrong in
public, so review was not optional.

> **This file was drafted with the assistant and edited by me**, under the same rule as the code: no
> sentence stays unless it is true of the repository.

Two phases:

- **Phase 1 — scaffold (ChatGPT).** The original Java/Spring Boot + React skeleton, before this build.
- **Phase 2 — the build (Arena agent).** Ledger, auth, concurrency, tests, UI, PostgreSQL, Redis, docs.

---

## Who did what

| Activity | Me | Assistant | Where you can check |
|---|---|---|---|
| Defining what must be true after every money movement | decided | proposed a generic CRUD ledger | `THREATS.md` §2, the invariant tests |
| Stack: Python/FastAPI instead of the Spring Boot scaffold | decided, against its advice | advised keeping Spring Boot | D1, `docs/ARCHITECTURE.md` |
| No ORM; explicit SQL and transaction boundaries | decided | proposed SQLAlchemy | D4, `ledger.py`, `db.py` |
| Where the lock is taken and why | decided | proposed middleware-level checks | D7, `tests/test_concurrency.py` |
| First draft of most modules | reviewed and corrected all of it | wrote it | commit history; defect tables below |
| Adversarial tests (concurrency, tamper, idempotency) | specified them and demanded them first | wrote the first versions | `tests/test_concurrency.py`, `test_integrity_tamper.py`, `test_idempotency.py` |
| Finding defects in generated code | found 32 | produced them | the two tables below |
| Deciding what **not** to claim in the docs | decided | kept proposing confident prose | `README.md` → "Known limitations" |
| Database roles, bootstrap, health checks | specified the threat model | implemented | `deploy/postgres/`, `tests/test_db_roles.py` |

## Decision log

*Every row below is a proposal I did not accept. The things I did accept are listed separately,*
*under "What I kept from it" — this table is the record of my overrides, not of the assistant's help.*

| # | Decision | What the AI proposed | Why I overrode it |
|---|---|---|---|
| D1 | **Stack: Python/FastAPI instead of Java/Spring Boot** | Keep the Spring Boot scaffold and write the backend blind | The build environment had **no JDK, no Maven, and no network route to Maven Central** (`java`, `mvn`, `psql` all absent; only GitHub/npm/PyPI reachable). I could have produced Java source, but not one line of it could be compiled, run, or attacked — and 35 of the 100 marks are "what a judge can break on your deployed system". I chose the stack where I could prove every claim with an executable test. Cost: the Java scaffold I had already set up is not the deliverable. Benefit: every guarantee in the README is backed by a test I ran. |
| D2 | **Money as integer minor units** | A `NUMERIC` column with Python `Decimal` and a `Money` class | Integers plus a strict decimal-string parser remove an entire class of rounding and serialisation bugs, and make "legs sum to zero" a trivial `INTEGER` check. Decimals reappear only at the display boundary. |
| D3 | **SQLite instead of Postgres** | Postgres + SQLAlchemy ORM | No Postgres in the environment, and an ORM hides exactly the transaction boundaries this project is about. Raw SQL with explicit `BEGIN IMMEDIATE` makes the lock behaviour visible and testable. Postgres porting notes are in `docs/ARCHITECTURE.md`. |
| D4 | **No ORM** | SQLAlchemy models | The posting engine must be obvious to a reviewer (and to me, under live questioning). Explicit SQL is easier to defend than ORM-generated SQL. |
| D5 | **Idempotency row in the same transaction as the movement** | "Check for the key, then do the transfer, then save the key" | That ordering has a crash window where money moved but the key was not saved — the exact double-pay bug. Writing both in one transaction has only two outcomes, both safe. |
| D6 | **Deny responses return 404, not 403, for foreign resources** | 403 "forbidden" | 403 confirms the resource exists. 404 leaks nothing. |
| D7 | **The daily limit is checked inside the write transaction, not in middleware** | A middleware or dependency that sums "spent today" before the handler runs | A check outside `BEGIN IMMEDIATE` is a TOCTOU race: ten concurrent requests all read the same total and all pass. Putting it next to the posting makes the counter and the movement share one lock. The test proves it (10 concurrent → exactly 3 succeed). |
| D8 | **The body cap counts bytes on the ASGI stream, not `Content-Length`** | Keep the header check and call it "size limited" | A chunked request sends no `Content-Length` at all, so a header check is bypassed by changing the client's transport, not its payload. The proof is a real 120 KiB chunked `curl` that still gets `413`. |
| D9 | **Reverse D3: PostgreSQL is now the deployment database** | Keep SQLite (D3) and describe Postgres as future work | The clarification made PostgreSQL a mandatory requirement. D3 was the right call for the environment I *had* (no server, nothing to test against); it stopped being defensible once the requirement was explicit. Reversal recorded rather than quietly rewriting D3 — the earlier reasoning was sound for the facts at the time. |
| D10 | **Prisma owns DDL; Python owns runtime** | Rewrite the API in Node with Prisma Client, to match the tool the brief names | Recorder/API rewrite would throw away a tested money path for no requirement. Prisma's migration engine is the part the brief actually needs: it applies and versions the schema. The boundary is enforced by `tests/test_schema_parity.py`, and the API has no Prisma import at all. |
| D11 | **Both engines, not a switch** | Replace SQLite entirely with Postgres | The suite, the offline demo and the video all run without a server. Keeping SQLite keeps those fast; running the *whole* suite against Postgres (`LEDGERLINE_TEST_DATABASE_URL`) keeps the new claim honest. Two engines is a cost, paid once, in one adapter file. |
| D12 | **Serialise Postgres writers with an advisory lock** | Use Postgres defaults and rely on the conditional `UPDATE` | The conditional `UPDATE` protects the balance, but the daily-limit check reads a sum and then posts. With parallel writers that is a TOCTOU race the SQLite version never had. Keeping one writer keeps one money code path and lets the same six concurrency tests cover both engines. Cost: writes do not scale horizontally — stated in the README limitations. |
| D13 | **The Redis limiter fails open** | Fail closed: reject writes when Redis is down | Every money invariant lives in the database, so a cache outage should not stop the bank. Fail-closed turns a limiter outage into an outage, which is the worse failure for this product. The choice is in THREATS.md §8 and visible in `/api/health`, so it is a documented decision rather than a hidden one. |
| D14 | **Bootstrap is a script, not an endpoint** | An unauthenticated `POST /api/admin/bootstrap` that works when no admin exists | That endpoint is a permanent backdoor: it must exist forever, and its guard ("only when zero admins") is one race away from being wrong. A script needs database credentials, creates the role it is told to (ADMIN, hard-coded), refuses to run twice, and writes an audited event. |
| D15 | **One image, two deployment targets: a shared entrypoint instead of a Fly-only release hook** | Keep the migration wired to Fly's `release_command` and copy the same idea into Render's dashboard settings | The first cut assumed Fly: the migration ran in `release_command`, and the port was hardcoded to 8000 in both the CMD and the health check. Render assigns `$PORT` at runtime and only offers a pre-deploy command on some plans, so that design was either a 502 or an unmigrated database — see G32 and G33. Moving the migration into `scripts/start.sh` and reading `${PORT:-8000}` makes the image deployable on both without forking it; Fly keeps its release hook as an extra pre-traffic gate, and can skip the duplicate with `LEDGERLINE_SKIP_STARTUP_MIGRATION=1`. |
| D16 | **Reverse the deployment plan: free tiers, and a documented cold start** | Ship the always-on configuration and treat a sleeping service as unacceptable | The deployment target was chosen for permanence (no cold starts, no expiry). The instruction then became **free services only**, which cannot be reconciled with that: on Render, free spins down after 15 idle minutes and free Postgres expires in 30 days. Rather than quietly shipping a $7/month blueprint behind a "free" request, the blueprint now declares `plan: free`, the database moved off Render (Neon free does not expire), and the ~1 minute cold start plus the upgrade path are written into docs/DEPLOYMENT.md. The test that used to forbid `free` now forbids *omitting* `plan`, because an absent plan is what silently bills. |

---

## Phase 1: scaffold with ChatGPT (before this session)

These are the problems from my earlier report; I am keeping them here because they are the most
honest evidence of direction — each one is a case where generated configuration did not match my
machine and I had to correct it.

| # | What the AI produced | What was wrong | How I found it | Fix |
|---|---|---|---|---|
| P1 | `pom.xml` targeting Java 25 | My JDK is 21 | Maven build failed immediately | Set `<java.version>21</java.version>` |
| P2 | A "governance starter" dependency | Does not exist on Maven Central | Dependency resolution error | Removed — not needed for the skeleton |
| P3 | Flyway configuration | Version-management conflicts with Spring Boot's managed versions | Maven build warnings/errors | Corrected the dependency block; later dropped Flyway in favour of an idempotent schema file |
| P4 | Postgres connection settings | Application user authentication failed | `spring-boot:run` startup failure | Reset the DB user's password and passed it via an environment variable instead of hard-coding it |
| P5 | Health endpoint | Worked, but was the only endpoint | — | Kept the `/api/health` contract (it survives into the final build as `GET /api/health`) |

**What I learned and applied in phase 2:** generated configuration is a *hypothesis* about my
environment. Every claim it makes about a version number, a package name or a credential has to be
checked by actually running the thing.

---

## Phase 2: the build — how I directed it

### The loop I ran on every step

The assistant is fast at producing plausible code. That is exactly why the loop below exists — none
of its output was accepted on plausibility.

1. **State the invariant, not the task.** "The balance check and the balance write are one
   statement" is a constraint it cannot quietly relax; "implement deposits" is not.
2. **Demand the test that would break it, first.** I asked for 20 concurrent withdrawals, duplicate
   idempotency keys and tampered history *before* reading the implementation. Most of the defects
   below surfaced this way, because a hostile test is harder to fake than a passing one.
3. **Read the diff and refuse to skip lines I could not explain.** G1 (a fabricated bcrypt hash) and
   G16 (a patch that silently re-indented a function) both look fine at a glance; both were found by
   reading, not by running.
4. **Run it, and run it on the harder configuration.** Correct on SQLite proved nothing about
   PostgreSQL — on the first full run there, 150 tests passed while 17 failed or errored, which is
   exactly the kind of thing a demo would have hidden.
5. **Only then accept it — and re-run the whole suite**, because an accepted change is a change to
   everything else too.

The prompts that did most of the work, in the order I actually used them:

1. "Design the invariants first: what must be true after every money movement, and where each
   invariant is enforced (code, transaction, or database constraint)?"
2. "Implement the posting engine. The balance check and the balance write must be one statement. No
   read-then-write."
3. "Write the tests that would break this: 20 concurrent withdrawals, duplicate idempotency keys,
   tampered history. Do not write a test that only exercises the happy path."
4. "Now list the attacks a judge would try against the deployed system, and tell me which of them
   currently succeed."
5. "Write the threat model as a table: threat → attack → control → the test that proves the control."

The strongest habit was **step 2**: asking for the adversarial test *before* believing the
implementation. Most of the defects below were found that way.

### Defects it produced, and I caught

| # | Where | What the AI produced | Why it was wrong | How it surfaced | What I did |
|---|---|---|---|---|---|
| G1 | `security.py` | A hard-coded bcrypt "dummy hash" constant for timing-equalisation on unknown emails | **The hash was fabricated.** It is not a valid bcrypt digest, so `bcrypt.checkpw` would raise (caught and swallowed) and the "same CPU cost as a real user" property was silently false | I read the constant and recognised the format was invented | Generated a real cost-12 hash and pasted that instead |
| G2 | `deps.py` | `def enforce_money_rate_limit(request: Request, principal: Principal)` used as a FastAPI dependency | FastAPI would treat `principal` as a **request body model**, so the throttle could never resolve the caller correctly | Reading the dependency signature against FastAPI's resolution rules | Changed to `principal: Principal = Depends(current_principal)` |
| G3 | `deps.py` | A leftover `... if False else None` expression plus a stubbed route that `raise NotImplementedError` in `transfers.py` | Dead code and a route that would 500 if ever hit | Reviewing the file before committing | Deleted both, reimplemented the transaction route properly |
| G4 | `test_money.py` | `assert customer_funds == "900.00"` | **The AI's arithmetic was wrong, not the ledger's.** 1000 deposited, 300 withdrawn, then 200 transferred internally → 700 total. (A transfer moves money; it does not change the total.) | Test failed with `700.00` | Fixed the expectation; kept the ledger behaviour |
| G5 | `test_money.py` | A round-trip test asserting `parse_amount(format_amount(-12345)) == 12345` | `parse_amount` is the *client input* path and deliberately rejects signs; negative amounts only exist inside the ledger | Test failed | Split into two tests: positive round-trip, and a separate assertion that negative client input is rejected |
| G6 | Lockout design | "Count failed logins in the last 15 minutes" using the audit log | An admin unlock cleared the user's counter but **not** the audit-derived window, so the account stayed unusable after an unlock | `test_admin_can_unlock_a_locked_user` failed with `429` | Redesigned to "consecutive failures since the last success or admin unlock", derived from the audit log with an id watermark |
| G7 | `audit.py` call sites | Writing the DENY audit row inside the failing transaction | The rollback that follows the denial would delete the very audit row that records it | Thought through the exception path | Denials are written **after** the transaction has rolled back, on a fresh connection; success rows stay inside the transaction |
| G8 | Audit response | `GET /api/audit` omitted `user_agent` | The audit trail is supposed to answer "who, from where, with what client" | `test_login_failure_is_recorded_with_ip_and_subject` failed | Added the field to the projection |
| G9 | Test fixtures | Tests called `api.open_account(admin, "alice@example.com")` without requesting the `alice` fixture | The user did not exist, so the test failed with a 404 that looked like an application bug | Ran the suite | Added the fixture dependency — a good reminder that a failing test is not automatically a product bug |
| G10 | UI copy / validation | Test data used account name `"A"` | The API requires 2–80 characters | 422 from the API | Fixed the test data, kept the stricter validation |
| G11 | `styles.css` | `border radius: var(--radius);` | Invalid CSS property (typo) | Reading the diff | Removed the line |
| G12 | `vite.config.ts` | `process.env.LEDGERLINE_API_URL` | TypeScript error: no Node types | `npm run build` failed | Added `@types/node` and the `node` type entry |
| G13 | CSP | My first header set included `X-Frame-Options: DENY` | It would have broken the hosted live preview, which embeds the app in an iframe | Reasoning about the deployment environment before shipping | Dropped `X-Frame-Options`, kept `frame-ancestors *`, and documented the production setting (R9 in THREATS.md) |
| G14 | Session design | Access tokens valid until expiry (15 min) after logout | "Log out" that leaves the token usable for 15 minutes is not logging out; a judge can demonstrate it in ten seconds | Writing the THREATS.md row for repudiation — the row could not honestly be marked "solved" | Added a `sessions` table; access tokens carry `sid`; logout/reuse/password-change revoke the session and the token dies immediately. Three new tests |
| G15 | Red-team script | Called `client.post(auditor, …)` where `login()` returned a headers dict | The attack script itself crashed with `TypeError: unhashable type: 'dict'` | Running it against the live server | Made login store tokens by email and made the call sites use the email key |
| G16 | `services/money_service.py` | A scripted edit that wrapped the idempotency call in `try/except` | The generated patch **re-indented half the argument list**, producing code that looked like it ran but was misaligned | I printed the function instead of trusting the edit | Rewrote `withdraw` and `transfer` in full, then re-ran the money tests |
| G17 | `tests/test_limits.py` | `assert meta["daily_outflow_limit"] == "1,000,000.00"` | `format_amount` emits `1000000.00` with no thousands separators — the AI assumed a locale format the code never had | The assertion failed with the real value | Fixed the expectation. Lesson repeated from G4: the AI invents the *format* as readily as the arithmetic |
| G18 | `ledger.py` | `created_at < f"{prefix}T99"` as the upper bound for "today" | String comparison that happens to work is not the same as a correct date range; a reviewer should not have to reason about it | Reading my own diff before running it | Replaced with `datetime.strptime(day, "%Y-%m-%d")` and an explicit `+ 1 day` bound |
| G19 | `tests/test_limits.py` | Three tests: a balance of `600.00` after `500 + 100 − 100`, `denials[0]["detail"]["reason"]` on a JSON **string**, and a retry case that re-sent an amount which still broke the cap | Each test encoded a wrong mental model: forgotten subtraction, a response shape the endpoint never returns, and a scenario that could not pass by design | The three failures were in the tests, not the feature — the failures printed the actual values | Fixed the arithmetic, decoded the JSON, and redesigned the retry case to use an amount that fits. I did **not** weaken the feature to make the tests green |
| G20 | `main.py` (body cap) | Raising an exception from the ASGI `receive` wrapper | FastAPI has a handler registered for `Exception`, so the raise would be converted into a **500** before my middleware ever saw it | Reasoning about middleware ordering before writing it | Used the swallow-and-answer pattern instead: report end-of-body, drop the app's reaction, send one `413`. The test uses a real chunked upload to prove it |

### The same, during the PostgreSQL port

| # | What the AI wrote | What was wrong | How I found it | Fix |
|---|---|---|---|---|
| G22 | `dict_row` for every Postgres result | The money path indexes rows positionally in places (`row[0]` for a scalar aggregate). Postgres dict rows raise `KeyError: 0` | 23 tests errored on login with `KeyError: 0` | `PgRow`, a dict subclass that also answers `row[0]` — fixed once in the adapter instead of touching every call site |
| G23 | `WHERE (? IS NULL OR r.id < ?)` for the statement cursor | Postgres cannot infer the type of an untyped NULL parameter: `IndeterminateDatatype` | 2 statement tests failed on the new engine | Build the predicate in Python (`cursor_clause`), which is also faster |
| G24 | `UPDATE chain_state SET last_hash = 'f' * 64` inside a tamper test | `'f' * 64` is a SQLite-only expression; Postgres tries to multiply and fails parsing `'f'` as int | Tamper test failed with `InvalidTextRepresentation` | Build the literal in Python and bind it |
| G25 | "Just run `npx prisma migrate deploy` to verify" | The Prisma CLI cannot fetch its engine binaries in this environment (`binaries.prisma.sh` blocked; the WASM-engine env vars do not help) | Every CLI command died on a TLS disconnect | Wrote the schema and migration anyway, applied the SQL directly to a real PostgreSQL 16 server in tests, and added `tests/test_schema_parity.py` as the offline drift guard. The limitation is stated in the README and DEPLOYMENT.md rather than papered over |
| G26 | A production guard for demo data, described in THREATS.md before it existed | I nearly shipped a security claim with no code behind it | Cross-checking the claim against `seed.py` before committing the doc | Implemented the refusal (`seed_demo` raises when `env == prod`, no override) and added three tests; only then kept the claim |
| G27 | `pytest.raises(sqlite3.DatabaseError)` in the tamper tests | "This must fail" tests are engine-specific: Postgres raises `psycopg.errors` | 11 tamper tests failed on Postgres | `tests/dialect.py` centralises the engine differences (exception family, `DROP TRIGGER ... ON table`, droppable `CHECK`) so the tests assert the same *guarantee* on both engines |
| G28 | `scripts/seed.py` building `Database(...)` directly | It ignored `LEDGERLINE_DATABASE_URL`, so on a PostgreSQL deployment it seeded a local SQLite file and the API started against an empty database | Caught only by running the real thing: the seeded demo users could not log in through the Postgres-backed API | Used `Settings.from_env()` + `make_database()`, print the backend and target, and added a test that seeds through the script and reads the users back from the configured engine |

| G29 | `rate_limited()` returning a bare `429` | No `Retry-After`, and `AppError` had no way to carry response headers at all — so a throttled client could only guess how long to wait, which usually means retrying immediately | Hit the live limit with 120 bad logins and read the response headers, rather than reading the code | Added a `headers` field to `AppError`, forwarded it from the exception handler, and set `Retry-After: 60` on the money throttle and the login throttle; two tests pin it |
| G30 | The first version of `scripts/migrate.py` | With no `LEDGERLINE_DATABASE_URL` it fell through to the default SQLite path, so a release step in a container would have "succeeded" by creating a throwaway file that the next deploy discards | Its own test: `test_refuses_when_there_is_no_target` returned 0 instead of 1 | A production release now refuses without an explicit database URL; the test asserts exit 1 and a message that names the variable |
| G31 | `PostgresDatabase.schema_version()` | It raised `UndefinedTable` on an empty database — exactly the state a release step exists to fix — while the SQLite adapter returned `None` for the same situation | Running `python -m scripts.migrate` against a genuinely empty database, not against the test fixtures | Missing relation now reads as "no version yet" on both engines, and `/api/ready` can answer before the first migration |
| G32 | The container's listening port | The Dockerfile hardcoded `--port 8000`, and so did the image health check. On Render the platform assigns the port in `$PORT`, so the process would have looked healthy in the logs while the load balancer answered **502** | Reading Render's port documentation before writing the blueprint, then noticing the number was also baked into `HEALTHCHECK` | `scripts/start.sh` binds `${PORT:-8000}` (Fly's `internal_port` still works through the fallback), the health check reads the same variable, and `tests/test_deploy_config.py` fails if a bare `--port 8000` ever returns |
| G33 | Where the migration runs on a platform without a release hook | The release step was wired only to Fly's `release_command`. Render offers a pre-deploy command only on some plans, so a deploy there would have started the API against a database with no schema — the app would answer `/api/health` with 200 and 500 on every real route | Walking the Render deploy path start to finish instead of assuming the Fly design generalised | The entrypoint now runs `python -m scripts.migrate` before uvicorn under `set -eu` (a failed migration aborts the boot), with `LEDGERLINE_SKIP_STARTUP_MIGRATION=1` for platforms that run it as a separate pre-traffic step. Verified by booting the entrypoint on an arbitrary port against an empty database |

> The numbering skips **G21**: that defect belongs to a change that is not in this commit
> history. I kept the numbers stable instead of renumbering, because other documents refer to them.

G26 is the one I want to highlight: the failure mode of AI-written documentation is not a typo, it is
a confident sentence describing code that was never written. The only defence I found is to grep for
the implementation of every security claim before committing the paragraph.

### What I kept from it — after checking

Not everything was a defect. These suggestions were good and I kept them after verifying them:

- **Pinning the JWT algorithm list** rather than trusting the token header — this is what defeats
  `alg: none`.
- **Re-reading the role from the database on every request** instead of trusting a role claim.
- **A request fingerprint stored with the idempotency key**, so key reuse with a different body is a
  `409` rather than a silent second payment.
- **`extra="forbid"` on request models**, which turns a mass-assignment attempt into a `422`.
- **A deterministic leg ordering** in the posting engine, which is what prevents deadlocks once this
  moves to Postgres.
- **Recording the `Idempotent-Replay` response header**, which made the guarantee visible in the UI
  and in the red-team output rather than something I merely assert.

---

## What I did not delegate

- **The money invariants.** I decided what must be true (conservation, non-negative balances,
  at-most-once, append-only) and where each one is enforced. The assistant implemented them; I checked
  each one against a test that fails if the control is removed.
- **The threat model.** I would not accept a generic OWASP list; `THREATS.md` is organised around the
  specific things a judge would try on *this* system, with the residual risks written down — including
  the ones I chose to accept, and why (the fail-open limiter, the single writer).
- **The definition of done.** "It works on my machine" was never the bar: the bar was the whole suite
  on both engines, the red-team script exiting zero against a live server, and a claim I could point
  at a line of code for.
- **The final review of every file in the money path** (`ledger.py`, `money_service.py`, `chain.py`,
  `schema.sql`). If I cannot explain a line, it does not ship.
- **What not to say.** The assistant repeatedly proposed confident prose for things that were not
  true yet (G26 is the clearest example: a production guard documented before it existed). Deciding
  what the project is allowed to claim was mine, and it is the part I would defend hardest in a viva.

## How much of this is mine, honestly

Most of the characters in `backend/ledgerline/` were typed by the assistant. I am not going to pretend
otherwise, and anyone reading the commit history will see that pattern. What is mine is everything
that decides whether those characters are correct:

| Contribution | Evidence in this repo |
|---|---|
| The invariants, and the enforcement point chosen for each | `THREATS.md` §2; every invariant has a test that fails if it is removed |
| Every decision in the log above is a rejection of a proposal — the accepted ones are listed separately | the "what the AI proposed" column |
| 32 defects found by reading, reasoning and running — not by trusting | the two defect tables, each with "how it surfaced" |
| The adversarial cases that make the guarantees meaningful | `test_concurrency.py`, `test_integrity_tamper.py`, `test_idempotency.py`, `scripts/redteam.py` |
| The decision to document gaps instead of hiding them | `README.md` → "Known limitations" (11 items), `docs/VERIFICATION.md` §9 |

The bar I held myself to: **I can explain any line in the money path, and I can break the system on
purpose and show you the test that catches it.** That is what "built with AI" means here. The tool
wrote; I decided; and the result is mine to answer for.

If the assistant were removed tomorrow, the design would not change — the money invariants come from
double-entry bookkeeping, not from a model — it would just take me longer to type. That is the honest
version of "not dependent on AI": I depend on it the way I depend on a fast editor, not the way I
depend on knowing what a ledger is.

## What I would do differently

1. **Ask for the environment check first.** One prompt — "what is actually installed here?" — would
   have saved the entire Java scaffold phase.
2. **Ask for failing tests before implementations**, not after. Every time I asked for the adversarial
   test first, the design improved immediately (the idempotency transaction boundary, the session
   revocation).
3. **Be meaner about constants.** G1 (the fabricated bcrypt hash) is the scariest defect in this list
   precisely because it looks plausible and silently disables a defence. Any magic constant in
   AI-generated security code needs to be regenerated, not trusted.
4. **Keep the red-team script in the loop from day one.** Turning the threat model into a script that
   exits non-zero was more valuable than any amount of prose: it caught the registration role
   injection and the oversized-body behaviour as I built them.

## Rules I followed

- No secrets, tokens or customer data in prompts.
- No generated code merged without a test that would fail if the control were removed.
- **I check the artefact, not the description.** Every security claim in the docs was grepped back to
  an implementation before it was allowed to stay (G26 is why this is a rule and not an intention).
- Every claim in `README.md` is backed by a test or a script in this repository, and every gap is in
  "Known limitations". I would rather be asked about a limitation I documented than be caught claiming
  something that does not exist.
- When the assistant and I disagreed about what was possible, I ran the thing and let the output
  decide — that is how the PostgreSQL port, the Redis limiter and the two-role database got proven
  rather than asserted.
