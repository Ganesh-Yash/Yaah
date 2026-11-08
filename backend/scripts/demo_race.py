"""Fire N concurrent withdrawals at one account — the concurrency demo.

``docs/DEMO.md`` used a bash/``jq``/``uuidgen`` loop, which does not exist on
Windows. This does the same thing from the project's own venv, and it reports a
tally you can read out loud on camera.

    python scripts/demo_race.py --email demo@ledgerline.test --password '...'

What it proves: the balance check and the balance write are one conditional
``UPDATE`` under ``BEGIN IMMEDIATE``, so ``attempts`` racing withdrawals against
a balance of ``attempts/2 * amount`` land exactly half of them, and the balance
floors at ``0.00`` instead of going negative.

Every attempt uses its own ``Idempotency-Key`` — these are genuinely separate
payments, not retries. (Retries reusing one key are the other demo.)
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

DEFAULT_PASSWORD = "LedgerLine!Demo2024"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--email", required=True, help="customer account to drain")
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument(
        "--account",
        default="",
        help="substring of the account name (default: the first account with a balance)",
    )
    parser.add_argument("--amount", default="10.00")
    parser.add_argument("--attempts", type=int, default=20)
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        login = client.post(
            "/api/auth/login", json={"email": args.email, "password": args.password}
        )
        if login.status_code != 200:
            print(f"[race] could not sign in as {args.email}: {login.status_code} {login.text}")
            return 2
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        accounts = client.get("/api/accounts", headers=headers).json()["accounts"]
        if not accounts:
            print(f"[race] {args.email} has no accounts")
            return 2

        if args.account:
            matches = [a for a in accounts if args.account.lower() in a["name"].lower()]
            if not matches:
                names = ", ".join(a["name"] for a in accounts)
                print(f"[race] no account matching {args.account!r}; have: {names}")
                return 2
            account = matches[0]
        else:
            funded = [a for a in accounts if float(a["balance"]) > 0]
            account = (funded or accounts)[0]

        start = account["balance"]
        print(f"[race] account: {account['name']} ({account['account_number']})")
        print(f"[race] balance: {start}   attempts: {args.attempts} x {args.amount}")

        def attempt(_: int) -> int:
            response = client.post(
                f"/api/accounts/{account['id']}/withdraw",
                json={"amount": args.amount, "description": "race demo"},
                headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
            )
            return response.status_code

        with ThreadPoolExecutor(max_workers=args.attempts) as pool:
            statuses = list(pool.map(attempt, range(args.attempts)))

        tally = Counter(statuses)
        for code, count in sorted(tally.items()):
            label = {
                201: "moved",
                409: "refused (insufficient funds)",
                429: "throttled (money writes per minute)",
            }.get(code, "unexpected")
            print(f"  HTTP {code} x{count:<3} {label}")

        final = client.get(f"/api/accounts/{account['id']}", headers=headers).json()["account"]
        print(f"[race] final balance: {final['balance']}")

        if any(code >= 500 for code in statuses):  # pragma: no cover - failure path
            print("[race] FAIL: the server returned a 5xx under load")
            return 1
        if float(final["balance"]) < 0:
            print("[race] FAIL: the balance went negative")
            return 1
        if tally.get(429):
            print("[race] note: some attempts were rate-limited; wait 60s and retry for a clean run")
        return 0


if __name__ == "__main__":
    sys.exit(main())
