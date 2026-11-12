"""The release step, tested the way a platform uses it.

A release hook runs before the API starts, on every deploy, and its failure means
the deploy stops. So the two properties worth pinning are: it brings an empty
database to the expected schema, and it is safe to run again afterwards.
"""

from __future__ import annotations

import os

import pytest

from ledgerline.config import Settings
from ledgerline.db import make_database
from scripts import migrate as migrate_script


def test_migrates_an_empty_database(settings, monkeypatch, capsys):
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.delenv("LEDGERLINE_MIGRATION_DATABASE_URL", raising=False)

    assert migrate_script.main() == 0

    db = make_database(settings)
    assert db.schema_version() == "2", "the release step did not leave a usable schema"
    assert "done" in capsys.readouterr().out.lower()


def test_is_idempotent_on_the_second_run(settings, monkeypatch, capsys):
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.delenv("LEDGERLINE_MIGRATION_DATABASE_URL", raising=False)

    assert migrate_script.main() == 0
    capsys.readouterr()  # discard the first run's output

    assert migrate_script.main() == 0
    output = capsys.readouterr().out
    assert "nothing to do" in output
    # and it still reports the version, which is what an operator wants to see
    assert "2" in output


def test_does_not_wipe_an_existing_ledger(settings, monkeypatch):
    """Running the release step against a live database must not touch data."""
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.delenv("LEDGERLINE_MIGRATION_DATABASE_URL", raising=False)

    db = make_database(settings)
    db.initialize()
    with db.write() as conn:
        conn.execute(
            """
            INSERT INTO users (id, email, full_name, password_hash, role, status,
                               failed_login_count, created_at, updated_at)
            VALUES ('u-1', 'keep@example.com', 'Keep Me', 'x', 'CUSTOMER', 'ACTIVE', 0,
                    '2026-01-01T00:00:00.000000Z', '2026-01-01T00:00:00.000000Z')
            """
        )

    assert migrate_script.main() == 0

    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE email = 'keep@example.com'").fetchone()[0] == 1


def test_refuses_when_there_is_no_target(monkeypatch, capsys):
    """A release hook must fail loudly rather than start the API against nothing."""
    empty = Settings(env="prod", jwt_secret="x" * 48)  # no database_url
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: empty))
    monkeypatch.delenv("LEDGERLINE_DATABASE_URL", raising=False)
    monkeypatch.delenv("LEDGERLINE_MIGRATION_DATABASE_URL", raising=False)

    assert migrate_script.main() == 1
    assert "LEDGERLINE_DATABASE_URL" in capsys.readouterr().err


@pytest.mark.skipif(
    not os.environ.get("LEDGERLINE_TEST_DATABASE_URL"),
    reason="needs a PostgreSQL server to exercise the migrator-credential override",
)
def test_migration_url_takes_precedence(settings, monkeypatch):
    """A deployment can run DDL as the migrator while the API runs as the app role."""
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    # An unreachable migrator URL must be used instead of the working app URL,
    # which is only observable as a failure to connect.
    monkeypatch.setenv("LEDGERLINE_MIGRATION_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/nope")
    assert migrate_script.main() == 1
