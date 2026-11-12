-- BalanceVault: two roles, two jobs.
--
--   balancevault_migrator  owns the DDL. Used only by `prisma migrate deploy`
--                          and by the bootstrap step that creates the first
--                          admin. Credentials live in the migration job, never
--                          in the API pod.
--   balancevault_app       the API's role. Can read everything and write only
--                          the rows the API is designed to write. No CREATE, no
--                          ALTER, no DROP, and no UPDATE/DELETE on the
--                          append-only tables.
--
-- Running the API as the schema owner is the common shortcut, and it means a
-- SQL injection that reaches the database can rewrite the ledger or drop the
-- audit table. This file exists so that it cannot.
--
-- Run order: 01-roles.sql -> `prisma migrate deploy` -> 02-grants.sql.
-- (01 needs no tables; 02 grants per-table privileges, so it runs after DDL.)

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'balancevault_migrator') THEN
        CREATE ROLE balancevault_migrator LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'balancevault_app') THEN
        CREATE ROLE balancevault_app LOGIN;
    END IF;
END
$$;

-- The migrator owns the schema, so `prisma migrate deploy` can create tables
-- without handing CREATE to the API's role. Run this against the deployment
-- database: on a shared cluster, scope it to that database and leave other
-- schemas alone.
ALTER SCHEMA public OWNER TO balancevault_migrator;

-- The migrator may create schema objects; the app may only use them.
GRANT USAGE, CREATE ON SCHEMA public TO balancevault_migrator;
GRANT USAGE ON SCHEMA public TO balancevault_app;

-- Old privileges are revoked first so this file is safe to re-run after a role
-- has been granted something by hand: the file is the source of truth.
REVOKE CREATE ON SCHEMA public FROM balancevault_app;
