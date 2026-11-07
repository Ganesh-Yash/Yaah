import { FormEvent, useState } from "react";
import { ApiClient, ApiError, User } from "../api";
import { Alert, Button, Field } from "../components/ui";

export default function AuthView({
  api,
  onSignedIn,
}: {
  api: ApiClient;
  onSignedIn: (user: User) => void;
}) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (mode === "register") {
        await api.register(email.trim(), password, fullName.trim());
        setNotice("Account created. Sign in to continue.");
        setMode("login");
        setPassword("");
      } else {
        const user = await api.login(email.trim(), password);
        onSignedIn(user);
      }
    } catch (caught) {
      setError(caught instanceof ApiError ? caught : new ApiError(0, "NETWORK", "Network error."));
    } finally {
      setBusy(false);
    }
  }

  function fillDemo(role: "customer" | "admin" | "auditor") {
    const map = {
      customer: "alice@ledgerline.test",
      admin: "admin@ledgerline.test",
      auditor: "auditor@ledgerline.test",
    };
    setMode("login");
    setEmail(map[role]);
    setPassword("LedgerLine!Demo2024");
  }

  return (
    <div className="auth">
      <div className="auth__panel">
        <div className="brand">
          <span className="brand__mark">◧</span>
          <div>
            <h1>LedgerLine</h1>
            <p className="muted">Money movement with a ledger you can prove.</p>
          </div>
        </div>

        <div className="tabs tabs--inline">
          <button
            className={mode === "login" ? "tab tab--active" : "tab"}
            onClick={() => setMode("login")}
          >
            Sign in
          </button>
          <button
            className={mode === "register" ? "tab tab--active" : "tab"}
            onClick={() => setMode("register")}
          >
            Create account
          </button>
        </div>

        {notice && <Alert kind="info">{notice}</Alert>}
        {error && (
          <Alert>
            <strong>{error.code}</strong> — {error.message}
            {error.requestId && <div className="tiny muted">request {error.requestId}</div>}
          </Alert>
        )}

        <form onSubmit={submit} className="form">
          {mode === "register" && (
            <Field label="Full name">
              <input
                value={fullName}
                onChange={(event) => setFullName(event.target.value)}
                required
                minLength={2}
                autoComplete="name"
                placeholder="Ada Lovelace"
              />
            </Field>
          )}
          <Field label="Email">
            <input
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
              autoComplete="username"
              placeholder="you@example.com"
            />
          </Field>
          <Field
            label="Password"
            hint={mode === "register" ? "At least 12 characters. No upper-case dance required." : undefined}
          >
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
              autoComplete={mode === "register" ? "new-password" : "current-password"}
              placeholder="••••••••••••"
            />
          </Field>
          <Button type="submit" busy={busy}>
            {mode === "register" ? "Create account" : "Sign in"}
          </Button>
        </form>

        <div className="demo">
          <p className="tiny muted">Demo accounts (seeded, password <code>LedgerLine!Demo2024</code>)</p>
          <div className="demo__buttons">
            <button className="chip" onClick={() => fillDemo("customer")}>
              account holder
            </button>
            <button className="chip" onClick={() => fillDemo("admin")}>
              administrator
            </button>
            <button className="chip" onClick={() => fillDemo("auditor")}>
              auditor
            </button>
          </div>
        </div>
      </div>

      <aside className="auth__aside">
        <h2>What this system guarantees</h2>
        <ul>
          <li>
            <strong>Every movement balances.</strong> Two ledger legs, summing to zero, written in one
            transaction.
          </li>
          <li>
            <strong>No double pay.</strong> An <code>Idempotency-Key</code> per request; the key row is
            committed with the movement itself.
          </li>
          <li>
            <strong>No lost updates.</strong> The balance check and the balance write are a single
            conditional SQL statement under a write lock.
          </li>
          <li>
            <strong>History you can verify.</strong> Ledger and audit rows are hash-chained and
            append-only; the auditor view re-computes both chains.
          </li>
        </ul>
      </aside>
    </div>
  );
}
