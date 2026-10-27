import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { Loader2 } from "lucide-react";

import type { JobReportDownloadController } from "@/pages/jobs/detail/use-job-report-download";

type JobReportDownloadButtonProps = {
  className?: string;
  controller: JobReportDownloadController;
  idleLabel?: string;
  testId: string;
};

function downloadLabel(status: JobReportDownloadController["status"], idleLabel: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (status === "preparing") {
    return t("react.jobs.preparing_report", "Preparing report…");
  }
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

  if (!controller.available) {
    return null;
  }

  return (
    <button
      className={className}
      data-testid={testId}
      disabled={controller.busy}
      onClick={controller.start}
      type="button"
    >
      {controller.status === "preparing" ? (
        <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" />
      ) : null}
      {label}
    </button>
  );
}
