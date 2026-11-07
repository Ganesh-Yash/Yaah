import { useCallback, useEffect, useState } from "react";
import { ApiClient, ApiError, AuditEntry, IntegrityReport, timestamp } from "../api";
import { Alert, Badge, Button, Card, Loading, Table } from "../components/ui";

const ACTION_FILTERS = [
  "",
  "DEPOSIT",
  "WITHDRAWAL",
  "TRANSFER",
  "LOGIN_FAILURE",
  "AUTHZ_DENIED",
  "IDEMPOTENT_REPLAY",
  "IDEMPOTENCY_KEY_REUSED",
  "REFRESH_TOKEN_REUSE",
];

export default function AuditView({ api }: { api: ApiClient }) {
  const [report, setReport] = useState<IntegrityReport | null>(null);
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [action, setAction] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  const runIntegrity = useCallback(async () => {
    setRunning(true);
    try {
      setReport(await api.integrity());
      setError(null);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Verification failed.");
    } finally {
      setRunning(false);
    }
  }, [api]);

  const loadLog = useCallback(async () => {
    try {
      setEntries(await api.auditLog(action || undefined));
      setError(null);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load the audit trail.");
    }
  }, [api, action]);

  useEffect(() => {
    void runIntegrity();
    void loadLog();
  }, [runIntegrity, loadLog]);

  return (
    <div className="stack">
      {error && <Alert>{error}</Alert>}

      <Card
        title="Ledger integrity"
        subtitle="Recomputed from the raw tables — stored balances, zero-sum transactions, both hash chains."
        tone={report ? (report.ok ? "ok" : "warn") : "plain"}
        actions={
          <Button variant="ghost" onClick={() => void runIntegrity()} busy={running}>
            Re-run verification
          </Button>
        }
      >
        {!report ? (
          <Loading label="Verifying the books…" />
        ) : (
          <>
            <div className="row row--wrap">
              <Badge tone={report.ok ? "good" : "bad"}>
                {report.ok ? "all checks passed" : "INTEGRITY FAILURE"}
              </Badge>
              <span className="tiny muted">checked {timestamp(report.generated_at)}</span>
            </div>

            <dl className="stats">
              <div>
                <dt>Customer funds</dt>
                <dd className="mono">{report.totals.customer_funds}</dd>
              </div>
              <div>
                <dt>Transactions</dt>
                <dd className="mono">{report.totals.transactions}</dd>
              </div>
              <div>
                <dt>Ledger entries</dt>
                <dd className="mono">{report.totals.ledger_entries}</dd>
              </div>
              <div>
                <dt>Audit rows</dt>
                <dd className="mono">{report.totals.audit_rows}</dd>
              </div>
            </dl>

            <Table head={["Check", "What it proves", "Result"]}>
              {report.checks.map((check) => (
                <tr key={check.name}>
                  <td className="mono tiny">{check.name}</td>
                  <td className="tiny muted">{check.description}</td>
                  <td>
                    {check.ok ? (
                      <Badge tone="good">pass</Badge>
                    ) : (
                      <Badge tone="bad">fail</Badge>
                    )}
                  </td>
                </tr>
              ))}
            </Table>

            <p className="tiny muted mono">
              ledger head {report.totals.ledger_head_hash ?? "—"}
              <br />
              audit head {report.totals.audit_head_hash ?? "—"}
            </p>
          </>
        )}
      </Card>

      <Card
        title="Audit trail"
        subtitle="Append-only and hash-chained. Denials are recorded too, not just successes."
        actions={
          <select value={action} onChange={(event) => setAction(event.target.value)}>
            {ACTION_FILTERS.map((option) => (
              <option key={option || "all"} value={option}>
                {option || "All actions"}
              </option>
            ))}
          </select>
        }
      >
        {!entries ? (
          <Loading label="Loading audit trail…" />
        ) : (
          <Table head={["#", "When", "Actor", "Action", "Outcome", "Target", "Request"]} empty={entries.length === 0}>
            {entries.map((entry) => (
              <tr key={entry.id}>
                <td className="mono tiny muted">{entry.id}</td>
                <td className="tiny">{timestamp(entry.created_at)}</td>
                <td className="tiny">
                  {entry.actor_role ?? entry.subject ?? "anonymous"}
                </td>
                <td className="tiny">
                  <span className="mono">{entry.action}</span>
                </td>
                <td>
                  <Badge tone={entry.outcome === "ALLOW" ? "good" : entry.outcome === "DENY" ? "bad" : "warn"}>
                    {entry.outcome}
                  </Badge>
                </td>
                <td className="mono tiny muted">
                  {entry.target_type ? `${entry.target_type}:${(entry.target_id ?? "").slice(0, 8)}` : "—"}
                </td>
                <td className="mono tiny muted">{entry.request_id ?? "—"}</td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </div>
  );
}
