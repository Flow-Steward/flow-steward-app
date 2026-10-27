import { definitionFromWorkflow, type WorkflowDefinitionModel } from "@/pages/workflows/builder/change-set";
import type { GraphJournal, JournalItem, PendingEntry } from "@/pages/workflows/builder/canvas-history";
import { storableGraphJournal } from "@/pages/workflows/builder/canvas-history";
import type { WorkflowDetail } from "@/pages/workflows/shared/api";

/**
 * The builder's edit session (Canvas › Undo, edit session): the author's local
 * copy of one workflow, its Undo/Redo history and the last confirmed server
 * checkpoint, as one serializable record kept in the browser.
 *
 * Only the author's public content is stored: the fields an author owns, the
 * definition and the canvas placements of the history. Nothing the server owns
 * (enabled, provenance, notes, secret and file authority) and no credential
 * typed into a form: a draft keeps only the fact that such a field must be
 * entered again.
 */

export const WORKFLOW_EDIT_SESSION_SCHEMA_VERSION = 1;

/** The fields an author owns, as the server's edit session contract accepts them. */
export const AUTHORING_FIELDS = [
  "version", "display_name", "description", "tags", "builder_metadata_jsonb", "trigger",
  "settings", "steps", "groups", "for_each",
] as const;
/** Settings kept by their own services: never part of the author's content. */
const SERVICE_OWNED_SETTINGS = new Set(["canvas_annotations", "canvas_annotations_revision"]);
/** Input fields that point at stored credentials and files: never stored, never sent. */
const INPUT_AUTHORITY_KEYS = new Set(["default_secret_ref", "default_configured", "sample_file"]);
/** Draft fields whose values are credentials: kept only as "enter it again". */
const SENSITIVE_DRAFT_KEY = /(?:api[_-]?key|authorization|bearer|cookie|credential|passphrase|password|private[_-]?key|secret|token)/i;

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
export type WorkflowAuthoringContent = {
  [key: string]: JsonValue;
  settings: { [key: string]: JsonValue };
  steps: JsonValue[];
  groups: JsonValue[];
  for_each: JsonValue[];
};
export type WorkflowEditSessionIdentity = Readonly<{ accountId: string; userId: string; workflowId: string }>;
/** A form's unsaved values; `reentry` names the credential fields that were left out. */
export type StoredFormDraft = { value: JsonValue; reentry: string[] };
export type WorkflowServerCheckpoint = { revision: number; content: WorkflowAuthoringContent };

export type WorkflowEditSession = {
  schemaVersion: typeof WORKFLOW_EDIT_SESSION_SCHEMA_VERSION;
  sessionId: string;
  identity: WorkflowEditSessionIdentity;
  /** One more for every committed author action, Undo, Redo or draft change. */
  editSequence: number;
  origin: WorkflowServerCheckpoint;
  serverCheckpoint: WorkflowServerCheckpoint;
  history: GraphJournal;
  currentContent: WorkflowAuthoringContent;
  formDrafts: Record<string, StoredFormDraft>;
  savedAt: string;
};

export type RecoveryProblem = "version" | "corrupt" | "identity" | "checkpoint";
export type ParsedEditSession =
  | { status: "ok"; session: WorkflowEditSession }
  | { status: "unavailable"; reason: RecoveryProblem };

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

/**
 * A JSON copy of *value*: functions, files, blobs and other non-plain objects
 * are left out, so nothing but data can reach the browser's database.
 */
export function storableJson(value: unknown, seen = new WeakSet<object>()): JsonValue | undefined {
  if (value === null) return null;
  if (typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (Array.isArray(value)) {
    if (seen.has(value)) return undefined;
    seen.add(value);
    const items = value.map((item) => storableJson(item, seen) ?? null);
    seen.delete(value);
    return items;
  }
  if (!isRecord(value) || Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null) return undefined;
  if (seen.has(value)) return undefined;
  seen.add(value);
  const result: { [key: string]: JsonValue } = {};
  Object.entries(value).forEach(([key, item]) => {
    const stored = storableJson(item, seen);
    if (stored !== undefined) result[key] = stored;
  });
  seen.delete(value);
  return result;
}

function inputsWithoutAuthority(settings: { [key: string]: JsonValue }): { [key: string]: JsonValue } {
  const callable = settings.callable;
  if (!isRecord(callable) || !Array.isArray(callable.expected_inputs)) return settings;
  return {
    ...settings,
    callable: {
      ...(callable as { [key: string]: JsonValue }),
      expected_inputs: (callable.expected_inputs as JsonValue[]).map((row) => isRecord(row)
        ? Object.fromEntries(Object.entries(row).filter(([key]) => !INPUT_AUTHORITY_KEYS.has(key))) as { [key: string]: JsonValue }
        : row),
    },
  };
}

/** The author's content of a workflow as the server's edit session contract accepts it. */
export function authoringContentOf(workflow: Partial<WorkflowDetail> | Record<string, unknown> | null | undefined): WorkflowAuthoringContent {
  const source = (workflow ?? {}) as Record<string, unknown>;
  const content: { [key: string]: JsonValue } = {};
  AUTHORING_FIELDS.forEach((field) => {
    if (!(field in source)) return;
    const value = storableJson(source[field]);
    if (value !== undefined) content[field] = value;
  });
  const rawSettings = isRecord(content.settings) ? content.settings as { [key: string]: JsonValue } : {};
  const settings = Object.fromEntries(Object.entries(rawSettings).filter(([key]) => !SERVICE_OWNED_SETTINGS.has(key)));
  return {
    ...content,
    settings: inputsWithoutAuthority(settings),
    steps: storedOwners(content.steps),
    groups: storedOwners(content.groups),
    for_each: storedOwners(content.for_each),
  };
}

/**
 * The detail read shows only the valid rows of a boundary contract and keeps
 * the author's rows beside it (`raw_expected_inputs`): the content keeps the
 * author's rows, so saving it whole never drops an unfinished row.
 */
function storedOwners(value: JsonValue | undefined): JsonValue[] {
  if (!Array.isArray(value)) return [];
  return value.map((owner) => {
    if (!isRecord(owner) || !("raw_expected_inputs" in owner)) return owner;
    const rest = { ...(owner as { [key: string]: JsonValue }) };
    const raw = rest.raw_expected_inputs;
    delete rest.raw_expected_inputs;
    delete rest.expected_input_issues;
    const boundary = isRecord(rest.boundary_contract) ? rest.boundary_contract as { [key: string]: JsonValue } : {};
    return { ...rest, boundary_contract: { ...boundary, entry: raw } };
  });
}

/** The content with another definition: the steps, containers and routing graph. */
export function contentWithDefinition(content: WorkflowAuthoringContent, definition: WorkflowDefinitionModel): WorkflowAuthoringContent {
  return {
    ...content,
    settings: { ...content.settings, execution_graph: storableJson(definition.graph) ?? null },
    steps: (storableJson(definition.steps) as JsonValue[] | undefined) ?? [],
    groups: (storableJson(definition.groups) as JsonValue[] | undefined) ?? [],
    for_each: (storableJson(definition.forEach) as JsonValue[] | undefined) ?? [],
  };
}

export function definitionOfContent(content: WorkflowAuthoringContent): WorkflowDefinitionModel {
  return definitionFromWorkflow(content as unknown as WorkflowDetail);
}

/**
 * A form's unsaved values for storage. Credential-like fields (by name, at any
 * depth) and the fields the form names are left out; their paths are kept so
 * the restored form asks for them again instead of clearing a saved value.
 */
export function storableFormDraft(value: unknown, sensitiveFields: readonly string[] = []): StoredFormDraft {
  const reentry: string[] = [];
  const named = new Set(sensitiveFields);
  const strip = (item: unknown, path: string): JsonValue | undefined => {
    if (Array.isArray(item)) return item.map((entry, index) => strip(entry, `${path}[${index}]`) ?? null);
    if (!isRecord(item)) return storableJson(item);
    const result: { [key: string]: JsonValue } = {};
    Object.entries(item).forEach(([key, entry]) => {
      const entryPath = path ? `${path}.${key}` : key;
      if (named.has(entryPath) || SENSITIVE_DRAFT_KEY.test(key)) {
        if (entry !== "" && entry !== null && entry !== undefined) reentry.push(entryPath);
        return;
      }
      const stored = strip(entry, entryPath);
      if (stored !== undefined) result[key] = stored;
    });
    return result;
  };
  return { value: strip(value, "") ?? null, reentry };
}

export function newEditSessionId(): string {
  try {
    if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
  } catch {
    // A failing randomUUID can still use getRandomValues below.
  }
  const crypto = globalThis.crypto;
  if (typeof crypto?.getRandomValues !== "function") throw new Error("Secure random session IDs are unavailable");
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return `session-${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

/** Whether the session holds anything to recover: unapplied work, history or form drafts. */
export function sessionHasLocalWork(session: Pick<WorkflowEditSession, "history" | "formDrafts">): boolean {
  return session.history.items.length > 0 || session.history.pending.length > 0 || Object.keys(session.formDrafts).length > 0;
}

export function buildEditSession(input: {
  sessionId: string;
  identity: WorkflowEditSessionIdentity;
  editSequence: number;
  origin: WorkflowServerCheckpoint;
  serverCheckpoint: WorkflowServerCheckpoint;
  journal: GraphJournal;
  currentDefinition: WorkflowDefinitionModel;
  formDrafts: Record<string, StoredFormDraft>;
  now?: Date;
}): WorkflowEditSession {
  return {
    schemaVersion: WORKFLOW_EDIT_SESSION_SCHEMA_VERSION,
    sessionId: input.sessionId,
    identity: { ...input.identity },
    editSequence: input.editSequence,
    origin: input.origin,
    serverCheckpoint: input.serverCheckpoint,
    history: storableJson(storableGraphJournal(input.journal)) as unknown as GraphJournal,
    currentContent: contentWithDefinition(input.serverCheckpoint.content, input.currentDefinition),
    formDrafts: input.formDrafts,
    savedAt: (input.now ?? new Date()).toISOString(),
  };
}

function validCheckpoint(value: unknown): value is WorkflowServerCheckpoint {
  return isRecord(value) && Number.isSafeInteger(value.revision) && Number(value.revision) > 0 &&
    isRecord(value.content) && Array.isArray(value.content.steps) && Array.isArray(value.content.groups) &&
    Array.isArray(value.content.for_each) && isRecord(value.content.settings);
}

function validItem(value: unknown): value is JournalItem {
  return isRecord(value) && Number.isSafeInteger(value.id) && Array.isArray(value.operations) &&
    Number.isFinite(value.seq) && (value.inverse === undefined || Array.isArray(value.inverse));
}

function validPending(value: unknown): value is PendingEntry {
  return isRecord(value) && Number.isSafeInteger(value.item) && (value.direction === "forward" || value.direction === "inverse") &&
    Array.isArray(value.operations);
}

function validJournal(value: unknown): value is GraphJournal {
  if (!isRecord(value) || !Array.isArray(value.items) || !Array.isArray(value.pending)) return false;
  if (!Number.isSafeInteger(value.cursor) || Number(value.cursor) < 0 || Number(value.cursor) > value.items.length) return false;
  return Number.isSafeInteger(value.nextId) && value.items.every(validItem) && value.pending.every(validPending);
}

/**
 * A stored record as a session of this identity, or why it cannot be
 * recovered. A partly readable record is never used: the whole record is
 * valid, or recovery is unavailable and the record stays until the author
 * decides.
 */
export function parseStoredEditSession(raw: unknown, identity: WorkflowEditSessionIdentity): ParsedEditSession {
  if (!isRecord(raw)) return { status: "unavailable", reason: "corrupt" };
  if (raw.schemaVersion !== WORKFLOW_EDIT_SESSION_SCHEMA_VERSION) return { status: "unavailable", reason: "version" };
  const stored = raw.identity;
  if (!isRecord(stored)) return { status: "unavailable", reason: "corrupt" };
  if (stored.accountId !== identity.accountId || stored.userId !== identity.userId || stored.workflowId !== identity.workflowId) {
    return { status: "unavailable", reason: "identity" };
  }
  if (!validCheckpoint(raw.serverCheckpoint) || !validCheckpoint(raw.origin)) return { status: "unavailable", reason: "checkpoint" };
  if (typeof raw.sessionId !== "string" || !raw.sessionId || !Number.isSafeInteger(raw.editSequence) ||
    !validJournal(raw.history) || !isRecord(raw.currentContent) || !isRecord(raw.formDrafts)) {
    return { status: "unavailable", reason: "corrupt" };
  }
  return { status: "ok", session: raw as unknown as WorkflowEditSession };
}
