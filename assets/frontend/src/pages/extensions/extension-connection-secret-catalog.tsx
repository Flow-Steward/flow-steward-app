import { useI18n } from "@/lib/i18n/useI18n";
import { useState, type ReactNode } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";

import { DsAlert, DsBadge, DsValueChip } from "@/components/shared/design-system-primitives";
import { Button } from "@/components/ui/button";
import { isHttpError } from "@/lib/http/errors";
import { connectionCatalogBindings, stringFrom } from "@/pages/extensions/extension-manifest-utils";
import { approveConnectionSecretUse, fetchConnectionSecretApproval } from "@/pages/projects/connections/api";
import { StoredSecretSelect } from "@/pages/registry/secrets/stored-secret-select";

/**
 * Catalog secrets in an extension connection's credential fields (ADR 0057).
 *
 * A chosen stored secret is bound when the connection is saved; a typed value keeps
 * saving through the connection's secrets endpoint as before.
 */

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

type ConnectionStoredSecretFieldProps = Readonly<{
  field: string;
  fieldLabel: string;
  /** The stored secret the saved connection uses for this field, or "". */
  boundSecretId: string;
  /** The person's choice in this form: undefined keeps the saved one. */
  choice: string | undefined;
  onChoose: (secretId: string) => void;
  /** The typed-value control, shown while no stored secret is chosen. */
  children: ReactNode;
}>;

/**
 * A credential field of an extension connection: choose a stored secret from the
 * catalog, or type a value. The choice is part of the form and is bound when the
 * connection is saved, new connections included (ADR 0057).
 */
export function ConnectionStoredSecretField({ field, fieldLabel, boundSecretId, choice, onChoose, children }: ConnectionStoredSecretFieldProps) {
  const { t } = useI18n();
  const value = choice ?? boundSecretId;
  const changed = choice !== undefined && choice !== boundSecretId;
  return (
    <div data-testid={`connection-secret-binding-${field}`}>
      {/* The typed-value control carries the field's label; while a stored secret is chosen the label stays. */}
      {value ? <span className="fs-field-label">{fieldLabel}</span> : children}
      <StoredSecretSelect
        fieldLabel={fieldLabel}
        helper={
          changed
            ? t("react.extensions.secret_catalog.applied_on_save", "Applied when you save the connection.")
            : value
              ? t("react.extensions.secret_catalog.uses_stored", "Uses this stored secret. Replace its value in Registry → Secrets.")
              : undefined
        }
        onChange={onChoose}
        value={value}
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
  const bindingsKey = JSON.stringify(connectionCatalogBindings(row.secret_refs));
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
