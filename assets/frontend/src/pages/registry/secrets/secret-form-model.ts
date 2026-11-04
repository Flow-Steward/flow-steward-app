import type { CatalogSecretUsage } from "@/pages/registry/secrets/api";

export type SecretEditorState = {
  name: string;
  description: string;
  value: string;
};

export const EMPTY_SECRET_EDITOR: SecretEditorState = {
  name: "",
  description: "",
  value: "",
};

/** Where a consumer's binding is managed: its own page, or the project tab that lists it. */
export function usageLink(usage: CatalogSecretUsage): string | null {
  const consumer = encodeURIComponent(usage.consumer_id || "");
  if (usage.consumer_kind === "data_connection") return "/registry/data-connections";
  if (usage.consumer_kind === "mcp_connector" && consumer) return `/registry/mcp/${consumer}`;
  if (usage.consumer_kind === "llm_provider") return "/extensions";
  if (!usage.project_id) return null;
  const tab = usage.consumer_kind === "workflow_input" ? "workflows" : "connections";
  return `/projects/${encodeURIComponent(usage.project_id)}?tab=${tab}`;
}
