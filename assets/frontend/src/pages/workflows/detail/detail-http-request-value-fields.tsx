import { useEffect, useId, useMemo, useState, type ReactNode } from "react";
import { DsSelect, RequiredLabel } from "@/components/shared/design-system";
import { useI18n } from "@/lib/i18n/useI18n";
import { useStableRowKeys } from "@/lib/utils/stable-row-keys";
import { formatHttpValue, parseHttpValue, parseHttpStatuses, type HttpField, type HttpValueMode } from "./detail-http-request-fields";
import type { HttpRequestIntent } from "./detail-http-request-intent";

type VariableInput = (props: { ariaLabel: string; className: string; mode?: "template" | "path"; multiline?: boolean; onChange: (text: string) => void; panelId: string; placeholder: string; value: string; ariaInvalid?: boolean; ariaDescribedBy?: string }) => ReactNode;
const inputClass = "mt-1 w-full rounded border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] px-2 py-1 text-sm text-[var(--fs-text-primary)]";
export function HttpValueField({ field, label, value, onChange, intent, renderVariableInput, multiline = false, message = "" }: Readonly<{ field: HttpField; label: string; value: unknown; onChange: (value: unknown) => void; intent: HttpRequestIntent; renderVariableInput: VariableInput; multiline?: boolean; message?: string }>) {
  const { t } = useI18n();
  const id = useId();
  const editor = intent.capture().editor;
  const registration = useMemo(() => intent.registration(id, editor), [intent, id, editor]);
  const source = JSON.stringify(value);
  const [draft, setDraft] = useState(() => ({ source, editor, ...formatHttpValue(value), error: "" }));
  if (draft.editor !== editor || draft.source !== source) setDraft({ source, editor, ...formatHttpValue(value), error: "" });
  useEffect(() => () => intent.release(registration), [intent, registration]);
  const displayError = draft.error || message;
  const errorId = `${id}-error`;
  function edit(text: string) {
    intent.touchField(field);
    const result = parseHttpValue(draft.mode, text);
    const error = result.ok ? "" : result.error === "finite"
      ? t("react.workflows.http_json_finite", "JSON numbers must be finite.")
      : t("react.workflows.http_json_invalid", "Enter valid JSON.");
    intent.raw(registration, error);
    setDraft({ ...draft, text, error, source: result.ok ? JSON.stringify(result.value) : source });
    if (result.ok) onChange(result.value);
  }
  return <div data-http-field={field} data-http-raw-invalid={draft.error ? "true" : undefined}>
    <span className="fs-field-label">{label}</span>
    <DsSelect ariaLabel={`${label} ${t("react.workflows.http_value_mode", "value format")}`} value={draft.mode} options={[
      { value: "text", label: t("react.workflows.http_value_text", "Text") },
      { value: "json", label: t("react.workflows.http_value_json", "JSON") },
    ]} onChange={(next) => {
      if (draft.error) return;
      intent.touchField(field);
      const mode = next as HttpValueMode;
      // Selecting Text explicitly converts a non-string JSON value to its JSON text.
      const currentValue = value === undefined ? "" : value;
      const nextValue = mode === "text" && typeof currentValue !== "string" ? JSON.stringify(currentValue) : currentValue;
      let text = "";
      if (mode === "json") {
        text = JSON.stringify(nextValue, null, 2);
      } else if (typeof nextValue === "string") {
        text = nextValue;
      }
      setDraft({ editor, source: JSON.stringify(nextValue), mode, text, error: "" });
      onChange(nextValue);
    }} />
    <p className="fs-field-helper">{t("react.workflows.http_value_help", "Text preserves characters and templates. JSON preserves types. Selecting Text converts a JSON value to text.")}</p>
    {draft.mode === "text" ? renderVariableInput({ ariaLabel: label, className: inputClass, mode: "template", multiline, onChange: edit, panelId: `${id}-suggestions`, placeholder: "", value: draft.text, ariaInvalid: Boolean(displayError), ariaDescribedBy: displayError ? errorId : undefined }) : <textarea aria-label={label} aria-invalid={Boolean(displayError)} aria-describedby={displayError ? errorId : undefined} className={`${inputClass} font-mono`} onChange={(event) => edit(event.target.value)} value={draft.text} />}
    {displayError ? <span id={errorId} className="fs-field-helper error block" role="alert">{displayError}</span> : null}
  </div>;
}

type Row = { key: string; value: unknown };
export function HttpMapField({ field, label, value, onChange, intent, renderVariableInput, message = "" }: Readonly<{ field: "headers" | "query_params"; label: string; value: unknown; onChange: (value: Record<string, unknown>) => void; intent: HttpRequestIntent; renderVariableInput: VariableInput; message?: string }>) {
  const { t } = useI18n();
  const id = useId();
  const editor = intent.capture().editor;
  const object = value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
  const source = JSON.stringify(value);
  const [local, setLocal] = useState(() => ({ source, editor, rows: Object.entries(object ?? {}).map(([key, entry]) => ({ key, value: entry })) }));
  if (local.source !== source || local.editor !== editor) setLocal({ source, editor, rows: Object.entries(object ?? {}).map(([key, entry]) => ({ key, value: entry })) });
  const keys = useStableRowKeys(local.rows.length, field);
  const registration = useMemo(() => intent.registration(`${id}-keys`, editor), [intent, id, editor]);
  useEffect(() => () => intent.release(registration), [intent, registration]);
  const duplicate = new Set(local.rows.map((row) => row.key)).size !== local.rows.length;
  const keyError = duplicate ? t("react.workflows.http_map_duplicate", "Each key must be unique. Keep editing or remove the duplicate row.") : "";
  function publish(rows: Row[]) {
    intent.touchField(field);
    const invalid = new Set(rows.map((row) => row.key)).size !== rows.length;
    intent.raw(registration, invalid ? t("react.workflows.http_map_duplicate", "Each key must be unique. Keep editing or remove the duplicate row.") : "");
    const next = Object.fromEntries(rows.map((row) => [row.key, row.value]));
    // Track our own publication so integer-key JSON order never rearranges local rows.
    setLocal({ editor, source: invalid ? source : JSON.stringify(next), rows });
    if (!invalid) onChange(next);
  }
  if (!object && value !== undefined) return <div data-http-field={field}><span className="fs-field-label">{label}</span><p className="fs-field-helper">{t("react.workflows.http_map_advanced", "This stored value is not an object. It is retained; use Advanced to edit it.")}</p><pre>{source}</pre></div>;
  return <section data-http-field={field} data-http-raw-invalid={duplicate ? "true" : undefined} className="rounded-xl border border-[var(--fs-border-default)] p-3">
    <span className="fs-field-label">{label}</span>
    <button type="button" onClick={() => {
      let count = 1; while (local.rows.some((row) => row.key === `key_${count}`)) count += 1;
      keys.append(); publish([...local.rows, { key: `key_${count}`, value: "" }]);
    }}>{t("react.workflows.add", "+ Add")} {label}</button>
    {local.rows.map((row, index) => <div key={`${editor}:${keys.keys[index]}`} className="mt-2 grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]">
      <input aria-label={`${label} ${t("react.workflows.http_key", "key")} ${index + 1}`} className={inputClass} aria-invalid={duplicate || Boolean(message) || undefined} aria-describedby={keyError || message ? `${id}-keys` : undefined} value={row.key} onChange={(event) => publish(local.rows.map((entry, position) => position === index ? { ...entry, key: event.target.value } : entry))} />
      <HttpValueField field={field} label={`${label} ${t("react.workflows.http_value", "value")} ${index + 1}`} value={row.value} intent={intent} renderVariableInput={renderVariableInput} onChange={(next) => publish(local.rows.map((entry, position) => position === index ? { ...entry, value: next } : entry))} />
      <button type="button" aria-label={`${t("react.workflows.http_remove", "Remove")} ${label} ${index + 1}`} onClick={() => { keys.remove(index); publish(local.rows.filter((_, position) => position !== index)); }}>{t("react.workflows.http_remove", "Remove")}</button>
    </div>)}
    {keyError || message ? <span id={`${id}-keys`} className="fs-field-helper error block" role="alert">{keyError || message}</span> : null}
  </section>;
}

export function HttpStatusesField({ value, onChange, intent, label, message = "", serverError = false }: Readonly<{ value: unknown; onChange: (value: number[]) => void; intent: HttpRequestIntent; label: string; message?: string; serverError?: boolean }>) {
  const { t } = useI18n(); const id = useId(); const editor = intent.capture().editor;
  const registration = useMemo(() => intent.registration(id, editor), [intent, id, editor]);
  const source = JSON.stringify(value);
  const representable = Array.isArray(value) && value.every((item) => Number.isSafeInteger(item));
  const text = representable ? value.join(", ") : "";
  const [draft, setDraft] = useState({ source, editor, text, error: "" });
  if (draft.source !== source || draft.editor !== editor) setDraft({ source, editor, text, error: "" });
  useEffect(() => () => intent.release(registration), [intent, registration]);
  const error = draft.error || message;
  return <div data-http-field="expected_statuses" data-http-raw-invalid={draft.error ? "true" : undefined}>
    <label htmlFor={id}><RequiredLabel>{label}</RequiredLabel></label>
    <input id={id} aria-required="true" aria-invalid={Boolean(error)} aria-describedby={error ? `${id}-error` : undefined} className={inputClass} value={draft.text} onChange={(event) => {
      intent.touchField("expected_statuses");
      const raw = event.target.value; const parsed = parseHttpStatuses(raw);
      const syntax = parsed.ok ? "" : t("react.workflows.http_csv_invalid", "Enter comma-separated exactly representable integers, or keep editing. No partial token is saved.");
      intent.raw(registration, syntax);
      setDraft({ editor, source: parsed.ok ? JSON.stringify(parsed.value) : source, text: raw, error: syntax });
      if (parsed.ok) onChange(parsed.value);
    }} />
    {!representable ? <p className="fs-field-helper">{t("react.workflows.http_status_advanced", "The stored typed value is retained. Use Advanced to edit it, or enter a new integer list here.")}</p> : null}
    {!representable ? <pre>{source}</pre> : null}
    {error ? <span id={`${id}-error`} className="fs-field-helper error block" role={draft.error || serverError ? "alert" : undefined}>{error}</span> : null}
  </div>;
}
