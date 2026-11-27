import { isSameOriginApiPath, reportMapping, reportText } from "@/pages/settings/hub/resource-report";
import { isTruthy } from "@/lib/utils/unknown-values";

/**
 * The `status_document` declaration as the host renderer reads it (ADR 0082,
 * section 6): one status object shown as label and value rows, and a text
 * document (typed or read from a file) sent to the module with PUT. The server
 * validates the declaration at registration; the paths are checked again here
 * before they reach a request.
 */

export type StatusDocumentField = {
  key: string;
  label: string;
  type: "badge" | "datetime" | "text";
};

export type StatusDocumentSurface = {
  /** File-picker hint such as `.txt,.json`; empty accepts any file. */
  accept: string;
  description: string;
  emptyText: string;
  fields: StatusDocumentField[];
  kind: "status_document";
  maxBytes: number;
  /** The status object is the response itself, or the object under this key. */
  statusKey: string;
  statusPath: string;
  uploadDescription: string;
  /** The JSON body key the document text is sent under. */
  uploadField: string;
  uploadLabel: string;
  uploadPath: string;
};

const FIELD_TYPES = new Set(["badge", "datetime", "text"]);
const SURFACE_KEY = /^[A-Za-z_]\w*$/;
const DEFAULT_MAX_BYTES = 65_536;
const MAX_BYTES_LIMIT = 1_048_576;

function verbatimApiPath(value: unknown): string {
  const path = reportText(value);
  // Both paths are called as declared, so they carry no query, fragment or token.
  if (!isSameOriginApiPath(path) || /[?#{}]/.test(path)) return "";
  return path;
}

function normalizeField(value: unknown): StatusDocumentField | null {
  const record = reportMapping(value);
  if (!record) return null;
  const key = reportText(record.key);
  const type = reportText(record.type) || "text";
  if (!SURFACE_KEY.test(key) || !FIELD_TYPES.has(type)) return null;
  return { key, label: reportText(record.label) || key, type: type as StatusDocumentField["type"] };
}

export function normalizeStatusDocumentSurface(value: unknown): StatusDocumentSurface | undefined {
  const record = reportMapping(value);
  if (record?.kind !== "status_document") return undefined;
  const statusPath = verbatimApiPath(record.status_path);
  const uploadPath = verbatimApiPath(record.upload_path);
  if (!statusPath || !uploadPath) return undefined;
  const fields = Array.isArray(record.fields) ? record.fields.map(normalizeField).filter(isTruthy) : [];
  if (fields.length === 0) return undefined;
  const uploadField = reportText(record.upload_field) || "document";
  const statusKey = reportText(record.status_key);
  if (!SURFACE_KEY.test(uploadField) || (statusKey && !SURFACE_KEY.test(statusKey))) return undefined;
  const declaredMax = Number(record.max_bytes);
  const maxBytes = Number.isInteger(declaredMax) && declaredMax >= 1 && declaredMax <= MAX_BYTES_LIMIT ? declaredMax : DEFAULT_MAX_BYTES;
  return {
    accept: reportText(record.accept),
    description: reportText(record.description),
    emptyText: reportText(record.empty_text),
    fields,
    kind: "status_document",
    maxBytes,
    statusKey,
    statusPath,
    uploadDescription: reportText(record.upload_description),
    uploadField,
    uploadLabel: reportText(record.upload_label),
    uploadPath,
  };
}

/** The status object from the response, or null when there is none yet. */
export function statusObject(surface: StatusDocumentSurface, payload: unknown): Record<string, unknown> | null {
  const record = reportMapping(payload);
  if (!record) return null;
  return surface.statusKey ? reportMapping(record[surface.statusKey]) : record;
}

/** A value as one line of text; nested values as compact JSON. Empty for nothing. */
export function statusValueText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return "";
  }
}

/** Size of the text as it is sent (UTF-8). */
export function documentByteLength(text: string): number {
  return new TextEncoder().encode(text).length;
}
