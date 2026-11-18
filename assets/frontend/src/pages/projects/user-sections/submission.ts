import type { InputFieldModel } from "./schema";
import type { ProjectUserSection } from "./types";

/** One run a submission sends: frozen inputs, its files and a stable key for retries. */
export type PreparedRun = {
  itemId: string;
  label: string;
  idempotencyKey: string;
  inputs: Record<string, unknown>;
  files: { inputName: string; file: File }[];
};

export type SubmissionProblem =
  | { code: "required"; field: string }
  | { code: "nothing_to_send" };

function isEmpty(value: unknown): boolean {
  return value === undefined || value === null || value === "" || (Array.isArray(value) && value.length === 0);
}

/** Drops empty optional values: the contract decides defaults, not the form. */
function cleanInputs(fields: InputFieldModel[], values: Record<string, unknown>): Record<string, unknown> {
  const inputs: Record<string, unknown> = {};
  for (const field of fields) {
    if (field.kind === "file") continue;
    const value = values[field.name];
    if (Array.isArray(value)) {
      const items = value.filter((item) => !isEmpty(item));
      if (items.length) inputs[field.name] = items;
    } else if (!isEmpty(value)) {
      inputs[field.name] = value;
    }
  }
  return inputs;
}

/** A file's identity within one submission: two receipts named alike stay two items. */
function fileIdentity(file: File, index: number): string {
  return `${index}-${file.size}-${file.lastModified}`;
}

/**
 * Turns one form submission into the runs it starts (ADR 0075).
 *
 * For each: one run per file (a file input) or per list item (a described list,
 * sent as a one-item list); the other values repeat in every run. Bulk: one run.
 * Each run's key is the submission id plus the item's identity, so retrying a
 * failed item resends the same request and never doubles an accepted one.
 */
export function prepareSectionRuns({
  section,
  fields,
  values,
  files,
  submissionId,
}: Readonly<{
  section: Pick<ProjectUserSection, "id" | "submission">;
  fields: InputFieldModel[];
  values: Record<string, unknown>;
  files: Record<string, File[]>;
  submissionId: string;
}>): { runs: PreparedRun[]; problems: SubmissionProblem[] } {
  const problems: SubmissionProblem[] = [];
  const target = section.submission.mode === "foreach" ? section.submission.target_input ?? "" : "";
  for (const field of fields) {
    if (!field.required || field.name === target) continue;
    const missing = field.kind === "file" ? !(files[field.name] ?? []).length : isEmpty(values[field.name]);
    if (missing) problems.push({ code: "required", field: field.name });
  }
  const shared = cleanInputs(fields, values);
  const singleFiles = fields
    .filter((field) => field.kind === "file" && field.name !== target)
    .flatMap((field) => (files[field.name] ?? []).slice(0, 1).map((file) => ({ inputName: field.name, file })));
  const key = (itemId: string) => `section:${section.id}:${submissionId}:${itemId}`;

  let runs: PreparedRun[] = [];
  if (!target) {
    runs = [{ itemId: "all", label: section.id, idempotencyKey: key("all"), inputs: shared, files: singleFiles }];
  } else {
    const targetField = fields.find((field) => field.name === target);
    if (targetField?.kind === "file") {
      runs = (files[target] ?? []).map((file, index) => {
        const itemId = fileIdentity(file, index);
        return {
          itemId,
          label: file.name,
          idempotencyKey: key(itemId),
          inputs: shared,
          files: [...singleFiles, { inputName: target, file }],
        };
      });
    } else {
      const items = Array.isArray(values[target]) ? (values[target] as unknown[]).filter((item) => !isEmpty(item)) : [];
      runs = items.map((item, index) => {
        const itemId = `item-${index}`;
        return {
          itemId,
          label: typeof item === "object" ? `#${index + 1}` : String(item),
          idempotencyKey: key(itemId),
          inputs: { ...shared, [target]: [item] },
          files: singleFiles,
        };
      });
    }
    if (!runs.length) problems.push({ code: "nothing_to_send" });
  }
  return { runs: problems.length ? [] : runs, problems };
}

/** Runs promises with at most `limit` in flight; one failure does not stop the others. */
export async function runWithConcurrency<T>(
  tasks: ReadonlyArray<() => Promise<T>>,
  limit: number,
  isCancelled: () => boolean = () => false,
): Promise<void> {
  let next = 0;
  const worker = async () => {
    while (next < tasks.length && !isCancelled()) {
      const task = tasks[next];
      next += 1;
      try {
        await task();
      } catch {
        // Each task reports its own failure; the batch goes on.
      }
    }
  };
  await Promise.all(Array.from({ length: Math.max(1, Math.min(limit, tasks.length)) }, worker));
}
