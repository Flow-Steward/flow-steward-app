import { useState, type ReactNode } from "react";

import { BlankCell, DsBadge } from "@/components/shared/design-system";
import { DsPopover } from "@/components/shared/popover";
import { useI18n } from "@/lib/i18n/useI18n";
import { formatCalendarDate, formatDate, formatDateTime } from "@/lib/time/date-time-format";
import { jobStatusGroup, jobStatusGroupLabel, jobStatusGroupTone } from "@/pages/jobs/status-groups";

import type { OperatorFileValue, OperatorListColumn } from "./types";

/** Files past this many collapse into "+N" in a cell; it opens the list of them all. */
const STRIP_VISIBLE_FILES = 2;

function isFileValue(value: unknown): value is OperatorFileValue {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const row = value as Record<string, unknown>;
  return typeof row.artifact_id === "string" || typeof row.filename === "string";
}

function FileLink({ jobId, file }: Readonly<{ jobId: string; file: OperatorFileValue }>) {
  const name = file.filename || file.artifact_id || "";
  if (!file.artifact_id || !jobId) return <span title={name}>{name}</span>;
  return (
    <a
      className="fs-operator-file-link"
      download={file.filename || undefined}
      href={`/api/jobs/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(file.artifact_id)}`}
      onClick={(event) => event.stopPropagation()}
      title={name}
    >
      {name}
    </a>
  );
}

/**
 * Several files in one cell: the first ones as links, then a "+N" button that
 * opens every file as a download link, so each one is reachable by keyboard.
 */
function FileStrip({ jobId, files }: Readonly<{ jobId: string; files: OperatorFileValue[] }>) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const visible = files.slice(0, STRIP_VISIBLE_FILES);
  const rest = files.length - visible.length;
  const label = t("react.operator.all_files", "All files ({count})", { count: String(files.length) });
  return (
    <span className="fs-operator-file-strip" data-files={files.length}>
      {visible.map((file, index) => (
        <FileLink file={file} jobId={jobId} key={`${file.artifact_id ?? file.filename}-${index}`} />
      ))}
      {rest > 0 ? (
        <DsPopover
          label={label}
          onOpenChange={setOpen}
          open={open}
          portal
          trigger={(props) => (
            <button
              {...props}
              aria-label={label}
              className="fs-operator-file-more"
              onClick={(event) => {
                // The row opens its job; this button only opens the list.
                event.stopPropagation();
                props.onClick();
              }}
              type="button"
            >
              +{rest}
            </button>
          )}
        >
          {/* The popover is portaled, but React events still bubble to the row. */}
          {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions -- only stops the row's click, not an action. */}
          <ul aria-label={label} className="fs-operator-file-list" onClick={(event) => event.stopPropagation()}>
            {files.map((file, index) => (
              <li key={`${file.artifact_id ?? file.filename}-${index}`}>
                <FileLink file={file} jobId={jobId} />
              </li>
            ))}
          </ul>
        </DsPopover>
      ) : null}
    </span>
  );
}

function numberText(value: number, decimals?: number): string {
  return new Intl.NumberFormat(undefined, decimals === undefined ? undefined : { minimumFractionDigits: decimals, maximumFractionDigits: decimals }).format(value);
}

/**
 * One configured column of a section's list (ADR 0075): the row's
 * `operator_values` by column id, shown by the column's format. Files are names
 * with download links; a missing value is a blank cell, never a guess.
 */
export function OperatorCell({
  column,
  row,
  decimals,
}: Readonly<{
  column: OperatorListColumn;
  row: Record<string, unknown>;
  /** Digits after the decimal mark of a number, as a result field configures it. */
  decimals?: number | null;
}>) {
  const { t } = useI18n();
  const values = row.operator_values && typeof row.operator_values === "object" ? (row.operator_values as Record<string, unknown>) : {};
  const value = values[column.id];
  const jobId = String(row.job_id ?? row.id ?? "");
  const format = column.format || "auto";
  if (value === null || value === undefined || value === "") return <BlankCell />;
  if (Array.isArray(value)) {
    if (format === "count") return <>{String(value.length)}</>;
    if (value.length && value.every(isFileValue)) return <FileStrip files={value} jobId={jobId} />;
    return <>{value.map((item) => (item && typeof item === "object" ? JSON.stringify(item) : String(item))).join(", ")}</>;
  }
  if (isFileValue(value)) return <FileLink file={value} jobId={jobId} />;
  let shown: ReactNode;
  if (format === "user" || column.path === "submitted_by") {
    const author = value && typeof value === "object" ? (value as Record<string, unknown>) : {};
    const name = String(author.name ?? author.display_name ?? author.id ?? "");
    shown = author.kind === "service_account" ? t("react.operator.author_api", "{name} (API)", { name }) : name;
  } else if (typeof value === "object") {
    shown = JSON.stringify(value);
  } else if (format === "status" || (column.source === "job" && column.path === "status")) {
    const group = jobStatusGroup(value);
    shown = <DsBadge tone={jobStatusGroupTone(group)}>{group ? jobStatusGroupLabel(group, t) : String(value)}</DsBadge>;
  } else if (typeof value === "boolean" || format === "boolean") {
    shown = value === true || value === "true" ? t("react.operator.value_yes", "Yes") : t("react.operator.value_no", "No");
  } else if (format === "date" && typeof value === "string") {
    shown = /^\d{4}-\d{2}-\d{2}$/.test(value) ? formatCalendarDate(value) : formatDate(value);
  } else if ((format === "datetime" || (format === "auto" && column.source === "job" && column.path.endsWith("_at"))) && typeof value === "string") {
    shown = formatDateTime(value);
  } else if (format === "currency" && typeof value === "number") {
    shown = numberText(value, 2);
  } else if ((format === "number" || format === "auto") && typeof value === "number") {
    shown = numberText(value, decimals ?? undefined);
  } else {
    shown = String(value);
  }
  return <>{shown}</>;
}
