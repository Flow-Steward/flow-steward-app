import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useEffect, useRef, useState } from "react";

import { DsAlert, DsCheckbox, DsDialog } from "@/components/shared/design-system";
import type { ExtensionUninstallPlan, ExtensionUninstallPlanRow } from "@/pages/extensions/api";

import "@/components/shared/extension-plan-dialog.css";

type Props = {
  busy?: boolean;
  confirmDisabled?: boolean;
  error?: string;
  onClose: () => void;
  onConfirm: () => void;
  plan: ExtensionUninstallPlan | null;
};

/** The API sends a plain `label`; an older row falls back to its kind, in words. */
function rowLabel(row: ExtensionUninstallPlanRow, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (row.label) {
    return row.label;
  }
  const words = row.kind.replaceAll("_", " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : t("react.shared.item", "Item");
}

/** Rows with nothing in them say nothing to the person confirming. */
function visibleRows(rows: ExtensionUninstallPlanRow[] | undefined): ExtensionUninstallPlanRow[] {
  return (rows || []).filter((row) => row.count !== 0);
}

/**
 * Label, name and project for one plan row. The row id is never shown: it is a
 * storage key (often a UUID), not something a person recognises.
 */
function rowCopy(row: ExtensionUninstallPlanRow, t: I18nContextValue["t"] = fallbackContext.t) {
  return (
    <span className="fs-plan-dialog-row-copy">
      <span className="fs-plan-dialog-kind">{rowLabel(row, t)}</span>
      {row.display_name ? <span className="fs-plan-dialog-value">{row.display_name}</span> : null}
      {row.project_name ? <span className="fs-plan-dialog-reason">{t("settings.notifications.project", "Project:")} {row.project_name}</span> : null}
    </span>
  );
}

/**
 * The uninstall cascade, on the published Form dialog: it has a scrollable
 * plan and an acknowledgement field, which modal-dialog.md sends to Form dialog
 * rather than to the short confirmation. The irreversible line, the pending
 * label and the verb-plus-object Danger action follow the confirmation contract.
 */
export function ExtensionUninstallPlanModal({
  busy = false,
  confirmDisabled = false,
  error = "",
  onClose,
  onConfirm,
  plan,
}: Readonly<Props>) {
  const { t } = useI18n();
  const [confirmed, setConfirmed] = useState(false);
  const errorRef = useRef<HTMLDivElement>(null);
  // A refusal must be seen where the user is: the alert sits at the top of the
  // plan and is scrolled into view, not left under a long list.
  useEffect(() => {
    if (error) errorRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [error]);
  if (!plan) {
    return null;
  }
  const extensionName = plan.extension_name || plan.extension_id;
  const remove = visibleRows(plan.remove);
  const retained = visibleRows(plan.retained);
  return (
    <DsDialog
      busy={busy}
      description={t("react.shared.everything_listed_under_will_be_removed_is_deleted_with_the_extension", "Everything listed under “Will be removed” is deleted with the extension.")}
      eyebrow={t("settings.extensions.title", "Extensions")}
      footer={
        <>
          <button className="fs-button" disabled={busy} onClick={onClose} type="button">
            {t("common.cancel", "Cancel")} </button>
          <button
            className="fs-button danger"
            disabled={busy || confirmDisabled || !confirmed}
            onClick={onConfirm}
            type="button"
          >
            {busy ? t("react.shared.uninstalling", "Uninstalling…") : t("react.shared.uninstall_extension", "Uninstall extension")}
          </button>
        </>
      }
      onClose={onClose}
      open
      title={t("react.shared.extension_uninstall_plan", "Extension uninstall plan")}
    >
      <p className="fs-plan-dialog-subject">
        {[extensionName, plan.extension_version].filter(Boolean).join(" ")}
      </p>
      {extensionName === plan.extension_id ? null : (
        <p className="fs-plan-dialog-id">{plan.extension_id}</p>
      )}
      {error ? (
        <div ref={errorRef}>
          <DsAlert title={error} tone="danger" />
        </div>
      ) : null}

      <section className="fs-plan-dialog-section">
        <h3>{t("react.shared.will_be_removed", "Will be removed")}</h3>
        <ul className="fs-plan-dialog-list is-plan">
          {remove.map((row) => (
            <li key={`${row.kind}:${row.id}`}>
              {rowCopy(row, t)}
              {row.count && row.count > 1 ? <span className="fs-plan-dialog-count">{row.count}</span> : null}
            </li>
          ))}
        </ul>
      </section>

      {retained.length ? (
        <section className="fs-plan-dialog-section">
          <h3>{t("react.shared.retained_audit_history", "Retained audit history")}</h3>
          <ul className="fs-plan-dialog-list is-plan">
            {retained.map((row) => (
              <li key={`${row.kind}:${row.id}`}>
                {rowCopy(row, t)}
                {row.count && row.count > 1 ? <span className="fs-plan-dialog-count">{row.count}</span> : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <section className="fs-plan-dialog-section">
        <h3>{t("react.shared.will_remain_but_stop_working", "Will remain but stop working")}</h3>
        {plan.affected.length ? (
          <ul className="fs-plan-dialog-list is-plan">
            {plan.affected.map((row) => (
              <li key={`${row.kind}:${row.id}`}>
                <span className="fs-plan-dialog-row-copy">
                  <span>
                    {row.display_name || t("react.shared.unnamed_value1", "Unnamed {value1}", { value1: String(rowLabel(row, t).toLowerCase()) })}
                    <span className="fs-plan-dialog-kind is-inline">{rowLabel(row, t)}</span>
                  </span>
                  {row.project_name ? (
                    <span className="fs-plan-dialog-reason">{t("settings.notifications.project", "Project:")} {row.project_name}</span>
                  ) : null}
                  <span className="fs-plan-dialog-reason">{row.explanation || row.reason}</span>
                </span>
              </li>
            ))}
          </ul>
        ) : <p className="fs-plan-dialog-empty">{t("react.shared.nothing_else_depends_on_this_extension", "Nothing else depends on this extension.")}</p>}
      </section>

      <p className="fs-plan-dialog-irreversible">{t("react.shared.this_cannot_be_undone", "This cannot be undone.")}</p>
      <DsCheckbox checked={confirmed} onChange={setConfirmed}>
        {t("react.shared.i_understand_this_can_t_be_undone", "I understand this can't be undone.")} </DsCheckbox>
      <p aria-live="polite" className="fs-sr-only" role="status">{busy ? t("react.shared.uninstalling", "Uninstalling…") : ""}</p>
    </DsDialog>
  );
}
