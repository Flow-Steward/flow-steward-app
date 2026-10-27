import { useI18n } from "@/lib/i18n/useI18n";
import { useState } from "react";
import { firstReadableText, humanizeDiagnosticLabel, objectOrEmpty, type Entity } from "./job-detail-core";
import { JobFileItem } from "./job-detail-file-preview";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";

const PROJECTION_MARKER = "_flow_steward_projection";

/** Items a bounded preview left out, from the marker the runtime appends. */
function omittedItems(item: unknown): number | null {
  const marker = objectOrEmpty(objectOrEmpty(item)[PROJECTION_MARKER]);
  return Object.keys(marker).length ? Number(marker.omitted_items) || 0 : null;
}

const CUT_LIST_TOTAL = /\((\d+) in total\)$/;

/** A list's full length, read from the marker a bounded preview ends it with. */
function listLength(value: unknown[]): number {
  const last = value.at(-1);
  const total = typeof last === "string" ? CUT_LIST_TOTAL.exec(last) : null;
  if (total) {
    return Number(total[1]);
  }
  return value.reduce<number>((count, item) => count + (omittedItems(item) ?? 1), 0);
}

function shortValue(value: unknown): string {
  if (Array.isArray(value)) {
    const count = listLength(value);
    return `${count} item${count === 1 ? "" : "s"}`;
  }
  if (typeof value === "object" && value !== null) {
    const count = Object.keys(value).filter((key) => key !== PROJECTION_MARKER).length;
    return `${count} field${count === 1 ? "" : "s"}`;
  }
  return readableFieldValue(value);
}

function readableFieldValue(value: unknown): string {
  if (typeof value === "boolean") {
    return value ? "Yes" : "No";
  }
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  if (typeof value === "string" || typeof value === "number") {
    return stringFromUnknownValue(value);
  }
  if (Array.isArray(value)) {
    const scalars = value.every((item) => item === null || typeof item !== "object");
    if (scalars && value.length) {
      const total = listLength(value);
      const shown = typeof value.at(-1) === "string" && CUT_LIST_TOTAL.test(String(value.at(-1)))
        ? value.slice(0, -1)
        : value;
      return shown.slice(0, 6).map((item) => readableFieldValue(item)).join(", ")
        + (total > 6 ? ` … (${total} in total)` : "");
    }
    return shortValue(value);
  }
  const entries = Object.entries(objectOrEmpty(value)).filter(([key]) => key !== PROJECTION_MARKER);
  if (!entries.length) {
    return "—";
  }
  return entries
    .slice(0, 4)
    .map(([key, item]) => `${key}: ${shortValue(item)}`)
    .join(" · ") + (entries.length > 4 ? ` · +${entries.length - 4} more` : "");
}

function isStructuredFieldValue(value: unknown): boolean {
  return Array.isArray(value) || (typeof value === "object" && value !== null);
}

/** A small mapping of plain values, such as query parameters or headers. */
function isFlatRecord(value: unknown): value is Entity {
  if (Array.isArray(value) || typeof value !== "object" || value === null) {
    return false;
  }
  const entries = Object.entries(value);
  return entries.length > 0 && entries.length <= 8
    && entries.every(([, item]) => item === null || typeof item !== "object");
}

function withoutProjectionMarkers(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => {
      const omitted = omittedItems(item);
      return omitted === null ? withoutProjectionMarkers(item) : `… ${omitted} more items`;
    });
  }
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(
      Object.entries(value)
        .filter(([key]) => key !== PROJECTION_MARKER)
        .map(([key, item]) => [key, withoutProjectionMarkers(item)]),
    );
  }
  return value;
}

function formattedFieldValue(value: unknown): string {
  try {
    return JSON.stringify(withoutProjectionMarkers(value), null, 2);
  } catch {
    return String(value);
  }
}

function StructuredFieldValue({ value }: Readonly<{ value: unknown }>) {
  if (isFlatRecord(value)) {
    return (
      <ul className="space-y-0.5">
        {Object.entries(value).map(([key, item]) => (
          <li key={key}>
            <span className="fs-ink-tertiary">{key}:</span> {readableFieldValue(item)}
          </li>
        ))}
      </ul>
    );
  }
  return (
    <details>
      <summary className="cursor-pointer select-none font-medium">
        {readableFieldValue(value)}
      </summary>
      <pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-words rounded border border-subtle p-3 text-xs fs-surface-raised">
        {formattedFieldValue(value)}
      </pre>
    </details>
  );
}

/** A preview cut a long text: "…(1027 characters in total)". */
const CUT_TEXT = /… \(\d+ characters in total\)$/u;

function FullTextValue({
  field,
  loadFullValue,
  preview,
}: Readonly<{
  field: string;
  loadFullValue: (field: string) => Promise<unknown>;
  preview: string;
}>) {
  const { t } = useI18n();
  const [full, setFull] = useState<string | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "failed">("idle");
  async function load() {
    setState("loading");
    try {
      const value = await loadFullValue(field);
      setFull(typeof value === "string" ? value : formattedFieldValue(value));
      setState("idle");
    } catch {
      setState("failed");
    }
  }
  if (full !== null) {
    return (
      <pre className="mt-1 max-h-[32rem] overflow-auto whitespace-pre-wrap break-words rounded border border-subtle p-3 text-xs fs-surface-raised">
        {full}
      </pre>
    );
  }
  return (
    <>
      {preview}
      <span className="mt-1 flex flex-wrap items-center gap-2">
        <button
          className="fs-link text-xs"
          disabled={state === "loading"}
          onClick={() => ignoreAsyncResult(load())}
          type="button"
        >
          {state === "loading" ? t("react.jobs.loading_full_text", "Loading full text…") : t("react.jobs.show_full_text", "Show full text")}
        </button>
        {state === "failed" ? (
          <span className="text-xs fs-ink-tertiary" role="alert">{t("react.jobs.the_full_text_could_not_be_loaded", "The full text could not be loaded.")}</span>
        ) : null}
      </span>
    </>
  );
}

export function ReadableDataFields({
  emptyText: suppliedEmptyText,
  fieldLabelKeys,
  loadFullValue,
  value,
}: Readonly<{
  emptyText?: string;
  /** Localization keys supplied for known first-party schema fields only. */
  fieldLabelKeys?: Entity;
  /** Loads the full value of a field the preview cut short. */
  loadFullValue?: (field: string) => Promise<unknown>;
  value: unknown;
}>) {
  const { t } = useI18n();
  const emptyText = suppliedEmptyText ?? t("react.jobs.no_values_recorded", "No values were recorded.");
  const entries = Object.entries(objectOrEmpty(value)).filter(
    ([key]) => key !== PROJECTION_MARKER,
  );
  if (!entries.length) {
    return <p className="fs-empty-copy">{emptyText}</p>;
  }
  return (
    <dl className="grid min-w-0 gap-2 sm:grid-cols-2">
      {entries.map(([key, fieldValue]) => (
        <div
          className="min-w-0 fs-surface-radius border border-subtle fs-surface-inset px-3 py-2"
          key={key}
        >
          <dt className="text-xs uppercase tracking-[0.14em] fs-ink-tertiary">
            {firstReadableText(fieldLabelKeys?.[key])
              ? t(firstReadableText(fieldLabelKeys?.[key]), humanizeDiagnosticLabel(key))
              : humanizeDiagnosticLabel(key)}
          </dt>
          <dd className="mt-1 break-words text-sm fs-ink-primary">
            {isStructuredFieldValue(fieldValue) ? (
              <StructuredFieldValue value={fieldValue} />
            ) : loadFullValue && typeof fieldValue === "string" && CUT_TEXT.test(fieldValue) ? (
              <FullTextValue field={key} loadFullValue={loadFullValue} preview={fieldValue} />
            ) : (
              readableFieldValue(fieldValue)
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function ReadableFileList({ files }: Readonly<{ files: Entity[] }>) {
  const { t } = useI18n();
  if (!files.length) {
    return <p className="fs-empty-copy">{t("react.jobs.no_files_were_attached", "No files were attached.")}</p>;
  }
  return (
    <ul className="space-y-2">
      {files.map((file, index) => (
        <li
          className="fs-surface-radius border border-subtle fs-surface-inset px-3 py-2"
          key={firstReadableText(file.artifact_id, file.id, `${firstReadableText(file.filename, file.name)}-${index}`)}
        >
          <JobFileItem fallbackName={`File ${index + 1}`} file={file} showType />
        </li>
      ))}
    </ul>
  );
}
