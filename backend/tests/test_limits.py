"""Velocity limits: a per-account ceiling on the value that can leave per day.

R5 in THREATS.md used to be an accepted risk ("no daily/velocity limits"). These
tests pin the behaviour that replaced it: withdrawals and outgoing transfers
share one counter per account per UTC day, the check runs inside the same write
transaction as the posting (so concurrency cannot slip past it), refusals are
audited, and deposits or incoming transfers never consume the allowance.
"""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from conftest import Api
from ledgerline.main import create_app

DAILY_LIMIT_MINOR = 10_000  # 100.00 per account per day


@pytest.fixture()
def limited(tmp_path, settings) -> Api:
    app = create_app(
        settings.with_overrides(
            database_path=str(tmp_path / "limited.db"),
            daily_outflow_limit_minor=DAILY_LIMIT_MINOR,
        )
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield Api(client, app.state.db, app.state.settings)


def _funded_customer(api: Api, admin, email: str = "limited@example.com", amount: str = "500.00"):
    api.create_user(email)
    headers = api.headers(email)
    account = api.open_account(admin, email)
    assert api.deposit(headers, account["id"], amount).status_code == 201
    return headers, account


def test_withdrawals_are_capped_at_the_daily_limit(limited):
    api = limited
    admin = api.staff("ADMIN")
    customer, account = _funded_customer(api, admin)

    assert api.withdraw(customer, account["id"], "60.00").status_code == 201
    assert api.withdraw(customer, account["id"], "40.00").status_code == 201

    refused = api.withdraw(customer, account["id"], "0.01")
    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert error["code"] == "DAILY_LIMIT_EXCEEDED"
    assert error["details"] == {
        "limit": "100.00",
        "used_today": "100.00",
        "remaining_today": "0.00",
    }
    # Nothing moved: the refusal rolled back with the rest of the transaction.
    assert api.balance(customer, account["id"]) == "400.00"


def test_outgoing_transfers_share_the_same_counter(limited):
    api = limited
    admin = api.staff("ADMIN")
    customer, account = _funded_customer(api, admin)
    api.create_user("payee@example.com")
    destination = api.open_account(admin, "payee@example.com")

    moved = api.transfer(
        customer,
        from_account_id=account["id"],
        to_account_number=destination["account_number"],
        amount="100.00",
    )
    assert moved.status_code == 201, moved.text

    # The allowance is spent, so a withdrawal from the same account is refused:
    # switching endpoints must not reset the counter.
    refused = api.withdraw(customer, account["id"], "0.01")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "DAILY_LIMIT_EXCEEDED"
    assert api.balance(customer, account["id"]) == "400.00"


def test_incoming_money_does_not_consume_the_limit(limited):
    api = limited
    admin = api.staff("ADMIN")
    customer, account = _funded_customer(api, admin)
    api.create_user("sender@example.com")
    sender_headers = api.headers("sender@example.com")
    sender_account = api.open_account(admin, "sender@example.com", name="Sender")
    api.deposit(sender_headers, sender_account["id"], "500.00")
    api.transfer(
        sender_headers,
        from_account_id=sender_account["id"],
        to_account_number=account["account_number"],
        amount="100.00",
    )

    # A credit is not an outflow: the full daily allowance is still available.
    response = api.withdraw(customer, account["id"], "100.00")
    assert response.status_code == 201, response.text
    # 500 deposited + 100 received - 100 withdrawn
    assert response.json()["account"]["balance"] == "500.00"


def test_a_limit_denial_is_audited(limited):
    api = limited
    admin = api.staff("ADMIN")
    auditor = api.staff("AUDITOR")
    customer, account = _funded_customer(api, admin)

    assert api.withdraw(customer, account["id"], "100.00").status_code == 201
    assert api.withdraw(customer, account["id"], "1.00").status_code == 409

    trail = api.client.get(
        "/api/audit", params={"action": "WITHDRAWAL", "outcome": "DENY"}, headers=auditor
    )
    assert trail.status_code == 200, trail.text
    denials = [entry for entry in trail.json()["entries"] if entry["outcome"] == "DENY"]
    assert denials, trail.text
    assert denials[0]["target_id"] == account["id"]
    # The endpoint returns the stored canonical JSON, so decode it.
    detail = json.loads(denials[0]["detail"])
    assert detail == {"reason": "DAILY_LIMIT_EXCEEDED", "amount": "1.00"}


def test_concurrent_withdrawals_cannot_overshoot_the_limit(limited):
    """The counter is read under the same write lock as the posting."""
    api = limited
    admin = api.staff("ADMIN")
    customer, account = _funded_customer(api, admin, amount="1000.00")

    def attempt(index: int) -> int:
        return api.withdraw(customer, account["id"], "30.00", key=f"limit-race-{index:02d}").status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        statuses = list(pool.map(attempt, range(10)))

    # 3 x 30.00 fits in a 100.00 daily allowance; the 4th would reach 120.00.
    assert statuses.count(201) == 3, statuses
    assert statuses.count(409) == 7, statuses
    assert api.balance(customer, account["id"]) == "910.00"


def test_a_limit_of_zero_disables_the_check(tmp_path, settings):
    app = create_app(
        settings.with_overrides(
            database_path=str(tmp_path / "unlimited.db"),
            daily_outflow_limit_minor=0,
        )
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        api = Api(client, app.state.db, app.state.settings)
        admin = api.staff("ADMIN")
        customer, account = _funded_customer(api, admin, amount="500.00")
        response = api.withdraw(customer, account["id"], "500.00")
        assert response.status_code == 201, response.text
        assert response.json()["account"]["balance"] == "0.00"


def test_limit_refusal_is_not_cached_as_an_idempotent_success(limited):
    """A denied attempt must not poison the key: nothing moved, so it can retry."""
    api = limited
    admin = api.staff("ADMIN")
    customer, account = _funded_customer(api, admin)

    assert api.withdraw(customer, account["id"], "60.00").status_code == 201

    key = str(uuid.uuid4())
    refused = api.withdraw(customer, account["id"], "50.00", key=key)
    assert refused.status_code == 409  # 60 + 50 > 100

    # Same key, amount that still fits: a fresh attempt, not a replay.
    retried = api.withdraw(customer, account["id"], "40.00", key=key)
    assert retried.status_code == 201, retried.text
    assert api.balance(customer, account["id"]) == "400.00"


class TestRateLimitResponsesAreActionable:
    """A 429 must say when to come back.

    RFC 6585 defines Retry-After for 429. Without it a client can only guess, and
    guessing usually means retrying immediately — which is how a throttle turns
    into a retry storm.
    """

    def test_money_throttle_sets_retry_after(self, api, alice, admin):
        account = api.open_account(admin, "alice@example.com")
        api.deposit(alice, account["id"], "10.00")
        limited = None
        for _ in range(api.settings.money_writes_per_minute + 2):
            response = api.client.post(
                f"/api/accounts/{account['id']}/deposit",
                json={"amount": "0.01"},
                headers={**alice, "Idempotency-Key": str(uuid.uuid4())},
            )
            if response.status_code == 429:
                limited = response
                break
        assert limited is not None, "the money throttle never triggered"
        assert limited.json()["error"]["code"] == "RATE_LIMITED"
        assert limited.headers.get("Retry-After") == "60"

    def test_login_throttle_sets_retry_after(self, api):
        response = None
        for _ in range(120):
            response = api.client.post(
                "/api/auth/login",
                json={"email": "spray@example.com", "password": "wrong-password-1"},
            )
            if response.status_code == 429:
                break
        assert response is not None and response.status_code == 429, "the login throttle never triggered"
        assert response.headers.get("Retry-After") == "60"
