import { useI18n } from "@/lib/i18n/useI18n";
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
  restartPending: boolean;
}>) {
  const { t } = useI18n();
  return (
    <div className="flex flex-wrap items-center justify-end gap-2">
      <JobReportDownloadButton className="fs-button secondary" controller={reportDownload} testId="job-download-report" />
      {canMutate && canAcknowledge ? (
        <button className="fs-button secondary" data-testid="job-acknowledge-button" disabled={acknowledgePending} onClick={() => requestAcknowledgeJob(!acknowledged)} type="button">
          {acknowledged ? t("react.jobs.unacknowledge", "Unacknowledge") : t("react.jobs.acknowledge", "Acknowledge")}
        </button>
      ) : null}
      {canMutate && isCancellable ? <button className="fs-button accent" data-testid="job-cancel-button" disabled={cancelPending} onClick={requestCancelJob} type="button">{cancelPending ? t("react.jobs.cancelling", "Cancelling…") : t("registry.workflow_detail.automation.action.cancel_job", "Cancel job")}</button> : null}
      {canMutate && isTerminal ? <button className="fs-button primary" data-testid="job-restart-button" disabled={restartPending} onClick={requestRestartJob} type="button">{restartPending ? t("react.jobs.restarting", "Restarting…") : t("react.jobs.restart_job", "Restart job")}</button> : null}
    </div>
  );
}
