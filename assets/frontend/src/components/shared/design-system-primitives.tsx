import { useI18n } from "@/lib/i18n/useI18n";
import { AlertCircle, Check, Eye, EyeOff, Info, Minus } from "lucide-react";
import { useId, useState, type HTMLAttributes, type InputHTMLAttributes, type PropsWithChildren, type ReactNode } from "react";

export type PageWidth = "standard" | "wide" | "fluid";

/**
 * The page declares its container mode so its geometry is visible and
 * searchable in the page source.
 */
export function PageContainer({
  children,
  className,
  width,
}: Readonly<{ children: ReactNode; className?: string; width: PageWidth }>) {
  return (
    <div className={className ? `fs-page ${className}` : "fs-page"} data-page-width={width} data-testid="page-container">
      {children}
    </div>
  );
}

/**
 * The published Page header. Named for settings while it lived there; the
 * eyebrow is the section, so it is a parameter rather than a constant.
 */
export function DsPageHeader({
  title,
  description,
  action,
  eyebrow,
  titleId,
  media,
}: Readonly<{ title: string; description: string; action?: ReactNode; eyebrow?: string; titleId?: string; media?: ReactNode }>) {
  const { t } = useI18n();
  return (
    <header className={`fs-page-header${action ? " fs-page-header-with-action" : ""}${media ? " fs-page-header-with-media" : ""}`}>
      {/* An object's own mark (an extension's logo) before its name. */}
      {media ? <div className="fs-page-header-media">{media}</div> : null}
      <div className="fs-page-header-copy">
        <span className="fs-page-eyebrow">{eyebrow ?? t("shell.nav.settings", "Settings")}</span>
        {/* An id makes the title a focus target of last resort, for when the
            object a user acted on is gone and has no neighbour. */}
        <h1 className="fs-page-title" id={titleId} tabIndex={titleId ? -1 : undefined}>{title}</h1>
        <p className="fs-page-description">{description}</p>
      </div>
      {action ? <div className="fs-page-header-actions">{action}</div> : null}
    </header>
  );
}

export function DsCard({
  title,
  description,
  children,
  footer,
  className = "",
  testId,
}: PropsWithChildren<{
  title?: string;
  description?: string;
  footer?: ReactNode;
  className?: string;
  testId?: string;
}>) {
  return (
    <article className={`fs-card ${className}`.trim()} data-testid={testId}>
      {title || description ? (
        <header className="fs-card-header">
          {title ? <h2 className="fs-card-title">{title}</h2> : null}
          {description ? <p className="fs-card-description">{description}</p> : null}
        </header>
      ) : null}
      {/* The body is optional in the published anatomy: a header-only card must
          not carry an empty padded block under its divider. */}
      {children ? <div className="fs-card-body">{children}</div> : null}
      {footer ? <footer className="fs-card-footer">{footer}</footer> : null}
    </article>
  );
}

export function DsAlert({
  tone = "neutral",
  title,
  children,
}: PropsWithChildren<{ tone?: "neutral" | "success" | "warning" | "danger"; title: string }>) {
  const Icon = tone === "neutral" ? Info : AlertCircle;
  const detailId = useId();
  const titleContent = <strong>{title}</strong>;
  return (
    <div className={`fs-alert ${tone === "neutral" ? "" : tone}`.trim()} role={tone === "danger" ? "alert" : undefined} aria-live={tone === "danger" ? undefined : "polite"} aria-atomic="true">
      <span className="fs-alert-icon" aria-hidden="true"><Icon /></span>
      {/* A div, not a p: callers legitimately pass block content — the workflow
          not-found surface passes two paragraphs — and a p cannot contain one.
          The browser closes the outer p, React hydrates a different tree than it
          rendered, and every such alert logs a hydration warning. */}
      <div>{tone === "danger" ? titleContent : <output aria-describedby={children ? detailId : undefined}>{titleContent}</output>}{children ? <div className="fs-alert-body" id={detailId}>{children}</div> : null}</div>
    </div>
  );
}

/** Placement and pointer hold are the caller's; tone and motion stay the DS toast's. */
export function DsToast({
  children,
  className,
  tone = "success",
  role = "status",
  ...rest
}: PropsWithChildren<{ tone?: "success" | "danger" | "neutral"; className?: string } & Omit<HTMLAttributes<HTMLDivElement>, "className">>) {
  return <div aria-live={role === "alert" ? "assertive" : "polite"} {...rest} className={className ? `fs-toast ${className}` : "fs-toast"} data-tone={tone} role={role}>{children}</div>;
}

/** A separate, localized optional note that remains part of a field's name. */
export function OptionalMarker() {
  const { t } = useI18n();
  return <span className="fs-field-optional">{t("auth.common.optional", "(optional)")}</span>;
}

export function OptionalLabel({ children, id }: Readonly<PropsWithChildren<{ id?: string }>>) {
  return <span className="fs-field-label" id={id}>{children}{" "}<OptionalMarker /></span>;
}

export function RequiredLabel({ children }: Readonly<PropsWithChildren>) {
  return <span className="fs-field-label fs-required-label">{children}<span className="fs-required" aria-hidden="true">*</span></span>;
}

/**
 * The published checkbox mark. The native input stays for semantics and is
 * visually hidden; this carries checked and mixed. Exported so tables and
 * pickers that own their own input can still render the same anatomy.
 */
export function DsCheckboxMark() {
  return (
    <span className="fs-checkbox-mark" aria-hidden="true">
      <Check />
      <Minus className="fs-checkbox-mixed" />
    </span>
  );
}

export function DsCheckbox({
  checked,
  onChange,
  children,
  disabled,
  name,
  value,
}: PropsWithChildren<{
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  name?: string;
  value?: string;
}>) {
  return (
    <label className="fs-checkbox-label">
      <input checked={checked} disabled={disabled} name={name} onChange={(event) => onChange(event.target.checked)} type="checkbox" value={value} />
      <DsCheckboxMark />
      <span>{children}</span>
    </label>
  );
}

export function DsRadio({
  checked,
  onChange,
  children,
  name,
  value,
}: PropsWithChildren<{ checked: boolean; onChange: () => void; name: string; value: string }>) {
  return (
    <label className="fs-radio-label">
      <input checked={checked} name={name} onChange={onChange} type="radio" value={value} />
      <span className="fs-radio-mark" aria-hidden="true" />
      <span>{children}</span>
    </label>
  );
}

export function SensitiveInput({
  ariaLabel,
  ...props
}: InputHTMLAttributes<HTMLInputElement> & { ariaLabel: string }) {
  const { t } = useI18n();
  const [revealed, setRevealed] = useState(false);
  return (
    <span className="fs-input-shell">
      <input {...props} aria-label={ariaLabel} type={revealed ? "text" : "password"} />
      <button
        aria-label={`${revealed ? t("component.secret.hide", "Hide") : t("component.secret.show", "Show")} ${ariaLabel.toLowerCase()}`}
        aria-pressed={revealed}
        /* The published reveal action — `input.md:138`, a 22px transparent tile
           with tertiary icon at rest and the primary wash on hover and focus.
           `.fs-icon-button` was the standalone icon button and drew its own 1px
           border inside the field; `.fs-input-clear` is the neighbouring *clear*
           role, which hovers on the muted surface and is 20px. Focus also shows
           on the field itself, because `.fs-input-shell:focus-within` rings. */
        className="fs-input-action"
        onClick={() => setRevealed((value) => !value)}
        type="button"
      >
        {revealed ? <Eye aria-hidden="true" /> : <EyeOff aria-hidden="true" />}
      </button>
    </span>
  );
}

/** `new` is the Iridescent pill an item wears for its first weeks in a catalog. */
export type DsBadgeTone = "neutral" | "green" | "orange" | "danger" | "new";

/** Status pill for a table cell or an inline state. */
export function DsBadge({
  tone = "neutral",
  children,
}: PropsWithChildren<{ tone?: DsBadgeTone }>) {
  return <span className={`fs-badge ${tone === "neutral" ? "" : tone}`.trim()}>{children}</span>;
}

/**
 * A reference value: an auth scheme, a transport, an identifier. `code` rather
 * than `span` because the element carries the meaning and the class carries the
 * face. The pill shape is reserved for state, so this is the 4px chip.
 */
export function DsValueChip({ children }: PropsWithChildren<object>) {
  return <code className="fs-value-chip">{children}</code>;
}
