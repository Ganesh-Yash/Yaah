"""Schema parity: Prisma models, the Postgres migration, and SQLite must agree.

`prisma validate` needs the Prisma engine binaries, which this environment
cannot download. This test is the offline substitute that actually matters for
correctness: it reads the three artefacts and asserts they describe the same
tables with the same columns.

It fails if someone adds a column to schema.prisma and forgets the migration, or
if the two engines drift apart — the second failure mode is the dangerous one,
because the test suite would stay green while production quietly differed.
"""

from __future__ import annotations

import re
from pathlib import Path

SCALARS = {"String", "Int", "BigInt", "Boolean", "DateTime", "Float", "Decimal", "Json", "Bytes"}

ROOT = Path(__file__).resolve().parents[2]
PRISMA_SCHEMA = ROOT / "prisma" / "schema.prisma"
MIGRATION = sorted((ROOT / "prisma" / "migrations").glob("*/migration.sql"))[-1]
SQLITE_SCHEMA = ROOT / "backend" / "ledgerline" / "schema.sql"


def parse_prisma(path: Path) -> dict[str, set[str]]:
    """{table_name: {column names}} from the Prisma schema."""
    text = path.read_text()
    models: dict[str, set[str]] = {}
    for block in re.finditer(r"^model\s+(\w+)\s*\{(.*?)^\}", text, re.S | re.M):
        body = block.group(2)
        table = re.search(r"@@map\(\"(\w+)\"\)", body)
        name = table.group(1) if table else block.group(1)
        columns: set[str] = set()
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("//") or line.startswith("@@"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            field = parts[0]
            # Relation fields are not columns: their type is a model name, not
            # one of Prisma's scalar types.
            declared = parts[1].rstrip("?[]")
            if parts[1].endswith("[]") or declared not in SCALARS:
                continue
            mapped = re.search(r"@map\(\"(\w+)\"\)", line)
            columns.add(mapped.group(1) if mapped else field)
        models[name] = columns
    return models


def parse_postgres(path: Path) -> dict[str, set[str]]:
    text = path.read_text()
    tables: dict[str, set[str]] = {}
    for match in re.finditer(r"CREATE TABLE (\w+) \((.*?)\n\);", text, re.S):
        name = match.group(1)
        columns = set()
        for line in match.group(2).splitlines():
            line = line.strip()
            if not line or line.startswith("--") or line.startswith("CHECK") or line.startswith("UNIQUE"):
                continue
            first = line.split()[0]
            if re.fullmatch(r"[a-z_]+", first):
                columns.add(first)
        tables[name] = columns
    return tables


def parse_sqlite(path: Path) -> dict[str, set[str]]:
    text = path.read_text()
    tables: dict[str, set[str]] = {}
    for match in re.finditer(
        r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", text, re.S
    ):
        name = match.group(1)
        columns = set()
        for line in match.group(2).splitlines():
            line = line.strip()
            if not line or line.startswith("--") or line.startswith("CHECK") or line.startswith("UNIQUE") or line.startswith("FOREIGN"):
                continue
            first = line.split()[0]
            if re.fullmatch(r"[a-z_]+", first):
                columns.add(first)
        tables[name] = columns
    return tables


PRISMA = parse_prisma(PRISMA_SCHEMA)
POSTGRES = parse_postgres(MIGRATION)
SQLITE = parse_sqlite(SQLITE_SCHEMA)


class TestPrismaMatchesPostgres:
    def test_every_postgres_table_has_a_model(self):
        assert set(POSTGRES) <= set(PRISMA), set(POSTGRES) - set(PRISMA)

    def test_every_model_has_a_postgres_table(self):
        assert set(PRISMA) <= set(POSTGRES), set(PRISMA) - set(POSTGRES)

    def test_columns_match(self):
        mismatched = {t: (PRISMA[t], POSTGRES[t]) for t in PRISMA if t in POSTGRES and PRISMA[t] != POSTGRES[t]}
        assert not mismatched, mismatched


class TestEnginesAgree:
    def test_same_tables_on_both_engines(self):
        # chain_state and schema_meta exist on both; no table may be missing.
        assert set(SQLITE) == set(POSTGRES), {
            "sqlite_only": set(SQLITE) - set(POSTGRES),
            "postgres_only": set(POSTGRES) - set(SQLITE),
        }

    def test_same_columns_on_both_engines(self):
        mismatched = {t: (SQLITE[t], POSTGRES[t]) for t in SQLITE if t in POSTGRES and SQLITE[t] != POSTGRES[t]}
        assert not mismatched, mismatched

    def test_append_only_tables_are_guarded_on_both_engines(self):
        postgres_text = MIGRATION.read_text()
        sqlite_text = SQLITE_SCHEMA.read_text()
        for table in ("ledger_entries", "audit_log", "transactions"):
            assert f"ON {table}" in postgres_text, f"no Postgres trigger on {table}"
            assert f"ON {table}" in sqlite_text, f"no SQLite trigger on {table}"

    def test_money_is_integer_minor_units_on_both_engines(self):
        assert "balance_minor  BIGINT NOT NULL" in MIGRATION.read_text()
        # No floats anywhere near money: floats cannot represent 0.10.
        for schema in (MIGRATION.read_text(), SQLITE_SCHEMA.read_text()):
            lowered = schema.lower()
            for forbidden in ("real", "double precision", "float"):
                assert f" {forbidden} " not in lowered, forbidden

    def test_both_engines_seed_a_chain_head(self):
        for text in (MIGRATION.read_text(), SQLITE_SCHEMA.read_text()):
            assert "chain_state" in text
            assert "0000000000000000000000000000000000000000000000000000000000000000" in text
