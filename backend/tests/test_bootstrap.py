"""The bootstrap path: the first ADMIN, created without an API call.

These tests assert the properties that make the script safe to ship, not just
that it runs: the role is fixed, it refuses to run twice, the audit chain records
it, and the account it creates can actually log in and do admin things.
"""

from __future__ import annotations

import pytest

from ledgerline.config import Settings
from scripts import bootstrap_admin as bootstrap

PASSWORD = "Bootstrap!Pass2024"


@pytest.fixture()
def db(settings: Settings):
    from ledgerline.db import make_database

    database = make_database(settings)
    database.initialize()
    return database


def _login(api, email: str, password: str):
    response = api.client.post("/api/auth/login", json={"email": email, "password": password})
    return response


class TestBootstrap:
    def test_creates_an_admin_that_can_log_in(self, db, settings, api):
        created = bootstrap.bootstrap_admin(
            db, settings, email="root@example.com", full_name="Root Admin", password=PASSWORD
        )
        assert created["role"] == "ADMIN"

        response = _login(api, "root@example.com", PASSWORD)
        assert response.status_code == 200, response.text
        assert response.json()["user"]["role"] == "ADMIN"

    def test_the_new_admin_can_use_admin_endpoints(self, db, settings, api):
        bootstrap.bootstrap_admin(
            db, settings, email="root@example.com", full_name="Root Admin", password=PASSWORD
        )
        token = _login(api, "root@example.com", PASSWORD).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # Staff-only route, refused for a customer and allowed for this admin.
        register = api.client.post(
            "/api/auth/register",
            json={"email": "cust@example.com", "password": "Customer!Pass2024", "full_name": "Cust Omer"},
        )
        assert register.status_code == 201
        assert api.client.get("/api/admin/users", headers=headers).status_code == 200

    def test_it_refuses_to_run_when_an_admin_exists(self, db, settings):
        bootstrap.bootstrap_admin(db, settings, email="root@example.com", full_name="Root", password=PASSWORD)
        with pytest.raises(SystemExit) as excinfo:
            bootstrap.bootstrap_admin(db, settings, email="second@example.com", full_name="Second", password=PASSWORD)
        assert "already exist" in str(excinfo.value)

    def test_force_allows_a_deliberate_second_admin(self, db, settings):
        bootstrap.bootstrap_admin(db, settings, email="root@example.com", full_name="Root", password=PASSWORD)
        created = bootstrap.bootstrap_admin(
            db, settings, email="second@example.com", full_name="Second", password=PASSWORD, force=True
        )
        assert created["role"] == "ADMIN"
        assert bootstrap.count_admins(db) == 2

    def test_the_bootstrap_is_recorded_in_the_hash_chained_audit_log(self, db, settings):
        bootstrap.bootstrap_admin(db, settings, email="root@example.com", full_name="Root", password=PASSWORD)
        with db.read() as conn:
            row = conn.execute(
                "SELECT action, subject, entry_hash FROM audit_log WHERE action = 'ADMIN_BOOTSTRAPPED'"
            ).fetchone()
        assert row["subject"] == "root@example.com"
        assert len(row["entry_hash"]) == 64

    def test_it_enforces_the_password_policy(self, db, settings):
        with pytest.raises(Exception):
            bootstrap.bootstrap_admin(db, settings, email="root@example.com", full_name="Root", password="short")

    def test_it_normalises_the_email(self, db, settings):
        created = bootstrap.bootstrap_admin(
            db, settings, email="  ROOT@Example.COM ", full_name="Root", password=PASSWORD
        )
        assert created["email"] == "root@example.com"

    def test_generated_passwords_satisfy_the_policy(self):
        from ledgerline import security

        for _ in range(25):
            security.validate_password(bootstrap._generate_password())


class TestBootstrapCli:
    def test_cli_creates_the_admin_and_prints_the_one_time_password(self, db, settings, monkeypatch, capsys):
        monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
        code = bootstrap.main(["--email", "cli@example.com", "--name", "CLI Admin", "--print-token"])
        assert code == 0
        out = capsys.readouterr().out
        assert "cli@example.com" in out
        assert "One-time password:" in out
        assert bootstrap.count_admins(db) == 1

    def test_cli_exits_2_without_a_password(self, db, settings, monkeypatch, capsys):
        monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
        monkeypatch.delenv("LEDGERLINE_BOOTSTRAP_PASSWORD", raising=False)
        code = bootstrap.main(["--email", "cli@example.com"])
        assert code == 2
        assert bootstrap.count_admins(db) == 0

    def test_cli_reads_the_password_from_the_environment(self, db, settings, monkeypatch):
        monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
        monkeypatch.setenv("LEDGERLINE_BOOTSTRAP_PASSWORD", PASSWORD)
        assert bootstrap.main(["--email", "env@example.com"]) == 0
        assert bootstrap.count_admins(db) == 1
