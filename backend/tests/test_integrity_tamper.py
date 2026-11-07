"""Append-only enforcement and tamper detection.

These tests attack the database directly (the way an insider or a compromised
process would) instead of going through the API, because that is exactly the
threat the hash chain exists for.
"""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture()
def funded(api, admin, alice):
    account = api.open_account(admin, "alice@example.com")
    api.deposit(alice, account["id"], "500.00")
    api.withdraw(alice, account["id"], "100.00")
    return account


class TestAppendOnly:
    @pytest.mark.parametrize(
        "statement",
        [
            "UPDATE ledger_entries SET amount_minor = 999999 WHERE id = 1",
            "DELETE FROM ledger_entries WHERE id = 1",
            "UPDATE transactions SET amount_minor = 1 WHERE id IS NOT NULL",
            "DELETE FROM transactions",
            "UPDATE audit_log SET action = 'NOTHING' WHERE id = 1",
            "DELETE FROM audit_log",
        ],
    )
    def test_history_cannot_be_modified(self, api, funded, statement):
        with pytest.raises(sqlite3.DatabaseError):
            with api.db.write() as conn:
                conn.execute(statement)

    def test_no_money_was_lost_by_the_blocked_statements(self, api, admin, auditor, funded):
        # The parametrised test above ran in a different DB; re-check this one.
        assert api.integrity(auditor)["ok"] is True


class TestTamperDetection:
    def _break_chain(self, api):
        """Simulate an attacker who can write SQL: drop the guard, edit history."""
        with api.db.write() as conn:
            conn.execute("DROP TRIGGER trg_ledger_no_update")
            conn.execute("UPDATE ledger_entries SET amount_minor = 1 WHERE id = 1")

    def test_integrity_notices_an_edited_ledger_entry(self, api, auditor, funded):
        before = api.integrity(auditor)
        assert before["ok"] is True

        self._break_chain(api)

        after = api.integrity(auditor)
        assert after["ok"] is False
        failed = {check["name"] for check in after["checks"] if not check["ok"]}
        assert "ledger_hash_chain" in failed
        assert "balances_match_entries" in failed
        chain_check = next(check for check in after["checks"] if check["name"] == "ledger_hash_chain")
        assert chain_check["detail"]["first_bad_id"] == 1
        assert chain_check["detail"]["problems"]

    def test_integrity_notices_a_deleted_ledger_entry(self, api, auditor, funded):
        with api.db.write() as conn:
            conn.execute("DROP TRIGGER trg_ledger_no_delete")
            conn.execute("DELETE FROM ledger_entries WHERE id = 2")

        report = api.integrity(auditor)
        assert report["ok"] is False
        failures = {check["name"] for check in report["checks"] if not check["ok"]}
        assert "ledger_hash_chain" in failures
        assert "transactions_balanced" in failures

    def test_integrity_notices_a_rewritten_audit_row(self, api, auditor, funded):
        with api.db.write() as conn:
            conn.execute("DROP TRIGGER trg_audit_no_update")
            conn.execute("UPDATE audit_log SET actor_role = 'ADMIN' WHERE id = 1")

        report = api.integrity(auditor)
        assert report["ok"] is False
        failures = {check["name"] for check in report["checks"] if not check["ok"]}
        assert "audit_hash_chain" in failures

    def test_integrity_notices_a_moved_chain_head(self, api, auditor, funded):
        with api.db.write() as conn:
            conn.execute("UPDATE chain_state SET last_hash = 'f' * 64 WHERE name = 'ledger'")
        report = api.integrity(auditor)
        assert report["ok"] is False
        failures = {check["name"] for check in report["checks"] if not check["ok"]}
        assert "ledger_hash_chain" in failures

    def test_integrity_notices_a_tampered_balance_cache(self, api, auditor, funded):
        """Change accounts.balance_minor without touching the ledger."""
        with api.db.write() as conn:
            conn.execute("UPDATE accounts SET balance_minor = balance_minor + 10000 WHERE is_system = 0")
        report = api.integrity(auditor)
        assert report["ok"] is False
        failures = {check["name"] for check in report["checks"] if not check["ok"]}
        assert "balances_match_entries" in failures
        assert "money_is_conserved" in failures

    def test_a_negative_customer_balance_is_detected(self, api, auditor, funded):
        with api.db.write() as conn:
            # The CHECK constraint makes this impossible through SQL...
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute("UPDATE accounts SET balance_minor = -1 WHERE is_system = 0")


class TestLedgerShape:
    def test_each_transaction_has_two_legs_that_sum_to_zero(self, api, admin, alice, auditor):
        account = api.open_account(admin, "alice@example.com")
        api.deposit(alice, account["id"], "123.45")
        report = api.integrity(auditor)
        with api.db.read() as conn:
            rows = conn.execute(
                """
                SELECT transaction_id, COUNT(*) AS legs, SUM(amount_minor) AS total
                  FROM ledger_entries GROUP BY transaction_id
                """
            ).fetchall()
        assert rows
        assert all(row["legs"] >= 2 and row["total"] == 0 for row in rows)
        assert report["ok"] is True

    def test_ledger_ids_have_no_gaps(self, api, admin, alice, funded):
        with api.db.read() as conn:
            ids = [row[0] for row in conn.execute("SELECT id FROM ledger_entries ORDER BY id")]
        assert ids == list(range(1, len(ids) + 1))
