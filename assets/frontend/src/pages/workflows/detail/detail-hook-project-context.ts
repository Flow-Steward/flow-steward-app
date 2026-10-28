import { useQuery } from "@tanstack/react-query";

import { useAuth } from "@/lib/auth/useAuth";
import { fetchWorkflowLinkedProjects } from "@/pages/workflows/shared/project-context";

/** Hook authority requires a deliberate project and a current successful membership lookup. */
export function useWorkflowHookProjectContext({ workflowId, projectHint, enabled }: Readonly<{
  workflowId: string; projectHint: string; enabled: boolean;
}>) {
  const { session } = useAuth();
  const actorKey = JSON.stringify([session?.user?.id || "", session?.user?.account_id || ""]);
  const projectsQuery = useQuery({
    queryKey: ["workflows", workflowId, "hook-projects", actorKey],
    queryFn: () => fetchWorkflowLinkedProjects(workflowId, { presentation: "background" }),
    enabled: enabled && Boolean(workflowId),
    retry: false,
  });
  // A failed refresh retains cached data, but immediately withdraws its authority.
  const projects = enabled && projectsQuery.isSuccess ? projectsQuery.data : [];
  const projectId = projects.some((project) => project.id === projectHint) ? projectHint : "";
  return { actorKey, projectId, projects, projectsQuery };
}
