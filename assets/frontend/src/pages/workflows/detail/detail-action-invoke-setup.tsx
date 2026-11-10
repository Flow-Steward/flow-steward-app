import { useEffect, type ReactNode } from "react";

import { useQuery } from "@tanstack/react-query";

import { DsAlert, DsSelect, RequiredLabel } from "@/components/shared/design-system";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n/useI18n";
import { WorkflowStepExtensionMark } from "@/pages/workflows/shared/workflow-step-icon";

import { actionInvokeRef } from "./detail-action-invoke";
import {
  contractRows,
  listFromUnknown,
  objectOrEmpty,
  saveJson,
  textOrEmpty,
} from "@/pages/workflows/detail/detail-helpers";

type Row = Record<string, unknown>;

function connectionLabel(connection: Row): string {
  const name = textOrEmpty(connection.display_name) || textOrEmpty(connection.connection_id);
  const account = textOrEmpty(connection.verified_email) || textOrEmpty(connection.verified_display_name);
  return account && account !== name ? `${name} (${account})` : name;
}

export type ActionInvokeSetupContext = Readonly<{
  stepKindConfig: Row;
  selectedProjectId?: string;
  availableWorkflowVariableSuggestions: unknown;
  updateStepKindConfig: (stepKind: string, patch: Row) => void;
  renderWorkflowContractMappingEditor: (
    field: string,
    title: string,
    rows: Row[],
    placeholder: string,
    suggestions: unknown,
  ) => ReactNode;
  onChooseAnotherAction?: () => void;
}>;

/** An Invoke step that calls an extension action: the action, its connections, its mappings. */
export function ActionInvokeSetup({ ctx }: Readonly<{ ctx: ActionInvokeSetupContext }>) {
  const { t, locale } = useI18n();
  const { stepKindConfig, updateStepKindConfig } = ctx;
  const ref = actionInvokeRef(stepKindConfig);
  const installId = ref?.installId || "";
  const projectId = textOrEmpty(ctx.selectedProjectId);
  const contractQuery = useQuery({
    // The same key as the connector editor, so choosing the action reuses its answer.
    queryKey: ["workflows", "meta", "extension-install-contract", installId, projectId, locale],
    queryFn: () => {
      const projectQuery = projectId ? `&project_id=${encodeURIComponent(projectId)}` : "";
      return saveJson(
        `/api/workflows/meta/extension-install-contract?install_id=${encodeURIComponent(installId)}${projectQuery}&locale=${encodeURIComponent(locale)}`,
        { method: "GET" },
      );
    },
    enabled: Boolean(installId),
    retry: false,
  });
  const operation = listFromUnknown(objectOrEmpty(contractQuery.data).operations).find(
    (row) => textOrEmpty(row.action_id || row.operation_id) === ref?.actionId,
  );
  const implementation = objectOrEmpty(objectOrEmpty(operation).implementation);
  const slots = listFromUnknown(implementation.slots);
  const connections = objectOrEmpty(stepKindConfig.connections);
  const resolvedInstalls = JSON.stringify(objectOrEmpty(implementation.installs));
  const savedInstalls = JSON.stringify(objectOrEmpty(stepKindConfig.installs));
  const resolvedWorkflowId = textOrEmpty(implementation.workflow_id);

  useEffect(() => {
    // The release decides the workflow and the dependency installs; the step follows it.
    if (!resolvedWorkflowId) return;
    const patch: Row = {};
    if (resolvedInstalls !== "{}" && resolvedInstalls !== savedInstalls) patch.installs = JSON.parse(resolvedInstalls) as Row;
    if (resolvedWorkflowId !== textOrEmpty(stepKindConfig.workflow_id)) patch.workflow_id = resolvedWorkflowId;
    if (Object.keys(patch).length) updateStepKindConfig("invoke_workflow", patch);
  }, [resolvedInstalls, resolvedWorkflowId, savedInstalls, stepKindConfig.workflow_id, updateStepKindConfig]);

  if (!ref) return null;
  const actionName = textOrEmpty(objectOrEmpty(operation).display_name) || ref.actionId;
  const unavailable = contractQuery.isError || (contractQuery.isSuccess && !resolvedWorkflowId);
  return (
    <div className="space-y-3 rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] p-3" data-testid="action-invoke-setup">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="flex min-w-0 items-center gap-2 text-sm font-medium text-[var(--fs-text-primary)]">
          <WorkflowStepExtensionMark extensionId={ref.extensionId} />
          <span className="truncate">{actionName}</span>
        </p>
        {ctx.onChooseAnotherAction ? (
          <Button size="sm" variant="secondary" onClick={ctx.onChooseAnotherAction}>
            {t("react.workflows.choose_action", "Choose action")}
          </Button>
        ) : null}
      </div>
      {textOrEmpty(objectOrEmpty(operation).description) ? (
        <p className="text-xs text-[var(--fs-text-secondary)]">{textOrEmpty(objectOrEmpty(operation).description)}</p>
      ) : null}
      {unavailable ? (
        <DsAlert
          tone="danger"
          title={t("react.workflows.action_invoke_unavailable", "This extension action is no longer available. Choose the action again.")}
        />
      ) : null}
      {slots.map((slot) => {
        const name = textOrEmpty(slot.slot);
        const options = listFromUnknown(slot.connections);
        const selected = textOrEmpty(objectOrEmpty(connections[name]).connection_id);
        const known = options.some((row) => textOrEmpty(row.connection_id) === selected);
        const label = t("react.workflows.action_invoke_connection", "Connection: {value0}", { value0: name });
        return (
          <label className="block text-xs text-[var(--fs-text-secondary)]" key={name}>
            <RequiredLabel>{label}</RequiredLabel>
            <DsSelect
              ariaLabel={label}
              required
              invalid={!selected || !known}
              className="mt-1"
              onChange={(nextValue) =>
                updateStepKindConfig("invoke_workflow", {
                  connections: { ...connections, [name]: { connection_id: nextValue } },
                })}
              value={selected}
              options={[
                { value: "", label: t("react.workflows.choose_connection", "Choose connection") },
                ...(selected && !known ? [{ value: selected, label: selected, disabled: true }] : []),
                ...options.map((row) => ({ value: textOrEmpty(row.connection_id), label: connectionLabel(row) })),
              ]}
            />
          </label>
        );
      })}
      {ctx.renderWorkflowContractMappingEditor(
        "input_mapping",
        t("react.workflows.display_input_mapping", "Input mapping"),
        contractRows(objectOrEmpty(operation).parameters),
        "state.order",
        ctx.availableWorkflowVariableSuggestions,
      )}
      {ctx.renderWorkflowContractMappingEditor(
        "output_mapping",
        t("react.workflows.output_mapping", "Output mapping"),
        contractRows(objectOrEmpty(operation).returns),
        "state.result",
        ctx.availableWorkflowVariableSuggestions,
      )}
    </div>
  );
}
