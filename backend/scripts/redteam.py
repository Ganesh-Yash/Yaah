#!/usr/bin/env python3
"""Run the attack suite against a *running* LedgerLine instance.

    python scripts/redteam.py --base-url http://localhost:8000

Each line is an attack a reviewer might try, the control that is supposed to
stop it, and PASS/FAIL. Exit code is non-zero if anything is exploitable, so it
can gate a deployment. It creates two throw-away customers ("rt-victim…" and
"rt-attacker…") and a small amount of ledger history; both are timestamped.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import httpx

DEMO_PASSWORD = "LedgerLine!Demo2024"
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


class Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base_url, timeout=60)
        self.tokens: dict[str, str] = {}

    def login(self, email: str, password: str = DEMO_PASSWORD) -> dict[str, str]:
        response = self.http.post("/api/auth/login", json={"email": email, "password": password})
        if response.status_code != 200:
            raise SystemExit(f"cannot log in as {email}: {response.status_code} {response.text}")
        self.tokens[email] = response.json()["access_token"]
        return self.headers(email)

    def headers(self, email: str, key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.tokens[email]}"}
        if key:
            headers["Idempotency-Key"] = key
        return headers

    def get(self, email: str, path: str, **kwargs: Any) -> httpx.Response:
        return self.http.get(path, headers=self.headers(email), **kwargs)

    def post(self, email: str, path: str, body: Any = None, key: str | None = None) -> httpx.Response:
        return self.http.post(path, json=body, headers=self.headers(email, key))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()

    client = Client(args.base_url)
    print(f"LedgerLine red-team run against {args.base_url}\n")

    health = client.http.get("/api/health")
    if health.status_code != 200:
        raise SystemExit(f"service is not healthy: {health.status_code}")

    ADMIN, AUDITOR = "admin@ledgerline.test", "auditor@ledgerline.test"
    client.login(ADMIN)
    client.login(AUDITOR)
    client.login("alice@ledgerline.test")

    stamp = uuid.uuid4().hex[:8]
    victim_email, attacker_email = f"rt-victim-{stamp}@example.com", f"rt-attacker-{stamp}@example.com"
    for email in (victim_email, attacker_email):
        response = client.http.post(
            "/api/auth/register",
            json={"email": email, "password": "RedTeamPassphrase9", "full_name": f"RT {email[:12]}"},
        )
        assert response.status_code == 201, response.text
    client.login(victim_email, "RedTeamPassphrase9")
    client.login(attacker_email, "RedTeamPassphrase9")

    def open_account(email: str) -> dict[str, Any]:
        response = client.http.post(
            "/api/accounts",
            json={"owner_email": email, "currency": "USD", "name": f"RT {email[:18]}"},
            headers=client.headers(ADMIN),
        )
        assert response.status_code == 201, response.text
        return response.json()["account"]

    victim_account = open_account(victim_email)
    attacker_account = open_account(attacker_email)
    client.post(victim_email, f"/api/accounts/{victim_account['id']}/deposit", {"amount": "500.00"}, str(uuid.uuid4()))

    print("Authentication and tokens")

    response = client.http.post(
        "/api/auth/login", json={"email": "alice@ledgerline.test", "password": "WrongPassword123"}
    )
    record("wrong password rejected", response.status_code == 401, f"HTTP {response.status_code}")

    response = client.http.get(
        "/api/auth/me", headers={"Authorization": "Bearer eyJhbGciOiJub25lIn0.e30."}
    )
    record("alg=none JWT rejected", response.status_code == 401, f"HTTP {response.status_code}")

    forged = client.tokens[victim_email][:-4] + "AAAA"
    response = client.http.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"})
    record("tampered JWT signature rejected", response.status_code == 401, f"HTTP {response.status_code}")

    response = client.http.post(
        "/api/auth/register",
        json={"email": f"rt-{stamp}@example.com", "password": "EscalatePassphrase9", "full_name": "RT Escalate", "role": "ADMIN"},
    )
    record("role injection at registration rejected", response.status_code == 422, f"HTTP {response.status_code}")

    print("\nAuthorisation")

    response = client.get(victim_email, f"/api/accounts/{victim_account['id']}")
    record("victim can read own account", response.status_code == 200, f"HTTP {response.status_code}")

    response = client.get(attacker_email, f"/api/accounts/{victim_account['id']}")
    record("other customer's account is 404 (not 403)", response.status_code == 404, f"HTTP {response.status_code}")

    response = client.get(attacker_email, f"/api/accounts/{victim_account['id']}/statement")
    record("other customer's statement is 404", response.status_code == 404, f"HTTP {response.status_code}")

    response = client.get(attacker_email, "/api/audit")
    record("customer cannot read the audit trail", response.status_code == 403, f"HTTP {response.status_code}")

    response = client.post(AUDITOR, f"/api/accounts/{victim_account['id']}/deposit", {"amount": "10.00"}, str(uuid.uuid4()))
    record("auditor cannot deposit", response.status_code == 403, f"HTTP {response.status_code}")

    response = client.post(ADMIN, f"/api/accounts/{victim_account['id']}/deposit", {"amount": "10.00"}, str(uuid.uuid4()))
    record("admin cannot deposit (separation of duties)", response.status_code == 403, f"HTTP {response.status_code}")

    response = client.post(
        attacker_email,
        "/api/transfers",
        {
            "from_account_id": victim_account["id"],
            "to_account_number": attacker_account["account_number"],
            "amount": "100.00",
        },
        str(uuid.uuid4()),
    )
    record("cannot transfer out of another customer's account", response.status_code == 404, f"HTTP {response.status_code}")

    print("\nMoney integrity")

    response = client.post(victim_email, f"/api/accounts/{victim_account['id']}/withdraw", {"amount": "500.01"}, str(uuid.uuid4()))
    record(
        "overdraw rejected",
        response.status_code == 409 and response.json()["error"]["code"] == "INSUFFICIENT_FUNDS",
        f"HTTP {response.status_code}",
    )

    response = client.post(victim_email, f"/api/accounts/{victim_account['id']}/deposit", {"amount": "1.001"}, str(uuid.uuid4()))
    record("three decimal places rejected", response.status_code == 422, f"HTTP {response.status_code}")

    response = client.post(victim_email, f"/api/accounts/{victim_account['id']}/deposit", {"amount": "-50.00"}, str(uuid.uuid4()))
    record("negative amount rejected", response.status_code == 422, f"HTTP {response.status_code}")

    response = client.post(victim_email, f"/api/accounts/{victim_account['id']}/deposit", {"amount": " 5.00"}, str(uuid.uuid4()))
    record("whitespace-padded amount rejected", response.status_code == 422, f"HTTP {response.status_code}")

    response = client.post(victim_email, f"/api/accounts/{victim_account['id']}/deposit", {"amount": "10.00"})
    record("missing Idempotency-Key rejected", response.status_code == 400, f"HTTP {response.status_code}")

    key = str(uuid.uuid4())
    client.post(victim_email, f"/api/accounts/{victim_account['id']}/deposit", {"amount": "10.00"}, key)
    response = client.post(victim_email, f"/api/accounts/{victim_account['id']}/deposit", {"amount": "999.00"}, key)
    record(
        "key reuse with a different body is a 409",
        response.status_code == 409 and response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED",
        f"HTTP {response.status_code}",
    )

    response = client.post(
        victim_email,
        "/api/transfers",
        {
            "from_account_id": victim_account["id"],
            "to_account_number": "SYS-CASH-USD",
            "amount": "10.00",
        },
        str(uuid.uuid4()),
    )
    record("transfer to the system clearing account rejected", response.status_code == 422, f"HTTP {response.status_code}")

    print("\nConcurrency")

    drain_account = open_account(attacker_email)
    client.post(attacker_email, f"/api/accounts/{drain_account['id']}/deposit", {"amount": "100.00"}, str(uuid.uuid4()))
    barrier = threading.Barrier(20)

    def race_withdraw(index: int) -> int:
        barrier.wait(timeout=30)
        return client.post(
            attacker_email,
            f"/api/accounts/{drain_account['id']}/withdraw",
            {"amount": "10.00"},
            str(uuid.uuid4()),
        ).status_code

    with ThreadPoolExecutor(max_workers=20) as pool:
        codes = list(pool.map(race_withdraw, range(20)))
    balance = client.get(attacker_email, f"/api/accounts/{drain_account['id']}").json()["account"]["balance"]
    record(
        "20 racing withdrawals: 10 succeed, balance floors at 0.00",
        codes.count(201) == 10 and balance == "0.00" and not (set(codes) - {201, 409}),
        f"success={codes.count(201)} balance={balance}",
    )

    double_key = str(uuid.uuid4())
    barrier = threading.Barrier(10)

    def race_duplicate(index: int) -> httpx.Response:
        barrier.wait(timeout=30)
        return client.post(
            victim_email,
            f"/api/accounts/{victim_account['id']}/deposit",
            {"amount": "20.00"},
            double_key,
        )

    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(race_duplicate, range(10)))
    transaction_ids = {response.json()["transaction"]["id"] for response in responses}
    record(
        "10 duplicate requests with one key create one transaction",
        len(transaction_ids) == 1 and {response.status_code for response in responses} <= {200, 201},
        f"transactions={len(transaction_ids)}",
    )

    print("\nInjection and input handling")

    response = client.post(
        victim_email,
        f"/api/accounts/{victim_account['id']}/deposit",
        {"amount": "5.00", "description": "'; DROP TABLE ledger_entries; --"},
        str(uuid.uuid4()),
    )
    record("SQL metacharacters are stored as data", response.status_code == 201, f"HTTP {response.status_code}")

    response = client.get(attacker_email, "/api/accounts/1%20OR%201=1")
    record("SQL-ish path parameter is a 404", response.status_code == 404, f"HTTP {response.status_code}")

    response = client.http.get("/api/accounts", headers={"Authorization": "Bearer " + "x" * 500})
    record("garbage token is rejected quickly", response.status_code == 401, f"HTTP {response.status_code}")

    response = client.http.post(
        "/api/auth/login", json={"email": "a@b.com", "password": "x" * 90_000}
    )
    record("oversized body rejected", response.status_code in (413, 422), f"HTTP {response.status_code}")

    print("\nLedger verification after all attacks")

    report = client.get(AUDITOR, "/api/auditor/integrity").json()
    failed = [check["name"] for check in report["checks"] if not check["ok"]]
    record("integrity report is green", report["ok"] is True, f"failed={failed or 'none'}")
    record(
        "customer funds are conserved and non-negative",
        report["totals"]["customer_funds"] != "" and not report["totals"]["customer_funds"].startswith("-"),
        f"customer_funds={report['totals']['customer_funds']}",
    )

    entries = client.get(AUDITOR, "/api/audit?action=AUTHZ_DENIED&limit=50").json()["entries"]
    record("denied attempts appear in the audit trail", len(entries) >= 3, f"denials={len(entries)}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    if passed != len(RESULTS):
        print("FAILURES:")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name} ({detail})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
