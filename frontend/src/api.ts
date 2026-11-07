/**
 * API client.
 *
 * Two behaviours are deliberate:
 *  1. Access tokens live in memory only; the refresh token is persisted so a
 *     page reload does not force a re-login. The CSRF/XSS trade-off is written
 *     up in THREATS.md.
 *  2. Money calls pass an Idempotency-Key chosen by the *form*, not by the
 *     client on every submit. A retry reuses the key, so a double click or a
 *     retried request can never pay twice.
 */

export type Role = "ADMIN" | "CUSTOMER" | "AUDITOR";

export interface User {
  id: string;
  email: string;
  full_name: string;
  role: Role;
  status: string;
}

export interface Account {
  id: string;
  account_number: string;
  name: string;
  currency: string;
  balance: string;
  balance_minor: number;
  status: string;
  is_system: boolean;
  version: number;
  created_at: string;
  owner_email?: string;
}

export interface LedgerEntry {
  id: number;
  account_id: string;
  account_number?: string;
  direction: "CREDIT" | "DEBIT";
  amount: string;
  amount_minor: number;
  signed_amount_minor?: number;
  currency?: string;
}

export interface Transaction {
  id: string;
  type: "DEPOSIT" | "WITHDRAWAL" | "TRANSFER";
  currency: string;
  amount: string;
  amount_minor: number;
  description: string | null;
  created_at: string;
  entries: LedgerEntry[];
}

export interface StatementRow {
  entry_id: number;
  transaction_id: string;
  type: string;
  direction: "CREDIT" | "DEBIT";
  amount: string;
  balance_after: string;
  description: string | null;
  created_at: string;
}

export interface Statement {
  account: Account;
  entries: StatementRow[];
  next_cursor: number | null;
}

export interface MoneyResult {
  transaction: Transaction;
  account: Account;
  from_account?: Account;
  to_account?: { account_number: string; name: string };
  replayed: boolean;
}

export interface AuditEntry {
  id: number;
  actor_user_id: string | null;
  actor_role: string | null;
  subject: string | null;
  action: string;
  outcome: "ALLOW" | "DENY" | "ERROR";
  target_type: string | null;
  target_id: string | null;
  ip: string | null;
  user_agent: string | null;
  request_id: string | null;
  detail: string | null;
  created_at: string;
  entry_hash: string;
}

export interface IntegrityCheck {
  name: string;
  description: string;
  ok: boolean;
  detail: Record<string, unknown>;
}

export interface IntegrityReport {
  ok: boolean;
  generated_at: string;
  checks: IntegrityCheck[];
  totals: {
    accounts: number;
    customer_funds: string;
    ledger_entries: number;
    transactions: number;
    audit_rows: number;
    ledger_head_hash: string | null;
    audit_head_hash: string | null;
  };
}

export class ApiError extends Error {
  code: string;
  status: number;
  details: Record<string, unknown>;
  requestId: string | null;

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> = {},
    requestId: string | null = null
  ) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
  }

  static async from(response: Response): Promise<ApiError> {
    let code = "HTTP_ERROR";
    let message = `Request failed (${response.status}).`;
    let details: Record<string, unknown> = {};
    let requestId: string | null = response.headers.get("X-Request-ID");
    try {
      const body = await response.json();
      if (body?.error) {
        code = body.error.code ?? code;
        message = body.error.message ?? message;
        details = body.error.details ?? {};
        requestId = body.error.request_id ?? requestId;
      }
    } catch {
      /* non-JSON error body */
    }
    return new ApiError(response.status, code, message, details, requestId);
  }
}

const REFRESH_TOKEN_KEY = "ledgerline.refresh_token";

export class ApiClient {
  private accessToken: string | null = null;
  private refreshToken: string | null = localStorage.getItem(REFRESH_TOKEN_KEY);
  onSessionLost: (() => void) | null = null;

  hasStoredSession(): boolean {
    return Boolean(this.refreshToken);
  }

  clearSession(): void {
    this.accessToken = null;
    this.refreshToken = null;
    localStorage.removeItem(REFRESH_TOKEN_KEY);
  }

  private storeTokens(access: string, refresh: string): void {
    this.accessToken = access;
    this.refreshToken = refresh;
    localStorage.setItem(REFRESH_TOKEN_KEY, refresh);
  }

  private async send(
    method: string,
    path: string,
    body: unknown,
    idempotencyKey?: string
  ): Promise<Response> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (this.accessToken) headers["Authorization"] = `Bearer ${this.accessToken}`;
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
    return fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "omit",
    });
  }

  private async refreshSession(): Promise<boolean> {
    if (!this.refreshToken) return false;
    const response = await fetch("/api/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: this.refreshToken }),
      credentials: "omit",
    });
    if (!response.ok) {
      this.clearSession();
      this.onSessionLost?.();
      return false;
    }
    const data = await response.json();
    this.storeTokens(data.access_token, data.refresh_token);
    return true;
  }

  private async request<T>(
    method: string,
    path: string,
    options: { body?: unknown; idempotencyKey?: string; retry?: boolean } = {}
  ): Promise<{ data: T; replayed: boolean }> {
    let response = await this.send(method, path, options.body, options.idempotencyKey);
    if (response.status === 401 && this.refreshToken && options.retry !== false) {
      const refreshed = await this.refreshSession();
      if (refreshed) {
        response = await this.send(method, path, options.body, options.idempotencyKey);
      }
    }
    if (!response.ok) throw await ApiError.from(response);
    const replayed = response.headers.get("Idempotent-Replay") === "true";
    if (response.status === 204) return { data: undefined as T, replayed };
    return { data: (await response.json()) as T, replayed };
  }

  // -- auth ------------------------------------------------------------

  async login(email: string, password: string): Promise<User> {
    const { data } = await this.request<{
      access_token: string;
      refresh_token: string;
      user: User;
    }>("POST", "/api/auth/login", { body: { email, password } });
    this.storeTokens(data.access_token, data.refresh_token);
    return data.user;
  }

  async register(email: string, password: string, fullName: string): Promise<void> {
    await this.request("POST", "/api/auth/register", {
      body: { email, password, full_name: fullName },
    });
  }

  async me(): Promise<User> {
    const { data } = await this.request<{ user: User }>("GET", "/api/auth/me");
    return data.user;
  }

  async logout(): Promise<void> {
    if (this.refreshToken) {
      try {
        await this.request("POST", "/api/auth/logout", {
          body: { refresh_token: this.refreshToken },
        });
      } catch {
        /* revoking locally is enough for the demo */
      }
    }
    this.clearSession();
  }

  // -- accounts --------------------------------------------------------

  async listAccounts(): Promise<Account[]> {
    const { data } = await this.request<{ accounts: Account[] }>("GET", "/api/accounts");
    return data.accounts;
  }

  async statement(accountId: string, beforeId?: number): Promise<Statement> {
    const query = beforeId ? `?limit=50&before_id=${beforeId}` : "?limit=50";
    const { data } = await this.request<Statement>(
      "GET",
      `/api/accounts/${encodeURIComponent(accountId)}/statement${query}`
    );
    return data;
  }

  async deposit(accountId: string, amount: string, key: string, description?: string) {
    return this.request<MoneyResult>("POST", `/api/accounts/${accountId}/deposit`, {
      body: { amount, description },
      idempotencyKey: key,
    });
  }

  async withdraw(accountId: string, amount: string, key: string, description?: string) {
    return this.request<MoneyResult>("POST", `/api/accounts/${accountId}/withdraw`, {
      body: { amount, description },
      idempotencyKey: key,
    });
  }

  async transfer(
    fromAccountId: string,
    toAccountNumber: string,
    amount: string,
    key: string,
    description?: string
  ) {
    return this.request<MoneyResult>("POST", "/api/transfers", {
      body: {
        from_account_id: fromAccountId,
        to_account_number: toAccountNumber,
        amount,
        description,
      },
      idempotencyKey: key,
    });
  }

  // -- admin -----------------------------------------------------------

  async createAccount(ownerEmail: string, currency: string, name: string): Promise<Account> {
    const { data } = await this.request<{ account: Account }>("POST", "/api/accounts", {
      body: { owner_email: ownerEmail, currency, name },
    });
    return data.account;
  }

  async setFrozen(accountId: string, frozen: boolean): Promise<Account> {
    const { data } = await this.request<{ account: Account }>(
      "POST",
      `/api/accounts/${accountId}/freeze`,
      { body: { frozen } }
    );
    return data.account;
  }

  async listUsers(): Promise<Array<User & { failed_login_count: number; locked_until: string | null }>> {
    const { data } = await this.request<{ users: Array<User & { failed_login_count: number; locked_until: string | null }> }>(
      "GET",
      "/api/admin/users"
    );
    return data.users;
  }

  async createStaff(email: string, password: string, fullName: string, role: "ADMIN" | "AUDITOR") {
    await this.request("POST", "/api/admin/users", {
      body: { email, password, full_name: fullName, role },
    });
  }

  // -- audit -----------------------------------------------------------

  async auditLog(action?: string, beforeId?: number): Promise<AuditEntry[]> {
    const params = new URLSearchParams({ limit: "100" });
    if (action) params.set("action", action);
    if (beforeId) params.set("before_id", String(beforeId));
    const { data } = await this.request<{ entries: AuditEntry[] }>(
      "GET",
      `/api/audit?${params.toString()}`
    );
    return data.entries;
  }

  async integrity(): Promise<IntegrityReport> {
    const { data } = await this.request<IntegrityReport>("GET", "/api/auditor/integrity");
    return data;
  }
}

export function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `key-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
}

/** Display-only formatting. Arithmetic never happens in the browser. */
export function money(amount: string, currency: string): string {
  const value = Number(amount);
  if (!Number.isFinite(value)) return `${currency} ${amount}`;
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency,
      minimumFractionDigits: 2,
    }).format(value);
  } catch {
    return `${currency} ${amount}`;
  }
}

export function timestamp(iso: string): string {
  return iso.replace("T", " ").replace("Z", " UTC").slice(0, 19) + " UTC";
}
