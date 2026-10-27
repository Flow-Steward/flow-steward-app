import { useI18n } from "@/lib/i18n/useI18n";
import { DsAlert, DsBadge } from "@/components/shared/design-system";

/**
 * Registry detail pages report a save in two places, by outcome. A success is
 * a short, fixed status that fits the header's action slot; a failure carries
 * the server's reason, which can be long, so it is a danger alert under the
 * header where it can wrap and is announced.
 */
export function SaveStatusBadge({ status }: Readonly<{ status: string }>) {
  const { t } = useI18n();
  if (!status) return null;
  const label = status === "No changes." ? t("react.registry.display_no_changes_period", "No changes.")
    : status === "Saved." ? t("react.registry.display_saved_period", "Saved.")
    : status === "Enabled state updated." ? t("react.registry.display_enabled_state_updated", "Enabled state updated.")
    : status;
  return <DsBadge tone={status === "No changes." ? "neutral" : "green"}>{label}</DsBadge>;
}

export function SaveErrorAlert({ error }: Readonly<{ error?: string }>) {
  const { t } = useI18n();
  if (!error) return null;
  return (
    <DsAlert title={t("react.registry.changes_were_not_saved", "Changes were not saved")} tone="danger">
      {error}
    </DsAlert>
  );
}
