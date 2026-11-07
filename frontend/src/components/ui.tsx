import { ReactNode } from "react";

export function Card({
  title,
  subtitle,
  actions,
  children,
  tone,
}: {
  title?: string;
  subtitle?: string;
  actions?: ReactNode;
  children: ReactNode;
  tone?: "plain" | "ok" | "warn";
}) {
  return (
    <section className={`card${tone && tone !== "plain" ? ` card--${tone}` : ""}`}>
      {(title || actions) && (
        <header className="card__head">
          <div>
            {title && <h2>{title}</h2>}
            {subtitle && <p className="muted">{subtitle}</p>}
          </div>
          {actions && <div className="card__actions">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "good" | "bad" | "warn" | "info";
}) {
  return <span className={`badge badge--${tone}`}>{children}</span>;
}

export function Button({
  children,
  onClick,
  type = "button",
  variant = "primary",
  disabled,
  busy,
}: {
  children: ReactNode;
  onClick?: () => void;
  type?: "button" | "submit";
  variant?: "primary" | "ghost" | "danger" | "subtle";
  disabled?: boolean;
  busy?: boolean;
}) {
  return (
    <button
      className={`btn btn--${variant}`}
      type={type}
      onClick={onClick}
      disabled={disabled || busy}
    >
      {busy ? <span className="spinner" aria-hidden /> : null}
      {children}
    </button>
  );
}

export function Field({
  label,
  hint,
  children,
  htmlFor,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  htmlFor?: string;
}) {
  return (
    <label className="field" htmlFor={htmlFor}>
      <span className="field__label">{label}</span>
      {children}
      {hint && <span className="field__hint">{hint}</span>}
    </label>
  );
}

export function Alert({
  kind = "error",
  children,
}: {
  kind?: "error" | "info" | "success";
  children: ReactNode;
}) {
  return <div className={`alert alert--${kind}`}>{children}</div>;
}

export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="modal__head">
          <h2>{title}</h2>
          <button className="modal__close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>
        {children}
      </div>
    </div>
  );
}

export function Table({
  head,
  children,
  empty,
}: {
  head: string[];
  children: ReactNode;
  empty?: boolean;
}) {
  if (empty) return <p className="muted empty">Nothing here yet.</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {head.map((label) => (
              <th key={label}>{label}</th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="loading">
      <span className="spinner" aria-hidden /> {label}
    </div>
  );
}
