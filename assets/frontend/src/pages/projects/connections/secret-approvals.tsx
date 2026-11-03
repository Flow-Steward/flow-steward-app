import { useI18n } from "@/lib/i18n/useI18n";
import { useMemo, useState } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, ShieldCheck } from "lucide-react";

import { DsAlert, DsBadge, DsCheckbox, DsValueChip } from "@/components/shared/design-system-primitives";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  approveProjectSecretUse,
  fetchProjectSecretApprovals,
  type SecretApprovalStatus,
} from "@/pages/projects/connections/api";

type ProjectSecretApprovalsProps = Readonly<{ projectId: string }>;

function releaseLabel(release: SecretApprovalStatus["release"] | null | undefined): string {
  if (!release?.release_id) {
    return "—";
  }
  const digest = release.descriptor_sha256 ? ` · ${release.descriptor_sha256.slice(0, 12)}` : "";
  return `${release.release_id}${digest}`;
}

/**
 * Connections whose use of stored secrets needs a person's approval (ADR 0057).
 * The server approves exactly the fingerprints shown here; a configuration that
 * changed after this list loaded is refused as a conflict, and the list reloads.
 */
export function ProjectSecretApprovals({ projectId }: ProjectSecretApprovalsProps) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const queryKey = ["projects", projectId, "secret-approvals"];
  const approvalsQuery = useQuery({
    queryKey,
    queryFn: () => fetchProjectSecretApprovals(projectId),
    enabled: Boolean(projectId),
  });
  const pending = useMemo(
    () => (approvalsQuery.data ?? []).filter((row) => row.status === "approval_required"),
    [approvalsQuery.data],
  );
  const chosen = pending.filter((row) => selected[row.connection_id]);
  const chosenReleases = new Set(chosen.map((row) => row.release.release_id));
  const approve = useMutation({
    mutationFn: (rows: SecretApprovalStatus[]) => {
      const releases = new Set(rows.map((row) => row.release.release_id));
      const releaseId = releases.size === 1 ? [...releases][0] : "";
      return approveProjectSecretUse(
        projectId,
        Object.fromEntries(rows.map((row) => [row.connection_id, row.fingerprint])),
        releaseId,
      );
    },
    onSettled: async () => {
      setSelected({});
      await queryClient.invalidateQueries({ queryKey });
    },
  });

  if (approvalsQuery.isLoading) {
    return (
      <div className="flex items-center gap-2 text-sm fs-ink-secondary">
        <Loader2 className="h-4 w-4 animate-spin" />
        {t("react.projects.secret_approval.loading", "Checking the use of stored secrets...")}
      </div>
    );
  }
  if (!pending.length && !approve.isError) {
    return null;
  }

  return (
    <Card className="fs-card">
      <CardHeader>
        <CardTitle className="text-lg">
          {t("react.projects.secret_approval.title", "Stored secrets need approval")}
        </CardTitle>
        <CardDescription>
          {t(
            "react.projects.secret_approval.description",
            "These connections changed since a person approved their use of stored secrets. Until you approve them, they cannot use those secrets.",
          )}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {approve.isError ? (
          <DsAlert tone="danger" title={t("react.projects.secret_approval.failed", "Not approved")}>
            {approve.error instanceof Error ? approve.error.message : null}
          </DsAlert>
        ) : null}
        {pending.map((row) => (
          <div
            className="fs-surface-radius border border-subtle fs-surface-inset px-3 py-3 text-sm fs-ink-secondary"
            key={row.connection_id}
          >
            <DsCheckbox
              checked={Boolean(selected[row.connection_id])}
              onChange={(checked) => setSelected((current) => ({ ...current, [row.connection_id]: checked }))}
            >
              <span className="font-medium fs-ink-brand">{row.display_name || row.connection_id}</span>{" "}
              <DsBadge tone="orange">{t("react.projects.secret_approval.required", "approval required")}</DsBadge>
            </DsCheckbox>
            <dl className="mt-2 grid gap-1 text-xs">
              <div>
                <dt className="inline fs-ink-tertiary">{t("react.projects.secret_approval.secrets", "Secrets")}: </dt>
                <dd className="inline">
                  {row.secrets.map((secret) => (
                    <DsValueChip key={secret.field}>{`${secret.field} → ${secret.name}`}</DsValueChip>
                  ))}
                </dd>
              </div>
              <div>
                <dt className="inline fs-ink-tertiary">{t("react.projects.secret_approval.destinations", "Sent to")}: </dt>
                <dd className="inline">
                  {row.destinations.length
                    ? row.destinations.map((d) => (
                        <DsValueChip key={`${d.scheme}://${d.host}:${d.port}`}>{`${d.scheme}://${d.host}:${d.port}`}</DsValueChip>
                      ))
                    : t("react.projects.secret_approval.extension_code", "the extension's code (this release)")}
                </dd>
              </div>
              <div>
                <dt className="inline fs-ink-tertiary">{t("react.projects.secret_approval.release", "Release")}: </dt>
                <dd className="inline">
                  {row.previous_release && row.previous_release.release_id !== row.release.release_id
                    ? `${releaseLabel(row.previous_release)} → ${releaseLabel(row.release)}`
                    : releaseLabel(row.release)}
                </dd>
              </div>
              {row.changes.length ? (
                <div>
                  <dt className="inline fs-ink-tertiary">{t("react.projects.secret_approval.changed", "Changed")}: </dt>
                  <dd className="inline">{row.changes.join(", ")}</dd>
                </div>
              ) : null}
            </dl>
            <div className="mt-2">
              <Button disabled={approve.isPending} onClick={() => approve.mutate([row])} size="sm" variant="secondary">
                <ShieldCheck className="h-3.5 w-3.5" />
                {t("react.projects.secret_approval.approve_one", "Approve use of secrets")}
              </Button>
            </div>
          </div>
        ))}
        {chosen.length > 1 ? (
          <Button disabled={approve.isPending} onClick={() => approve.mutate(chosen)}>
            <ShieldCheck className="h-4 w-4" />
            {chosenReleases.size === 1
              ? t("react.projects.secret_approval.approve_release", "Approve release for {count} selected connections", { count: chosen.length })
              : t("react.projects.secret_approval.approve_selected", "Approve {count} selected connections", { count: chosen.length })}
          </Button>
        ) : null}
      </CardContent>
    </Card>
  );
}
