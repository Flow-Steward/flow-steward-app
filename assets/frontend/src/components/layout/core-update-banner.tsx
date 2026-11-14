import { useQuery } from "@tanstack/react-query";

import { DsAlert } from "@/components/shared/design-system-primitives";
import { useIsAdmin } from "@/lib/auth/use-is-admin";
import { fetchJson } from "@/lib/http/client";
import { useI18n } from "@/lib/i18n/useI18n";

const RECHECK_MS = 5 * 60_000;
const VERSION = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;

type AvailableNotice = { current_version: string; latest_version: string; release_url: string };

function availableNotice(value: unknown): AvailableNotice | null {
  if (!value || typeof value !== "object") return null;
  const { status, current_version, latest_version, release_url } = value as Record<string, unknown>;
  if (status !== "update_available" || typeof current_version !== "string" || typeof latest_version !== "string") return null;
  if (!VERSION.test(current_version) || !VERSION.test(latest_version)) return null;
  const officialUrl = `https://github.com/Flow-Steward/flow-steward-app/releases/tag/v${latest_version}`;
  if (release_url !== officialUrl) return null;
  return { current_version, latest_version, release_url: officialUrl };
}

/** Optional metadata never gates navigation, and cached notices remain admin-only. */
export function CoreUpdateBanner() {
  const isAdmin = useIsAdmin();
  const { t } = useI18n();
  const { data, isError } = useQuery({
    queryKey: ["system", "core-updates"],
    queryFn: () => fetchJson<unknown>("/api/system/core-updates"),
    enabled: isAdmin,
    select: availableNotice,
    retry: false,
    staleTime: RECHECK_MS,
    refetchInterval: RECHECK_MS,
    refetchOnWindowFocus: true,
  });
  if (!isAdmin || isError || !data) return null;
  return (
    <div className="px-4 py-2" data-testid="core-update-notice">
      <DsAlert title={t("react.system.core_update_available", "Flow Steward {version} is available", { version: data.latest_version })}>
        <div className="flex flex-wrap gap-x-3 gap-y-1">
          <span>{t("react.system.core_installed_version", "Installed version: {version}", { version: data.current_version })}</span>
          <a className="fs-link" href={data.release_url} rel="noopener noreferrer" target="_blank">
            {t("react.system.core_release_notes", "Release notes")}
          </a>
        </div>
      </DsAlert>
    </div>
  );
}
