import { useI18n } from "@/lib/i18n/useI18n";
import { useState } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, ShieldCheck } from "lucide-react";

import { DsAlert, DsBadge, DsValueChip } from "@/components/shared/design-system-primitives";
import { DsSelect } from "@/components/shared/design-system-selects";
import { Button } from "@/components/ui/button";
import { isHttpError } from "@/lib/http/errors";
import { catalogSecretsQueryKey, fetchCatalogSecrets } from "@/pages/registry/secrets/api";
import { CatalogSecretPicker } from "@/pages/registry/secrets/catalog-secret-picker";
import {
  approveWorkflowSecretUse,
  bindWorkflowInputSecret,
  fetchWorkflowSecretApproval,
  unbindWorkflowInputSecret,
  type WorkflowSecretApprovalStatus,
  type WorkflowSecretDestination,
  workflowSecretApprovalQueryKey,
} from "@/pages/workflows/shared/workflow-secret-catalog-api";

/**
 * Catalog secrets in a workflow's secret inputs (ADR 0055).
 *
 * Binding, unbinding and approval are each their own request. None of them is
 * part of the workflow save, so neither a normal save nor applying an AI Builder
 * draft can bind a secret or approve its use.
 */

function useWorkflowSecretApproval(projectId: string, workflowId: string, revision: number | null | undefined) {
  return useQuery({
    queryKey: workflowSecretApprovalQueryKey(projectId, workflowId, revision),
    queryFn: () => fetchWorkflowSecretApproval(projectId, workflowId),
    retry: false,
  });
}

function useStatusWriter(projectId: string, workflowId: string, revision: number | null | undefined) {
  const queryClient = useQueryClient();
  return (status: WorkflowSecretApprovalStatus) => {
    queryClient.setQueryData(workflowSecretApprovalQueryKey(projectId, workflowId, revision), status);
    void queryClient.invalidateQueries({ queryKey: ["secrets"] });
  };
}

type WorkflowInputSecretBindingProps = Readonly<{
  projectId: string;
  workflowId: string;
  revision?: number | null;
  /** The input's saved name; the server binds only inputs the saved workflow declares. */
  field: string;
  /** Whether this row is a secret input of the saved workflow under the same name. */
  saved: boolean;
}>;

/** The stored secret a secret input uses, chosen from the project's catalog. */
export function WorkflowInputSecretBinding(props: WorkflowInputSecretBindingProps) {
  const { t } = useI18n();
  if (!props.projectId || !props.workflowId) {
    return <p className="fs-field-helper mt-1">{t("react.workflows.secret_catalog.needs_project", "Open this workflow from a project to use a stored secret for this input.")}</p>;
  }
  if (!props.saved || !props.field) {
    return <p className="fs-field-helper mt-1">{t("react.workflows.secret_catalog.save_first", "Save the workflow to choose a stored secret for this input.")}</p>;
  }
  return <WorkflowInputSecretPicker {...props} />;
}

function WorkflowInputSecretPicker({ projectId, workflowId, revision, field }: WorkflowInputSecretBindingProps) {
  const { t } = useI18n();
  const status = useWorkflowSecretApproval(projectId, workflowId, revision);
  const writeStatus = useStatusWriter(projectId, workflowId, revision);
  const bind = useMutation({
    mutationFn: (secretId: string) => bindWorkflowInputSecret(projectId, workflowId, field, secretId),
    onSuccess: writeStatus,
  });
  const unbind = useMutation({
    mutationFn: () => unbindWorkflowInputSecret(projectId, workflowId, field),
    onSuccess: writeStatus,
  });
  const boundSecretId = status.data?.secrets.find((row) => row.field === field)?.secret_id ?? "";
  return (
    <div className="mt-2 border-t border-subtle pt-2" data-testid={`workflow-input-secret-binding-${field}`}>
      <p className="font-medium fs-ink-primary">{t("react.workflows.secret_catalog.title", "Stored secret")}</p>
      <p className="fs-field-helper mt-1">
        {t("react.workflows.secret_catalog.help", "A chosen stored secret is used instead of the value above. Runs can use it only after a person approves the workflow's use of secrets.")}
      </p>
      <CatalogSecretPicker
        allowCreate
        boundSecretId={boundSecretId}
        busy={bind.isPending || unbind.isPending || status.isLoading}
        error={bind.error || unbind.error || status.error}
        fieldLabel={field}
        onChoose={(secretId) => bind.mutateAsync(secretId)}
        onRemove={() => unbind.mutate()}
        projectId={projectId}
      />
    </div>
  );
}

type RuleDraft = { id: number; scheme: string; host: string; port: string; path_prefix: string };

let ruleSequence = 0;
function emptyRule(): RuleDraft {
  ruleSequence += 1;
  return { id: ruleSequence, scheme: "https", host: "", port: "", path_prefix: "" };
}

function destinationLabel(rule: WorkflowSecretDestination): string {
  const port = rule.port ? `:${rule.port}` : "";
  return `${rule.scheme}://${rule.host}${port}${rule.path_prefix || ""}`;
}

/** The exact-host rules a person entered, without the empty rows. */
function destinationsFromDrafts(rules: RuleDraft[]): WorkflowSecretDestination[] {
  return rules
    .filter((rule) => rule.host.trim())
    .map((rule) => {
      const destination: WorkflowSecretDestination = { kind: "exact_host", scheme: rule.scheme, host: rule.host.trim() };
      const port = Number(rule.port.trim());
      if (rule.port.trim() && Number.isInteger(port) && port > 0) destination.port = port;
      if (rule.path_prefix.trim()) destination.path_prefix = rule.path_prefix.trim();
      return destination;
    });
}

function DestinationRules({ rules, onChange, disabled }: Readonly<{ rules: RuleDraft[]; onChange: (rules: RuleDraft[]) => void; disabled: boolean }>) {
  const { t } = useI18n();
  const update = (id: number, patch: Partial<RuleDraft>) => onChange(rules.map((rule) => (rule.id === id ? { ...rule, ...patch } : rule)));
  return (
    <div className="space-y-2">
      {rules.map((rule, index) => (
        <div className="flex flex-wrap items-end gap-2" key={rule.id}>
          <label className="block">
            <span className="fs-ink-tertiary">{t("react.workflows.secret_approval.scheme", "Scheme")}</span>
            <DsSelect
              ariaLabel={t("react.workflows.secret_approval.scheme_n", "Scheme of rule {index}", { index: index + 1 })}
              disabled={disabled}
              onChange={(scheme) => update(rule.id, { scheme })}
              options={[{ value: "https", label: "https" }, { value: "http", label: "http" }]}
              value={rule.scheme}
            />
          </label>
          <label className="block">
            <span className="fs-ink-tertiary">{t("react.workflows.secret_approval.host", "Host")}</span>
            <input
              aria-label={t("react.workflows.secret_approval.host_n", "Host of rule {index}", { index: index + 1 })}
              className="fs-input"
              disabled={disabled}
              onChange={(event) => update(rule.id, { host: event.target.value })}
              placeholder="api.example.com"
              value={rule.host}
            />
          </label>
          <label className="block">
            <span className="fs-ink-tertiary">{t("react.workflows.secret_approval.port", "Port (optional)")}</span>
            <input
              aria-label={t("react.workflows.secret_approval.port_n", "Port of rule {index}", { index: index + 1 })}
              className="fs-input w-24"
              disabled={disabled}
              inputMode="numeric"
              onChange={(event) => update(rule.id, { port: event.target.value })}
              placeholder="443"
              value={rule.port}
            />
          </label>
          <label className="block">
            <span className="fs-ink-tertiary">{t("react.workflows.secret_approval.path_prefix", "Path prefix (optional)")}</span>
            <input
              aria-label={t("react.workflows.secret_approval.path_prefix_n", "Path prefix of rule {index}", { index: index + 1 })}
              className="fs-input"
              disabled={disabled}
              onChange={(event) => update(rule.id, { path_prefix: event.target.value })}
              placeholder="/v1"
              value={rule.path_prefix}
            />
          </label>
          <Button disabled={disabled} onClick={() => onChange(rules.filter((item) => item.id !== rule.id))} size="sm" type="button" variant="ghost-danger">
            {t("react.workflows.secret_approval.remove_rule", "Remove rule")}
          </Button>
        </div>
      ))}
      <Button disabled={disabled} onClick={() => onChange([...rules, emptyRule()])} size="sm" type="button" variant="secondary">
        <Plus className="h-3.5 w-3.5" />
        {t("react.workflows.secret_approval.add_rule", "Add destination")}
      </Button>
    </div>
  );
}

type WorkflowSecretApprovalPanelProps = Readonly<{
  projectId: string;
  workflowId: string;
  revision?: number | null;
  /** The saved workflow declares at least one secret input. */
  hasSecretInputs: boolean;
  /** Edits on screen that the saved workflow, and so the approval, does not include yet. */
  unsavedChanges?: boolean;
}>;

/**
 * Whether a person approved this workflow's use of stored secrets, and the
 * action that approves exactly the definition shown. HTTP steps whose URL is
 * computed need explicit exact-host rules before the server accepts it.
 */
export function WorkflowSecretApprovalPanel(props: WorkflowSecretApprovalPanelProps) {
  // A workflow without a saved secret input has nothing to approve and asks nothing.
  if (!props.hasSecretInputs || !props.projectId || !props.workflowId) return null;
  return <WorkflowSecretApprovalCard {...props} />;
}

function WorkflowSecretApprovalCard({ projectId, workflowId, revision, unsavedChanges = false }: WorkflowSecretApprovalPanelProps) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [rules, setRules] = useState<RuleDraft[]>(() => [emptyRule()]);
  const [conflict, setConflict] = useState(false);
  const status = useWorkflowSecretApproval(projectId, workflowId, revision);
  const catalog = useQuery({
    queryKey: catalogSecretsQueryKey(projectId),
    queryFn: () => fetchCatalogSecrets(projectId),
    enabled: Boolean(status.data?.secrets.length),
  });
  const writeStatus = useStatusWriter(projectId, workflowId, revision);
  const approve = useMutation({
    mutationFn: (current: WorkflowSecretApprovalStatus) =>
      approveWorkflowSecretUse(projectId, workflowId, current.fingerprint, current.dynamic_http_steps.length ? destinationsFromDrafts(rules) : []),
    onMutate: () => setConflict(false),
    onSuccess: writeStatus,
    onError: async (error) => {
      if (isHttpError(error) && error.status === 409) {
        setConflict(true);
        await queryClient.invalidateQueries({ queryKey: ["workflows", workflowId, "secret-approval"] });
      }
    },
  });
  const data = status.data;
  if (!data || data.status === "not_required") {
    return null;
  }
  const names = new Map((catalog.data ?? []).map((secret) => [secret.id, secret.name]));
  const approved = data.status === "approved";
  const dynamic = data.dynamic_http_steps;
  const explicit = destinationsFromDrafts(rules);
  const missingRules = dynamic.length > 0 && explicit.length === 0;
  const failure = approve.error && !conflict ? (approve.error instanceof Error ? approve.error.message : "") : "";

  return (
    <section
      aria-label={t("react.workflows.secret_approval.title", "Use of stored secrets")}
      className="workflow-settings-group space-y-2 text-xs fs-ink-secondary"
      data-testid="workflow-secret-approval"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="fs-drawer-label">{t("react.workflows.secret_approval.title", "Use of stored secrets")}</span>
        {approved ? (
          <DsBadge tone="green">{t("react.workflows.secret_approval.approved", "approved")}</DsBadge>
        ) : (
          <DsBadge tone="orange">{t("react.workflows.secret_approval.required", "approval required")}</DsBadge>
        )}
      </div>
      <p className="fs-field-helper">
        {approved
          ? t("react.workflows.secret_approval.approved_help", "A person approved this saved workflow's use of the stored secrets below. Any change to its steps or bindings needs a new approval.")
          : t("react.workflows.secret_approval.required_help", "Runs cannot use the stored secrets below until a person approves this saved workflow. Saving the workflow or applying an AI Builder draft never approves it.")}
      </p>
      {unsavedChanges ? (
        <DsAlert tone="warning" title={t("react.workflows.secret_approval.unsaved", "Approval covers the saved workflow")}>
          {t("react.workflows.secret_approval.unsaved_help", "Your unsaved changes are not part of this approval. Save them first, then review and approve again.")}
        </DsAlert>
      ) : null}
      {conflict ? (
        <DsAlert tone="warning" title={t("react.workflows.secret_approval.conflict", "The workflow changed after it was shown")}>
          {t("react.workflows.secret_approval.conflict_help", "Nothing was approved. The status below is reloaded; review it and approve again.")}
        </DsAlert>
      ) : null}
      {failure ? (
        <DsAlert tone="danger" title={t("react.workflows.secret_approval.failed", "Not approved")}>{failure}</DsAlert>
      ) : null}
      <dl className="grid gap-1">
        <div>
          <dt className="inline fs-ink-tertiary">{t("react.workflows.secret_approval.secrets", "Secrets")}: </dt>
          <dd className="inline">
            {data.secrets.map((row) => (
              <DsValueChip key={row.field}>{`${row.field} → ${names.get(row.secret_id) || row.secret_id}`}</DsValueChip>
            ))}
          </dd>
        </div>
        <div>
          <dt className="inline fs-ink-tertiary">{t("react.workflows.secret_approval.destinations", "Sent to")}: </dt>
          <dd className="inline">
            {data.destinations.length
              ? data.destinations.map((rule) => <DsValueChip key={destinationLabel(rule)}>{destinationLabel(rule)}</DsValueChip>)
              : t("react.workflows.secret_approval.no_literal_destinations", "no HTTP step with a fixed URL")}
          </dd>
        </div>
        {approved && data.approved_destinations.length ? (
          <div>
            <dt className="inline fs-ink-tertiary">{t("react.workflows.secret_approval.approved_destinations", "Approved destinations")}: </dt>
            <dd className="inline">
              {data.approved_destinations.map((rule) => <DsValueChip key={destinationLabel(rule)}>{destinationLabel(rule)}</DsValueChip>)}
            </dd>
          </div>
        ) : null}
        {dynamic.length ? (
          <div>
            <dt className="inline fs-ink-tertiary">{t("react.workflows.secret_approval.dynamic_steps", "HTTP steps with a computed URL")}: </dt>
            <dd className="inline">{dynamic.map((step) => <DsValueChip key={step}>{step}</DsValueChip>)}</dd>
          </div>
        ) : null}
      </dl>
      {!approved ? (
        <>
          {dynamic.length ? (
            <div className="space-y-1">
              <p className="fs-field-helper">
                {t("react.workflows.secret_approval.rules_help", "These steps build their URL while the workflow runs, so a secret may reach them only on the exact hosts you list here. A path prefix matches whole path segments.")}
              </p>
              <DestinationRules disabled={approve.isPending} onChange={setRules} rules={rules} />
            </div>
          ) : null}
          <Button disabled={approve.isPending || missingRules || status.isFetching} onClick={() => approve.mutate(data)} size="sm" type="button">
            <ShieldCheck className="h-3.5 w-3.5" />
            {t("react.workflows.secret_approval.approve", "Approve use of secrets")}
          </Button>
          {missingRules ? (
            <p className="fs-field-helper">{t("react.workflows.secret_approval.rules_required", "Add at least one destination for the steps with a computed URL.")}</p>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
