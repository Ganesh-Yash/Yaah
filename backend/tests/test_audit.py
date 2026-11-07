"""The audit trail: what is recorded, who can read it, and that it chains."""

from __future__ import annotations

import json
import uuid


def test_login_failure_is_recorded_with_ip_and_subject(api, client, auditor):
    api.create_user("audited@example.com")
    response = client.post(
        "/api/auth/login",
        json={"email": "audited@example.com", "password": "NotThePassword1"},
        headers={"User-Agent": "pytest-agent"},
    )
    assert response.status_code == 401

    log = client.get("/api/audit?action=LOGIN_FAILURE", headers=auditor).json()
    assert log["entries"]
    entry = log["entries"][-1]
    assert entry["subject"] == "audited@example.com"
    assert entry["outcome"] == "DENY"
    assert entry["user_agent"] == "pytest-agent"
    assert json.loads(entry["detail"])["reason"] == "bad_password"


def test_successful_movement_is_recorded_with_transaction_id(api, admin, alice, auditor):
    account = api.open_account(admin, "alice@example.com")
    response = api.deposit(alice, account["id"], "42.00", description="salary")
    transaction_id = response.json()["transaction"]["id"]

    log = api.client.get("/api/audit?action=DEPOSIT", headers=auditor).json()
    entry = log["entries"][0]
    assert entry["actor_role"] == "CUSTOMER"
    assert entry["target_id"] == account["id"]
    detail = json.loads(entry["detail"])
    assert detail["transaction_id"] == transaction_id
    assert detail["amount"] == "42.00"
    assert detail["idempotency_key"]


def test_forbidden_attempts_are_recorded(api, admin, alice, auditor):
    api.client.get("/api/audit", headers=alice)  # customer -> 403
    api.client.post(
        "/api/accounts",
        json={"owner_email": "alice@example.com", "currency": "USD", "name": "Nope"},
        headers=alice,
    )
    log = api.client.get("/api/audit?action=AUTHZ_DENIED", headers=auditor).json()
    assert len(log["entries"]) >= 2
    assert all(entry["outcome"] == "DENY" for entry in log["entries"])
    assert json.loads(log["entries"][0]["detail"])["actual_role"] == "CUSTOMER"


def test_request_id_correlates_response_and_audit(api, admin, alice, auditor):
    account = api.open_account(admin, "alice@example.com")
    response = api.deposit(alice, account["id"], "5.00")
    request_id = response.headers["X-Request-ID"]

    entries = api.client.get("/api/audit?action=DEPOSIT", headers=auditor).json()["entries"]
    assert entries[0]["request_id"] == request_id


def test_client_supplied_request_id_is_echoed_but_sanitised(api, client, auditor):
    response = client.get("/api/health", headers={"X-Request-ID": "client-trace-123"})
    assert response.headers["X-Request-ID"] == "client-trace-123"
    # A request id with illegal characters must be replaced, never reflected.
    response = client.get("/api/health", headers={"X-Request-ID": "bad id <script>"})
    assert response.headers["X-Request-ID"] != "bad id <script>"
    assert response.headers["X-Request-ID"].startswith("req_")


def test_audit_pagination_and_filters(api, admin, alice, auditor):
    account = api.open_account(admin, "alice@example.com")
    for _ in range(5):
        api.deposit(alice, account["id"], "1.00")

    page1 = api.client.get("/api/audit?limit=3", headers=auditor).json()
    assert len(page1["entries"]) == 3
    assert page1["next_cursor"]
    page2 = api.client.get(
        f"/api/audit?limit=3&before_id={page1['next_cursor']}", headers=auditor
    ).json()
    assert {entry["id"] for entry in page1["entries"]}.isdisjoint(
        {entry["id"] for entry in page2["entries"]}
    )

    deposits = api.client.get("/api/audit?action=DEPOSIT&limit=100", headers=auditor).json()
    assert {entry["action"] for entry in deposits["entries"]} == {"DEPOSIT"}
    denied = api.client.get("/api/audit?outcome=DENY", headers=auditor).json()
    assert all(entry["outcome"] == "DENY" for entry in denied["entries"])


def test_audit_chain_is_valid_and_complete(api, admin, alice, auditor):
    account = api.open_account(admin, "alice@example.com")
    api.deposit(alice, account["id"], "10.00")
    api.withdraw(alice, account["id"], "5.00")

    with api.db.read() as conn:
        ids = [row[0] for row in conn.execute("SELECT id FROM audit_log ORDER BY id")]
    assert ids == list(range(1, len(ids) + 1))

    report = api.integrity(auditor)
    audit_check = next(check for check in report["checks"] if check["name"] == "audit_hash_chain")
    assert audit_check["ok"] is True
    assert audit_check["detail"]["rows"] == len(ids)


def test_audit_rows_contain_no_secrets(api, admin, alice, auditor):
    api.create_user("secret@example.com")
    api.client.post(
        "/api/auth/login",
        json={"email": "secret@example.com", "password": "CorrectHorseBattery9"},
    )
    entries = api.client.get("/api/audit?limit=200", headers=auditor).json()["entries"]
    blob = json.dumps(entries).lower()
    for forbidden in ("correcthorsebattery9", "password_hash", "bcrypt", "$2b$", "authorization"):
        assert forbidden not in blob


def test_idempotency_conflict_is_recorded_as_deny(api, admin, alice, auditor):
    account = api.open_account(admin, "alice@example.com")
    key = str(uuid.uuid4())
    api.deposit(alice, account["id"], "10.00", key=key)
    api.deposit(alice, account["id"], "99.00", key=key)

    log = api.client.get("/api/audit?action=IDEMPOTENCY_KEY_REUSED", headers=auditor).json()
    assert log["entries"]
    assert log["entries"][0]["outcome"] == "DENY"
