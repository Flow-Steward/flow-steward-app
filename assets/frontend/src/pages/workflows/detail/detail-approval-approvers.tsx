import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useMemo, useState } from "react";

import { MultiCombobox, type DsOption } from "@/components/shared/design-system";
import { Button } from "@/components/ui/button";
import { useFeature } from "@/lib/auth/features";
import { useI18n } from "@/lib/i18n/useI18n";

import {
  approverQueryKeys,
  fetchApproverCandidates,
  fetchStepApprovers,
  saveStepApprovers,
  type ApproverCandidate,
} from "./detail-approval-approvers-api";

/** The entitlement that offers the approver picker (ADR 0082). */
const APPROVAL_ASSIGNEES_FEATURE = "approval_assignees";

function personOption(person: ApproverCandidate): DsOption {
  const name = (person.display_name || "").trim() || person.username;
  return {
    value: person.user_id,
    label: name,
    description: person.username && person.username !== name ? person.username : undefined,
  };
}

function sameIds(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((id) => right.includes(id));
}

/**
 * Who may decide an Approval step. The list lives beside the workflow, not in
 * its definition, so it is saved on its own with "Save approvers" and takes
 * effect at once; the step's other parameters keep their own save. A step the
 * server does not have yet has no stable id, so the picker waits until the
 * workflow with this step is saved.
 */
export function ApprovalStepApprovers({ workflowId, stepId, stepOnServer }: Readonly<{
  workflowId: string;
  stepId: string;
  stepOnServer: boolean;
}>) {
  const { t } = useI18n();
  const feature = useFeature(APPROVAL_ASSIGNEES_FEATURE);
  if (!feature.enabled) return null;
  if (!stepOnServer || !workflowId || !stepId) {
    return (
      <div className="fs-field" data-testid="approval-approvers">
        <span className="fs-field-label">{t("react.workflows.approvers", "Approvers")}</span>
        <p className="fs-field-helper">
          {t("react.workflows.approvers_after_save", "Save the workflow with this step to choose who may decide it.")}
        </p>
      </div>
    );
  }
  return <ApproverPicker base={feature.endpoint} key={`${workflowId}\u0000${stepId}`} stepId={stepId} workflowId={workflowId} />;
}

function ApproverPicker({ base, workflowId, stepId }: Readonly<{ base: string; workflowId: string; stepId: string }>) {
  const { t } = useI18n();
  const helperId = useId();
  const queryClient = useQueryClient();
  const candidatesQuery = useQuery({
    queryKey: approverQueryKeys.candidates(base, workflowId),
    queryFn: () => fetchApproverCandidates(base, workflowId),
  });
  const stepKey = approverQueryKeys.step(base, workflowId, stepId);
  const approversQuery = useQuery({
    queryKey: stepKey,
    queryFn: () => fetchStepApprovers(base, workflowId, stepId),
  });
  const savedIds = useMemo(() => (approversQuery.data?.approvers ?? []).map((person) => person.user_id), [approversQuery.data]);
  const [draft, setDraft] = useState<string[] | null>(null);
  const [saved, setSaved] = useState(false);
  const selected = draft ?? savedIds;
  const dirty = draft !== null && !sameIds(draft, savedIds);
  const mutation = useMutation({
    mutationFn: (userIds: string[]) => saveStepApprovers(base, workflowId, stepId, userIds),
    onSuccess: (result) => {
      queryClient.setQueryData(stepKey, result);
      setDraft(null);
      setSaved(true);
    },
  });
  // Assigned people stay visible even when the candidate list no longer offers them.
  const options = useMemo(() => {
    const rows = new Map<string, DsOption>();
    for (const person of [...(candidatesQuery.data ?? []), ...(approversQuery.data?.approvers ?? [])]) {
      if (person.user_id && !rows.has(person.user_id)) rows.set(person.user_id, personOption(person));
    }
    return [...rows.values()];
  }, [approversQuery.data, candidatesQuery.data]);
  const loadFailed = candidatesQuery.isError || approversQuery.isError;
  const label = t("react.workflows.approvers", "Approvers");
  return (
    <div className="fs-field min-w-0" data-testid="approval-approvers">
      <span className="fs-field-label">{label}</span>
      <MultiCombobox
        ariaLabel={label}
        disabled={approversQuery.isPending || mutation.isPending}
        loading={candidatesQuery.isPending || approversQuery.isPending}
        loadingLabel={t("react.workflows.loading_people", "Loading people…")}
        onChange={(next) => { setDraft(next); setSaved(false); mutation.reset(); }}
        options={options}
        placeholder={t("react.workflows.approvers_placeholder", "Search people")}
        selected={selected}
        selectionLayout="list"
      />
      <span className="fs-field-helper" id={helperId}>
        {selected.length
          ? t("react.workflows.approvers_restricted_help", "Only the people chosen here can decide this step.")
          : t("react.workflows.approvers_anyone_help", "No one chosen: anyone who may decide approvals can decide this step.")}
      </span>
      {loadFailed ? (
        <p className="fs-field-helper error" role="alert">
          {t("react.workflows.approvers_load_failed", "Approvers could not be loaded. Reopen the step to try again.")}
        </p>
      ) : null}
      {mutation.isError ? (
        <p className="fs-field-helper error" role="alert">
          {mutation.error instanceof Error && mutation.error.message
            ? t("react.workflows.approvers_save_failed_reason", "Approvers were not saved: {reason}", { reason: mutation.error.message })
            : t("react.workflows.approvers_save_failed", "Approvers were not saved.")}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          aria-describedby={helperId}
          busy={mutation.isPending}
          busyLabel={t("react.workflows.approvers_saving", "Saving approvers…")}
          disabled={!dirty || loadFailed}
          onClick={() => { if (draft !== null) mutation.mutate(draft); }}
          size="sm"
          type="button"
          variant="secondary"
        >
          {t("react.workflows.approvers_save", "Save approvers")}
        </Button>
        <span aria-live="polite" className="fs-field-helper">
          {saved && !dirty ? t("react.workflows.approvers_saved", "Approvers saved.") : ""}
        </span>
      </div>
      <p className="fs-field-helper">
        {t("react.workflows.approvers_saved_separately", "Approvers are saved on their own and apply at once; Save parameters does not include them.")}
      </p>
    </div>
  );
}
