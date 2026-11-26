import type { ReactNode } from "react";

/**
 * The one layout of a job of a section (ADR 0075), whatever its state: the
 * original on the left when the job has files, the panel on the right with its
 * heading and cards, and the footer with the way back and the job's actions.
 * Without files the panel takes the whole width.
 */
export function OperatorPageShell({
  original,
  originalLabel,
  panelLabel,
  head,
  children,
  footNote,
  actions,
  testId,
}: Readonly<{
  /** The job's files; nothing when it has none. */
  original?: ReactNode;
  originalLabel: string;
  panelLabel: string;
  head: ReactNode;
  children: ReactNode;
  /** A line at the left of the footer: an error or why nothing can be done. */
  footNote?: ReactNode;
  actions: ReactNode;
  testId?: string;
}>) {
  return (
    <div className="fs-operator-review" data-original={original ? "shown" : "none"} data-testid={testId}>
      {original ? (
        <section aria-label={originalLabel} className="fs-operator-review-original" data-testid="operator-page-original">
          {original}
        </section>
      ) : null}
      <section aria-label={panelLabel} className="fs-operator-review-check">
        <div className="fs-operator-review-scroll">
          {head}
          {children}
        </div>
        <footer className="fs-operator-review-foot">
          {footNote || <p className="fs-operator-review-foot-note" />}
          <div className="fs-operator-review-actions">{actions}</div>
        </footer>
      </section>
    </div>
  );
}

/** The panel's heading: title with the job's status, a line under it, and the steps through the list. */
export function OperatorPageHead({
  title,
  status,
  sub,
  steps,
  testId,
}: Readonly<{ title: ReactNode; status?: ReactNode; sub?: ReactNode; steps?: ReactNode; testId?: string }>) {
  return (
    <header className="fs-operator-review-head" data-testid={testId}>
      <div className="fs-operator-review-head-text">
        <h1 className="fs-operator-review-title">{title}</h1>
        {sub || status ? (
          <p className="fs-operator-review-sub">
            {status}
            {status && sub ? <span aria-hidden="true"> · </span> : null}
            {sub}
          </p>
        ) : null}
      </div>
      {steps}
    </header>
  );
}
