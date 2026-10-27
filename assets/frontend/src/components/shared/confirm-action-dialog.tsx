import { useI18n } from "@/lib/i18n/useI18n";
import { DsDialog } from "@/components/shared/design-system";

import "@/components/shared/confirm-action-dialog.css";

const LISTED_OBJECTS = 5;

/**
 * A blocking confirmation, usually for an irreversible action, built to the canon's
 * Modal / Dialog contract (03_components/overlays-feedback/modal-dialog.md):
 * - the title is the question, with the count for a bulk action;
 * - one sentence states the consequence, and an irreversible action adds the
 *   fixed line "This cannot be undone." as its own sentence;
 * - up to five objects are named, the rest summarised as "and N more";
 * - the confirming action is verb plus object, never "Yes";
 * - while the request is in flight both actions disable, the label takes its
 *   progress form, the dialog refuses dismissal and the pending state is
 *   announced;
 * - on failure the dialog stays open with the reason beside the actions.
 *
 * Success, and where focus goes when the trigger did not survive, belong to the
 * page: the dialog is gone by then.
 */
export function ConfirmActionDialog({
  open,
  eyebrow,
  title,
  consequence,
  irreversible = true,
  objects = [],
  confirmLabel,
  pendingLabel,
  pending,
  error = "",
  closeLabel,
  confirmTone = "danger",
  onCancel,
  onConfirm,
}: Readonly<{
  open: boolean;
  eyebrow: string;
  title: string;
  consequence: string;
  irreversible?: boolean;
  objects?: readonly string[];
  confirmLabel: string;
  pendingLabel: string;
  pending: boolean;
  error?: string;
  closeLabel?: string;
  /**
   * Danger when something is removed. Primary for a decision that destroys
   * nothing, such as trusting what a check found, so red keeps its meaning.
   */
  confirmTone?: "danger" | "primary";
  onCancel: () => void;
  onConfirm: () => void;
}>) {
  const { t } = useI18n();
  const listed = objects.length > 1 ? objects.slice(0, LISTED_OBJECTS) : [];
  const more = objects.length > LISTED_OBJECTS ? objects.length - LISTED_OBJECTS : 0;
  return (
    <DsDialog
      busy={pending}
      closeLabel={closeLabel}
      description={consequence}
      eyebrow={eyebrow}
      footer={
        <>
          <button className="fs-button" disabled={pending} onClick={onCancel} type="button">
            {t("common.cancel", "Cancel")} </button>
          <button className={`fs-button ${confirmTone}`} disabled={pending} onClick={onConfirm} type="button">
            {pending ? pendingLabel : confirmLabel}
          </button>
        </>
      }
      onClose={onCancel}
      open={open}
      title={title}
      variant="confirm"
    >
      {listed.length ? (
        <ul aria-label={t("react.shared.affected_objects", "Affected objects")} className="fs-confirm-objects">
          {listed.map((name) => (
            <li key={name}>{name}</li>
          ))}
          {more ? <li className="fs-confirm-objects-more">{t("react.shared.additional_objects_count", "and {count} more", { count: more })}</li> : null}
        </ul>
      ) : null}
      {irreversible ? <p className="fs-confirm-irreversible">{t("react.shared.this_cannot_be_undone", "This cannot be undone.")}</p> : null}
      {error ? (
        <p className="fs-confirm-error" role="alert">
          {error}
        </p>
      ) : null}
      <p aria-live="polite" className="fs-sr-only" role="status">
        {pending ? pendingLabel : ""}
      </p>
    </DsDialog>
  );
}
