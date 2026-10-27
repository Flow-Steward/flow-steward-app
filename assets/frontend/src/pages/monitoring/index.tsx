import { useI18n } from "@/lib/i18n/useI18n";

import { getMonitoringTabItems } from "./monitoring-data";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router";
import { DsDialog, DsPageHeader, DsTabs, PageContainer } from "@/components/shared/design-system";
import { queryKeys } from "@/lib/query/keys";
import { EventsAuditTrail } from "@/pages/monitoring/shared/events";
import { cleanupMonitoringStorageOrphan, fetchMonitoringStorage, fetchMonitoringSummary } from "@/pages/monitoring/api";
import { ProcessesControlPanel } from "@/pages/monitoring/shared/processes";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { type Entity, type OrphanStorageRow, type MonitoringTabKey, objectOrEmpty, listFromUnknown, normalizeMonitoringTab, stringValue, numberValue, normalizeCheckRows, effectiveReadiness, unknownRows, orphanStorageScope, normalizeComponents } from "./monitoring-data";
import { MonitoringStatusStrip, MonitoringOverview, MonitoringServices, MonitoringQueryStatus } from "./monitoring-panels";
import { MonitoringStorage } from "./monitoring-storage";
import { JobReportDownloadButton } from "@/pages/jobs/detail/job-report-download-button";
import { useJobReportDownload } from "@/pages/jobs/detail/use-job-report-download";
import "./monitoring.css";

// The redacted installation report an operator attaches to a bug report.
const SYSTEM_DIAGNOSTICS_REPORT_URL = "/api/system/diagnostics/report";

export function MonitoringPage() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const [orphanCleanupFeedback, setOrphanCleanupFeedback] = useState<
    { message: string; tone: "success" | "danger" } | null
  >(null);
  const [orphanCleanupTarget, setOrphanCleanupTarget] = useState("");
  const [pendingOrphan, setPendingOrphan] = useState<OrphanStorageRow | null>(null);
  const diagnosticsDownload = useJobReportDownload(SYSTEM_DIAGNOSTICS_REPORT_URL);
  const summaryQuery = useQuery({
    queryKey: queryKeys.monitoringSummary,
    queryFn: fetchMonitoringSummary,
    refetchInterval: 20_000,
  });
  const storageQuery = useQuery({
    queryKey: queryKeys.monitoringStorage,
    queryFn: fetchMonitoringStorage,
    refetchInterval: 20_000,
  });
  const cleanupOrphanMutation = useMutation({
    mutationFn: cleanupMonitoringStorageOrphan,
    onMutate: () => {
      setOrphanCleanupFeedback(null);
    },
    onSuccess: async (payload) => {
      setOrphanCleanupFeedback({
        message: stringValue(payload.message, t("react.monitoring.orphaned_storage_scope_deleted", "Orphaned storage scope deleted.")),
        tone: "success",
      });
      await queryClient.invalidateQueries({ queryKey: queryKeys.monitoringStorage });
    },
    onError: (error) => {
      setOrphanCleanupFeedback({
        message: error instanceof Error ? error.message : t("react.monitoring.failed_to_clean_orphaned_storage_scope", "Failed to clean orphaned storage scope."),
        tone: "danger",
      });
    },
    onSettled: () => {
      setOrphanCleanupTarget("");
      setPendingOrphan(null);
    },
  });

  const summary = summaryQuery.data;
  const storage = storageQuery.data;
  const requestedTab = firstStringFromUnknownValue([searchParams.get("tab"), ""]).trim();
  const activeTab = normalizeMonitoringTab(requestedTab);
  const totals = objectOrEmpty(summary?.summary);
  const checks = normalizeCheckRows(summary?.checks, t);
  // Without this the header renders the zero-defaults of an absent payload as
  // if they were measurements: "Failed checks: 0"
  // on a deployment the page could not read at all.
  const summaryUnavailable = summaryQuery.isError || !summary;
  const components = summaryUnavailable ? null : normalizeComponents(summary?.components);
  const meta = objectOrEmpty(summary?.meta);
  const ready = typeof summary?.ready === "boolean" ? effectiveReadiness(summary.ready, checks) : null;
  const eventRows = (() => {
    const rootEvents = listFromUnknown((summary as Entity | undefined)?.events);
    if (rootEvents.length) {
      return rootEvents;
    }
    const metaEvents = listFromUnknown(meta.events);
    if (metaEvents.length) {
      return metaEvents;
    }
    return checks
      .filter((check) => Boolean(check.detail))
      .map((check) => ({
        category: "watchdog",
        message: check.detail || "",
        source: check.key,
        status: check.status,
      }));
  })();

  const storageBackendMatrix = objectOrEmpty(storage?.backend_matrix);
  const storageSummary = objectOrEmpty(storage?.summary);
  const storageMemory = objectOrEmpty(storage?.memory);
  const storageVector = objectOrEmpty(storage?.vector);
  const orphanRows: OrphanStorageRow[] = [
    ...unknownRows(storageMemory.orphan_rows).map((row) => ({ backend: "memory", ...row })),
    ...unknownRows(storageVector.orphan_rows).map((row) => ({ backend: "vector", ...row })),
  ];
  const requestOrphanCleanup = (row: OrphanStorageRow) => {
    if (!orphanStorageScope(row) || !stringValue(row.backend)) {
      setOrphanCleanupFeedback({ message: t("react.monitoring.storage_cleanup_requires_a_backend_and_scope", "Storage cleanup requires a backend and scope."), tone: "danger" });
      return;
    }
    setPendingOrphan(row);
  };
  const confirmOrphanCleanup = () => {
    if (!pendingOrphan) return;
    const scope = orphanStorageScope(pendingOrphan);
    const backend = stringValue(pendingOrphan.backend);
    const confirmToken = stringValue(pendingOrphan.cleanup_confirm, "DELETE_ORPHAN_STORAGE");
    setOrphanCleanupTarget(`${backend}:${scope}`);
    cleanupOrphanMutation.mutate({ backend, scope, confirm: confirmToken });
  };
  const pendingOrphanBackend = pendingOrphan ? stringValue(pendingOrphan.backend) : "";
  const pendingOrphanScope = pendingOrphan ? orphanStorageScope(pendingOrphan) : "";

  function selectTab(tab: MonitoringTabKey) {
    const nextParams = new URLSearchParams(searchParams);
    if (tab === "overview") {
      nextParams.delete("tab");
    } else {
      nextParams.set("tab", tab);
    }
    setSearchParams(nextParams, { replace: true });
  }

  return (
    <PageContainer width="wide">
      <section className="fs-panel-stack" data-section="monitoring">
        <DsPageHeader
          action={
            <JobReportDownloadButton
              className="fs-button secondary"
              controller={diagnosticsDownload}
              idleLabel={t("react.monitoring.download_diagnostics", "Download diagnostics")}
              testId="monitoring-download-diagnostics"
            />
          }
          description={t("react.monitoring.review_readiness_services_processes_and_audit_activity_from_one_dashboard", "Review readiness, services, processes, and audit activity from one dashboard.")}
          eyebrow={t("shell.nav.system", "System")}
          title={t("react.monitoring.system_monitoring", "System monitoring")}
        />
        <MonitoringStatusStrip
          components={components}
          meta={meta}
          ready={ready}
          summaryUnavailable={summaryUnavailable}
          totals={totals}
        />
        <MonitoringQueryStatus
          error={summaryQuery.isError ? summaryQuery.error : null}
          loading={summaryQuery.isLoading}
        />
        <section aria-label={t("react.monitoring.system_monitoring", "System monitoring")} className="fs-settings-tabs">
          <DsTabs
            active={activeTab}
            ariaLabel={t("react.monitoring.monitoring_sections", "Monitoring sections")}
            idPrefix="monitoring"
            items={getMonitoringTabItems(t)}
            onSelect={(key) => selectTab(key as MonitoringTabKey)}
          />
          <div
            aria-labelledby={`monitoring-tab-${activeTab}`}
            className="fs-tabs-panel"
            id={`monitoring-panel-${activeTab}`}
            role="tabpanel"
          >
            <div className="fs-panel-stack">
              {activeTab === "overview" ? (
                <MonitoringOverview
                  components={components}
                  checks={checks}
                  checksFailed={numberValue(totals.checks_failed) ?? 0}
                  eventCount={eventRows.length}
                  loading={summaryQuery.isLoading}
                  meta={meta}
                  summaryUnavailable={summaryUnavailable}
                />
              ) : null}
              {activeTab === "events" ? <EventsAuditTrail /> : null}
              {activeTab === "processes" ? <ProcessesControlPanel /> : null}
              {activeTab === "services" ? (
                <MonitoringServices
                  components={components}
                  checks={checks}
                  loading={summaryQuery.isLoading}
                />
              ) : null}
              {activeTab === "storage" ? (
                <MonitoringStorage
                  backendMatrix={storageBackendMatrix}
                  cleanupPending={cleanupOrphanMutation.isPending}
                  cleanupTarget={orphanCleanupTarget}
                  feedback={orphanCleanupFeedback}
                  loading={storageQuery.isLoading}
                  memory={storageMemory}
                  onClean={requestOrphanCleanup}
                  orphanRows={orphanRows}
                  summary={storageSummary}
                  vector={storageVector}
                />
              ) : null}
            </div>
          </div>
        </section>
        <DsDialog
          busy={cleanupOrphanMutation.isPending}
          closeLabel={t("react.monitoring.close_cleanup_confirmation", "Close cleanup confirmation")}
          description={t("react.monitoring.delete_orphaned_value1_storage_scope_value2_this_cannot_be_undone", "Delete orphaned {value1} storage scope {value2}? This cannot be undone.", { value1: String(pendingOrphanBackend), value2: String(pendingOrphanScope) })}
          eyebrow={t("shell.nav.system", "System")}
          footer={
            <>
              <button
                className="fs-button"
                disabled={cleanupOrphanMutation.isPending}
                onClick={() => setPendingOrphan(null)}
                type="button"
              >
                {t("common.cancel", "Cancel")} </button>
              <button
                className="fs-button danger"
                disabled={cleanupOrphanMutation.isPending}
                onClick={confirmOrphanCleanup}
                type="button"
              >
                {cleanupOrphanMutation.isPending ? t("react.monitoring.deleting", "Deleting...") : t("react.monitoring.delete_orphan_data", "Delete orphan data")}
              </button>
            </>
          }
          onClose={() => setPendingOrphan(null)}
          open={Boolean(pendingOrphan)}
          title={t("react.monitoring.delete_orphaned_storage", "Delete orphaned storage")}
          variant="confirm"
        />
      </section>
    </PageContainer>
  );
}
