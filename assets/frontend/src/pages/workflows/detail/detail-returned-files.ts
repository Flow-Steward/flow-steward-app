/**
 * "Return from workflow" on a step's file results.
 *
 * The setting lives on the step, in `override.returned_files`: one entry per
 * ticked result, `{ result, key }`. The key is made once, from the result's
 * variable, when the result is ticked, and is never derived again: a later
 * file name, run or rename of the step does not change what a parent maps.
 * The workflow's returned files (its file outputs) are derived from these
 * entries on the server; Outputs only lists them.
 */
import { contractItemName, contractItemType, objectOrEmpty, textOrEmpty } from "@/pages/workflows/detail/detail-helpers";

export const RETURNED_FILES_FIELD = "returned_files";

export type StepFileResult = {
  /** The result id within the step: "file", "response", or an output's name. */
  result: string;
  /** The workflow variable the step saves the file in. */
  target: string;
};

export type ReturnedFileEntry = { result: string; key: string; required?: boolean };

const FILE_OUTPUT_TYPES = new Set(["file", "artifact", "artifact_handle", "artifacts", "files"]);

function stateTarget(value: unknown): string {
  const target = textOrEmpty(value);
  return target.startsWith("state.") && target.length > "state.".length ? target : "";
}

/** The step's results that hold a file, as the step is configured now. */
export function stepFileResults(
  stepKind: string,
  override: Record<string, unknown>,
  connectorOutputRows: Array<Record<string, unknown>> = [],
): StepFileResult[] {
  const config = objectOrEmpty(override[stepKind]);
  const rows: StepFileResult[] = [];
  if (stepKind === "file_from_base64") {
    rows.push({ result: "file", target: stateTarget(config.result_target) });
  } else if (stepKind === "http_request") {
    if (textOrEmpty(config.response_mode) === "artifact") rows.push({ result: "response", target: stateTarget(config.response_target) });
  } else if (stepKind === "invoke_workflow") {
    Object.entries(objectOrEmpty(config.artifact_mapping)).forEach(([result, target]) => rows.push({ result, target: stateTarget(target) }));
  } else if (stepKind === "connector") {
    const fileOutputs = new Set(connectorOutputRows
      .filter((row) => FILE_OUTPUT_TYPES.has(contractItemType(row).toLowerCase()))
      .map((row) => contractItemName(row)));
    Object.entries(objectOrEmpty(config.output_mapping)).forEach(([result, target]) => {
      if (fileOutputs.has(result)) rows.push({ result, target: stateTarget(target) });
    });
  }
  return rows.filter((row) => row.result && row.target);
}

/** The saved entries, or `null` when the step has never had the setting. */
export function returnedFileEntries(override: Record<string, unknown>): ReturnedFileEntry[] | null {
  if (!Object.hasOwn(override, RETURNED_FILES_FIELD)) return null;
  const raw = override[RETURNED_FILES_FIELD];
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((item) => {
    const row = objectOrEmpty(item);
    const result = textOrEmpty(row.result);
    if (!result) return [];
    return [{ result, key: textOrEmpty(row.key), ...(row.required === true ? { required: true } : {}) }];
  });
}

const RESERVED_KEYS = new Set([
  "sys", "inputs", "outputs", "workflow_data", "materialized_inputs", "task_id",
  "task_summary", "task_source", "mode", "files",
]);

/** A key for a newly ticked result: its variable name, not used by another output. */
export function newReturnedFileKey(result: StepFileResult, takenKeys: Iterable<string>): string {
  const taken = new Set(takenKeys);
  const segments = result.target.slice("state.".length).split(".");
  let base = (segments.at(-1) || result.result).replaceAll(/[^A-Za-z0-9_]+/g, "_");
  let start = 0;
  while (base[start] === "_") start += 1;
  let end = base.length;
  while (end > start && base[end - 1] === "_") end -= 1;
  base = base.slice(start, end);
  if (!base || RESERVED_KEYS.has(base)) base = `${base || "file"}_file`;
  let key = base;
  for (let index = 2; taken.has(key); index += 1) key = `${base}_${index}`;
  return key;
}

/**
 * The step's entries after ticking or unticking *result*.
 *
 * *current* is what the step returns now — its own entries, or, for a step
 * saved before the setting existed, the results the server linked to it — so
 * the first change keeps every other returned result and its key.
 */
export function toggledReturnedFiles(
  current: ReturnedFileEntry[],
  result: StepFileResult,
  checked: boolean,
  takenKeys: Iterable<string>,
): ReturnedFileEntry[] {
  const others = current.filter((entry) => entry.result !== result.result);
  if (!checked) return others;
  const existing = current.find((entry) => entry.result === result.result);
  if (existing) return current;
  return [...others, { result: result.result, key: newReturnedFileKey(result, takenKeys) }];
}

/**
 * What the step returns now: its saved entries, or, when it has none yet, the
 * contract's returned files whose source is this step (a workflow saved before
 * the setting existed).
 */
export function effectiveReturnedFiles(
  override: Record<string, unknown>,
  stepId: string,
  contractReturnedFiles: Array<Record<string, unknown>>,
): ReturnedFileEntry[] {
  const saved = returnedFileEntries(override);
  if (saved) return saved;
  return contractReturnedFiles.flatMap((row) => {
    const source = objectOrEmpty(row.source);
    if (textOrEmpty(source.step_id) !== stepId) return [];
    return [{ result: textOrEmpty(source.result), key: contractItemName(row), ...(row.required === true ? { required: true } : {}) }];
  });
}
