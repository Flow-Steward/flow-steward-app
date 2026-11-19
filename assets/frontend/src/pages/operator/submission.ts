import {
  collectFieldValue,
  collectFiles,
  collectInputs,
  formProblems,
  isFileField,
  shownFields,
  type FormFieldView,
  type FormFiles,
  type FormProblem,
  type FormValues,
} from "@/components/workflow-inputs/form-field";
import { exceedsRunUploadLimits } from "@/lib/auth/run-upload-limits";
import type { RunUploadLimits } from "@/lib/auth/AuthProvider";

import type { OperatorSubmission } from "./types";

/** Requests in flight at once for a For each submission; not a server capacity. */
export const OPERATOR_RUN_CONCURRENCY = 3;

/** One run a submission sends: frozen inputs, its files and a stable key for retries. */
export type PreparedRun = {
  itemId: string;
  label: string;
  idempotencyKey: string;
  inputs: Record<string, unknown>;
  /** One entry per file, in upload order; a list of files repeats its input name. */
  files: { inputName: string; file: File }[];
};

export type SubmissionProblem = FormProblem | { field: string; code: "nothing_to_send" };

/** A file's identity within one submission: two receipts named alike stay two items. */
function fileIdentity(file: File, index: number): string {
  return `${index}-${file.size}-${file.lastModified}`;
}

/**
 * Turns one form submission into the runs it starts (ADR 0075 «Режимы отправки»).
 *
 * Bulk: one run with every shown value and every file. For each over the target
 * input: one run per file (a file or list-of-files input) or per list item (sent
 * as a one-item list); the other values and files repeat in every run. Each
 * run's key is the submission id plus the item's identity, so retrying a failed
 * item resends the same request and never doubles an accepted one.
 */
export function prepareOperatorRuns({
  keyPrefix,
  submission,
  fields,
  values,
  files,
  submissionId,
}: Readonly<{
  keyPrefix: string;
  submission: OperatorSubmission | undefined;
  fields: readonly FormFieldView[];
  values: FormValues;
  files: FormFiles;
  submissionId: string;
}>): { runs: PreparedRun[]; problems: SubmissionProblem[] } {
  const shown = shownFields(fields, values, files);
  const targetField = submission?.mode === "foreach" ? shown.find((field) => field.name === submission.target_input) : undefined;
  const target = targetField?.name ?? "";
  const problems: SubmissionProblem[] = formProblems(fields, values, files, target);
  const key = (itemId: string) => `operator:${keyPrefix}:${submissionId}:${itemId}`;
  const sharedInputs = collectInputs(fields, values, files);
  delete sharedInputs[target];
  const allFiles = collectFiles(fields, values, files);
  const sharedFiles = allFiles.filter((entry) => entry.inputName !== target);

  let runs: PreparedRun[];
  if (!targetField) {
    runs = [{ itemId: "all", label: "", idempotencyKey: key("all"), inputs: sharedInputs, files: allFiles }];
  } else if (isFileField(targetField)) {
    runs = (files[target] ?? []).map((file, index) => {
      const itemId = fileIdentity(file, index);
      return { itemId, label: file.name, idempotencyKey: key(itemId), inputs: sharedInputs, files: [...sharedFiles, { inputName: target, file }] };
    });
  } else {
    const items = collectFieldValue(targetField, values);
    runs = (Array.isArray(items) ? items : []).map((item, index) => {
      const itemId = `item-${index}`;
      return {
        itemId,
        label: item && typeof item === "object" ? `#${index + 1}` : String(item),
        idempotencyKey: key(itemId),
        inputs: { ...sharedInputs, [target]: [item] },
        files: sharedFiles,
      };
    });
  }
  if (targetField && !runs.length && !problems.some((problem) => problem.field === target)) {
    problems.push({ field: target, code: "nothing_to_send" });
  }
  return { runs: problems.length ? [] : runs, problems };
}

/** Whether a run's files break the installation's upload policy, or one is empty. */
export function runBreaksUploadLimits(run: Pick<PreparedRun, "files">, limits: RunUploadLimits | undefined): boolean {
  const sizes = run.files.map(({ file }) => file.size);
  return sizes.some((size) => size <= 0) || exceedsRunUploadLimits(sizes, limits);
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
