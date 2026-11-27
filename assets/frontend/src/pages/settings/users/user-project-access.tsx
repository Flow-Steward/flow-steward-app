import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useMemo, useState } from "react";

import { DsAlert, DsSelect, type DsOption } from "@/components/shared/design-system";
import { DsAccordion } from "@/components/shared/ds-accordion";
import { DsSwitch } from "@/components/shared/ds-switch";
import { Button } from "@/components/ui/button";
import { useFeature } from "@/lib/auth/features";
import { useI18n } from "@/lib/i18n/useI18n";
import type { UserRecord } from "@/pages/settings/shared/types";

import {
  fetchPlaceCandidates,
  fetchProjectGrants,
  liftUserProjectLimit,
  limitUserToPlaces,
  projectAccessQueryKeys,
  type ProjectRole,
} from "./user-project-access-api";
import {
  SCOPED_ROLES_FEATURE,
  grantsFromPlaceRoles,
  inheritedRole,
  isProjectRole,
  placeRolesFromGrants,
  placeTree,
  projectKey,
  workflowKey,
  workspaceKey,
  type PlaceKey,
  type PlaceRoles,
} from "./user-project-access-model";

const NO_GRANT = "none";

type Draft = { limited: boolean; roles: PlaceRoles };

/**
 * Place access of one existing person in the Edit access dialog (ADR 0076 §8b):
 * workspaces → projects → workflows, a role per place. Saved on its own with
 * "Save project access", like the approver picker: the person's role and sign-in
 * controls keep the dialog's own save.
 */
export function UserProjectAccess({ user, role }: Readonly<{ user: UserRecord | null; role: string }>) {
  const feature = useFeature(SCOPED_ROLES_FEATURE);
  if (!feature.enabled || !user) return null;
  return <ProjectAccessEditor base={feature.endpoint} key={user.id} role={role} user={user} />;
}

function ProjectAccessEditor({ base, user, role }: Readonly<{ base: string; user: UserRecord; role: string }>) {
  const { t } = useI18n();
  const helperId = useId();
  const queryClient = useQueryClient();
  const label = t("react.settings.project_access", "Project access");
  const grantsQuery = useQuery({
    queryKey: projectAccessQueryKeys.grants(base, user.id),
    queryFn: () => fetchProjectGrants(base, user.id),
  });
  const candidatesQuery = useQuery({
    queryKey: projectAccessQueryKeys.candidates(base),
    queryFn: () => fetchPlaceCandidates(base),
  });
  const userId = user.id;
  const grantsData = grantsQuery.data;
  const stored = useMemo<Draft>(
    () => ({ limited: Boolean(grantsData?.limitedUserIds.has(userId)), roles: placeRolesFromGrants(grantsData?.grants ?? [], userId) }),
    [grantsData, userId],
  );
  const [draft, setDraft] = useState<Draft | null>(null);
  const [openWorkspace, setOpenWorkspace] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const current = draft ?? stored;
  const tree = useMemo(() => placeTree(candidatesQuery.data, grantsData?.grants ?? [], userId), [candidatesQuery.data, grantsData, userId]);
  const mutation = useMutation({
    mutationFn: async (next: Draft) => {
      if (!next.limited) {
        await liftUserProjectLimit(base, user.id);
        return;
      }
      await limitUserToPlaces(base, user.id, grantsFromPlaceRoles(next.roles));
    },
    onSuccess: async () => {
      setDraft(null);
      setSaved(true);
      await queryClient.invalidateQueries({ queryKey: projectAccessQueryKeys.all(base) });
    },
  });

  if (role === "admin") {
    return (
      <div className="fs-field" data-testid="user-project-access">
        <span className="fs-field-label">{label}</span>
        <span className="fs-field-helper">{t("react.settings.project_access_admin", "Administrators reach every project.")}</span>
      </div>
    );
  }

  const roleName = (value: ProjectRole) =>
    value === "operator" ? t("react.settings.role_operator", "Operator") : t("react.settings.role_viewer", "Viewer");
  const roleOptions = (inherited: ProjectRole | null): DsOption[] => [
    {
      value: NO_GRANT,
      label: inherited
        ? t("react.settings.project_access_inherited", "As above ({role})", { role: roleName(inherited) })
        : t("react.settings.project_access_no_access", "No access"),
    },
    { value: "operator", label: roleName("operator") },
    { value: "viewer", label: roleName("viewer") },
  ];
  const loading = grantsQuery.isPending || candidatesQuery.isPending;
  const loadFailed = grantsQuery.isError || candidatesQuery.isError;
  const update = (next: Draft) => { setDraft(next); setSaved(false); mutation.reset(); };
  const setRole = (key: PlaceKey, value: string) => {
    const roles = { ...current.roles };
    if (isProjectRole(value)) roles[key] = value;
    else delete roles[key];
    update({ ...current, roles });
  };
  const select = (key: PlaceKey, name: string, inherited: ProjectRole | null) => (
    <DsSelect
      ariaLabel={t("react.settings.project_access_role_for", "Role in {project}", { project: name })}
      disabled={mutation.isPending}
      onChange={(value) => setRole(key, value)}
      options={roleOptions(inherited)}
      value={current.roles[key] ?? NO_GRANT}
    />
  );
  const switchLabel = t("react.settings.project_access_limit_places", "Limit to chosen places");

  const items = tree.map((workspace) => {
    const ownRole = current.roles[workspaceKey(workspace.workspaceId)];
    return {
      id: workspace.workspaceId,
      header: (
        <span>
          {workspace.name}
          {ownRole ? ` · ${roleName(ownRole)}` : ""}
        </span>
      ),
      panel: (
        <div className="fs-control-grid">
          <span className="fs-footer-actions">
            <span className="min-w-0 flex-1">{t("react.settings.project_access_whole_workspace", "Whole workspace, including projects added later")}</span>
            {select(workspaceKey(workspace.workspaceId), workspace.name, null)}
          </span>
          <ul aria-label={t("react.settings.project_access_projects_of", "Projects of {workspace}", { workspace: workspace.name })} className="fs-control-grid">
            {workspace.projects.map((project) => {
              const projectInherited = inheritedRole(current.roles, { workspaceId: workspace.workspaceId, projectId: project.projectId });
              return (
                <li className="fs-control-grid" key={project.projectId}>
                  <span className="fs-footer-actions">
                    <span className="min-w-0 flex-1">{project.name}</span>
                    {select(projectKey(project.projectId), project.name, projectInherited)}
                  </span>
                  {project.workflows.length ? (
                    <ul aria-label={t("react.settings.project_access_workflows_of", "Workflows of {project}", { project: project.name })} className="fs-control-grid pl-6">
                      {project.workflows.map((workflow) => (
                        <li className="fs-footer-actions" key={workflow.workflowId}>
                          <span className="min-w-0 flex-1">{workflow.name}</span>
                          {select(
                            workflowKey(project.projectId, workflow.workflowId),
                            workflow.name,
                            inheritedRole(current.roles, { workspaceId: workspace.workspaceId, projectId: project.projectId, workflow: true }),
                          )}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </div>
      ),
    };
  });

  return (
    <fieldset className="fs-field" data-testid="user-project-access">
      <legend className="fs-field-label">{label}</legend>
      <span className="fs-footer-actions">
        <DsSwitch checked={current.limited} describedBy={helperId} disabled={loading || loadFailed || mutation.isPending} label={switchLabel} onCheckedChange={(limited) => update({ ...current, limited })} />
        <strong>{switchLabel}</strong>
      </span>
      <span className="fs-field-helper" id={helperId}>
        {current.limited
          ? t(
              "react.settings.project_access_places_help",
              "This person reaches only the places chosen here. A workspace covers all its projects, a project all its workflows; the most specific choice applies, never above their own role.",
            )
          : t("react.settings.project_access_all_help", "This person reaches every project their role reaches.")}
      </span>
      {current.limited ? <DsAccordion items={items} onOpenChange={setOpenWorkspace} openId={openWorkspace} /> : null}
      {loadFailed ? (
        <p className="fs-field-helper error" role="alert">
          {t("react.settings.project_access_load_failed", "Project access could not be loaded. Reopen the dialog to try again.")}
        </p>
      ) : null}
      {mutation.isError ? (
        <DsAlert title={t("react.settings.project_access_not_saved", "Project access was not saved")} tone="danger">
          {mutation.error instanceof Error ? mutation.error.message : ""}
        </DsAlert>
      ) : null}
      <span className="fs-footer-actions">
        <Button
          busy={mutation.isPending}
          busyLabel={t("react.settings.saving", "Saving…")}
          disabled={draft === null || loading || loadFailed}
          onClick={() => mutation.mutate(current)}
          size="sm"
          type="button"
          variant="secondary"
        >
          {t("react.settings.project_access_save", "Save project access")}
        </Button>
        {saved ? <span className="fs-field-helper" role="status">{t("react.settings.project_access_saved", "Project access saved.")}</span> : null}
      </span>
    </fieldset>
  );
}
