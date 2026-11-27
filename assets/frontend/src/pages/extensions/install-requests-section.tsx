import { useState, type ReactNode } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { ConfirmActionDialog } from "@/components/shared/confirm-action-dialog";
import { DsAlert, DsBadge, DsTable, type DsTableColumn } from "@/components/shared/design-system";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n/useI18n";
import { formatDateTime } from "@/lib/time/date-time-format";
import { ignoreAsyncResult } from "@/lib/utils/async";
import type { ExtensionRecord } from "@/pages/extensions/api";
import {
  EXTENSION_CATALOG_QUERY_KEY,
  fetchExtensionCatalog,
  type ExtensionCatalogEntry,
  type ExtensionCatalogInstallState,
} from "@/pages/extensions/catalog-api";
import { errorMessage } from "@/pages/extensions/hub-support";
import {
  approveExtensionInstallRequest,
  cancelExtensionInstallRequest,
  catalogEntryInstalled,
  declineExtensionInstallRequest,
  enableableExtensions,
  EXTENSION_INSTALL_REQUESTS_QUERY_KEY,
  type ExtensionInstallRequest,
  fetchExtensionInstallRequests,
  INSTALL_REQUEST_TEXT_LIMIT,
  type InstallRequestActions,
  latestRequestByExtension,
  requestExtensionInstall,
} from "@/pages/extensions/install-requests";
import { installLabel, installNotice } from "@/pages/extensions/marketplace-model";

type Dialog =
  | { kind: "ask"; entry: ExtensionCatalogEntry }
  | { kind: "decline"; request: ExtensionInstallRequest }
  | null;

/**
 * Requests to install an extension (ADR 0084), on the Extensions page.
 *
 * A company account's administrator asks from the catalog (`actions`); the
 * installation administrator answers in the "Installation requests" section.
 * Install there is the catalog's own install (`install`), approvals and errors
 * included, and the request is approved only after it succeeded.
 */
export function useExtensionInstallRequests({
  canRequest,
  canAnswer,
  extensions,
  installStateById,
  install,
  onEnable,
}: Readonly<{
  /** An administrator of a company account: asks instead of installing. */
  canRequest: boolean;
  /** The installation administrator: answers every company's requests. */
  canAnswer: boolean;
  extensions: readonly ExtensionRecord[];
  installStateById: Readonly<Record<string, ExtensionCatalogInstallState>>;
  install: (entry: ExtensionCatalogEntry, secretUpdateRepository?: string, onInstalled?: () => void) => void;
  onEnable: (extension: ExtensionRecord) => Promise<void> | void;
}>): { actions?: InstallRequestActions; section: ReactNode; dialog: ReactNode } {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const requestsQuery = useQuery({
    queryKey: EXTENSION_INSTALL_REQUESTS_QUERY_KEY,
    queryFn: fetchExtensionInstallRequests,
    enabled: canRequest || canAnswer,
    retry: false,
  });
  const [busyById, setBusyById] = useState<Record<string, boolean>>({});
  const [failure, setFailure] = useState<Record<string, string>>({});
  const [dialog, setDialog] = useState<Dialog>(null);
  const [text, setText] = useState("");
  const [dialogPending, setDialogPending] = useState(false);
  const [dialogError, setDialogError] = useState("");
  const items = requestsQuery.data?.items ?? [];
  const pending = items.filter((item) => item.status === "pending");

  const refresh = () => Promise.all([
    queryClient.invalidateQueries({ queryKey: EXTENSION_INSTALL_REQUESTS_QUERY_KEY }),
    queryClient.invalidateQueries({ queryKey: EXTENSION_CATALOG_QUERY_KEY }),
    queryClient.invalidateQueries({ queryKey: ["extensions", "all"] }),
  ]);

  async function busy(key: string, failureKey: string, work: () => Promise<unknown>, fallback: string) {
    setBusyById((current) => ({ ...current, [key]: true }));
    setFailure((current) => ({ ...current, [failureKey]: "" }));
    try {
      await work();
    } catch (error) {
      setFailure((current) => ({ ...current, [failureKey]: errorMessage(error, fallback) }));
    } finally {
      setBusyById((current) => ({ ...current, [key]: false }));
      await refresh();
    }
  }

  function openDialog(next: Dialog) {
    setText("");
    setDialogError("");
    setDialog(next);
  }

  const approve = (request: ExtensionInstallRequest) =>
    busy(request.id, request.id, () => approveExtensionInstallRequest(request.id), t("react.extensions.install_request_not_approved", "The request could not be approved."));

  async function installAndApprove(request: ExtensionInstallRequest) {
    let entry: ExtensionCatalogEntry | undefined;
    try {
      const listing = await queryClient.fetchQuery({
        queryKey: EXTENSION_CATALOG_QUERY_KEY,
        queryFn: () => fetchExtensionCatalog(),
      });
      entry = listing.items.find((item) => item.extension_id === request.extension_id);
    } catch (error) {
      setFailure((current) => ({ ...current, [request.id]: errorMessage(error, t("react.extensions.couldn_t_load_the_extension_catalog", "Couldn't load the extension catalog.")) }));
      return;
    }
    if (catalogEntryInstalled(entry)) {
      // Installed meanwhile by other means: only turn it on for the company.
      await approve(request);
      return;
    }
    if (!entry || entry.display_state !== "install") {
      setFailure((current) => ({ ...current, [request.id]: t("react.extensions.install_request_not_installable", "This extension cannot be installed from the catalog now.") }));
      return;
    }
    setFailure((current) => ({ ...current, [request.id]: "" }));
    install(entry, undefined, () => ignoreAsyncResult(approve(request)));
  }

  const actions: InstallRequestActions | undefined = canRequest
    ? {
        canRequest: true,
        latestById: latestRequestByExtension(items),
        enableableById: enableableExtensions(extensions),
        busyById,
        onRequest: (entry) => openDialog({ kind: "ask", entry }),
        onCancel: (request) => ignoreAsyncResult(busy(request.extension_id, "", () => cancelExtensionInstallRequest(request.id), t("react.extensions.install_request_not_cancelled", "The request could not be cancelled."))),
        onEnable: (extension) => ignoreAsyncResult(busy(extension.extension_id, "", async () => onEnable(extension), t("react.extensions.install_request_not_enabled", "The extension could not be turned on."))),
      }
    : undefined;

  async function confirmDialog() {
    if (!dialog) return;
    setDialogPending(true);
    setDialogError("");
    try {
      if (dialog.kind === "ask") await requestExtensionInstall(dialog.entry.extension_id, text);
      else await declineExtensionInstallRequest(dialog.request.id, text);
      setDialog(null);
      await refresh();
    } catch (error) {
      setDialogError(errorMessage(error, t("react.extensions.install_request_failed", "The request could not be sent.")));
    } finally {
      setDialogPending(false);
    }
  }

  const columns: DsTableColumn<ExtensionInstallRequest>[] = [
    { key: "company", header: t("react.extensions.install_request_company", "Company"), cell: (request) => request.account_name || request.account_id },
    {
      key: "extension",
      header: t("extension_page.extension_fallback", "Extension"),
      cell: (request) => (
        <div className="fs-extensions-name-cell">
          <span>{request.extension_name || request.extension_id}</span>
          {request.note ? <small>{request.note}</small> : null}
        </div>
      ),
    },
    { key: "requested_by", header: t("react.extensions.install_request_asked_by", "Asked by"), cell: (request) => request.requested_by_name || "—" },
    { key: "created_at", header: t("react.extensions.install_request_asked_on", "Asked on"), cell: (request) => formatDateTime(request.created_at) },
    {
      key: "actions",
      header: null,
      ariaLabel: t("extensions.table.actions", "Actions"),
      width: 220,
      cell: (request) => {
        const installing = installStateById[request.extension_id];
        const notice = failure[request.id] || installNotice(installing)?.text || "";
        const phase = installing?.phase ?? "idle";
        return (
          <div className="fs-extensions-status-cell">
            <div className="fs-extensions-actions">
              <Button
                aria-label={`${installLabel("idle", t)}: ${request.extension_name || request.extension_id}`}
                busy={phase === "installing" || Boolean(busyById[request.id])}
                busyLabel={installLabel("installing", t)}
                data-install-request-approve={request.id}
                disabled={phase === "awaiting_approval" || phase === "queued"}
                onClick={() => ignoreAsyncResult(installAndApprove(request))}
                size="sm"
                type="button"
              >
                {installLabel(phase === "installing" ? "idle" : phase, t)}
              </Button>
              <Button
                data-install-request-decline={request.id}
                disabled={phase !== "idle" || Boolean(busyById[request.id])}
                onClick={() => openDialog({ kind: "decline", request })}
                size="sm"
                type="button"
                variant="secondary"
              >
                {t("react.extensions.install_request_decline", "Decline")}
              </Button>
            </div>
            {notice ? <small role="alert">{notice}</small> : null}
          </div>
        );
      },
    },
  ];

  const section = canAnswer && pending.length ? (
    <article className="fs-card fs-table-card" data-testid="extension-install-requests">
      <header className="fs-card-header">
        <h2 className="fs-card-title">
          {t("react.extensions.installation_requests", "Installation requests")}{" "}
          <DsBadge tone="orange">{pending.length}</DsBadge>
        </h2>
        <p className="fs-card-description">
          {t("react.extensions.installation_requests_description", "Companies on this server asked for these extensions. Install adds the extension to the server and turns it on for the company that asked.")}
        </p>
      </header>
      <DsTable
        ariaLabel={t("react.extensions.installation_requests", "Installation requests")}
        columns={columns}
        rowKey={(request) => request.id}
        rows={pending}
      />
    </article>
  ) : null;

  const accountFailure = failure[""] ? <DsAlert title={failure[""]} tone="danger" /> : null;

  const asking = dialog?.kind === "ask" ? dialog.entry : null;
  const declining = dialog?.kind === "decline" ? dialog.request : null;
  const subject = asking?.name || declining?.extension_name || declining?.extension_id || "";
  const fieldLabel = asking
    ? t("react.extensions.install_request_note_label", "Note for the administrator (optional)")
    : t("react.extensions.install_request_reason_label", "Reason (optional)");
  const dialogNode = (
    <ConfirmActionDialog
      confirmLabel={asking ? t("react.extensions.install_request_send", "Send request") : t("react.extensions.install_request_decline_confirm", "Decline request")}
      confirmTone={asking ? "primary" : "danger"}
      consequence={asking
        ? t("react.extensions.install_request_ask_consequence", "The installation administrator is asked to install it for your company. You can cancel the request until they answer.")
        : t("react.extensions.install_request_decline_consequence", "{company} sees that the request was declined, with your reason if you give one. Nothing is installed.", { company: declining?.account_name || "" })}
      error={dialogError}
      eyebrow={subject || t("extension_page.extension_fallback", "Extension")}
      irreversible={false}
      onCancel={() => setDialog(null)}
      onConfirm={() => ignoreAsyncResult(confirmDialog())}
      open={Boolean(dialog)}
      pending={dialogPending}
      pendingLabel={asking ? t("react.extensions.install_request_sending", "Sending…") : t("react.extensions.install_request_declining", "Declining…")}
      title={asking
        ? t("react.extensions.install_request_ask_title", "Request installation of {name}?", { name: subject })
        : t("react.extensions.install_request_decline_title", "Decline the request for {name}?", { name: subject })}
    >
      <label className="fs-field">
        <span className="fs-field-label">{fieldLabel}</span>
        <textarea
          className="fs-textarea"
          data-testid="install-request-text"
          disabled={dialogPending}
          maxLength={INSTALL_REQUEST_TEXT_LIMIT}
          onChange={(event) => setText(event.target.value)}
          value={text}
        />
      </label>
    </ConfirmActionDialog>
  );

  return { actions, section: <>{accountFailure}{section}</>, dialog: dialogNode };
}
