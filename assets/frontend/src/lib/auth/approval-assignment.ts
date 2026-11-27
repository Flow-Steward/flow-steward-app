import { isHttpError } from "@/lib/http/errors";
import type { I18nContextValue } from "@/lib/i18n/context";

/**
 * Who may decide a waiting Approval step: the server's `can_decide` and the
 * step's assigned `approvers` on an approval gate or an awaiting-approval row.
 */
export type ApprovalAssignment = {
  /** Absent `can_decide` means the person may decide, exactly as before. */
  canDecide: boolean;
  /** Display names of the step's approvers; empty when the step is unrestricted. */
  approvers: string[];
};

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function approverName(value: unknown): string {
  const row = record(value);
  for (const candidate of [row.display_name, row.username]) {
    if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
  }
  return "";
}

/**
 * The assignment of the first source that states one. A gate may carry the
 * fields on itself or on its `payload`, so callers pass every shape they hold.
 */
export function approvalAssignment(...sources: unknown[]): ApprovalAssignment {
  const records = sources.map(record);
  const stated = records.find((row) => typeof row.can_decide === "boolean");
  const listed = records.map((row) => row.approvers).find((value): value is unknown[] => Array.isArray(value));
  const approvers = (listed ?? []).map(approverName).filter(Boolean);
  return { canDecide: stated?.can_decide !== false, approvers };
}

/** "Waiting for Ann, Bob." — what a person who may not decide sees instead of the buttons. */
export function approvalWaitingForText(approvers: readonly string[], t: I18nContextValue["t"]): string {
  return approvers.length
    ? t("react.approvals.waiting_for_approvers", "Waiting for {approvers} to decide.", { approvers: approvers.join(", ") })
    : t("react.approvals.waiting_for_assigned_approvers", "Waiting for the approvers assigned to this step to decide.");
}

/** A decision the server refused because the step is assigned to other people. */
export function isApprovalNotAssigned(error: unknown): boolean {
  if (!isHttpError(error) || error.status !== 403) return false;
  const payload = record(error.payload);
  return [payload.code, record(payload.detail).code, record(payload.error).code].includes("approval_not_assigned");
}

/** The message for a failed decision: the assignment refusal in plain words, anything else as it came. */
export function approvalDecisionErrorMessage(error: unknown, t: I18nContextValue["t"]): string {
  if (isApprovalNotAssigned(error)) {
    return t("react.approvals.not_assigned", "Only the approvers assigned to this step can decide it.");
  }
  return error instanceof Error ? error.message : t("react.jobs.approval_request_failed", "Approval request failed.");
}
