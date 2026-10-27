import { useI18n } from "@/lib/i18n/useI18n";

/** What a running step shows while it works: its activity, progress and finished files. */
import { DsProgress } from "@/components/shared/design-system";
import { firstReadableText, type Entity } from "@/pages/jobs/detail/job-detail-core";
import { formatElapsed } from "@/pages/jobs/detail/job-detail-execution";
import { JobFileItem } from "@/pages/jobs/detail/job-detail-file-preview";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";


export function StepLiveActivity({ now, step }: Readonly<{ now: number; step: Entity }>) {
  const { t } = useI18n();
  const startedAt = Date.parse(firstStringFromUnknownValue([step.started_at, ""]));
  const activityAt = Date.parse(firstStringFromUnknownValue([step.live_activity_at, ""]));
  const activity = firstReadableText(step.live_activity);
  const parts = [
    Number.isFinite(startedAt) ? `Running for ${formatElapsed(now - startedAt, t)}` : "Running",
    Number.isFinite(activityAt) ? `last signal ${formatElapsed(now - activityAt, t)} ago` : "",
  ].filter(Boolean);
  const done = typeof step.live_progress_done === "number" ? step.live_progress_done : undefined;
  const total = typeof step.live_progress_total === "number" ? step.live_progress_total : undefined;
  if (done !== undefined && total) {
    return (
      <div className="mt-2" data-testid="job-step-live-activity">
        <DsProgress
          label={activity || t("dashboard.classic.board.lane.working", "Working")}
          max={total}
          meta={parts.join(" · ")}
          value={Math.min(done, total)}
          valueText={`${done} of ${total}`}
        />
      </div>
    );
  }
  return (
    <p className="mt-1 break-words text-xs fs-ink-secondary" data-testid="job-step-live-activity">
      {parts.join(" · ")}
      {activity ? `: ${activity}` : ""}
    </p>
  );
}

export function StepLiveFiles({ files }: Readonly<{ files: Entity[] }>) {
  if (!files.length) {
    return null;
  }
  return (
    <ul className="mt-1 space-y-1 px-3 text-xs fs-ink-secondary" data-testid="job-step-live-files">
      {files.map((file) => (
        <li className="min-w-0" key={firstStringFromUnknownValue([file.id, file.filename, ""])}>
          <JobFileItem file={file} />
        </li>
      ))}
    </ul>
  );
}
