"""Concurrency: the tests that decide whether the balance can be broken.

These run against a real uvicorn process on a real socket, with real threads
and real SQLite connections -- ``TestClient`` would serialise too much of the
interesting behaviour. Every scenario finishes by asserting the ledger is still
balanced and that no customer balance went negative.
"""

from __future__ import annotations

import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import httpx
import pytest
import uvicorn

from conftest import PASSWORD
from ledgerline.config import Settings
from ledgerline.db import Database, utcnow_iso
from ledgerline.main import create_app
from ledgerline.security import hash_password


@pytest.fixture()
def live(settings: Settings):
    app = create_app(settings)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    if not server.started:  # pragma: no cover
        raise RuntimeError("uvicorn did not start")

    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60) as client:
        yield client, Database(settings.database_path, settings.sqlite_busy_timeout_ms)

    server.should_exit = True
    thread.join(timeout=10)


def _add_user(db: Database, settings: Settings, email: str, role: str = "CUSTOMER") -> None:
    now = utcnow_iso()
    with db.write() as conn:
        conn.execute(
            """
            INSERT INTO users (id, email, full_name, password_hash, role, status,
                               failed_login_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'ACTIVE', 0, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                email,
                "Concurrency Tester",
                hash_password(PASSWORD, settings.bcrypt_rounds),
                role,
                now,
                now,
            ),
        )


def _login(client: httpx.Client, email: str) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _parallel(count: int, fn: Callable[[int], Any]) -> list[Any]:
    """Fire ``count`` calls that all start at (almost) the same instant."""
    barrier = threading.Barrier(count)

    def worker(index: int) -> Any:
        barrier.wait(timeout=30)
        return fn(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(worker, range(count)))


class Scenario:
    def __init__(self, client: httpx.Client, db: Database, settings: Settings) -> None:
        self.client = client
        self.db = db
        self.settings = settings
        self.admin = self._staff("ADMIN", "admin@conc.test")
        self.alice = self._customer("alice@conc.test")
        self.alice_account = self._open_account(self.admin, "alice@conc.test", "Alice main")
        self.bob = self._customer("bob@conc.test")
        self.bob_account = self._open_account(self.admin, "bob@conc.test", "Bob main")

    def _staff(self, role: str, email: str) -> dict[str, str]:
        _add_user(self.db, self.settings, email, role)
        return _login(self.client, email)

    def _customer(self, email: str) -> dict[str, str]:
        _add_user(self.db, self.settings, email, "CUSTOMER")
        return _login(self.client, email)

    def _open_account(self, admin: dict[str, str], owner: str, name: str) -> dict[str, Any]:
        response = self.client.post(
            "/api/accounts",
            json={"owner_email": owner, "currency": "USD", "name": name},
            headers=admin,
        )
        assert response.status_code == 201, response.text
        return response.json()["account"]

    def fund(self, account_id: str, amount: str, headers: dict[str, str] | None = None) -> None:
        response = self.client.post(
            f"/api/accounts/{account_id}/deposit",
            json={"amount": amount},
            headers={**(headers or self.alice), "Idempotency-Key": str(uuid.uuid4())},
        )
        assert response.status_code == 201, response.text

    def balance(self, account_id: str) -> str:
        response = self.client.get(f"/api/accounts/{account_id}", headers=self.admin)
        assert response.status_code == 200, response.text
        return response.json()["account"]["balance"]

    def integrity_ok(self) -> dict[str, Any]:
        report = self.client.get("/api/auditor/integrity", headers=self.admin).json()
        return report

    def withdraw(self, index: int, amount: str = "10.00", key: str | None = None):
        return self.client.post(
            f"/api/accounts/{self.alice_account['id']}/withdraw",
            json={"amount": amount},
            headers={**self.alice, "Idempotency-Key": key or str(uuid.uuid4())},
        )

    def transfer(self, index: int, *, amount: str = "10.00", key: str | None = None, reverse: bool = False):
        source_headers = self.alice if not reverse else self.bob
        source = self.alice_account if not reverse else self.bob_account
        destination = self.bob_account if not reverse else self.alice_account
        return self.client.post(
            "/api/transfers",
            json={
                "from_account_id": source["id"],
                "to_account_number": destination["account_number"],
                "amount": amount,
            },
            headers={**source_headers, "Idempotency-Key": key or str(uuid.uuid4())},
        )


@pytest.fixture()
def scene(live, settings) -> Scenario:
    client, db = live
    return Scenario(client, db, settings)


class TestConcurrentWithdrawals:
    def test_only_as_many_withdrawals_succeed_as_the_balance_allows(self, scene: Scenario):
        scene.fund(scene.alice_account["id"], "100.00")

        responses = _parallel(20, scene.withdraw)
        codes = [response.status_code for response in responses]

        assert codes.count(201) == 10, codes
        assert set(codes) <= {201, 409}, codes
        assert all(codes.count(code) for code in set(codes))
        assert scene.balance(scene.alice_account["id"]) == "0.00"

        # No request may ever have observed a negative balance.
        statement = scene.client.get(
            f"/api/accounts/{scene.alice_account['id']}/statement?limit=200", headers=scene.alice
        ).json()
        assert all(float(entry["balance_after"]) >= 0 for entry in statement["entries"])
        assert scene.integrity_ok()["ok"] is True

    def test_balance_is_exactly_the_sum_of_concurrent_deposits(self, scene: Scenario):
        def deposit(index: int):
            return scene.client.post(
                f"/api/accounts/{scene.alice_account['id']}/deposit",
                json={"amount": "1.00"},
                headers={**scene.alice, "Idempotency-Key": str(uuid.uuid4())},
            )

        responses = _parallel(30, deposit)
        assert all(response.status_code == 201 for response in responses)
        # A lost update would leave a balance below 30.00 here.
        assert scene.balance(scene.alice_account["id"]) == "30.00"
        assert scene.integrity_ok()["ok"] is True


class TestConcurrentIdempotency:
    def test_twenty_duplicate_requests_pay_once(self, scene: Scenario):
        key = str(uuid.uuid4())

        def attempt(index: int):
            return scene.client.post(
                f"/api/accounts/{scene.alice_account['id']}/deposit",
                json={"amount": "25.00"},
                headers={**scene.alice, "Idempotency-Key": key},
            )

        responses = _parallel(20, attempt)
        codes = [response.status_code for response in responses]
        assert codes.count(201) == 1, codes
        assert codes.count(200) == 19, codes

        transaction_ids = {response.json()["transaction"]["id"] for response in responses}
        assert len(transaction_ids) == 1
        assert scene.balance(scene.alice_account["id"]) == "25.00"

        with scene.db.read() as conn:
            total = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        assert total == 1

    def test_duplicate_transfer_with_one_key_cannot_double_spend(self, scene: Scenario):
        scene.fund(scene.alice_account["id"], "100.00")
        key = str(uuid.uuid4())

        responses = _parallel(12, lambda index: scene.transfer(index, amount="100.00", key=key))
        assert {response.status_code for response in responses} <= {200, 201}
        assert len({response.json()["transaction"]["id"] for response in responses}) == 1
        assert scene.balance(scene.alice_account["id"]) == "0.00"
        assert scene.balance(scene.bob_account["id"]) == "100.00"
        assert scene.integrity_ok()["ok"] is True


class TestConcurrentTransfers:
    def test_money_is_conserved_under_opposite_transfers(self, scene: Scenario):
        scene.fund(scene.alice_account["id"], "500.00")
        scene.fund(scene.bob_account["id"], "500.00", headers=scene.bob)

        def attempt(index: int):
            return scene.transfer(index, amount="20.00", reverse=bool(index % 2))

        responses = _parallel(40, attempt)
        codes = [response.status_code for response in responses]
        # A deadlock or lost update shows up as a 500/503 or a wrong total.
        assert set(codes) <= {201}, codes

        alice = float(scene.balance(scene.alice_account["id"]))
        bob = float(scene.balance(scene.bob_account["id"]))
        assert alice + bob == 1000.00
        assert alice >= 0 and bob >= 0
        assert scene.integrity_ok()["ok"] is True

    def test_racing_transfers_cannot_overdraw_a_source_account(self, scene: Scenario):
        scene.fund(scene.alice_account["id"], "100.00")

        def attempt(index: int):
            return scene.transfer(index, amount="30.00")

        responses = _parallel(15, attempt)
        codes = [response.status_code for response in responses]
        assert codes.count(201) == 3, codes
        assert set(codes) <= {201, 409}, codes
        assert scene.balance(scene.alice_account["id"]) == "10.00"
        assert scene.balance(scene.bob_account["id"]) == "90.00"
