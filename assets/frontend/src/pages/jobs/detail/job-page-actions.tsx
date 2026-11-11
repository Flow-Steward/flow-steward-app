import { useI18n } from "@/lib/i18n/useI18n";
import { Button } from "@/components/ui/button";
import { JobReportDownloadButton } from "./job-report-download-button";
import type { useJobReportDownload } from "./use-job-report-download";

export function JobPageActions({
  acknowledged,
  acknowledgePending,
  canAcknowledge,
  canMutate,
  cancelPending,
  isCancellable,
  isTerminal,
  reportDownload,
  requestCancelJob,
  requestRestartJob,
  requestAcknowledgeJob,
  restartAllowed = true,
  restartPending,
}: Readonly<{
  acknowledged: boolean;
  acknowledgePending: boolean;
  canAcknowledge: boolean;
  canMutate: boolean;
  cancelPending: boolean;
  isCancellable: boolean;
  isTerminal: boolean;
  reportDownload: ReturnType<typeof useJobReportDownload>;
  requestCancelJob: () => void;
  requestRestartJob: () => void;
  requestAcknowledgeJob: (acknowledged: boolean) => void;
  /** False for a run that is restarted only through its parent run. */
  restartAllowed?: boolean;
  restartPending: boolean;
}>) {
  const { t } = useI18n();
  return (
    <div className="flex flex-wrap items-center justify-end gap-2">
      <JobReportDownloadButton className="fs-button secondary" controller={reportDownload} testId="job-download-report" />
      {canMutate && canAcknowledge ? (
        <Button busy={acknowledgePending} busyLabel={t("react.settings.updating", "Updating…")} data-testid="job-acknowledge-button" onClick={() => requestAcknowledgeJob(!acknowledged)} type="button" variant="secondary">
          {acknowledged ? t("react.jobs.unacknowledge", "Unacknowledge") : t("react.jobs.acknowledge", "Acknowledge")}
        </Button>
      ) : null}
      {canMutate && isCancellable ? <Button busy={cancelPending} busyLabel={t("react.jobs.cancelling", "Cancelling…")} data-testid="job-cancel-button" onClick={requestCancelJob} type="button" variant="accent">{t("registry.workflow_detail.automation.action.cancel_job", "Cancel job")}</Button> : null}
      {canMutate && isTerminal && restartAllowed ? <Button busy={restartPending} busyLabel={t("react.jobs.restarting", "Restarting…")} data-testid="job-restart-button" onClick={requestRestartJob} type="button" variant="primary">{t("react.jobs.restart_job", "Restart job")}</Button> : null}
    </div>
  );
}
