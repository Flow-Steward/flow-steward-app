import { useRef } from "react";

import { useI18n } from "@/lib/i18n/useI18n";
import { DsDialog } from "@/components/shared/design-system-overlays";
import type { ConfirmDialogState } from "@/components/shared/use-confirm-dialog";

import "@/components/shared/confirm-action-dialog.css";

/** Promise-based confirmations share the canonical modal portal and focus trap. */
export function ConfirmDialog({
  body,
  cancelLabel,
  confirmLabel,
  destructive,
  details,
  onCancel,
  onConfirm,
  open,
  restoreFocus = true,
  title,
}: Readonly<ConfirmDialogState>) {
  const { t } = useI18n();
  const cancelRef = useRef<HTMLButtonElement>(null);
  return (
    <DsDialog
      description={body || ""}
      eyebrow=""
      initialFocusRef={cancelRef}
      onClose={onCancel}
      open={open}
      restoreFocus={restoreFocus}
      role="dialog"
      title={title}
      variant="confirm"
      footer={<>
        <button className="fs-button" onClick={onCancel} ref={cancelRef} type="button">
          {cancelLabel || t("common.cancel", "Cancel")}
        </button>
        <button className={`fs-button ${destructive ? "danger" : "primary"}`} onClick={onConfirm} type="button">
          {confirmLabel || t("common.confirm", "Confirm")}
        </button>
      </>}
    >
      {details?.length ? (
        <ul className="fs-confirm-objects">
          {details.map((detail) => <li key={detail}>{detail}</li>)}
        </ul>
      ) : null}
    </DsDialog>
  );
}
