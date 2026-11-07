import { FormEvent, useCallback, useEffect, useState } from "react";
import { Account, ApiClient, ApiError, User } from "../api";
import { Alert, Badge, Button, Card, Field, Loading, Table } from "../components/ui";

type StaffUser = User & { failed_login_count: number; locked_until: string | null };

export default function AdminView({ api }: { api: ApiClient }) {
  const [users, setUsers] = useState<StaffUser[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [ownerEmail, setOwnerEmail] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [accountName, setAccountName] = useState("Main account");
  const [creatingAccount, setCreatingAccount] = useState(false);

  const [staffEmail, setStaffEmail] = useState("");
  const [staffName, setStaffName] = useState("");
  const [staffPassword, setStaffPassword] = useState("");
  const [staffRole, setStaffRole] = useState<"AUDITOR" | "ADMIN">("AUDITOR");
  const [creatingStaff, setCreatingStaff] = useState(false);

  const load = useCallback(async () => {
    try {
      setUsers(await api.listUsers());
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load users.");
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  async function createAccount(event: FormEvent) {
    event.preventDefault();
    setCreatingAccount(true);
    setError(null);
    setNotice(null);
    try {
      const account: Account = await api.createAccount(
        ownerEmail.trim(),
        currency,
        accountName.trim()
      );
      setNotice(`Account ${account.account_number} opened for ${account.owner_email}.`);
      setOwnerEmail("");
    } catch (caught) {
      setError(caught instanceof ApiError ? `${caught.code}: ${caught.message}` : "Failed.");
    } finally {
      setCreatingAccount(false);
    }
  }

  async function createStaff(event: FormEvent) {
    event.preventDefault();
    setCreatingStaff(true);
    setError(null);
    setNotice(null);
    try {
      await api.createStaff(staffEmail.trim(), staffPassword, staffName.trim(), staffRole);
      setNotice(`${staffRole.toLowerCase()} ${staffEmail} created.`);
      setStaffEmail("");
      setStaffName("");
      setStaffPassword("");
      await load();
    } catch (caught) {
      setError(caught instanceof ApiError ? `${caught.code}: ${caught.message}` : "Failed.");
    } finally {
      setCreatingStaff(false);
    }
  }

  if (users === null) return <Loading label="Loading administration…" />;

  return (
    <div className="stack">
      {error && <Alert>{error}</Alert>}
      {notice && <Alert kind="success">{notice}</Alert>}

      <div className="grid grid--two">
        <Card title="Open an account" subtitle="Accounts belong to CUSTOMER users only.">
          <form className="form" onSubmit={createAccount}>
            <Field label="Owner email">
              <input
                type="email"
                value={ownerEmail}
                onChange={(event) => setOwnerEmail(event.target.value)}
                required
                placeholder="alice@example.com"
              />
            </Field>
            <Field label="Account name">
              <input
                value={accountName}
                onChange={(event) => setAccountName(event.target.value)}
                required
                minLength={2}
                maxLength={80}
              />
            </Field>
            <Field label="Currency" hint="JPY and other non-2-decimal currencies are intentionally unsupported.">
              <select value={currency} onChange={(event) => setCurrency(event.target.value)}>
                {["USD", "EUR", "GBP", "INR"].map((code) => (
                  <option key={code}>{code}</option>
                ))}
              </select>
            </Field>
            <Button type="submit" busy={creatingAccount}>
              Open account
            </Button>
          </form>
        </Card>

        <Card title="Create a staff user" subtitle="Separation of duties: staff can never move money.">
          <form className="form" onSubmit={createStaff}>
            <Field label="Full name">
              <input
                value={staffName}
                onChange={(event) => setStaffName(event.target.value)}
                required
                minLength={2}
              />
            </Field>
            <Field label="Email">
              <input
                type="email"
                value={staffEmail}
                onChange={(event) => setStaffEmail(event.target.value)}
                required
              />
            </Field>
            <Field label="Temporary password" hint="At least 12 characters.">
              <input
                type="password"
                value={staffPassword}
                onChange={(event) => setStaffPassword(event.target.value)}
                required
                minLength={12}
                autoComplete="new-password"
              />
            </Field>
            <Field label="Role">
              <select
                value={staffRole}
                onChange={(event) => setStaffRole(event.target.value as "AUDITOR" | "ADMIN")}
              >
                <option value="AUDITOR">AUDITOR — read-only</option>
                <option value="ADMIN">ADMIN — accounts and users</option>
              </select>
            </Field>
            <Button type="submit" busy={creatingStaff}>
              Create user
            </Button>
          </form>
        </Card>
      </div>

      <Card title="Users" subtitle="Locked accounts can be unlocked here after repeated failures.">
        <Table head={["Email", "Name", "Role", "Status", "Failed logins", ""]}>
          {users.map((user) => (
            <tr key={user.id}>
              <td className="mono tiny">{user.email}</td>
              <td className="tiny">{user.full_name}</td>
              <td>
                <Badge tone={user.role === "ADMIN" ? "info" : user.role === "AUDITOR" ? "warn" : "neutral"}>
                  {user.role}
                </Badge>
              </td>
              <td className="tiny">{user.status}</td>
              <td className="mono tiny">{user.failed_login_count}</td>
              <td>
                {user.locked_until && user.locked_until > new Date().toISOString() ? (
                  <Badge tone="bad">locked</Badge>
                ) : (
                  <span className="tiny muted">—</span>
                )}
              </td>
            </tr>
          ))}
        </Table>
      </Card>
    </div>
  );
}
