"""Shared test fixtures and a small API client helper.

Every test gets its own SQLite file so concurrency tests exercise real
cross-connection locking rather than a shared in-memory database.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledgerline.config import Settings  # noqa: E402
from ledgerline.db import Database, utcnow_iso  # noqa: E402
from ledgerline.main import create_app  # noqa: E402
from ledgerline.security import hash_password  # noqa: E402

PASSWORD = "CorrectHorseBattery9"


PG_URL = os.environ.get("LEDGERLINE_TEST_DATABASE_URL")


def _new_pg_schema() -> str:
    """One schema per test, dropped afterwards — a real server, without the cost
    of 167 CREATE DATABASE calls."""
    import psycopg

    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_URL, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{name}"')
    return name


@pytest.fixture()
def settings(tmp_path: Path):
    """SQLite by default; PostgreSQL when LEDGERLINE_TEST_DATABASE_URL is set.

    The same 167 tests run on both engines — that is the point of the port, not
    a separate PostgreSQL-only suite.
    """
    if PG_URL:
        schema = _new_pg_schema()
        settings = Settings(
            env="test",
            database_path=str(tmp_path / "unused.db"),
            database_url=PG_URL,
            database_schema=schema,
            jwt_secret="test-secret-" + "x" * 40,
            bcrypt_rounds=4,
            max_amount_minor=100_000_00,
            demo_seed_enabled=False,
        )
        yield settings
        import psycopg

        with psycopg.connect(PG_URL, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        return

    yield Settings(
        env="test",
        database_path=str(tmp_path / "ledgerline.db"),
        jwt_secret="test-secret-" + "x" * 40,
        bcrypt_rounds=4,  # keep the suite fast; prod default is 12
        max_amount_minor=100_000_00,
        demo_seed_enabled=False,
    )


@pytest.fixture()
def app(settings: Settings):
    application = create_app(settings)
    # Entering the TestClient context runs the lifespan (schema + system accounts).
    with TestClient(application, raise_server_exceptions=False) as client:
        application.state.test_client = client
        yield application


@pytest.fixture()
def client(app):
    return app.state.test_client


class Api:
    """Thin helpers so tests read like scenarios instead of HTTP plumbing."""

    def __init__(self, client: TestClient, db: Database, settings: Settings) -> None:
        self.client = client
        self.db = db
        self.settings = settings

    # -- users ---------------------------------------------------------

    def create_user(
        self,
        email: str,
        *,
        role: str = "CUSTOMER",
        password: str = PASSWORD,
        status: str = "ACTIVE",
    ) -> str:
        user_id = str(uuid.uuid4())
        now = utcnow_iso()
        with self.db.write() as conn:
            conn.execute(
                """
                INSERT INTO users (id, email, full_name, password_hash, role, status,
                                   failed_login_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    user_id,
                    email.lower(),
                    f"Test {role.title()}",
                    hash_password(password, self.settings.bcrypt_rounds),
                    role,
                    status,
                    now,
                    now,
                ),
            )
        return user_id

    def login(self, email: str, password: str = PASSWORD) -> dict[str, Any]:
        response = self.client.post(
            "/api/auth/login", json={"email": email, "password": password}
        )
        assert response.status_code == 200, response.text
        return response.json()

    def headers(self, email: str, password: str = PASSWORD) -> dict[str, str]:
        tokens = self.login(email, password)
        return {"Authorization": f"Bearer {tokens['access_token']}"}

    def customer(self, email: str = "customer@example.com") -> tuple[str, dict[str, str]]:
        self.create_user(email)
        return email, self.headers(email)

    def staff(self, role: str, email: str | None = None) -> dict[str, str]:
        address = email or f"{role.lower()}-{uuid.uuid4().hex[:6]}@example.com"
        self.create_user(address, role=role)
        return self.headers(address)

    # -- accounts ------------------------------------------------------

    def open_account(
        self,
        admin_headers: dict[str, str],
        owner_email: str,
        *,
        currency: str = "USD",
        name: str = "Main account",
    ) -> dict[str, Any]:
        response = self.client.post(
            "/api/accounts",
            json={"owner_email": owner_email, "currency": currency, "name": name},
            headers=admin_headers,
        )
        assert response.status_code == 201, response.text
        return response.json()["account"]

    def deposit(
        self,
        headers: dict[str, str],
        account_id: str,
        amount: str,
        key: str | None = None,
        description: str | None = None,
    ):
        body: dict[str, Any] = {"amount": amount}
        if description is not None:
            body["description"] = description
        return self.client.post(
            f"/api/accounts/{account_id}/deposit",
            json=body,
            headers={**headers, "Idempotency-Key": key or str(uuid.uuid4())},
        )

    def withdraw(self, headers: dict[str, str], account_id: str, amount: str, key: str | None = None):
        return self.client.post(
            f"/api/accounts/{account_id}/withdraw",
            json={"amount": amount},
            headers={**headers, "Idempotency-Key": key or str(uuid.uuid4())},
        )

    def transfer(
        self,
        headers: dict[str, str],
        *,
        from_account_id: str,
        to_account_number: str,
        amount: str,
        key: str | None = None,
        description: str | None = None,
    ):
        body: dict[str, Any] = {
            "from_account_id": from_account_id,
            "to_account_number": to_account_number,
            "amount": amount,
        }
        if description is not None:
            body["description"] = description
        return self.client.post(
            "/api/transfers",
            json=body,
            headers={**headers, "Idempotency-Key": key or str(uuid.uuid4())},
        )

    def balance(self, headers: dict[str, str], account_id: str) -> str:
        response = self.client.get(f"/api/accounts/{account_id}", headers=headers)
        assert response.status_code == 200, response.text
        return response.json()["account"]["balance"]

    def integrity(self, headers: dict[str, str]) -> dict[str, Any]:
        response = self.client.get("/api/auditor/integrity", headers=headers)
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture()
def api(client, app) -> Api:
    return Api(client, app.state.db, app.state.settings)


@pytest.fixture()
def admin(api: Api) -> dict[str, str]:
    return api.staff("ADMIN")


@pytest.fixture()
def auditor(api: Api) -> dict[str, str]:
    return api.staff("AUDITOR")


@pytest.fixture()
def alice(api: Api):
    """A customer with one funded USD account holding 1000.00."""
    email = "alice@example.com"
    api.create_user(email)
    headers = api.headers(email)
    return headers


@pytest.fixture()
def bob(api: Api) -> dict[str, str]:
    api.create_user("bob@example.com")
    return api.headers("bob@example.com")
