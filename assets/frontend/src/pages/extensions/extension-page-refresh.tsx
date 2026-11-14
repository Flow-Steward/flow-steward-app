import { RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n/useI18n";

import type { ExtensionPageRefresh } from "./use-extension-page-refresh";

/** Reloads the page's queries; says when automatic refresh stopped after a failure. */
export function ExtensionPageRefreshControl({ refresh }: Readonly<{ refresh: ExtensionPageRefresh }>) {
  const { t } = useI18n();
  if (!refresh.available) return null;
  return (
    <div className="fs-extension-detail-action-row" data-testid="extension-page-refresh">
      <Button onClick={refresh.refresh} size="sm" type="button" variant="secondary">
        <RefreshCw className="h-3.5 w-3.5" />
        {t("react.extensions.page_refresh.refresh", "Refresh")}
      </Button>
      {refresh.stopped ? (
        <span className="fs-field-helper">
          {t("react.extensions.page_refresh.stopped", "Automatic refresh stopped after a failed load. Press Refresh to try again.")}
        </span>
      ) : null}
    </div>
  );
}
