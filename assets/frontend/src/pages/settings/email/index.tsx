import { useState } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { DsAlert, DsBadge, DsCard, DsSelect } from "@/components/shared/design-system";
import { DsLoader } from "@/components/shared/ds-loader";
import { InlineConfirm } from "@/components/shared/inline-confirm";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { Button } from "@/components/ui/button";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { isHttpError } from "@/lib/http/errors";
import { formatUserDateTime, useUserTimezone } from "@/lib/time/user-timezone";
import {
  clearSystemEmailSender,
  confirmSystemEmailSender,
  fetchSystemEmailSettings,
  selectSystemEmailSender,
  type SystemEmailCandidate,
  type SystemEmailSettings,
} from "@/pages/settings/email/api";

const SYSTEM_EMAIL_QUERY_KEY = ["settings", "system-email"];

/** A sender whose connection changed since it was confirmed: confirming it again restores it. */
const RECONFIRM_REASONS = new Set(["confirmation_revoked", "identity_changed"]);

type SenderAction = { kind: "select"; connectionId: string } | { kind: "confirm" } | { kind: "clear" };

function unavailableReason(reason: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  switch (reason) {
    case "not_selected":
      return t("react.settings.system_email_reason_not_selected", "No sender is selected: the platform sends no email; Forgot password? is hidden.");
    case "connection_missing":
      return t("react.settings.system_email_reason_connection_missing", "The sender's connection no longer exists. Choose another sender.");
    case "connection_disabled":
      return t("react.settings.system_email_reason_connection_disabled", "The sender's connection is disabled. Enable it on its extension page, or choose another sender.");
    case "install_unavailable":
      return t("react.settings.system_email_reason_install_unavailable", "The extension behind the sender's connection is not available. Enable it, or choose another sender.");
    case "not_recipient":
      return t("react.settings.system_email_reason_not_recipient", "The sender's connection cannot send to an email address. Choose another sender.");
    case "identity_changed":
    case "confirmation_revoked":
      return t("react.settings.system_email_reason_reconfirm", "The sender's connection changed since it was confirmed. Confirm it again.");
    default:
      return t("react.settings.system_email_reason_unknown", "System email is not available.");
  }
}

function candidateLabel(candidate: SystemEmailCandidate, t: I18nContextValue["t"] = fallbackContext.t): string {
  return t("react.settings.system_email_candidate_label", "{project} · {connection} · {extension}", {
    project: candidate.project_name || candidate.project_id,
    connection: candidate.display_name || candidate.connection_id,
    extension: candidate.extension_display_name || candidate.extension_key,
  });
}

function actionErrorMessage(error: unknown, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (isHttpError(error)) {
    const payload = error.payload && typeof error.payload === "object" ? (error.payload as Record<string, unknown>) : {};
    if (payload.code === "platform_admin_required" || error.message === "platform_admin_required") {
      return t("react.settings.system_email_admin_required", "Only a platform administrator can manage the system email sender.");
    }
  }
  return error instanceof Error && error.message
    ? error.message
    : t("react.settings.system_email_save_failed", "The system email sender could not be changed.");
}

function actionDoneMessage(action: SenderAction, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (action.kind === "select") return t("react.settings.system_email_saved", "System email sender saved.");
  if (action.kind === "confirm") return t("react.settings.system_email_confirmed", "System email sender confirmed.");
  return t("react.settings.system_email_cleared", "System email is off: no sender is selected.");
}

function runSenderAction(action: SenderAction): Promise<SystemEmailSettings> {
  if (action.kind === "select") return selectSystemEmailSender(action.connectionId);
  if (action.kind === "confirm") return confirmSystemEmailSender();
  return clearSystemEmailSender();
}

/**
 * Settings -> Email: which project connection sends the platform's own email
 * (password resets, invitations, account notices). Platform administrators only.
 */
export function SettingsEmailPage() {
  const { t } = useI18n();
  const timezone = useUserTimezone();
  const queryClient = useQueryClient();
  // The sender picked in the select; until one is picked it shows the current sender.
  const [picked, setPicked] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: "success" | "danger"; text: string } | null>(null);
  const [confirmingStop, setConfirmingStop] = useState(false);

  const settingsQuery = useQuery({
    queryKey: SYSTEM_EMAIL_QUERY_KEY,
    queryFn: fetchSystemEmailSettings,
  });
  const loaderShown = useLoaderDelay(settingsQuery.isLoading);
  const settings = settingsQuery.data;
  const selected = settings?.selected ?? null;
  const candidates = settings?.candidates ?? [];
  const selectedCandidate = selected
    ? candidates.find((candidate) => candidate.connection_id === selected.connection_id)
    : undefined;

  const choice = picked ?? selected?.connection_id ?? "";

  const mutation = useMutation({
    mutationFn: runSenderAction,
    onSuccess: (view, action) => {
      queryClient.setQueryData(SYSTEM_EMAIL_QUERY_KEY, view);
      setPicked(null);
      setConfirmingStop(false);
      setNotice({ tone: "success", text: actionDoneMessage(action, t) });
    },
    onError: (error) => {
      setNotice({ tone: "danger", text: actionErrorMessage(error, t) });
    },
  });
  const pendingKind = mutation.isPending ? mutation.variables?.kind : undefined;

  function run(action: SenderAction) {
    setNotice(null);
    mutation.mutate(action);
  }

  const candidateOptions = candidates.map((candidate) => ({
    value: candidate.connection_id,
    label: candidateLabel(candidate, t),
  }));
  if (selected && !candidateOptions.some((option) => option.value === selected.connection_id)) {
    candidateOptions.unshift({
      value: selected.connection_id,
      label: t("react.settings.system_email_current_unavailable", "{connection} (current, unavailable)", { connection: selected.display_name || selected.connection_id }),
    });
  }

  const senderRows = selected
    ? [
      { label: t("react.settings.system_email_project", "Project"), value: selectedCandidate?.project_name || selected.project_id || "—" },
      { label: t("react.settings.system_email_connection", "Connection"), value: selected.display_name || selected.connection_id },
      { label: t("react.settings.system_email_extension", "Extension"), value: selectedCandidate?.extension_display_name || selectedCandidate?.extension_key || "—" },
      {
        label: t("react.settings.system_email_confirmed_at", "Confirmed"),
        value: selected.confirmed_at
          ? formatUserDateTime(selected.confirmed_at, timezone)
          : selected.source === "upgrade_pin"
            ? t("react.settings.system_email_carried_over", "Carried over by an upgrade, not confirmed yet")
            : "—",
      },
    ]
    : [];

  return (
    <>
      {settingsQuery.isError ? (
        <DsAlert title={t("react.settings.system_email_load_failed", "System email settings could not be loaded")} tone="danger">
          {actionErrorMessage(settingsQuery.error, t)}
        </DsAlert>
      ) : null}

      <DsCard
        description={t("react.settings.system_email_description", "The project connection that sends the platform's own email: password resets, invitations and account notices.")}
        title={t("react.settings.system_email_sender", "System email sender")}
      >
        {loaderShown ? (
          <DsLoader label={t("react.settings.system_email_loading", "Loading system email settings…")} variant="orbit-inline" />
        ) : null}
        {settings ? (
          <div className="fs-panel-stack">
            <p>
              <DsBadge tone={settings.available ? "green" : "orange"}>
                {settings.available
                  ? t("react.settings.system_email_sending", "Sending system email")
                  : t("react.settings.system_email_not_sending", "Not sending system email")}
              </DsBadge>
            </p>
            {settings.available ? null : (
              <DsAlert title={unavailableReason(settings.reason || (selected ? "" : "not_selected"), t)} tone="warning" />
            )}
            {senderRows.length ? (
              <dl className="fs-form-grid">
                {senderRows.map((row) => (
                  <div className="fs-field" key={row.label}>
                    <dt className="fs-field-label">{row.label}</dt>
                    <dd>{row.value}</dd>
                  </div>
                ))}
              </dl>
            ) : null}
            {selected && RECONFIRM_REASONS.has(settings.reason) ? (
              <div className="fs-footer-actions">
                <Button
                  busy={pendingKind === "confirm"}
                  busyLabel={t("react.settings.system_email_confirming", "Confirming…")}
                  disabled={mutation.isPending}
                  onClick={() => run({ kind: "confirm" })}
                  type="button"
                  variant="primary"
                >
                  {t("react.settings.system_email_confirm_again", "Confirm again")}
                </Button>
              </div>
            ) : null}

            <div className="fs-field">
              <span className="fs-field-label" id="system-email-sender-label">{t("react.settings.system_email_choose_sender", "Sender")}</span>
              <DsSelect
                ariaLabel={t("react.settings.system_email_choose_sender", "Sender")}
                disabled={mutation.isPending}
                emptyLabel={t("react.settings.system_email_no_candidates", "No project connection can send email. Connect an email-capable extension in a project first.")}
                id="system-email-sender"
                labelledBy="system-email-sender-label"
                onChange={setPicked}
                options={candidateOptions}
                placeholder={t("react.settings.system_email_select_sender", "Select a sender")}
                value={choice}
              />
              <p className="fs-field-helper">
                {t("react.settings.system_email_choose_help", "Saving chooses the connection and confirms it as the sender.")}
              </p>
            </div>
            <div className="fs-footer-actions">
              <Button
                busy={pendingKind === "select"}
                busyLabel={t("react.settings.saving", "Saving…")}
                disabled={!choice || mutation.isPending}
                onClick={() => run({ kind: "select", connectionId: choice })}
                type="button"
                variant="primary"
              >
                {t("registry.common.save", "Save")}
              </Button>
              {selected && !confirmingStop ? (
                <Button
                  disabled={mutation.isPending}
                  onClick={() => { setNotice(null); setConfirmingStop(true); }}
                  type="button"
                  variant="ghost-danger"
                >
                  {t("react.settings.system_email_stop", "Stop sending system email")}
                </Button>
              ) : null}
            </div>
            {selected && confirmingStop ? (
              <InlineConfirm
                confirmLabel={t("react.settings.system_email_stop", "Stop sending system email")}
                onCancel={() => setConfirmingStop(false)}
                onConfirm={() => run({ kind: "clear" })}
                pending={pendingKind === "clear"}
                pendingLabel={t("react.settings.system_email_stopping", "Stopping…")}
                question={t("react.settings.system_email_stop_question", "Stop sending system email? Password resets and invitations are not sent, and Forgot password? is hidden.")}
                tone="danger"
              />
            ) : null}
          </div>
        ) : null}
        {notice ? <DsAlert title={notice.text} tone={notice.tone} /> : null}
      </DsCard>
    </>
  );
}
