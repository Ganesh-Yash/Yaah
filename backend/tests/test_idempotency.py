"""Idempotency: the "same request never pays twice" contract."""

from __future__ import annotations

import uuid

import pytest


def test_missing_idempotency_key_is_rejected(api, admin, alice):
    account = api.open_account(admin, "alice@example.com")
    response = api.client.post(
        f"/api/accounts/{account['id']}/deposit", json={"amount": "10.00"}, headers=alice
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert api.balance(alice, account["id"]) == "0.00"


@pytest.mark.parametrize("key", ["short", "has spaces in it", "semi;colon;key;123", "x" * 200])
def test_malformed_idempotency_key_is_rejected(api, admin, alice, key):
    account = api.open_account(admin, "alice@example.com")
    response = api.client.post(
        f"/api/accounts/{account['id']}/deposit",
        json={"amount": "10.00"},
        headers={**alice, "Idempotency-Key": key},
    )
    assert response.status_code == 422, response.text


def test_replay_returns_the_original_result_and_moves_money_once(api, admin, alice):
    account = api.open_account(admin, "alice@example.com")
    key = str(uuid.uuid4())

    first = api.deposit(alice, account["id"], "100.00", key=key)
    second = api.deposit(alice, account["id"], "100.00", key=key)
    third = api.deposit(alice, account["id"], "100.00", key=key)

    assert first.status_code == 201
    assert first.headers["Idempotent-Replay"] == "false"
    for replay in (second, third):
        assert replay.status_code == 200
        assert replay.headers["Idempotent-Replay"] == "true"
        assert replay.json()["transaction"]["id"] == first.json()["transaction"]["id"]

    assert api.balance(alice, account["id"]) == "100.00"

    with api.db.read() as conn:
        count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert count == 1


def test_same_key_with_different_body_is_a_conflict(api, admin, alice):
    account = api.open_account(admin, "alice@example.com")
    key = str(uuid.uuid4())
    assert api.deposit(alice, account["id"], "100.00", key=key).status_code == 201

    response = api.deposit(alice, account["id"], "250.00", key=key)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    # The second request must not have moved any money.
    assert api.balance(alice, account["id"]) == "100.00"


def test_same_key_on_a_different_account_is_a_conflict(api, admin, alice):
    first = api.open_account(admin, "alice@example.com", name="First")
    second = api.open_account(admin, "alice@example.com", name="Second")
    key = str(uuid.uuid4())
    assert api.deposit(alice, first["id"], "10.00", key=key).status_code == 201
    response = api.deposit(alice, second["id"], "10.00", key=key)
    assert response.status_code == 409
    assert api.balance(alice, second["id"]) == "0.00"


def test_keys_are_scoped_per_endpoint(api, admin, alice):
    """A deposit key and a withdrawal key are independent namespaces."""
    account = api.open_account(admin, "alice@example.com")
    api.deposit(alice, account["id"], "50.00")
    key = "shared-key-0001"
    assert api.deposit(alice, account["id"], "20.00", key=key).status_code == 201
    assert api.withdraw(alice, account["id"], "20.00", key=key).status_code == 201
    assert api.balance(alice, account["id"]) == "50.00"


def test_failed_movement_is_not_cached(api, admin, alice):
    """A rejected attempt moved nothing, so the key may be reused later."""
    account = api.open_account(admin, "alice@example.com")
    key = str(uuid.uuid4())

    failed = api.withdraw(alice, account["id"], "80.00", key=key)
    assert failed.status_code == 409
    assert failed.json()["error"]["code"] == "INSUFFICIENT_FUNDS"

    api.deposit(alice, account["id"], "100.00")
    retried = api.withdraw(alice, account["id"], "80.00", key=key)
    assert retried.status_code == 201, retried.text
    assert retried.headers["Idempotent-Replay"] == "false"
    assert api.balance(alice, account["id"]) == "20.00"


def test_transfer_replay_returns_same_transaction(api, admin, alice, bob):
    alice_account = api.open_account(admin, "alice@example.com", name="Alice main")
    bob_account = api.open_account(admin, "bob@example.com", name="Bob main")
    api.deposit(alice, alice_account["id"], "300.00")
    key = str(uuid.uuid4())

    first = api.transfer(
        alice,
        from_account_id=alice_account["id"],
        to_account_number=bob_account["account_number"],
        amount="75.00",
        key=key,
    )
    replay = api.transfer(
        alice,
        from_account_id=alice_account["id"],
        to_account_number=bob_account["account_number"],
        amount="75.00",
        key=key,
    )
    assert first.status_code == 201 and replay.status_code == 200
    assert first.json()["transaction"]["id"] == replay.json()["transaction"]["id"]
    assert api.balance(alice, alice_account["id"]) == "225.00"
    assert api.balance(bob, bob_account["id"]) == "75.00"


def test_keys_are_per_user(api, admin, alice, bob):
    """Two different users may use the same key value without interference."""
    alice_account = api.open_account(admin, "alice@example.com", name="Alice main")
    bob_account = api.open_account(admin, "bob@example.com", name="Bob main")
    key = "user-collision-key-1"
    assert api.deposit(alice, alice_account["id"], "10.00", key=key).status_code == 201
    assert api.deposit(bob, bob_account["id"], "10.00", key=key).status_code == 201
    assert api.balance(alice, alice_account["id"]) == "10.00"
    assert api.balance(bob, bob_account["id"]) == "10.00"


def test_replay_is_audited(api, admin, alice, auditor):
    account = api.open_account(admin, "alice@example.com")
    key = str(uuid.uuid4())
    api.deposit(alice, account["id"], "10.00", key=key)
    api.deposit(alice, account["id"], "10.00", key=key)

    log = api.client.get("/api/audit?action=IDEMPOTENT_REPLAY", headers=auditor).json()
    assert len(log["entries"]) == 1
    assert log["entries"][0]["outcome"] == "ALLOW"
