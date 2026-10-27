import { useI18n } from "@/lib/i18n/useI18n";
import { DsDialog } from "@/components/shared/design-system";

import "@/components/shared/extension-plan-dialog.css";

export type BrowserAuthorityApproval = {
  extensionId: string;
  impactHash: string;
  localAssetRefs: string[];
  remoteOrigins: string[];
  remoteScriptUrls: string[];
};

type Props = {
  approval: BrowserAuthorityApproval | null;
  busy?: boolean;
  error?: string;
  onCancel: () => void;
  onConfirm: () => void;
};

function SourceList({ items, title }: Readonly<{ items: string[]; title: string }>) {
  if (!items.length) return null;
  return (
    <section className="fs-plan-dialog-section">
      <h3>{title}</h3>
      <ul className="fs-plan-dialog-list is-sources">
        {items.map((item) => <li key={item}><span className="fs-plan-dialog-value">{item}</span></li>)}
      </ul>
    </section>
  );
}

/** Approval for vendor browser code, on the published Form dialog. */
export function BrowserAuthorityApprovalModal({
  approval,
  busy = false,
  error = "",
  onCancel,
  onConfirm,
}: Readonly<Props>) {
  const { t } = useI18n();
  if (!approval) return null;
  return (
    <DsDialog
      busy={busy}
      description={t("react.shared.this_extension_will_run_code_from_its_publisher_in_your_signed_in_flow_steward_page_that_code_has_the_same_bro", "This extension will run code from its publisher in your signed-in Flow Steward page. That code has the same browser access as the page and may read visible data or act through your session. Approve only if you trust the extension and every listed source.")}
      eyebrow={t("settings.extensions.title", "Extensions")}
      footer={
        <>
          <button className="fs-button" disabled={busy} onClick={onCancel} type="button">
            {t("common.cancel", "Cancel")} </button>
          <button className="fs-button primary" disabled={busy} onClick={onConfirm} type="button">
            {t("react.shared.confirm_and_continue", "Confirm and continue")} </button>
        </>
      }
      onClose={onCancel}
      open
      title={t("react.shared.approve_external_browser_code", "Approve external browser code")}
    >
      <p className="fs-plan-dialog-id">{approval.extensionId}</p>
      <SourceList items={approval.remoteScriptUrls} title={t("react.shared.scripts_loaded_from_the_internet", "Scripts loaded from the internet")} />
      <SourceList items={approval.remoteOrigins} title={t("react.shared.sites_it_may_connect_to", "Sites it may connect to")} />
      <SourceList items={approval.localAssetRefs} title={t("react.shared.files_included_with_the_extension", "Files included with the extension")} />
      {error ? <p className="fs-plan-dialog-error" role="alert">{error}</p> : null}
    </DsDialog>
  );
}
