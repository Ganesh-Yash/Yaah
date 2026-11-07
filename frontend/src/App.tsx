import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiClient, User } from "./api";
import AccountsView from "./views/AccountsView";
import AdminView from "./views/AdminView";
import AuditView from "./views/AuditView";
import AuthView from "./views/AuthView";
import { Badge, Loading } from "./components/ui";

type Tab = "accounts" | "admin" | "audit";

export default function App() {
  const api = useMemo(() => new ApiClient(), []);
  const [user, setUser] = useState<User | null>(null);
  const [booting, setBooting] = useState(true);
  const [tab, setTab] = useState<Tab>("accounts");

  useEffect(() => {
    api.onSessionLost = () => setUser(null);
    let cancelled = false;
    (async () => {
      if (api.hasStoredSession()) {
        try {
          const current = await api.me();
          if (!cancelled) setUser(current);
        } catch {
          api.clearSession();
        }
      }
      if (!cancelled) setBooting(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [api]);

  const signOut = useCallback(async () => {
    await api.logout();
    setUser(null);
    setTab("accounts");
  }, [api]);

  if (booting) {
    return (
      <div className="boot">
        <Loading label="Starting LedgerLine…" />
      </div>
    );
  }

  if (!user) return <AuthView api={api} onSignedIn={(signedIn) => setUser(signedIn)} />;

  const tabs: Array<{ id: Tab; label: string }> =
    user.role === "CUSTOMER"
      ? [{ id: "accounts", label: "My accounts" }]
      : user.role === "ADMIN"
        ? [
            { id: "accounts", label: "Accounts" },
            { id: "admin", label: "Administration" },
            { id: "audit", label: "Audit & integrity" },
          ]
        : [
            { id: "accounts", label: "All accounts" },
            { id: "audit", label: "Audit & integrity" },
          ];

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand brand--small">
          <span className="brand__mark">◧</span>
          <div>
            <h1>LedgerLine</h1>
            <p className="tiny muted">
              {user.role === "CUSTOMER"
                ? "Move money, see the ledger"
                : user.role === "ADMIN"
                  ? "Operate accounts and users"
                  : "Verify the books, read the trail"}
            </p>
          </div>
        </div>
        <div className="topbar__right">
          <span className="tiny muted">{user.email}</span>
          <Badge tone={user.role === "ADMIN" ? "info" : user.role === "AUDITOR" ? "warn" : "neutral"}>
            {user.role}
          </Badge>
          <button className="btn btn--ghost btn--sm" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </header>

      <nav className="tabs">
        {tabs.map((item) => (
          <button
            key={item.id}
            className={tab === item.id ? "tab tab--active" : "tab"}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      <main className="content">
        {tab === "accounts" && <AccountsView api={api} user={user} />}
        {tab === "admin" && user.role === "ADMIN" && <AdminView api={api} />}
        {tab === "audit" && user.role !== "CUSTOMER" && <AuditView api={api} />}
      </main>

      <footer className="footer tiny muted">
        LedgerLine · double-entry · idempotent money movement · hash-chained audit trail
      </footer>
    </div>
  );
}
