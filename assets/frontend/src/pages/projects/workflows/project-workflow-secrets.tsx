import { useI18n } from "@/lib/i18n/useI18n";

import { useQuery } from "@tanstack/react-query";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  WorkflowInputSecretBinding,
  WorkflowSecretApprovalPanel,
} from "@/pages/workflows/detail/detail-workflow-secret-catalog";
import {
  fetchWorkflowSecretApproval,
  workflowSecretApprovalQueryKey,
} from "@/pages/workflows/shared/workflow-secret-catalog-api";

type LinkedWorkflow = Readonly<{ workflowId: string; displayName: string }>;

function ProjectWorkflowSecretsRow({ projectId, workflow }: Readonly<{ projectId: string; workflow: LinkedWorkflow }>) {
  const status = useQuery({
    queryKey: workflowSecretApprovalQueryKey(projectId, workflow.workflowId, null),
    queryFn: () => fetchWorkflowSecretApproval(projectId, workflow.workflowId),
    retry: false,
  });
  const inputs = status.data?.secret_inputs ?? [];
  // Not linked yet, or nothing secret to bind: this workflow has no row here.
  if (status.isError || !inputs.length) return null;
  return (
    <div className="fs-surface-radius border border-subtle fs-surface-inset px-3 py-3 text-sm space-y-2" data-testid={`project-workflow-secrets-${workflow.workflowId}`}>
      <p className="font-medium fs-ink-brand">{workflow.displayName || workflow.workflowId}</p>
      {inputs.map((field) => (
        <WorkflowInputSecretBinding field={field} key={field} projectId={projectId} revision={null} saved workflowId={workflow.workflowId} />
      ))}
      <WorkflowSecretApprovalPanel hasSecretInputs projectId={projectId} revision={null} workflowId={workflow.workflowId} />
    </div>
  );
}

/**
 * Stored secrets of the project's workflows (ADR 0055), for operators.
 *
 * Choosing a secret for a secret input and approving its use are their own server
 * operations, checked for role and an interactive session there. Neither edits the
 * workflow, so this needs no access to the builder.
 */
export function ProjectWorkflowSecrets({ projectId, workflows }: Readonly<{ projectId: string; workflows: LinkedWorkflow[] }>) {
  const { t } = useI18n();
  if (!projectId || !workflows.length) return null;
  return (
    <Card className="fs-card">
      <CardHeader>
        <CardTitle className="text-lg">{t("react.projects.workflow_secrets.title", "Workflow secrets")}</CardTitle>
        <CardDescription>
          {t("react.projects.workflow_secrets.description", "Choose a stored secret for each secret input of a linked workflow and approve where runs may send it. This does not change the workflow.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {workflows.map((workflow) => (
          <ProjectWorkflowSecretsRow key={workflow.workflowId} projectId={projectId} workflow={workflow} />
        ))}
      </CardContent>
    </Card>
  );
}
