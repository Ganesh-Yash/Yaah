# Narration script and slide deck

Two artefacts in one place:

- **This file** — what to say, second by second, while you drive the app.
- **[LedgerLine-deck.pptx](LedgerLine-deck.pptx)** — 12 slides with the same beats in the speaker
  notes, for the talking-head / intro part or as a backdrop.

Total spoken word count is roughly **610 words**. At a calm 130–140 words per minute that is
**4 minutes 20 seconds** of speech, leaving ~40 seconds of clicks and pauses inside a five-minute
video. Do not rush the clicks: silence while a page loads reads as confidence, not dead air.

---

## Timing map

| Time | On screen | The one idea |
|---|---|---|
| 0:00–0:30 | Sign in as Alice, two balance cards | Who can act, and on what |
| 0:30–1:15 | Transfer 100.50 → both ledger legs | Money is a *pair*, never a number edited in place |
| 1:15–2:00 | Same request again → replay badge | At-most-once is a transaction boundary, not a flag |
| 2:00–2:45 | 20 racing withdrawals → 10 succeed, floor 0.00 | No read-then-write window |
| 2:45–3:30 | Admin deposit → 403; auditor read-only | Separation of duties |
| 3:30–4:15 | Integrity green → tamper → red | Proven books, not trusted books |
| 4:15–4:45 | `redteam.py` 27/27 | Every claim is executable |
| 4:45–5:00 | Back to the dashboard | The two-sentence summary |

---

## 0:00 — 0:30 · Sign in as an account holder

**On screen:** log in as `alice@ledgerline.test`. Let both account cards and the role badge sit still
for a second before you talk.

**Say** (~65 words):

> This is LedgerLine. I'm signing in as Alice — an ordinary account holder. She sees two accounts:
> Everyday with 1,250 dollars, Savings with 500. She cannot see anyone else's money, and the badge in
> the header tells you her role. Every account, every transfer and every audit entry you're about to
> see is served by one Python service — and every one of these screens is backed by a test.

**Core point:** authorization is scoped to the *person*, not the endpoint. The screen shows only what
Alice may see.

**If asked "what stack?"** — Python 3.11 + FastAPI, SQLite in WAL mode, React + TypeScript. SQLite is
a deliberate choice: one writer makes the concurrency guarantees provable.

---

## 0:30 — 1:15 · A transfer that balances

**On screen:** "Alice — Everyday" → **Transfer** → Bob's account number (the seed prints it; the third
of the three) → amount `100.50`, description `rent`. Slow down on the receipt: point at the
transaction id, then at the two legs.

**Say** (~85 words):

> I'll move 100.50 from Everyday to Bob's account. Watch the receipt. One transaction id — and two
> ledger legs: minus 100.50 here, plus 100.50 there. They sum to zero. That's the whole design. Money
> is never edited on one side; it's moved from one account to another, always in pairs. Alice is now
> 1,149.50, Bob is 400.50. The total money in the system didn't change — it just moved. If a bug ever
> broke that, the auditor screen we'll see later would refuse to call the books healthy.

**Core point:** double-entry is the invariant that makes every other guarantee checkable. A single
`balance` column can be wrong; a zero-sum pair cannot be wrong without being detectably wrong.

**If asked "why not just store a balance?"** — because a balance is a cache. Here it's a
materialised sum of immutable entries, and the verifier recomputes it from scratch.

---

## 1:15 — 2:00 · The same request cannot pay twice

**On screen:** on the receipt, click **Send the same request again** (same key, same body). Let the
**idempotent replay** badge, the identical transaction id and the unchanged balance sit on screen for
two full seconds. Optional extra, from Window 2: reuse that key with a *different* amount and show the
`409 IDEMPOTENCY_KEY_REUSED`.

**Say** (~85 words):

> Now the part that separates this from a tutorial project. I'll send the exact same transfer again.
> Nothing happens. The response is the original receipt, marked idempotent replay, and the
> balance stays 1,149.50. The reason is architectural, not a UI trick: the record of the request and
> the money movement are written in the same database transaction. There is no moment where money has
> moved and the record hasn't been saved — which is the classic double-payment bug. And if I reuse
> that key with a different amount, it's rejected with a 409.

**Core point:** at-most-once is about *where the commit boundary is*, not about checking a flag
before writing. Two rows commit or neither does.

---

## 2:00 — 2:45 · Twenty withdrawals, ten payments' worth of money

**On screen:** the terminal. Run `demo_race.py`. Let the tally print fully, then scroll back one line
to the final balance.

**Say** (~90 words):

> Concurrency is where these systems usually break. This account holds exactly 100 dollars. I'm firing
> twenty withdrawals of 10 dollars, all at the same instant — each with its own key, so they're twenty
> genuinely separate payments. Ten succeed. Ten are refused for insufficient funds. The balance lands
> on exactly zero and never goes negative. That is not luck, and it's not a lock bolted on in the
> application: the balance check and the balance write are a single conditional SQL update, inside a
> serialised write transaction. There is no read-then-write window for another request to slip into.

**Core point:** the classic bug is `SELECT balance` … think … `UPDATE balance`. Here the guard lives in
the `WHERE` clause, so the database either applies the debit or matches zero rows.

**If asked "what about a distributed database?"** — the same shape holds; you swap the write lock for
`SELECT … FOR UPDATE` in a fixed order, documented in `docs/ARCHITECTURE.md`.

---

## 2:45 — 3:30 · Three roles that cannot do each other's job

**On screen:** sign in as `admin@ledgerline.test`, open a new account, freeze one, then attempt a
deposit — the 403 is the shot. Switch to `auditor@ledgerline.test`, show the audit trail with
`AUTHZ_DENIED` rows.

**Say** (~75 words):

> Roles are separated on purpose. An administrator can open and freeze accounts — but if I try to
> deposit, I get a 403. Admins cannot create money. An auditor can read everything and run
> verification, but cannot move a cent. The person who administers, the person who moves money and the
> person who checks it are three different people by design. And every one of those denials is
> recorded in the audit trail you're looking at.

**Core point:** the highest-value insider attack here is collusion between "can open accounts" and
"can post money". The role split removes it structurally.

---

## 3:30 — 4:15 · Prove the books, don't trust them

**On screen:** auditor → **Audit & integrity** → all nine checks green. Then run the tamper one-liner,
click **Re-run verification**, and hold on the red result with `first_bad_id`.

**Say** (~95 words):

> The auditor can prove the books instead of trusting them. Nine independent checks: every balance
> recomputed from the entries, every transaction summing to zero, no negative customer balances, money
> conserved across the system, and both hash chains walking end to end. Now I'll attack the database
> directly — I drop the append-only guard and change one ledger entry by a single unit. Re-run. Red.
> The hash chain breaks at row one, and the balance no longer matches its entries. That is the
> difference between storing a ledger and being able to prove one.

**Core point:** append-only is enforced three ways — a trigger, a hash chain, and an independent
verifier that doesn't reuse the write path.

**If asked "couldn't someone rewrite the whole chain?"** — yes, with database write access; that's
accepted risk R1, and the fix is publishing the head hash to a store I don't control.

---

## 4:15 — 4:45 · The same attacks, as a script

**On screen:** `redteam.py` scrolling to `27/27 checks passed`.

**Say** (~60 words):

> Finally, the same attacks as a script: twenty-seven checks, all passing — forged tokens, alg-none
> tokens, another customer's account, an admin trying to move money, key reuse, oversized bodies,
> twenty racing withdrawals, and the integrity report at the end. 167 automated tests sit underneath
> that, and CI runs both on every push.

**Core point:** the rubric asks for tests that actually test it. These are executable claims, not
prose.

---

## 4:45 — 5:00 · Close

**On screen:** back to Alice's dashboard, both balances visible.

**Say** (~30 words):

> So: the books always balance, because every movement is a zero-sum pair. And the same request never
> pays twice, because the key and the money commit together. That's the whole system.

**Then stop talking.** Let the final screen rest before you cut.

---

## If you must cut to three minutes

Drop beats 5 and 7, and shorten beat 1 to one sentence ("Alice sees her own two accounts and nobody
else's"). Keep 3, 4 and 6 — they are the ones a judge cannot get from reading the README.

## Delivery notes

- **Say "zero-sum pair", not "double entry"** the first time. The phrase explains itself; the term
  needs a sentence of setup you don't have.
- **Point, then talk.** Move the cursor to the thing, pause, then speak. Narrating while clicking
  makes both unreadable.
- **Never say "should work".** If something fails on camera, say what it means: "that's the refusal —
  which is the point."
- **Numbers to have memorised:** 1,250 → 1,149.50; Bob 300 → 400.50; 100 → 0.00; 10 of 20; 167 tests;
  27 checks; 9 verification checks.

---

## Where each beat happens (operator run sheet)

Three surfaces, opened once, before you hit record:

| Surface | What it is | Opened how |
|---|---|---|
| **Window 1** | the server. Do not touch it after it boots | `cd backend` → `..\.venv\Scripts\uvicorn.exe ledgerline.main:app --port 8000` |
| **Window 2** | the terminal for the race, the tamper and the attack suite | `cd backend` (same venv, no second activation needed if you call the exe by path) |
| **Browser** | one tab on **http://localhost:8000** | single-port mode, so UI and API share the port |

Two things to write on paper before recording: **Bob's account number** (third in the seed line) and
the **`Payout Demo`** account number (printed when the admin opens it). Hunting for a number on camera
is the most common way a demo loses a minute.

| Beat | Surface | Exact action |
|---|---|---|
| 0:00 | Browser | login screen → click the **account holder** chip (fills Alice) → **Sign in**. Two cards, each showing its 12-digit number |
| 0:30 | Browser | **Transfer** on *Alice — Everyday* → "Destination account number" = Bob's → Amount `100.50` → Description `rent` → **Transfer**. Receipt shows transaction id, new balance, **Ledger legs** `+100.50  −100.50`, and the `Idempotency-Key` |
| 1:15 | Browser | on that same receipt, click **Send the same request again** → an *idempotent replay* badge appears, the transaction id is identical, the balance does not move. Do **not** press Done and re-open: that starts a new intent with a new key (which is correct — and worth saying out loud) |
| 2:00 (a) | Browser | Sign out → **Register** `demo.race@ledgerline.test` / `RaceDemoPass!2024` |
| 2:00 (b) | Browser | Sign out → **admin** chip → **Administration** → **Open account**: owner `demo.race@ledgerline.test`, name `Payout Demo`, currency USD. Note the number it prints |
| 2:00 (c) | Browser | Sign out → sign in as `demo.race@ledgerline.test` → **Deposit** `100.00` |
| 2:00 (d) | **Window 2** | `..\.venv\Scripts\python.exe scripts\demo_race.py --email demo.race@ledgerline.test --password "RaceDemoPass!2024" --account "Payout Demo"` → `10 × 201`, `10 × 409`, balance `0.00` |
| 2:45 (a) | Browser | Sign out → **admin** chip → **Administration** → open one more account (shows the admin's real job), then **Accounts** tab → **Freeze** on a card → **Unfreeze** |
| 2:45 (b) | Browser | still as admin → **Deposit** on any card → the dialog returns **403 FORBIDDEN**. That refusal is the shot |
| 2:45 (c) | Browser | Sign out → **auditor** chip → **Audit & integrity** → the trail, including `AUTHZ_DENIED` rows from 2:45 (b) |
| 3:30 (a) | Browser | auditor → **Audit & integrity** → **Re-run verification** → `all checks passed`, 9 green, head hashes at the bottom |
| 3:30 (b) | **Window 2** | the tamper one-liner (below) |
| 3:30 (c) | Browser | **Re-run verification** again → `INTEGRITY FAILURE`, `ledger_hash_chain` fails with `first_bad_id: 1` |
| 4:15 | **Window 2** | `..\.venv\Scripts\python.exe scripts\redteam.py --base-url http://127.0.0.1:8000` → `27/27 checks passed` |
| 4:45 | Browser | Sign out → **account holder** chip → the two balances again |

The tamper, for Windows `cmd` from `backend\`:

```bat
..\.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('data/ledgerline.db');c.executescript('DROP TRIGGER trg_ledger_no_update; UPDATE ledger_entries SET amount_minor=1 WHERE id=1;');c.commit()"
```

**After the tamper the database is deliberately broken.** Stop the server (Ctrl+C in Window 1), delete
`data\ledgerline.db*`, re-seed, and restart before any second take — otherwise every verification you
show afterwards is red.

### Order that is cheap to get wrong

- **Do the attack suite last.** It writes throwaway users and transactions into the same database, so
  running it at 2:00 would change the balances you narrate at 1:15.
- **The race needs its own account.** Never fire it at Alice's 1,250.00: twenty 10.00 withdrawals all
  succeed and there is nothing to see.
- **Register before opening.** `Open account` takes an owner email that already exists; the register
  form is on the sign-in screen, so this costs one extra sign-out.
- **One tab is enough.** Switching roles means signing out and back in; two profiles work too, but keep
  the mouse on one surface so the camera does not lose the action.


---

## Slide-by-slide outline (matches `LedgerLine-deck.pptx`)

Speaker notes on each slide carry its timing and the line to say, so you can present from the deck
alone. The ordering is deliberately *problem → invariant → mechanism → proof → honesty → evidence*,
not "here is my tech stack".

| # | Slide | What it argues |
|---|---|---|
| 1 | LedgerLine | the claim in one line: balances, at-most-once, provable |
| 2 | Three ways money systems fail | books drift / double payment / race on one balance — the beats that follow kill these |
| 3 | Every movement is a zero-sum pair | double-entry as the invariant; a balance is a cache |
| 4 | At-most-once is a commit boundary | key + money in one transaction; replay returns the receipt; reuse → 409 |
| 5 | The guard lives in the WHERE clause | no read-then-write window; 20 racing withdrawals → 10 succeed |
| 6 | Prove the books, don't trust them | 9 checks, two hash chains, tamper located by `first_bad_id` |
| 7 | Three roles that cannot do each other's job | separation of duties; denials are audited |
| 8 | Limits that assume the session is stolen | daily outflow cap, byte-counted body cap, prod framing |
| 9 | What I would rather admit than hide | R1, R2, R6/R8 — each with its fix |
| 10 | Every claim is executable | 167 tests, 27/27 attack checks, 9 integrity checks, CI |
| 11 | What I would build next | Postgres port, cookie + CSRF, external anchoring |
| 12 | The two-sentence close | balance by construction, at-most-once by commit boundary |

**If you get only two slides:** 3 and 4. That is the whole design; everything else is evidence.
