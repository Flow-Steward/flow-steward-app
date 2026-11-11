import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";

import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { ButtonBusyContent } from "@/components/ui/button";

import type { JobReportDownloadController } from "@/pages/jobs/detail/use-job-report-download";

type JobReportDownloadButtonProps = {
  className?: string;
  controller: JobReportDownloadController;
  idleLabel?: string;
  testId: string;
};

function downloadLabel(status: JobReportDownloadController["status"], idleLabel: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (status === "started") {
    return t("react.jobs.download_started", "Download started");
  }
  return status === "failed" ? t("react.jobs.retry_report_download", "Retry report download") : idleLabel;
}

export function JobReportDownloadButton({
  className = "",
  controller,
  idleLabel: suppliedIdleLabel,
  testId,
}: Readonly<JobReportDownloadButtonProps>) {
  const { t } = useI18n();
  const idleLabel = suppliedIdleLabel ?? t("react.jobs.download_full_report", "Download full report");
  const label = downloadLabel(controller.status, idleLabel, t);
  const preparing = controller.status === "preparing";
  const busyShown = useLoaderDelay(preparing);

  if (!controller.available) {
    return null;
  }

  return (
    <button
      aria-busy={preparing || undefined}
      className={className}
      data-testid={testId}
      disabled={controller.busy && !preparing}
      onClick={() => { if (!controller.busy) controller.start(); }}
      type="button"
    >
      <ButtonBusyContent busyLabel={t("react.jobs.preparing_report", "Preparing report…")} shown={busyShown}>{label}</ButtonBusyContent>
    </button>
  );
}
