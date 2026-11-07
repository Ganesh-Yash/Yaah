-- LedgerLine schema (SQLite dialect).
--
-- Design notes
-- ------------
-- * money is stored as INTEGER minor units; there is no REAL/FLOAT column
-- * every money movement writes >= 2 ledger_entries that sum to zero
-- * ledger_entries and audit_log are append-only (enforced by triggers)
-- * accounts.balance_minor is a *cache* of SUM(ledger_entries.amount_minor);
--   /api/auditor/integrity recomputes it from scratch and compares
--
-- The schema is intentionally Postgres-portable in shape: TEXT ids, INTEGER
-- money, TIMESTAMP-as-TEXT (swap for TIMESTAMPTZ), and one sequence-ish table
-- (chain_state) instead of AUTOINCREMENT for the hash-chained tables.

CREATE TABLE IF NOT EXISTS users (
    id                 TEXT PRIMARY KEY,
    email              TEXT NOT NULL UNIQUE,
    full_name          TEXT NOT NULL,
    password_hash      TEXT NOT NULL,
    role               TEXT NOT NULL CHECK (role IN ('ADMIN', 'CUSTOMER', 'AUDITOR')),
    status             TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'LOCKED', 'DISABLED')),
    failed_login_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_login_count >= 0),
    locked_until       TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

-- A session is one sign-in (one refresh-token family). Access tokens carry its
-- id, so logging out revokes the access token immediately instead of leaving it
-- valid until it expires.
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    revoked_at   TEXT,
    created_ip   TEXT,
    user_agent   TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id             TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL REFERENCES users(id),
    family_id      TEXT NOT NULL,
    token_hash     TEXT NOT NULL UNIQUE,
    issued_at      TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    used_at        TEXT,
    revoked_at     TEXT,
    replaced_by_id TEXT,
    created_ip     TEXT
);

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user ON refresh_tokens (user_id);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_family ON refresh_tokens (family_id);

CREATE TABLE IF NOT EXISTS accounts (
    id            TEXT PRIMARY KEY,
    account_number TEXT NOT NULL UNIQUE,
    owner_user_id TEXT REFERENCES users(id),
    name          TEXT NOT NULL,
    currency      TEXT NOT NULL CHECK (length(currency) = 3),
    balance_minor INTEGER NOT NULL DEFAULT 0,
    is_system     INTEGER NOT NULL DEFAULT 0 CHECK (is_system IN (0, 1)),
    status        TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'FROZEN', 'CLOSED')),
    version       INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at    TEXT NOT NULL,
    -- Structural guarantees, not just application checks:
    --   * a system account has no owner
    --   * a customer account can never be negative, even if a future code
    --     path forgets to check the balance
    CHECK (is_system = 0 OR owner_user_id IS NULL),
    CHECK (is_system = 1 OR balance_minor >= 0)
);

CREATE INDEX IF NOT EXISTS idx_accounts_owner ON accounts (owner_user_id);

CREATE TABLE IF NOT EXISTS transactions (
    id                TEXT PRIMARY KEY,
    type              TEXT NOT NULL CHECK (type IN ('DEPOSIT', 'WITHDRAWAL', 'TRANSFER')),
    currency          TEXT NOT NULL,
    amount_minor      INTEGER NOT NULL CHECK (amount_minor > 0),
    initiator_user_id TEXT REFERENCES users(id),
    description       TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id             INTEGER PRIMARY KEY CHECK (id > 0),
    transaction_id TEXT NOT NULL REFERENCES transactions(id),
    account_id     TEXT NOT NULL REFERENCES accounts(id),
    amount_minor   INTEGER NOT NULL CHECK (amount_minor <> 0),
    currency       TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    prev_hash      TEXT NOT NULL,
    entry_hash     TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_ledger_transaction ON ledger_entries (transaction_id);
CREATE INDEX IF NOT EXISTS idx_ledger_account ON ledger_entries (account_id, id);

-- Append-only enforcement. Application code cannot UPDATE or DELETE history;
-- even a compromised query path or a careless future feature cannot.
CREATE TRIGGER IF NOT EXISTS trg_ledger_no_update
BEFORE UPDATE ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger_entries is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_ledger_no_delete
BEFORE DELETE ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger_entries is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_transactions_no_update
BEFORE UPDATE ON transactions
BEGIN
    SELECT RAISE(ABORT, 'transactions is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_transactions_no_delete
BEFORE DELETE ON transactions
BEGIN
    SELECT RAISE(ABORT, 'transactions is append-only');
END;

-- Idempotency records are written in the SAME transaction as the money
-- movement they describe, so there is no window in which money moved but the
-- key was not recorded (or vice versa).
CREATE TABLE IF NOT EXISTS idempotency_keys (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    scope           TEXT NOT NULL,
    key             TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    response_status INTEGER NOT NULL,
    response_body   TEXT NOT NULL,
    transaction_id  TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (user_id, scope, key)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY CHECK (id > 0),
    actor_user_id TEXT,
    actor_role    TEXT,
    subject       TEXT,
    action        TEXT NOT NULL,
    outcome       TEXT NOT NULL CHECK (outcome IN ('ALLOW', 'DENY', 'ERROR')),
    target_type   TEXT,
    target_id     TEXT,
    ip            TEXT,
    user_agent    TEXT,
    request_id    TEXT,
    detail        TEXT,
    created_at    TEXT NOT NULL,
    prev_hash     TEXT NOT NULL,
    entry_hash    TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log (created_at);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log (action, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_subject ON audit_log (subject, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_ip ON audit_log (ip, created_at);

CREATE TRIGGER IF NOT EXISTS trg_audit_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

-- Head pointer for each hash chain. Keeping it here makes appends O(1) and
-- makes a "chain head changed underneath me" bug immediately visible.
CREATE TABLE IF NOT EXISTS chain_state (
    name      TEXT PRIMARY KEY,
    last_id   INTEGER NOT NULL CHECK (last_id >= 0),
    last_hash TEXT NOT NULL
);

INSERT OR IGNORE INTO chain_state (name, last_id, last_hash) VALUES
    ('ledger', 0, '0000000000000000000000000000000000000000000000000000000000000000'),
    ('audit',  0, '0000000000000000000000000000000000000000000000000000000000000000');

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('version', '1');
