import { useI18n } from "@/lib/i18n/useI18n";
import { useCallback, useEffect, useRef, useState } from "react";

import { TriangleAlert } from "lucide-react";

import { fetchJson } from "@/lib/http/client";
import { ignoreAsyncResult } from "@/lib/utils/async";

// Maintenance is switched rarely and by an operator; the API refuses changes while it
// is on, so the banner only needs to catch up, not to race it.
const MAINTENANCE_POLL_INTERVAL_MS = 5 * 60_000;
// Returning to the window re-checks, but not for every switch to DevTools and back.
const MAINTENANCE_FOCUS_RECHECK_MS = 60_000;

type MaintenanceStatus = "disabled" | "draining" | "enabled";

type MaintenanceStatusPayload = {
  activated_at: string | null;
  owner_kind: "manual" | "release" | null;
  status: MaintenanceStatus;
};

function maintenanceStatusFromUnknown(value: unknown): MaintenanceStatusPayload | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const payload = value as Record<string, unknown>;
  if (!(["disabled", "draining", "enabled"] as const).includes(payload.status as MaintenanceStatus)) {
    return null;
  }
  return {
    activated_at:
      typeof payload.activated_at === "string" ? payload.activated_at : null,
    owner_kind:
      payload.owner_kind === "manual" || payload.owner_kind === "release"
        ? payload.owner_kind
        : null,
    status: payload.status as MaintenanceStatus,
  };
}

export function MaintenanceBanner() {
  const { t } = useI18n();
  const [maintenance, setMaintenance] = useState<MaintenanceStatusPayload | null>(null);
  const activeRequest = useRef<AbortController | null>(null);
  const lastCheckedAt = useRef(0);

  const refresh = useCallback(async () => {
    lastCheckedAt.current = Date.now();
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    try {
      const payload = await fetchJson<unknown>("/api/system/maintenance", {
        signal: controller.signal,
      }, { presentation: "background" });
      const next = maintenanceStatusFromUnknown(payload);
      if (!controller.signal.aborted && next !== null) {
        setMaintenance(next);
      }
    } catch {
      // Keep the last confirmed status. Backend maintenance gates remain authoritative.
    } finally {
      if (activeRequest.current === controller) {
        activeRequest.current = null;
      }
    }
  }, []);

  useEffect(() => {
    if (globalThis.window === undefined) {
      return undefined;
    }
    ignoreAsyncResult(refresh());
    const refreshOnFocus = () => {
      if (Date.now() - lastCheckedAt.current >= MAINTENANCE_FOCUS_RECHECK_MS) {
        ignoreAsyncResult(refresh());
      }
    };
    const interval = globalThis.setInterval(
      () => ignoreAsyncResult(refresh()),
      MAINTENANCE_POLL_INTERVAL_MS,
    );
    globalThis.addEventListener("focus", refreshOnFocus);
    return () => {
      globalThis.clearInterval(interval);
      globalThis.removeEventListener("focus", refreshOnFocus);
      activeRequest.current?.abort();
      activeRequest.current = null;
    };
  }, [refresh]);

  if (maintenance === null || maintenance.status === "disabled") {
    return null;
  }

  const enabled = maintenance.status === "enabled";
  return (
    <div
      aria-live="polite"
      className="fs-maintenance-banner"
      data-status={maintenance.status}
      role="status"
    >
      <TriangleAlert aria-hidden="true" className="fs-maintenance-banner__icon" />
      <div className="fs-maintenance-banner__copy">
        <strong>
          {enabled ? t("react.shared.maintenance_mode_is_enabled", "Maintenance Mode is enabled") : t("react.shared.maintenance_mode_is_starting", "Maintenance Mode is starting")}
        </strong>
        <span>
          {enabled
            ? t("react.shared.jobs_schedules_integrations_and_changes_are_paused", "Jobs, schedules, integrations, and changes are paused.")
            : t("react.shared.new_work_is_paused_while_active_work_finishes", "New work is paused while active work finishes.")}
        </span>
      </div>
    </div>
  );
}

export { MAINTENANCE_POLL_INTERVAL_MS };
