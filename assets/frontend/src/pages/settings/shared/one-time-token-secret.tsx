import { useI18n } from "@/lib/i18n/useI18n";
import { useState } from "react";
import { DsAlert } from "@/components/shared/design-system";
import { ignoreAsyncResult } from "@/lib/utils/async";

import "@/pages/settings/shared/settings.css";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

export function OneTimeTokenSecret({
  label,
  onHide,
  secret,
}: Readonly<{
  label: string;
  onHide?: () => void;
  secret: string;
}>) {
  const { t } = useI18n();
  const [copyMessage, setCopyMessage] = useState("");
  const normalizedSecret = firstStringFromUnknownValue([secret, ""]).trim();

  if (!normalizedSecret) {
    return null;
  }

  async function copySecret() {
    try {
      await navigator.clipboard?.writeText(normalizedSecret);
      setCopyMessage(t("react.settings.token_copied", "Token copied."));
    } catch {
      setCopyMessage(t("react.settings.copy_is_not_available_in_this_browser", "Copy is not available in this browser."));
    }
  }

  return (
    <div className="fs-surface-radius fs-surface-inset border border-subtle fs-panel-group p-4">
      <DsAlert title={t("react.settings.new_token_value1", "New token ({value1})", { value1: String(label || "Token") })} tone="warning">
        {t("react.settings.this_secret_is_shown_once_copy_it_before_closing_this_drawer", "This secret is shown once. Copy it before closing this drawer.")} </DsAlert>
      <code className="fs-value-chip fs-settings-wrap">{normalizedSecret}</code>
      <div className="fs-footer-actions">
        <button className="fs-button" onClick={() => ignoreAsyncResult(copySecret())} type="button">
          {t("react.settings.copy_token", "Copy token")} </button>
        {onHide ? (
          <button className="fs-button ghost" onClick={onHide} type="button">
            {t("component.secret.hide", "Hide")} </button>
        ) : null}
        {copyMessage ? <span className="fs-field-helper" role="status">{copyMessage}</span> : null}
      </div>
    </div>
  );
}
