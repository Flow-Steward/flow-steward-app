import { useI18n } from "@/lib/i18n/useI18n";
import { useState } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, ShieldCheck } from "lucide-react";

import { DsAlert, DsBadge, DsValueChip } from "@/components/shared/design-system-primitives";
import { Button } from "@/components/ui/button";
import { isHttpError } from "@/lib/http/errors";
import { objectOrEmpty, stringFrom } from "@/pages/extensions/extension-manifest-utils";
import {
  approveConnectionSecretUse,
  bindConnectionFieldSecret,
  fetchConnectionSecretApproval,
  unbindConnectionFieldSecret,
} from "@/pages/projects/connections/api";
import { CatalogSecretPicker } from "@/pages/registry/secrets/catalog-secret-picker";

/**
 * Catalog secrets in an extension connection's credential fields (ADR 0057).
 *
 * Choosing a stored secret binds it through its own request; typing a value in
 * the field keeps saving through the connection's secrets endpoint as before.
 */

/** Field → catalog secret id, as the connection's refs projection records it. */
function catalogBindings(row: Record<string, unknown> | null | undefined): Record<string, string> {
  let refs: unknown = row?.secret_refs;
  if (typeof refs === "string") {
    try { refs = JSON.parse(refs); } catch { refs = {}; }
  }
  const catalog = objectOrEmpty(objectOrEmpty(refs).catalog);
  return Object.fromEntries(
    Object.entries(catalog).map(([field, id]) => [field, stringFrom(id)] as const).filter(([field, id]) => Boolean(field && id)),
  );
}

function useConnectionSecretRefresh(projectId: string) {
  const queryClient = useQueryClient();
  return async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["extensions", "project-connections"] }),
      queryClient.invalidateQueries({ queryKey: ["projects", projectId, "secret-approvals"] }),
      queryClient.invalidateQueries({ queryKey: ["secrets"] }),
    ]);
  };
}

type ConnectionSecretFieldBindingProps = Readonly<{
  projectId: string;
  /** The saved connection, or null while the form creates a new one. */
  row: Record<string, unknown> | null | undefined;
  field: string;
  fieldLabel: string;
}>;

/**
 * Next to a credential field: use a stored secret from the catalog instead of
 * typing one. Nothing here talks to the server until the person asks for it or
 * the field already uses a stored secret.
 */
export function ConnectionSecretFieldBinding({ projectId, row, field, fieldLabel }: ConnectionSecretFieldBindingProps) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const connectionId = stringFrom(row?.connection_id);
  const boundSecretId = catalogBindings(row)[field] ?? "";
  if (!projectId || !field) return null;
  if (!connectionId) {
    return (
      <p className="fs-field-helper">
        {t("react.extensions.secret_catalog.save_first", "After the connection is saved, you can use a stored secret for this field instead.")}
      </p>
    );
  }
  if (!boundSecretId && !open) {
    return (
      <div>
        <Button onClick={() => setOpen(true)} size="sm" type="button" variant="ghost">
          <KeyRound className="h-3.5 w-3.5" />
          {t("react.extensions.secret_catalog.use_existing", "Use an existing secret")}
        </Button>
      </div>
    );
  }
  return (
    <ConnectionSecretFieldPicker
      boundSecretId={boundSecretId}
      connectionId={connectionId}
      field={field}
      fieldLabel={fieldLabel}
      projectId={projectId}
    />
  );
}

function ConnectionSecretFieldPicker({
  projectId,
  connectionId,
  field,
  fieldLabel,
  boundSecretId,
}: Readonly<{ projectId: string; connectionId: string; field: string; fieldLabel: string; boundSecretId: string }>) {
  const { t } = useI18n();
  const refresh = useConnectionSecretRefresh(projectId);
  const bind = useMutation({
    mutationFn: (secretId: string) => bindConnectionFieldSecret(projectId, connectionId, field, secretId),
    onSuccess: refresh,
  });
  const unbind = useMutation({
    mutationFn: () => unbindConnectionFieldSecret(projectId, connectionId, field),
    onSuccess: refresh,
  });
  return (
    <div data-testid={`connection-secret-binding-${field}`}>
      {boundSecretId ? (
        <p className="fs-field-helper">
          {t("react.extensions.secret_catalog.bound_help", "A value typed above replaces this stored secret's value everywhere it is used.")}
        </p>
      ) : null}
      {/* Created in place as a project secret and bound at once (ADR 0057). */}
      <CatalogSecretPicker
        allowCreate
        boundSecretId={boundSecretId}
        busy={bind.isPending || unbind.isPending}
        error={bind.error || unbind.error}
        fieldLabel={fieldLabel}
        onChoose={(secretId) => bind.mutateAsync(secretId)}
        onRemove={() => unbind.mutate()}
        projectId={projectId}
      />
    </div>
  );
}

type ConnectionSecretApprovalStatusProps = Readonly<{
  projectId: string;
  row: Record<string, unknown>;
}>;

/**
 * On a connection's card: whether a person approved its use of stored secrets,
 * and the approval of exactly the configuration shown.
 */
export function ConnectionSecretApprovalStatus({ projectId, row }: ConnectionSecretApprovalStatusProps) {
  const connectionId = stringFrom(row.connection_id);
  const bindingsKey = JSON.stringify(catalogBindings(row));
  // A connection that uses no stored secret has nothing to approve and asks nothing.
  if (!projectId || !connectionId || bindingsKey === "{}") return null;
  return <ConnectionSecretApproval bindingsKey={bindingsKey} connectionId={connectionId} projectId={projectId} row={row} />;
}

function ConnectionSecretApproval({
  projectId,
  connectionId,
  bindingsKey,
  row,
}: Readonly<{ projectId: string; connectionId: string; bindingsKey: string; row: Record<string, unknown> }>) {
  const { t } = useI18n();
  const refresh = useConnectionSecretRefresh(projectId);
  const [conflict, setConflict] = useState(false);
  const status = useQuery({
    queryKey: ["projects", projectId, "secret-approvals", "connection", connectionId, bindingsKey],
    queryFn: () => fetchConnectionSecretApproval(projectId, connectionId),
    retry: false,
  });
  const approve = useMutation({
    mutationFn: (fingerprint: string) => approveConnectionSecretUse(projectId, connectionId, fingerprint),
    onMutate: () => setConflict(false),
    onError: (error) => {
      if (isHttpError(error) && error.status === 409) setConflict(true);
    },
    onSettled: refresh,
  });
  const data = status.data;
  if (!data || data.status === "not_required") return null;
  const approved = data.status === "approved";
  const failure = approve.error && !conflict && approve.error instanceof Error ? approve.error.message : "";
  return (
    <div className="mt-1 space-y-1 text-xs fs-ink-secondary" data-testid={`connection-secret-approval-${connectionId}`}>
      <div className="flex flex-wrap items-center gap-2">
        {approved ? (
          <DsBadge tone="green">{t("react.extensions.secret_catalog.approved", "stored secrets approved")}</DsBadge>
        ) : (
          <DsBadge tone="orange">{t("react.extensions.secret_catalog.approval_required", "stored secrets need approval")}</DsBadge>
        )}
        {data.secrets.map((secret) => (
          <DsValueChip key={secret.field}>{`${secret.field} → ${secret.name || secret.secret_id}`}</DsValueChip>
        ))}
      </div>
      {conflict ? (
        <DsAlert tone="warning" title={t("react.extensions.secret_catalog.conflict", "The connection changed after it was shown")}>
          {t("react.extensions.secret_catalog.conflict_help", "Nothing was approved. Review the reloaded status and approve again.")}
        </DsAlert>
      ) : null}
      {failure ? <DsAlert tone="danger" title={t("react.extensions.secret_catalog.failed", "Not approved")}>{failure}</DsAlert> : null}
      {approved ? null : (
        <>
          <p>
            {t("react.extensions.secret_catalog.sent_to", "Sent to")}:{" "}
            {data.destinations.length
              ? data.destinations.map((d) => <DsValueChip key={`${d.scheme}://${d.host}:${d.port}`}>{`${d.scheme}://${d.host}:${d.port}`}</DsValueChip>)
              : t("react.extensions.secret_catalog.extension_code", "the extension's code (this release)")}
          </p>
          <Button
            aria-label={t("react.extensions.secret_catalog.approve_for", "Approve use of secrets for {name}", { name: stringFrom(row.display_name || connectionId) })}
            disabled={approve.isPending || status.isFetching}
            onClick={() => approve.mutate(data.fingerprint)}
            size="sm"
            type="button"
            variant="secondary"
          >
            <ShieldCheck className="h-3.5 w-3.5" />
            {t("react.extensions.secret_catalog.approve", "Approve use of secrets")}
          </Button>
        </>
      )}
    </div>
  );
}
