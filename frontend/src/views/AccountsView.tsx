import { FormEvent, useCallback, useEffect, useState } from "react";
import {
  Account,
  ApiClient,
  ApiError,
  MoneyResult,
  Statement,
  User,
  money,
  newIdempotencyKey,
  timestamp,
} from "../api";
import { Alert, Badge, Button, Card, Field, Loading, Modal, Table } from "../components/ui";

type DialogKind = "deposit" | "withdraw" | "transfer" | null;

export default function AccountsView({ api, user }: { api: ApiClient; user: User }) {
  const [accounts, setAccounts] = useState<Account[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<{ kind: DialogKind; account: Account } | null>(null);
  const [statementFor, setStatementFor] = useState<Account | null>(null);
  const isCustomer = user.role === "CUSTOMER";

  const load = useCallback(async () => {
    try {
      setAccounts(await api.listAccounts());
      setError(null);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load accounts.");
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  async function toggleFreeze(account: Account) {
    try {
      await api.setFrozen(account.id, account.status === "ACTIVE");
      await load();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not change the account state.");
    }
  }

  if (accounts === null) return <Loading label="Loading accounts…" />;

  return (
    <>
      {error && <Alert>{error}</Alert>}

      {isCustomer ? (
        accounts.length === 0 ? (
          <Card title="No accounts yet" subtitle="An administrator has to open an account for you.">
            <p className="muted">
              Ask your administrator to open one against <code>{user.email}</code>.
            </p>
          </Card>
        ) : (
          <div className="grid">
            {accounts.map((account) => (
              <Card key={account.id} tone="plain">
                <div className="account">
                  <div>
                    <h3>{account.name}</h3>
                    <p className="mono tiny muted">{account.account_number}</p>
                  </div>
                  <Badge tone={account.status === "ACTIVE" ? "good" : "warn"}>{account.status}</Badge>
                </div>
                <p className="balance">{money(account.balance, account.currency)}</p>
                <p className="tiny muted">
                  {account.currency} · available balance · ledger v{account.version}
                </p>
                <div className="row row--wrap">
                  <Button onClick={() => setDialog({ kind: "deposit", account })}>Deposit</Button>
                  <Button variant="ghost" onClick={() => setDialog({ kind: "withdraw", account })}>
                    Withdraw
                  </Button>
                  <Button variant="ghost" onClick={() => setDialog({ kind: "transfer", account })}>
                    Transfer
                  </Button>
                  <Button variant="subtle" onClick={() => setStatementFor(account)}>
                    Statement
                  </Button>
                </div>
              </Card>
            ))}
          </div>
        )
      ) : (
        <Card
          title="All accounts"
          subtitle={
            user.role === "AUDITOR"
              ? "Read-only view. Auditors cannot move money or change account state."
              : "Administrators can open and freeze accounts, but cannot move money."
          }
        >
          <Table
            head={["Account", "Owner", "Currency", "Balance", "Status", ""]}
            empty={accounts.length === 0}
          >
            {accounts.map((account) => (
              <tr key={account.id}>
                <td>
                  <div>{account.name}</div>
                  <div className="mono tiny muted">{account.account_number}</div>
                </td>
                <td className="tiny">{account.owner_email ?? "— system —"}</td>
                <td className="tiny">{account.currency}</td>
                <td className="mono">
                  {account.is_system ? "—" : money(account.balance, account.currency)}
                </td>
                <td>
                  <Badge tone={account.status === "ACTIVE" ? "good" : "warn"}>{account.status}</Badge>
                </td>
                <td>
                  <div className="row">
                    {!account.is_system && (
                      <Button variant="subtle" onClick={() => setStatementFor(account)}>
                        Statement
                      </Button>
                    )}
                    {user.role === "ADMIN" && !account.is_system && (
                      <Button variant="ghost" onClick={() => void toggleFreeze(account)}>
                        {account.status === "ACTIVE" ? "Freeze" : "Unfreeze"}
                      </Button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </Table>
        </Card>
      )}

      {dialog && (
        <MoneyDialog
          api={api}
          kind={dialog.kind}
          account={dialog.account}
          accounts={accounts.filter((item) => !item.is_system && item.status === "ACTIVE")}
          onClose={() => setDialog(null)}
          onSettled={load}
        />
      )}

      {statementFor && (
        <StatementDialog api={api} account={statementFor} onClose={() => setStatementFor(null)} />
      )}
    </>
  );
}

function MoneyDialog({
  api,
  kind,
  account,
  accounts,
  onClose,
  onSettled,
}: {
  api: ApiClient;
  kind: DialogKind;
  account: Account;
  accounts: Account[];
  onClose: () => void;
  onSettled: () => Promise<void> | void;
}) {
  const [amount, setAmount] = useState("");
  const [description, setDescription] = useState("");
  const [destination, setDestination] = useState("");
  const [fromId, setFromId] = useState(account.id);
  // One key per *intent*: retrying this form reuses it, so a double submit or
  // a network retry cannot create a second movement.
  const [key, setKey] = useState(() => newIdempotencyKey());
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<MoneyResult | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  const titles: Record<Exclude<DialogKind, null>, string> = {
    deposit: "Deposit",
    withdraw: "Withdraw",
    transfer: "Transfer",
  };
  const title = titles[(kind ?? "deposit") as Exclude<DialogKind, null>];
  const source = accounts.find((item) => item.id === fromId) ?? account;

  async function submit(event: FormEvent) {
    event.preventDefault();
    await send();
  }

  // The same body with the same key: the server answers with the original
  // receipt and moves nothing. This is what "at most once" looks like from the
  // outside, so the receipt offers it as a button rather than hiding it behind
  // a terminal.
  async function send() {
    setBusy(true);
    setError(null);
    try {
      const response =
        kind === "deposit"
          ? await api.deposit(account.id, amount.trim(), key, description.trim() || undefined)
          : kind === "withdraw"
            ? await api.withdraw(account.id, amount.trim(), key, description.trim() || undefined)
            : await api.transfer(
                fromId,
                destination.trim(),
                amount.trim(),
                key,
                description.trim() || undefined
              );
      setResult({ ...response.data, replayed: response.replayed });
      await onSettled();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught : new ApiError(0, "NETWORK", "Network error."));
      setResult(null);
    } finally {
      setBusy(false);
    }
  }

  function finish() {
    setKey(newIdempotencyKey());
    onClose();
  }

  return (
    <Modal title={`${title} — ${account.name}`} onClose={finish}>
      {result ? (
        <div className="stack">
          <Alert kind="success">
            {result.replayed
              ? "This request had already been processed — the original result was returned, and no money moved twice."
              : `${title} posted.`}
          </Alert>
          {result.replayed && <Badge tone="info">idempotent replay</Badge>}
          <dl className="receipt">
            <div>
              <dt>Transaction</dt>
              <dd className="mono tiny">{result.transaction.id}</dd>
            </div>
            <div>
              <dt>Amount</dt>
              <dd className="mono">{money(result.transaction.amount, result.transaction.currency)}</dd>
            </div>
            <div>
              <dt>New balance</dt>
              <dd className="mono">{money(result.account.balance, result.account.currency)}</dd>
            </div>
            <div>
              <dt>Ledger legs</dt>
              <dd className="mono tiny">
                {result.transaction.entries
                  .map(
                    (entry) =>
                      `${entry.direction === "CREDIT" ? "+" : "−"}${entry.amount}`
                  )
                  .join("  ")}
              </dd>
            </div>
            <div>
              <dt>Idempotency-Key</dt>
              <dd className="mono tiny">{key}</dd>
            </div>
          </dl>
          <div className="row">
            <Button onClick={finish}>Done</Button>
            <Button variant="ghost" busy={busy} onClick={() => void send()}>
              Send the same request again
            </Button>
          </div>
          <p className="tiny muted">
            The second send reuses key <code className="mono">{key}</code> with the same body — the
            server must return this receipt again instead of paying twice.
          </p>
        </div>
      ) : (
        <form className="form" onSubmit={submit}>
          {error && (
            <Alert>
              <strong>{error.code}</strong> — {error.message}
              {error.details && Object.keys(error.details).length > 0 && (
                <div className="tiny">
                  {Object.entries(error.details).map(([name, value]) => (
                    <div key={name}>
                      {name}: <span className="mono">{String(value)}</span>
                    </div>
                  ))}
                </div>
              )}
            </Alert>
          )}

          {kind === "transfer" && (
            <>
              <Field label="From account">
                <select value={fromId} onChange={(event) => setFromId(event.target.value)}>
                  {accounts.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name} · {money(item.balance, item.currency)}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Destination account number" hint="12 digits, from the recipient's account card.">
                <input
                  value={destination}
                  onChange={(event) => setDestination(event.target.value)}
                  required
                  inputMode="numeric"
                  pattern="[0-9]{8,32}"
                  placeholder="40XXXXXXXXXX"
                />
              </Field>
            </>
          )}

          <Field
            label="Amount"
            hint={`Decimal only, two places max. Available: ${money(source.balance, source.currency)}`}
          >
            <input
              value={amount}
              onChange={(event) => setAmount(event.target.value)}
              required
              inputMode="decimal"
              pattern="[0-9]+(\.[0-9]{1,2})?"
              placeholder="25.00"
            />
          </Field>

          <Field label="Description (optional)">
            <input
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              maxLength={140}
              placeholder="Rent, invoice #42…"
            />
          </Field>

          <p className="tiny muted">
            Idempotency-Key <code className="mono">{key}</code> — reused if you retry, so this movement
            can only happen once.
          </p>

          <div className="row">
            <Button type="submit" busy={busy}>
              {title}
            </Button>
            <Button variant="ghost" onClick={finish}>
              Cancel
            </Button>
          </div>
        </form>
      )}
    </Modal>
  );
}

function StatementDialog({
  api,
  account,
  onClose,
}: {
  api: ApiClient;
  account: Account;
  onClose: () => void;
}) {
  const [statement, setStatement] = useState<Statement | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .statement(account.id)
      .then((data) => !cancelled && setStatement(data))
      .catch((caught) => setError(caught instanceof ApiError ? caught.message : "Failed"));
    return () => {
      cancelled = true;
    };
  }, [api, account.id]);

  async function loadOlder() {
    if (!statement?.next_cursor) return;
    setLoadingMore(true);
    try {
      const older = await api.statement(account.id, statement.next_cursor);
      setStatement({
        ...older,
        entries: [...statement.entries, ...older.entries],
      });
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Failed");
    } finally {
      setLoadingMore(false);
    }
  }

  return (
    <Modal title={`Statement — ${account.name}`} onClose={onClose}>
      {error && <Alert>{error}</Alert>}
      {!statement ? (
        <Loading label="Loading statement…" />
      ) : (
        <>
          <p className="muted tiny">
            Balance <strong className="mono">{money(statement.account.balance, account.currency)}</strong>{" "}
            · every row is a ledger entry, newest first.
          </p>
          <Table head={["#", "When", "Type", "Amount", "Balance", "Description"]} empty={statement.entries.length === 0}>
            {statement.entries.map((entry) => (
              <tr key={entry.entry_id}>
                <td className="mono tiny muted">{entry.entry_id}</td>
                <td className="tiny">{timestamp(entry.created_at)}</td>
                <td className="tiny">{entry.type}</td>
                <td className={`mono ${entry.direction === "CREDIT" ? "pos" : "neg"}`}>
                  {entry.direction === "CREDIT" ? "+" : "−"}
                  {money(entry.amount, account.currency)}
                </td>
                <td className="mono">{money(entry.balance_after, account.currency)}</td>
                <td className="tiny muted">{entry.description ?? "—"}</td>
              </tr>
            ))}
          </Table>
          {statement.next_cursor && (
            <Button variant="ghost" onClick={loadOlder} busy={loadingMore}>
              Load older entries
            </Button>
          )}
        </>
      )}
    </Modal>
  );
}
