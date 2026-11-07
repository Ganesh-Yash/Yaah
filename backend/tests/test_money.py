"""Money parsing, formatting and the happy-path ledger behaviour."""

from __future__ import annotations

import pytest

from ledgerline.money import format_amount, parse_amount
from ledgerline.errors import AppError


class TestAmountParsing:
    @pytest.mark.parametrize(
        "text,minor",
        [
            ("0", 0),
            ("1", 100),
            ("1.5", 150),
            ("1.05", 105),
            ("12.34", 1234),
            ("0.01", 1),
            ("100000.00", 10_000_000),
            ("999999999999999.99", 99_999_999_999_999_999),
        ],
    )
    def test_valid(self, text: str, minor: int) -> None:
        assert parse_amount(text) == minor

    @pytest.mark.parametrize(
        "text",
        [
            "",
            " 1.00",
            "1.00 ",
            "-1.00",
            "+1.00",
            "1.001",
            "1e5",
            "1,000.00",
            ".50",
            "1.",
            "abc",
            "1.2.3",
            "0x10",
            "١٢٣",  # non-ASCII digits must not slip through
        ],
    )
    def test_invalid(self, text: str) -> None:
        with pytest.raises(AppError) as exc:
            parse_amount(text)
        assert exc.value.status_code == 422

    def test_non_string_rejected(self) -> None:
        with pytest.raises(AppError):
            parse_amount(12.34)  # type: ignore[arg-type]

    @pytest.mark.parametrize("minor", [0, 1, 99, 100, 12345, 99_999_999])
    def test_round_trip(self, minor: int) -> None:
        assert parse_amount(format_amount(minor)) == minor

    def test_negative_formatting_is_internal_only(self) -> None:
        # Negative amounts exist inside the ledger (debit legs, the clearing
        # account) but are never accepted from a client.
        assert format_amount(-12345) == "-123.45"
        with pytest.raises(AppError):
            parse_amount("-123.45")


class TestLedgerBehaviour:
    def test_deposit_creates_balanced_double_entry(self, api, admin, alice):
        account = api.open_account(admin, "alice@example.com")
        response = api.deposit(alice, account["id"], "250.00", description="paycheck")
        assert response.status_code == 201, response.text
        body = response.json()

        entries = body["transaction"]["entries"]
        assert len(entries) == 2
        assert sum(entry["signed_amount_minor"] for entry in entries) == 0
        assert body["account"]["balance"] == "250.00"
        assert response.headers["Idempotent-Replay"] == "false"
        assert response.headers["Location"].endswith(body["transaction"]["id"])

    def test_withdrawal_moves_money_out(self, api, admin, alice):
        account = api.open_account(admin, "alice@example.com")
        api.deposit(alice, account["id"], "100.00")
        response = api.withdraw(alice, account["id"], "40.00")
        assert response.status_code == 201, response.text
        assert response.json()["account"]["balance"] == "60.00"

    def test_transfer_between_customers(self, api, admin, alice, bob):
        alice_account = api.open_account(admin, "alice@example.com", name="Alice main")
        bob_account = api.open_account(admin, "bob@example.com", name="Bob main")
        api.deposit(alice, alice_account["id"], "500.00")

        response = api.transfer(
            alice,
            from_account_id=alice_account["id"],
            to_account_number=bob_account["account_number"],
            amount="125.25",
        )
        assert response.status_code == 201, response.text
        assert response.json()["from_account"]["balance"] == "374.75"
        assert api.balance(bob, bob_account["id"]) == "125.25"

    def test_transfer_is_atomic_when_destination_is_frozen(self, api, admin, alice, bob):
        alice_account = api.open_account(admin, "alice@example.com")
        bob_account = api.open_account(admin, "bob@example.com")
        api.deposit(alice, alice_account["id"], "500.00")
        freeze = api.client.post(
            f"/api/accounts/{bob_account['id']}/freeze", json={"frozen": True}, headers=admin
        )
        assert freeze.status_code == 200, freeze.text

        response = api.transfer(
            alice,
            from_account_id=alice_account["id"],
            to_account_number=bob_account["account_number"],
            amount="100.00",
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DESTINATION_NOT_ACTIVE"
        # Nothing moved: the debit leg was rolled back with the credit leg.
        assert api.balance(alice, alice_account["id"]) == "500.00"

    def test_insufficient_funds_is_a_409_and_balance_is_unchanged(self, api, admin, alice):
        account = api.open_account(admin, "alice@example.com")
        api.deposit(alice, account["id"], "50.00")
        response = api.withdraw(alice, account["id"], "50.01")
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "INSUFFICIENT_FUNDS"
        assert error["details"] == {"available": "50.00", "requested": "50.01"}
        assert api.balance(alice, account["id"]) == "50.00"

    def test_exact_balance_withdrawal_is_allowed(self, api, admin, alice):
        account = api.open_account(admin, "alice@example.com")
        api.deposit(alice, account["id"], "50.00")
        response = api.withdraw(alice, account["id"], "50.00")
        assert response.status_code == 201, response.text
        assert response.json()["account"]["balance"] == "0.00"

    def test_frozen_source_account_cannot_move_money(self, api, admin, alice):
        account = api.open_account(admin, "alice@example.com")
        api.deposit(alice, account["id"], "100.00")
        api.client.post(f"/api/accounts/{account['id']}/freeze", json={"frozen": True}, headers=admin)
        response = api.withdraw(alice, account["id"], "10.00")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ACCOUNT_NOT_ACTIVE"

    def test_statement_running_balance(self, api, admin, alice):
        account = api.open_account(admin, "alice@example.com")
        api.deposit(alice, account["id"], "1000.00")
        api.withdraw(alice, account["id"], "150.00")
        api.deposit(alice, account["id"], "25.50")

        statement = api.client.get(f"/api/accounts/{account['id']}/statement", headers=alice).json()
        rows = list(reversed(statement["entries"]))
        assert [row["balance_after"] for row in rows] == ["1000.00", "850.00", "875.50"]
        assert [row["direction"] for row in rows] == ["CREDIT", "DEBIT", "CREDIT"]
        assert statement["account"]["balance"] == "875.50"

    def test_statement_pagination_cursor(self, api, admin, alice):
        account = api.open_account(admin, "alice@example.com")
        for _ in range(5):
            api.deposit(alice, account["id"], "10.00")
        first = api.client.get(
            f"/api/accounts/{account['id']}/statement?limit=2", headers=alice
        ).json()
        assert len(first["entries"]) == 2
        second = api.client.get(
            f"/api/accounts/{account['id']}/statement?limit=2&before_id={first['next_cursor']}",
            headers=alice,
        ).json()
        ids_first = {entry["entry_id"] for entry in first["entries"]}
        assert not ids_first & {entry["entry_id"] for entry in second["entries"]}
        # The running balance is computed over the whole account, not the page.
        assert second["entries"][0]["balance_after"] == "30.00"

    @pytest.mark.parametrize("amount", ["0", "0.00", "100000.01", "100001.00"])
    def test_limits(self, api, admin, alice, amount):
        account = api.open_account(admin, "alice@example.com")
        response = api.deposit(alice, account["id"], amount)
        assert response.status_code == 422, response.text

    @pytest.mark.parametrize("amount", ["1.001", "-5.00", "1e3", "fifty", ""])
    def test_invalid_amounts_are_422(self, api, admin, alice, amount):
        account = api.open_account(admin, "alice@example.com")
        response = api.deposit(alice, account["id"], amount)
        assert response.status_code == 422, response.text

    def test_transfer_to_self_is_rejected(self, api, admin, alice):
        account = api.open_account(admin, "alice@example.com")
        api.deposit(alice, account["id"], "100.00")
        response = api.transfer(
            alice,
            from_account_id=account["id"],
            to_account_number=account["account_number"],
            amount="10.00",
        )
        assert response.status_code == 422

    def test_transfer_to_system_account_is_rejected(self, api, admin, alice):
        account = api.open_account(admin, "alice@example.com")
        api.deposit(alice, account["id"], "100.00")
        response = api.transfer(
            alice,
            from_account_id=account["id"],
            to_account_number="SYS-CASH-USD",
            amount="10.00",
        )
        assert response.status_code == 422
        assert api.balance(alice, account["id"]) == "100.00"

    def test_cross_currency_transfer_is_rejected(self, api, admin, alice, bob):
        usd = api.open_account(admin, "alice@example.com", currency="USD")
        eur = api.open_account(admin, "bob@example.com", currency="EUR")
        api.deposit(alice, usd["id"], "100.00")
        response = api.transfer(
            alice,
            from_account_id=usd["id"],
            to_account_number=eur["account_number"],
            amount="10.00",
        )
        assert response.status_code == 422
        assert "currency" in response.json()["error"]["message"].lower()

    def test_unknown_destination_is_404(self, api, admin, alice):
        account = api.open_account(admin, "alice@example.com")
        api.deposit(alice, account["id"], "100.00")
        response = api.transfer(
            alice,
            from_account_id=account["id"],
            to_account_number="409999999999",
            amount="10.00",
        )
        assert response.status_code == 404

    def test_every_transaction_is_balanced_and_conserved(self, api, admin, alice, bob, auditor):
        alice_account = api.open_account(admin, "alice@example.com", name="Alice main")
        bob_account = api.open_account(admin, "bob@example.com", name="Bob main")
        api.deposit(alice, alice_account["id"], "1000.00")
        api.withdraw(alice, alice_account["id"], "300.00")
        api.transfer(
            alice,
            from_account_id=alice_account["id"],
            to_account_number=bob_account["account_number"],
            amount="200.00",
        )
        report = api.integrity(auditor)
        assert report["ok"] is True
        assert {check["name"] for check in report["checks"]} >= {
            "balances_match_entries",
            "transactions_balanced",
            "money_is_conserved",
            "ledger_hash_chain",
            "audit_hash_chain",
        }
        assert report["totals"]["customer_funds"] == "700.00"
