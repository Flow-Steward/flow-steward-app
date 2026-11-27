import { useState } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { useI18n } from "@/lib/i18n/useI18n";
import { formatDate } from "@/lib/time/date-time-format";
import { ignoreAsyncResult } from "@/lib/utils/async";
import {
  cancelAccountInvitation,
  fetchSentAccountInvitations,
  SENT_INVITATIONS_QUERY_KEY,
  type SentAccountInvitation,
} from "@/pages/settings/shared/api";

import { roleLabel } from "./user-form-model";

/**
 * Invitations of this company that wait for an answer (ADR 0085). Every invitation
 * sent is shown, the email as it was typed, so the list never says whether a person
 * exists. Shown only while there is at least one.
 */
export function PendingInvitations({
  onCancelled,
  onError,
}: Readonly<{
  onCancelled: (invitation: SentAccountInvitation) => void;
  onError: (message: string) => void;
}>) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [busyId, setBusyId] = useState("");
  const invitationsQuery = useQuery({
    queryKey: SENT_INVITATIONS_QUERY_KEY,
    queryFn: fetchSentAccountInvitations,
  });
  const invitations = invitationsQuery.data ?? [];

  async function cancel(invitation: SentAccountInvitation) {
    setBusyId(invitation.id);
    try {
      await cancelAccountInvitation(invitation.id);
      onCancelled(invitation);
    } catch (error) {
      onError(
        error instanceof Error
          ? error.message
          : t("react.settings.the_invitation_could_not_be_cancelled", "The invitation could not be cancelled."),
      );
    } finally {
      setBusyId("");
      await queryClient.invalidateQueries({ queryKey: SENT_INVITATIONS_QUERY_KEY });
    }
  }

  if (!invitations.length) {
    return null;
  }
  return (
    <article className="fs-card fs-table-card" data-testid="pending-invitations">
      <header className="fs-card-header">
        <h2 className="fs-card-title">{t("react.settings.pending_invitations", "Pending invitations")}</h2>
        <p className="fs-card-description">
          {t(
            "react.settings.pending_invitations_description",
            "People join this company only when they accept. An invitation to an address nobody uses stays here until it expires.",
          )}
        </p>
      </header>
      <div className="fs-table-scroll">
        <table
          aria-label={t("react.settings.pending_invitations", "Pending invitations")}
          className="fs-table fs-management-table"
        >
          <thead>
            <tr>
              <th>{t("auth.common.email", "Email")}</th>
              <th>{t("react.settings.role", "Role")}</th>
              <th>{t("react.settings.invited_by", "Invited by")}</th>
              <th>{t("account.tokens.expires", "Expires")}</th>
              <th aria-label={t("monitoring.table.actions", "Actions")} />
            </tr>
          </thead>
          <tbody>
            {invitations.map((invitation) => (
              <tr key={invitation.id}>
                <td>{invitation.email}</td>
                <td>
                  <span className="fs-badge">{roleLabel(invitation.role, t)}</span>
                </td>
                <td>{invitation.invited_by || "—"}</td>
                <td>{formatDate(invitation.expires_at)}</td>
                <td className="fs-table-action-cell">
                  <button
                    aria-label={t("react.settings.cancel_the_invitation_to_value1", "Cancel the invitation to {value1}", {
                      value1: invitation.email,
                    })}
                    className="fs-button"
                    disabled={busyId === invitation.id}
                    onClick={() => ignoreAsyncResult(cancel(invitation))}
                    type="button"
                  >
                    {t("common.cancel", "Cancel")}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </article>
  );
}
