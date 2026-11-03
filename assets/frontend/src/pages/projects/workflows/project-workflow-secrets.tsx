import { useI18n } from "@/lib/i18n/useI18n";
import { useEffect } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { DsAlert } from "@/components/shared/design-system-primitives";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  WorkflowInputSecretBinding,
  WorkflowSecretApprovalPanel,
} from "@/pages/workflows/detail/detail-workflow-secret-catalog";
import {
  fetchProjectWorkflowSecretApprovals,
  type ProjectWorkflowSecretStatus,
  projectWorkflowSecretsQueryKey,
  workflowSecretApprovalQueryKey,
} from "@/pages/workflows/shared/workflow-secret-catalog-api";

function ProjectWorkflowSecretsRow({ projectId, workflow }: Readonly<{ projectId: string; workflow: ProjectWorkflowSecretStatus }>) {
  const queryClient = useQueryClient();
  // The list already carries each workflow's status: seed the per-workflow query
  // the binding and approval controls read, so they need no request of their own.
  useEffect(() => {
    queryClient.setQueryData(workflowSecretApprovalQueryKey(projectId, workflow.workflow_id, null), workflow);
  }, [projectId, queryClient, workflow]);
  return (
    <div className="fs-surface-radius border border-subtle fs-surface-inset px-3 py-3 text-sm space-y-2" data-testid={`project-workflow-secrets-${workflow.workflow_id}`}>
      <p className="font-medium fs-ink-brand">{workflow.display_name || workflow.workflow_id}</p>
      {(workflow.secret_inputs ?? []).map((field) => (
        <WorkflowInputSecretBinding field={field} key={field} projectId={projectId} revision={null} saved workflowId={workflow.workflow_id} />
      ))}
      <WorkflowSecretApprovalPanel hasSecretInputs projectId={projectId} revision={null} workflowId={workflow.workflow_id} />
    </div>
  );
}

/**
 * Stored secrets of the project's workflows (ADR 0057), for operators.
 *
 * The list comes from its own endpoint, readable from viewer up, not from the
 * project's admin settings. Choosing a secret and approving its use are their own
 * server operations, checked for role and an interactive session there; neither
 * edits the workflow, so this needs no access to the builder.
 */
export function ProjectWorkflowSecrets({ projectId }: Readonly<{ projectId: string }>) {
  const { t } = useI18n();
  const workflows = useQuery({
    queryKey: projectWorkflowSecretsQueryKey(projectId),
    queryFn: () => fetchProjectWorkflowSecretApprovals(projectId),
    enabled: Boolean(projectId),
    retry: false,
  });
  // Nothing until the list arrives, and nothing when no linked workflow has a secret input.
  if (!projectId || workflows.isPending || (workflows.isSuccess && !(workflows.data ?? []).length)) return null;
  return (
    <Card className="fs-card">
      <CardHeader>
        <CardTitle className="text-lg">{t("react.projects.workflow_secrets.title", "Workflow secrets")}</CardTitle>
        <CardDescription>
          {t("react.projects.workflow_secrets.description", "Choose a stored secret for each secret input of a linked workflow and approve where runs may send it. This does not change the workflow.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {workflows.isError ? (
          <DsAlert tone="danger" title={t("react.projects.workflow_secrets.failed", "Workflow secrets could not be loaded")}>
            {workflows.error instanceof Error ? workflows.error.message : ""}
          </DsAlert>
        ) : null}
        {(workflows.data ?? []).map((workflow) => (
          <ProjectWorkflowSecretsRow key={workflow.workflow_id} projectId={projectId} workflow={workflow} />
        ))}
      </CardContent>
    </Card>
  );
}
