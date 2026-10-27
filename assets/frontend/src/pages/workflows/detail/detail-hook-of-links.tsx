import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { consumerPresentationKey, workflowConsumerLabels } from "./detail-usage-labels";

import { useI18n } from "@/lib/i18n/useI18n";
import { fetchWorkflowWhereUsed, workflowWhereUsedQueryKey } from "@/pages/workflows/shared/hook-api";
import { workflowIdsFromProjectBindingPayload } from "@/pages/workflows/shared/project-context";
import { fetchProjectWorkflowBindings } from "@/pages/projects/workflows/api";

/**
 * Compact extension/hook context over the builder of a workflow a
 * project connects to a hook (B14), linking back to that hook in the author's
 * builder. Nothing renders for a workflow no hook uses.
 */
export function WorkflowHookOfLinks({ projectId, workflowId }: Readonly<{ projectId: string; workflowId: string }>) {
  const { t, locale } = useI18n();
  const linkedProjects = useQuery({
    queryKey: ["workflows", workflowId, "linked-project-id", "hook-of-context", projectId],
    queryFn: () => fetchProjectWorkflowBindings(projectId),
    enabled: Boolean(projectId && workflowId),
    retry: false,
    staleTime: 30_000,
  });
  const contextReady = linkedProjects.isSuccess && workflowIdsFromProjectBindingPayload(linkedProjects.data).includes(workflowId);
  const whereUsedQuery = useQuery({
    queryKey: workflowWhereUsedQueryKey(projectId, workflowId, locale),
    queryFn: () => fetchWorkflowWhereUsed(projectId, workflowId, locale),
    enabled: contextReady,
    retry: false,
    staleTime: 15_000,
  });
  const hookUses = (contextReady ? whereUsedQuery.data?.consumers || [] : []).filter((consumer) => consumer.kind === "hook" && consumer.hook_id);
  const labels = workflowConsumerLabels(hookUses, t);
  if (!hookUses.length) return null;
  return (
    <nav aria-label={t("react.workflows.hook_of", "Hook of")} className="flex flex-wrap items-center gap-2 text-xs fs-ink-secondary" data-testid="hook-of-links">
      <span>{t("react.workflows.hook_of", "Hook of")}:</span>
      {hookUses.map((use, index) => {
        const params = new URLSearchParams({ project_id: use.project_id || projectId });
        if (use.step_id) {
          params.set("drawer", "step");
          params.set("step_id", use.step_id);
        }
        return (
          <Link
            className="fs-link"
            key={consumerPresentationKey(use)}
            to={`/registry/workflows/${encodeURIComponent(use.caller_workflow_id)}/builder?${params.toString()}`}
          >
            {labels[index]}
          </Link>
        );
      })}
    </nav>
  );
}
