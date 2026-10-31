import { useI18n } from "@/lib/i18n/useI18n";
import { DsAlert, DsBadge } from "@/components/shared/design-system";
import type { RegistryConflict } from "./registry-conflict";

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

/**
 * A taken id or name also lists the objects the server named and offers its
 * suggestion; the user applies it to the form and saves again themselves.
 */
export function SaveErrorAlert({
  error,
  title,
  conflict,
  onUseDisplayName,
  onUseId,
}: Readonly<{
  error?: string;
  title?: string;
  conflict?: RegistryConflict | null;
  onUseDisplayName?: (value: string) => void;
  onUseId?: (value: string) => void;
}>) {
  const { t } = useI18n();
  if (!error) return null;
  const suggestedName = conflict?.code === "registry_name_conflict" ? conflict.suggestedDisplayName : "";
  const suggestedId = conflict?.code === "registry_id_conflict" ? conflict.suggestedId : "";
  return (
    <DsAlert title={title || t("react.registry.changes_were_not_saved", "Changes were not saved")} tone="danger">
      {error}
      {conflict?.matches.length ? (
        <ul className="mt-2 flex flex-col gap-1" data-testid="registry-conflict-matches">
          {conflict.matches.map((match) => (
            <li key={match.id}>
              <strong>{match.display_name || match.id}</strong> <code>{match.id}</code>
              {" · "}
              {match.enabled ? t("settings.mcp.status.enabled", "Enabled") : t("react.registry.disabled", "Disabled")}
              {match.usedBy.length
                ? ` · ${t("react.registry.conflict_used_by_value0", "Used by: {value0}", {
                    value0: match.usedBy.map((row) => row.display_name || row.id).join(", "),
                  })}`
                : null}
            </li>
          ))}
        </ul>
      ) : null}
      {suggestedName && onUseDisplayName ? (
        <button className="fs-button mt-2" onClick={() => onUseDisplayName(suggestedName)} type="button">
          {t("react.registry.conflict_use_name_value0", "Use the name “{value0}”", { value0: suggestedName })}
        </button>
      ) : null}
      {suggestedId && onUseId ? (
        <button className="fs-button mt-2" onClick={() => onUseId(suggestedId)} type="button">
          {t("react.registry.conflict_use_id_value0", "Use the ID “{value0}”", { value0: suggestedId })}
        </button>
      ) : null}
    </DsAlert>
  );
}
