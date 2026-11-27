import { useState } from "react";

import { useQueryClient } from "@tanstack/react-query";

import { DsAlert, DsDialog } from "@/components/shared/design-system";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n/useI18n";
import { formatDate } from "@/lib/time/date-time-format";
import { ignoreAsyncResult } from "@/lib/utils/async";
import type { UserRecord } from "@/pages/settings/shared/types";

import {
  answerOwnershipTransfer,
  cancelOwnershipTransfer,
  offerOwnershipTransfer,
  useAccountOwner,
} from "./owner-transfer-api";
import { userLabel } from "./user-form-model";

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

/**
 * The open ownership offer: the recipient accepts or declines it, the owner sees whom
 * it waits for and may withdraw it. Nothing is shown without an open offer.
 */
export function OwnerTransferNotice({
  onDone,
  onError,
}: Readonly<{
  onDone: (message: string) => void;
  onError: (message: string) => void;
}>) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);
  const ownerQuery = useAccountOwner(true);
  const state = ownerQuery.data;
  const transfer = state?.pending_transfer;
  if (!state || !transfer || !(state.transfer_is_for_me || state.is_owner)) {
    return null;
  }

  async function run(action: "accept" | "decline" | "cancel") {
    if (!transfer) return;
    setBusy(true);
    try {
      if (action === "cancel") {
        await cancelOwnershipTransfer();
        onDone(t("react.settings.ownership_transfer_cancelled", "The ownership transfer was cancelled."));
      } else {
        await answerOwnershipTransfer(transfer.id, action === "accept");
        onDone(
          action === "accept"
            ? t("react.settings.you_are_now_the_owner", "You are now the owner of this company.")
            : t("react.settings.ownership_transfer_declined", "You declined the ownership transfer."),
        );
      }
    } catch (error) {
      onError(errorMessage(error, t("react.settings.the_action_could_not_be_completed", "The action could not be completed.")));
    } finally {
      setBusy(false);
      await queryClient.invalidateQueries({ queryKey: ["settings", "users"] });
    }
  }

  const until = transfer.expires_at ? formatDate(transfer.expires_at) : "";
  if (state.transfer_is_for_me) {
    return (
      <div data-testid="owner-transfer-offer">
        <DsAlert
          title={t("react.settings.value1_offers_you_ownership", "{value1} offers you ownership of this company", { value1: transfer.from_name })}
          tone="warning"
        >
          <p>
            {t(
              "react.settings.ownership_offer_description",
              "As the owner you keep the final say: nobody else can remove you, change your role or your sign-in. The offer is open until {value1}.",
              { value1: until },
            )}
          </p>
          <div className="fs-footer-actions">
            <Button busy={busy} onClick={() => ignoreAsyncResult(run("accept"))} type="button" variant="primary">
              {t("react.settings.accept_ownership", "Accept ownership")}
            </Button>
            <button className="fs-button" disabled={busy} onClick={() => ignoreAsyncResult(run("decline"))} type="button">
              {t("react.settings.decline", "Decline")}
            </button>
          </div>
        </DsAlert>
      </div>
    );
  }
  return (
    <div data-testid="owner-transfer-pending">
      <DsAlert
        title={t("react.settings.ownership_offered_to_value1", "Ownership offered to {value1}", { value1: transfer.to_name })}
      >
        <p>
          {t(
            "react.settings.ownership_offer_waits",
            "You stay the owner until they accept. The offer is open until {value1}.",
            { value1: until },
          )}
        </p>
        <div className="fs-footer-actions">
          <button className="fs-button" disabled={busy} onClick={() => ignoreAsyncResult(run("cancel"))} type="button">
            {t("react.settings.cancel_the_transfer", "Cancel the transfer")}
          </button>
        </div>
      </DsAlert>
    </div>
  );
}

/** The owner confirms offering ownership to another administrator of the company. */
export function TransferOwnershipDialog({
  onClose,
  onOffered,
  user,
}: Readonly<{
  onClose: () => void;
  onOffered: (message: string) => void;
  user: UserRecord | null;
}>) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const label = user ? userLabel(user) : "";

  async function offer() {
    if (!user || saving) return;
    setSaving(true);
    setError("");
    try {
      await offerOwnershipTransfer(user.id);
      await queryClient.invalidateQueries({ queryKey: ["settings", "users"] });
      onOffered(t("react.settings.ownership_offered_to_value1_message", "Ownership was offered to {value1}. You stay the owner until they accept.", { value1: label }));
    } catch (failure) {
      setError(errorMessage(failure, t("react.settings.the_action_could_not_be_completed", "The action could not be completed.")));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DsDialog
      busy={saving}
      closeLabel={t("react.settings.close_confirmation", "Close confirmation")}
      description={t(
        "react.settings.transfer_ownership_description",
        "{value1} gets the final say over this company once they accept. After that, they can remove you or change your role.",
        { value1: label },
      )}
      eyebrow={t("react.settings.users_access", "Users & access")}
      footer={
        <>
          <button className="fs-button" disabled={saving} onClick={onClose} type="button">
            {t("common.cancel", "Cancel")}
          </button>
          <Button busy={saving} busyLabel={t("react.settings.updating", "Updating…")} onClick={() => ignoreAsyncResult(offer())} type="button" variant="danger">
            {t("react.settings.transfer_ownership", "Transfer ownership")}
          </Button>
        </>
      }
      onClose={() => { if (!saving) { setError(""); onClose(); } }}
      open={Boolean(user)}
      title={t("react.settings.transfer_ownership_to_value1", "Transfer ownership to {value1}?", { value1: label })}
      variant="confirm"
    >
      {error ? (
        <DsAlert tone="danger" title={t("react.settings.ownership_transfer_failed", "The ownership transfer was not offered")}>
          {error}
        </DsAlert>
      ) : null}
    </DsDialog>
  );
}
