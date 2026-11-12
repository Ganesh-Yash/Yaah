"""Database roles: the API must not be able to rewrite history.

This runs against the real PostgreSQL server. It builds a throwaway database,
applies the same files a deployment applies (01-roles.sql, then the Prisma
migration as the migrator role, then 02-grants.sql), and then checks what the
application role can and cannot do.

If these tests pass, the claim in DEPLOYMENT.md ("the API role has no UPDATE on
the ledger") is a fact about the database rather than a comment in a document.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

PG_URL = os.environ.get("LEDGERLINE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="needs LEDGERLINE_TEST_DATABASE_URL")

ROOT = Path(__file__).resolve().parents[2]
ROLES_SQL = ROOT / "deploy" / "postgres" / "01-roles.sql"
GRANTS_SQL = ROOT / "deploy" / "postgres" / "02-grants.sql"
MIGRATIONS = sorted((ROOT / "prisma" / "migrations").glob("*/migration.sql"))


def _superuser(dbname: str = "postgres"):
    return psycopg.connect(PG_URL, dbname=dbname, autocommit=True)


@pytest.fixture(scope="module")
def role_database():
    """A disposable database with the production role/privilege setup applied."""
    name = f"roles_{uuid.uuid4().hex[:10]}"
    with _superuser() as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    # 01-roles.sql runs against the deployment database: it changes ownership of
    # that database's public schema.
    with _superuser(name) as admin:
        admin.execute(ROLES_SQL.read_text())
    try:
        yield name
    finally:
        with _superuser() as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="module")
def deployed(role_database):
    """Apply the migration as the migrator, then the grants as the owner."""
    with psycopg.connect(PG_URL, dbname=role_database, autocommit=True) as conn:
        conn.execute("SET ROLE balancevault_migrator")
        for migration in MIGRATIONS:
            conn.execute(migration.read_text())
        conn.execute("RESET ROLE")
        conn.execute(GRANTS_SQL.read_text())
    return role_database


@pytest.fixture()
def app_role(deployed):
    """A connection acting as the API's role."""
    with psycopg.connect(PG_URL, dbname=deployed, autocommit=True) as conn:
        conn.execute("SET ROLE balancevault_app")
        yield conn


@pytest.fixture()
def migrator(deployed):
    with psycopg.connect(PG_URL, dbname=deployed, autocommit=True) as conn:
        conn.execute("SET ROLE balancevault_migrator")
        yield conn


class TestApplicationRole:
    def test_can_read_every_table(self, app_role):
        tables = [
            "users",
            "sessions",
            "refresh_tokens",
            "accounts",
            "transactions",
            "ledger_entries",
            "idempotency_keys",
            "audit_log",
            "chain_state",
            "schema_meta",
        ]
        for table in tables:
            app_role.execute(f"SELECT * FROM {table} LIMIT 1").fetchall()

    def test_can_write_what_the_api_writes(self, app_role, migrator):
        # A ledger entry, an audit row and a chain-state bump: the three writes a
        # deposit performs.
        migrator.execute(
            "INSERT INTO transactions (id, type, currency, amount_minor, created_at) "
            "VALUES ('tx-1', 'DEPOSIT', 'USD', 100, '2026-01-01T00:00:00.000000Z')"
        )
        migrator.execute(
            "INSERT INTO accounts (id, account_number, name, currency, balance_minor, created_at) "
            "VALUES ('acct-1', '100000000001', 'A', 'USD', 0, '2026-01-01T00:00:00.000000Z')"
        )
        app_role.execute(
            "INSERT INTO ledger_entries (transaction_id, account_id, amount_minor, currency, created_at, prev_hash, entry_hash) "
            "VALUES ('tx-1', 'acct-1', 100, 'USD', '2026-01-01T00:00:00.000000Z', 'a', 'b')"
        )
        app_role.execute("UPDATE accounts SET balance_minor = 100 WHERE id = 'acct-1'")
        app_role.execute("UPDATE chain_state SET last_id = 1, last_hash = 'c' WHERE name = 'ledger'")
        app_role.execute(
            "INSERT INTO audit_log (action, outcome, created_at, prev_hash, entry_hash) "
            "VALUES ('X', 'ALLOW', '2026-01-01T00:00:00.000000Z', 'd', 'e')"
        )

    @pytest.mark.parametrize(
        "statement",
        [
            "UPDATE ledger_entries SET amount_minor = 1",
            "DELETE FROM ledger_entries",
            "UPDATE audit_log SET action = 'NOTHING'",
            "DELETE FROM audit_log",
            "UPDATE transactions SET amount_minor = 1",
            "TRUNCATE ledger_entries",
        ],
    )
    def test_cannot_rewrite_history(self, app_role, migrator, statement):
        migrator.execute(
            "INSERT INTO transactions (id, type, currency, amount_minor, created_at) "
            "VALUES ('tx-9', 'DEPOSIT', 'USD', 100, '2026-01-01T00:00:00.000000Z') ON CONFLICT DO NOTHING"
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app_role.execute(statement)

    def test_cannot_change_the_schema(self, app_role):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app_role.execute("CREATE TABLE sneaky (id int)")

    def test_cannot_drop_a_table(self, app_role):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app_role.execute("DROP TABLE ledger_entries")


class TestMigratorRole:
    def test_can_create_and_drop_objects(self, migrator):
        migrator.execute("CREATE TABLE scratch (id int)")
        migrator.execute("DROP TABLE scratch")

    def test_owns_the_schema(self, migrator):
        owner = migrator.execute(
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public'"
        ).fetchone()[0]
        assert owner == "balancevault_migrator"


class TestPrivilegeCoverage:
    def test_the_app_can_reach_exactly_the_tables_the_api_uses(self, app_role):
        """Every table the code touches must be readable; nothing else is granted."""
        rows = app_role.execute(
            """
            SELECT table_name, string_agg(privilege_type, ',' ORDER BY privilege_type)
              FROM information_schema.role_table_grants
             WHERE grantee = 'balancevault_app'
             GROUP BY table_name ORDER BY table_name
            """
        ).fetchall()
        granted = {row[0]: set(row[1].split(",")) for row in rows}
        assert "ledger_entries" in granted
        assert granted["ledger_entries"] == {"INSERT", "SELECT"}
        assert granted["audit_log"] == {"INSERT", "SELECT"}
        for table in ("users", "accounts", "sessions", "refresh_tokens", "chain_state"):
            assert "UPDATE" in granted[table], table
        # Nothing in the application needs DELETE except expired-token cleanup.
        deletable = {table for table, privs in granted.items() if "DELETE" in privs}
        assert deletable == {"refresh_tokens"}
