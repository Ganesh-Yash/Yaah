"""Role-based authorisation and account-level access control."""

from __future__ import annotations

import uuid

import pytest


def _setup(api, admin):
    alice_account = api.open_account(admin, "alice@example.com", name="Alice main")
    bob_account = api.open_account(admin, "bob@example.com", name="Bob main")
    api.deposit(api.headers("alice@example.com"), alice_account["id"], "500.00")
    return alice_account, bob_account


class TestRoleMatrix:
    def test_unauthenticated_access_is_401_everywhere(self, client, api, admin, alice):
        account = api.open_account(admin, "alice@example.com")
        for method, path in [
            ("get", "/api/accounts"),
            ("get", f"/api/accounts/{account['id']}"),
            ("get", f"/api/accounts/{account['id']}/statement"),
            ("post", f"/api/accounts/{account['id']}/deposit"),
            ("post", "/api/transfers"),
            ("post", "/api/accounts"),
            ("get", "/api/admin/users"),
            ("get", "/api/audit"),
            ("get", "/api/auditor/integrity"),
        ]:
            response = getattr(client, method)(path)
            assert response.status_code == 401, (method, path, response.text)

    def test_customer_cannot_reach_staff_endpoints(self, api, admin, alice):
        account = api.open_account(admin, "alice@example.com")
        attempts = [
            api.client.get("/api/audit", headers=alice),
            api.client.get("/api/auditor/integrity", headers=alice),
            api.client.get("/api/admin/users", headers=alice),
            api.client.post(
                "/api/accounts",
                json={"owner_email": "alice@example.com", "currency": "USD", "name": "Sneaky"},
                headers=alice,
            ),
            api.client.post(
                f"/api/accounts/{account['id']}/freeze", json={"frozen": True}, headers=alice
            ),
        ]
        assert [response.status_code for response in attempts] == [403] * len(attempts)

    def test_auditor_is_read_only(self, api, admin, auditor, alice):
        account = api.open_account(admin, "alice@example.com")
        api.deposit(api.headers("alice@example.com"), account["id"], "100.00")

        assert api.client.get("/api/audit", headers=auditor).status_code == 200
        assert api.client.get("/api/auditor/integrity", headers=auditor).status_code == 200
        assert api.client.get("/api/accounts", headers=auditor).status_code == 200

        denied = [
            api.deposit(auditor, account["id"], "10.00"),
            api.withdraw(auditor, account["id"], "10.00"),
            api.transfer(
                auditor,
                from_account_id=account["id"],
                to_account_number=account["account_number"],
                amount="1.00",
            ),
            api.client.post(
                "/api/accounts",
                json={"owner_email": "alice@example.com", "currency": "USD", "name": "Nope"},
                headers=auditor,
            ),
        ]
        assert [response.status_code for response in denied] == [403] * len(denied)
        assert api.balance(api.headers("alice@example.com"), account["id"]) == "100.00"

    def test_admin_cannot_move_money_separation_of_duties(self, api, admin, alice):
        account = api.open_account(admin, "alice@example.com")
        api.deposit(alice, account["id"], "100.00")
        assert api.deposit(admin, account["id"], "10.00").status_code == 403
        assert api.withdraw(admin, account["id"], "10.00").status_code == 403
        assert api.balance(alice, account["id"]) == "100.00"

    def test_admin_can_open_and_freeze_accounts(self, api, admin, alice):
        account = api.open_account(admin, "alice@example.com")
        freeze = api.client.post(
            f"/api/accounts/{account['id']}/freeze", json={"frozen": True}, headers=admin
        )
        assert freeze.status_code == 200
        assert freeze.json()["account"]["status"] == "FROZEN"
        unfreeze = api.client.post(
            f"/api/accounts/{account['id']}/freeze", json={"frozen": False}, headers=admin
        )
        assert unfreeze.json()["account"]["status"] == "ACTIVE"

    def test_system_accounts_cannot_be_frozen(self, api, admin):
        with api.db.read() as conn:
            system_id = conn.execute(
                "SELECT id FROM accounts WHERE is_system = 1 LIMIT 1"
            ).fetchone()["id"]
        response = api.client.post(
            f"/api/accounts/{system_id}/freeze", json={"frozen": True}, headers=admin
        )
        assert response.status_code == 422


class TestAccountIsolation:
    def test_customer_sees_only_own_accounts(self, api, admin, alice, bob):
        _setup(api, admin)
        alice_list = api.client.get("/api/accounts", headers=alice).json()["accounts"]
        bob_list = api.client.get("/api/accounts", headers=bob).json()["accounts"]
        assert {account["name"] for account in alice_list} == {"Alice main"}
        assert {account["name"] for account in bob_list} == {"Bob main"}

    def test_reading_another_customers_account_is_404_not_403(self, api, admin, alice, bob):
        _, bob_account = _setup(api, admin)
        response = api.client.get(f"/api/accounts/{bob_account['id']}", headers=alice)
        assert response.status_code == 404
        statement = api.client.get(
            f"/api/accounts/{bob_account['id']}/statement", headers=alice
        )
        assert statement.status_code == 404

    def test_cannot_transfer_from_another_customers_account(self, api, admin, alice, bob):
        alice_account, bob_account = _setup(api, admin)
        response = api.transfer(
            bob,
            from_account_id=alice_account["id"],
            to_account_number=bob_account["account_number"],
            amount="10.00",
        )
        assert response.status_code == 404
        assert api.balance(alice, alice_account["id"]) == "500.00"

    def test_cannot_deposit_into_another_customers_account(self, api, admin, alice, bob):
        _, bob_account = _setup(api, admin)
        assert api.deposit(bob, bob_account["id"], "10.00").status_code == 201
        response = api.deposit(alice, bob_account["id"], "10.00")
        assert response.status_code == 404

    def test_transaction_visibility_is_scoped(self, api, admin, alice, bob):
        alice_account, bob_account = _setup(api, admin)
        transfer = api.transfer(
            alice,
            from_account_id=alice_account["id"],
            to_account_number=bob_account["account_number"],
            amount="50.00",
        )
        assert transfer.status_code == 201
        transaction_id = transfer.json()["transaction"]["id"]

        # Both parties to a transfer can see it; an unrelated customer cannot.
        api.create_user("eve@example.com")
        eve = api.headers("eve@example.com")
        assert api.client.get(f"/api/transactions/{transaction_id}", headers=alice).status_code == 200
        assert api.client.get(f"/api/transactions/{transaction_id}", headers=bob).status_code == 200
        assert api.client.get(f"/api/transactions/{transaction_id}", headers=eve).status_code == 404

    def test_auditor_and_admin_can_see_every_account(self, api, admin, auditor, alice, bob):
        _setup(api, admin)
        for headers in (admin, auditor):
            accounts = api.client.get("/api/accounts", headers=headers).json()["accounts"]
            assert any(account["name"] == "Alice main" for account in accounts)
            assert any(account["name"] == "Bob main" for account in accounts)

    def test_unknown_account_id_is_404(self, api, alice):
        assert api.client.get(f"/api/accounts/{uuid.uuid4()}", headers=alice).status_code == 404


class TestAdminUserManagement:
    def test_admin_creates_auditor_and_cannot_create_customers(self, api, admin, client):
        response = client.post(
            "/api/admin/users",
            json={
                "email": "new.auditor@example.com",
                "password": "AuditOnlyPassphrase9",
                "full_name": "New Auditor",
                "role": "AUDITOR",
            },
            headers=admin,
        )
        assert response.status_code == 201, response.text
        assert response.json()["user"]["role"] == "AUDITOR"

        bad = client.post(
            "/api/admin/users",
            json={
                "email": "new.customer@example.com",
                "password": "CustomerPassphrase9",
                "full_name": "New Customer",
                "role": "CUSTOMER",
            },
            headers=admin,
        )
        assert bad.status_code == 422

    def test_admin_can_unlock_a_locked_user(self, api, admin, client):
        api.create_user("locked@example.com")
        for _ in range(5):
            client.post("/api/auth/login", json={"email": "locked@example.com", "password": "BadPassword123"})
        assert client.post(
            "/api/auth/login", json={"email": "locked@example.com", "password": "CorrectHorseBattery9"}
        ).status_code == 429

        users = client.get("/api/admin/users", headers=admin).json()["users"]
        target = next(user for user in users if user["email"] == "locked@example.com")
        assert client.post(f"/api/admin/users/{target['id']}/unlock", headers=admin).status_code == 200
        assert client.post(
            "/api/auth/login", json={"email": "locked@example.com", "password": "CorrectHorseBattery9"}
        ).status_code == 200

    def test_customer_cannot_create_staff(self, api, alice, client):
        response = client.post(
            "/api/admin/users",
            json={
                "email": "escalate@example.com",
                "password": "EscalatePassphrase9",
                "full_name": "Escalation Attempt",
                "role": "ADMIN",
            },
            headers=alice,
        )
        assert response.status_code == 403
