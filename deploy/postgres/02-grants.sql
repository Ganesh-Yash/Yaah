-- Run after `prisma migrate deploy`. Mirrors exactly what the application does:
-- see the query inventory in docs/POSTGRES.md ("what the app is allowed to do").
--
-- Read: every table.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO balancevault_app;

-- Write, table by table:
--   users, accounts, sessions, refresh_tokens, chain_state  -> INSERT + UPDATE
--   transactions, ledger_entries, audit_log, idempotency_keys -> INSERT only
--   refresh_tokens                                           -> DELETE (expired cleanup)
GRANT INSERT, UPDATE ON users, accounts, sessions, refresh_tokens, chain_state TO balancevault_app;
GRANT INSERT ON transactions, ledger_entries, audit_log, idempotency_keys TO balancevault_app;
GRANT DELETE ON refresh_tokens TO balancevault_app;

-- Identity columns need sequence access for INSERTs.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO balancevault_app;

-- Privileges granted by hand are removed: the file is the source of truth.
REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public FROM balancevault_app;
GRANT DELETE ON refresh_tokens TO balancevault_app;
REVOKE UPDATE, DELETE, TRUNCATE ON transactions, ledger_entries, audit_log, idempotency_keys FROM balancevault_app;
GRANT INSERT ON transactions, ledger_entries, audit_log, idempotency_keys TO balancevault_app;

-- Anything the migrator creates later (a new table) starts readable but not
-- writable for the app: fail closed, grant explicitly.
ALTER DEFAULT PRIVILEGES FOR ROLE balancevault_migrator IN SCHEMA public
    GRANT SELECT ON TABLES TO balancevault_app;
