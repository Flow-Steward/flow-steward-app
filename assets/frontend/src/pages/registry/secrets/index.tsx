import { useI18n } from "@/lib/i18n/useI18n";
import { formatUserDateTime } from "@/lib/time/user-timezone";
import { useMemo, useState } from "react";
import { Link } from "react-router";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Loader2 } from "lucide-react";

import {
  DsAlert,
  DsBadge,
  DsPageHeader,
  PageContainer,
  SensitiveInput,
} from "@/components/shared/design-system-primitives";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { fetchProjectsList } from "@/pages/projects/list/api";
import {
  type CatalogSecret,
  type CatalogSecretUsage,
  createCatalogSecret,
  deleteCatalogSecret,
  fetchCatalogSecret,
  fetchCatalogSecrets,
  replaceCatalogSecretValue,
  updateCatalogSecret,
} from "@/pages/registry/secrets/api";

function errorText(error: unknown): string | null {
  return error instanceof Error ? error.message : null;
}

/** Where a consumer's binding is managed: the project tab that lists it. */
function usageLink(usage: CatalogSecretUsage): string | null {
  if (!usage.project_id) return null;
  const tab = usage.consumer_kind === "workflow_input" ? "workflows" : "connections";
  return `/projects/${encodeURIComponent(usage.project_id)}?tab=${tab}`;
}

function SecretUsage({ usage }: Readonly<{ usage: CatalogSecretUsage }>) {
  const { t } = useI18n();
  const consumer =
    usage.consumer_kind === "workflow_input"
      ? `${t("react.registry.secrets.usage_workflow", "Workflow")} ${usage.workflow_name || usage.workflow_id || ""}`
      : `${t("react.registry.secrets.usage_connection", "Connection")} ${usage.connection_name || usage.connection_id || ""}`;
  const label = `${consumer.trim()} · ${usage.field}`;
  const link = usageLink(usage);
  return (
    <li>
      {link ? <Link className="underline" to={link}>{label}</Link> : label}
      {usage.project_name ? <span className="fs-ink-tertiary"> — {usage.project_name}</span> : null}
    </li>
  );
}

function ChangedLine({ label, at, by }: Readonly<{ label: string; at?: string | null; by?: string }>) {
  if (!at) return null;
  return (
    <div>
      <span className="fs-ink-tertiary">{label}: </span>
      {formatUserDateTime(at)}
      {by ? ` · ${by}` : ""}
    </div>
  );
}

function SecretCard({ secret, onChanged }: Readonly<{ secret: CatalogSecret; onChanged: () => void }>) {
  const { t } = useI18n();
  const [name, setName] = useState(secret.name);
  const [description, setDescription] = useState(secret.description);
  const [value, setValue] = useState("");
  const detail = useQuery({ queryKey: ["secrets", secret.id], queryFn: () => fetchCatalogSecret(secret.id) });
  const save = useMutation({
    mutationFn: () => updateCatalogSecret(secret.id, { name, description }),
    onSuccess: onChanged,
  });
  const replace = useMutation({
    mutationFn: () => replaceCatalogSecretValue(secret.id, value),
    onSuccess: () => {
      setValue("");
      onChanged();
    },
  });
  const remove = useMutation({ mutationFn: () => deleteCatalogSecret(secret.id), onSuccess: onChanged });
  const usages = detail.data?.usages ?? [];
  const failure = errorText(save.error) ?? errorText(replace.error) ?? errorText(remove.error);
  return (
    <div className="fs-surface-radius border border-subtle fs-surface-inset px-3 py-3 text-sm fs-ink-secondary space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium fs-ink-brand">{secret.name}</span>
        <DsBadge>{secret.owner === "project" ? t("react.registry.secrets.owner_project", "project") : t("react.registry.secrets.owner_workspace", "workspace")}</DsBadge>
        <DsBadge tone="green">{t("react.registry.secrets.configured", "configured")}</DsBadge>
      </div>
      {failure ? <DsAlert tone="danger" title={t("react.registry.secrets.failed", "Not saved")}>{failure}</DsAlert> : null}
      <label className="block">
        <span className="text-xs fs-ink-tertiary">{t("react.registry.secrets.name", "Name")}</span>
        <input className="fs-input" onChange={(event) => setName(event.target.value)} value={name} />
      </label>
      <label className="block">
        <span className="text-xs fs-ink-tertiary">{t("react.registry.secrets.description_label", "Description")}</span>
        <input className="fs-input" onChange={(event) => setDescription(event.target.value)} value={description} />
      </label>
      <Button disabled={save.isPending || !name.trim()} onClick={() => save.mutate()} size="sm" variant="secondary">
        {t("react.registry.secrets.save", "Save")}
      </Button>
      <div className="flex flex-wrap items-end gap-2">
        <label className="block">
          <span className="text-xs fs-ink-tertiary">{t("react.registry.secrets.new_value", "New value")}</span>
          <SensitiveInput ariaLabel={t("react.registry.secrets.new_value", "New value")} onChange={(event) => setValue(event.target.value)} value={value} />
        </label>
        <Button disabled={replace.isPending || !value} onClick={() => replace.mutate()} size="sm" variant="secondary">
          {t("react.registry.secrets.replace", "Replace value")}
        </Button>
      </div>
      <div className="text-xs space-y-0.5">
        <ChangedLine at={detail.data?.value_updated_at} by={detail.data?.value_updated_by} label={t("react.registry.secrets.value_changed", "Value replaced")} />
        <ChangedLine at={detail.data?.metadata_updated_at} by={detail.data?.metadata_updated_by} label={t("react.registry.secrets.metadata_changed", "Name or description changed")} />
        <ChangedLine at={detail.data?.created_at} by={detail.data?.created_by} label={t("react.registry.secrets.created", "Created")} />
      </div>
      <div className="text-xs">
        <span className="fs-ink-tertiary">{t("react.registry.secrets.used_by", "Used by")}: </span>
        {usages.length ? (
          <ul className="mt-1 list-disc pl-4 space-y-0.5">
            {usages.map((usage) => (
              <SecretUsage key={`${usage.consumer_kind}:${usage.connection_id ?? usage.workflow_id}:${usage.field}`} usage={usage} />
            ))}
          </ul>
        ) : (
          t("react.registry.secrets.unused", "nothing yet")
        )}
      </div>
      <Button disabled={remove.isPending || usages.length > 0} onClick={() => remove.mutate()} size="sm" variant="ghost-danger">
        {usages.length > 0
          ? t("react.registry.secrets.delete_blocked", "In use — remove its bindings to delete it")
          : t("react.registry.secrets.delete", "Delete")}
      </Button>
    </div>
  );
}

/**
 * Registry → Secrets (ADR 0057). A secret is created once, chosen by connections and
 * workflow inputs, and replaced in one place. Its value is write-only: this page
 * never receives one.
 */
export function RegistrySecretsPage() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [projectId, setProjectId] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [value, setValue] = useState("");
  const [owner, setOwner] = useState<"project" | "workspace">("project");
  const projectsQuery = useQuery({
    queryKey: ["projects", "secret-catalog-picker"],
    queryFn: () => fetchProjectsList({ page: 1, perPage: 100, search: "", includeAllWorkspaces: true }),
  });
  const projects = useMemo(() => projectsQuery.data?.projects ?? [], [projectsQuery.data]);
  const selectedProject = projects.find((project) => project.id === projectId);
  const secretsQuery = useQuery({
    queryKey: ["secrets", "project", projectId],
    queryFn: () => fetchCatalogSecrets(projectId),
    enabled: Boolean(projectId),
  });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["secrets"] });
  const create = useMutation({
    mutationFn: () =>
      createCatalogSecret({
        name,
        description,
        value,
        ...(owner === "project"
          ? { project_id: projectId }
          : { workspace_id: String(selectedProject?.workspace_id ?? "") }),
      }),
    onSuccess: async () => {
      setName("");
      setDescription("");
      setValue("");
      await refresh();
    },
  });

  return (
    <PageContainer width="standard">
      <DsPageHeader
        eyebrow={t("shell.nav.registry", "Registry")}
        title={t("react.registry.secrets.title", "Secrets")}
        description={t("react.registry.secrets.page_description", "Create a secret once, choose it in connections and workflow inputs, and replace it in one place. Stored values are never shown again.")}
      />
      <Card className="fs-card">
        <CardHeader>
          <CardTitle className="text-lg">{t("react.registry.secrets.project", "Project")}</CardTitle>
          <CardDescription>{t("react.registry.secrets.project_description", "Secrets of the project and of its workspace.")}</CardDescription>
        </CardHeader>
        <CardContent>
          <select aria-label={t("react.registry.secrets.project", "Project")} className="fs-input" onChange={(event) => setProjectId(event.target.value)} value={projectId}>
            <option value="">{t("react.registry.secrets.choose_project", "Choose a project")}</option>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>{project.name}</option>
            ))}
          </select>
        </CardContent>
      </Card>
      {projectId ? (
        <Card className="fs-card">
          <CardHeader>
            <CardTitle className="text-lg">{t("react.registry.secrets.create_title", "New secret")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {create.isError ? <DsAlert tone="danger" title={t("react.registry.secrets.failed", "Not saved")}>{errorText(create.error)}</DsAlert> : null}
            <input aria-label={t("react.registry.secrets.name", "Name")} className="fs-input" onChange={(event) => setName(event.target.value)} placeholder={t("react.registry.secrets.name", "Name")} value={name} />
            <input aria-label={t("react.registry.secrets.description_label", "Description")} className="fs-input" onChange={(event) => setDescription(event.target.value)} placeholder={t("react.registry.secrets.description_label", "Description")} value={description} />
            <SensitiveInput ariaLabel={t("react.registry.secrets.value", "Value")} onChange={(event) => setValue(event.target.value)} value={value} />
            <select aria-label={t("react.registry.secrets.owner", "Owner")} className="fs-input" onChange={(event) => setOwner(event.target.value === "workspace" ? "workspace" : "project")} value={owner}>
              <option value="project">{t("react.registry.secrets.owner_project_long", "This project")}</option>
              <option value="workspace">{t("react.registry.secrets.owner_workspace_long", "Every project of this workspace")}</option>
            </select>
            <Button disabled={create.isPending || !name.trim() || !value} onClick={() => create.mutate()}>
              <KeyRound className="h-4 w-4" />
              {t("react.registry.secrets.create", "Create secret")}
            </Button>
          </CardContent>
        </Card>
      ) : null}
      {secretsQuery.isLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
      {(secretsQuery.data ?? []).map((secret) => (
        <SecretCard key={secret.id} onChanged={() => void refresh()} secret={secret} />
      ))}
    </PageContainer>
  );
}
