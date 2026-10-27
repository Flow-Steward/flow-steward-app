import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useEffect, useMemo, useState } from "react";

import { Check } from "lucide-react";

import { DsMetricCard } from "@/components/shared/design-system";
import "@/pages/dashboard/dashboard-segmented.css";
import { formatAge } from "@/pages/dashboard/format";
import { FAILED_OR_STUCK_GROUPS, jobsListHref } from "@/pages/dashboard/job-links";
import { jobStatusGroupLabel } from "@/pages/jobs/status-groups";
import "@/pages/dashboard/operational-overview.css";



function getRanges(t: I18nContextValue["t"] = fallbackContext.t) {
  return [
  { key: "24h", label: t("react.dashboard.24h", "24h") },
  { key: "7d", label: t("react.dashboard.7d", "7d") },
  { key: "30d", label: t("react.dashboard.30d", "30d") },
] as const;
}

export type OperationalOverviewProps = {
  range: string;
  onRangeChange: (range: string) => void;
  /** Failed, stuck and approval jobs together: only the all-clear notice reads it. */
  needsAttention: number;
  failedOrStuck: number;
  inProgress: number;
  awaitingApproval: number;
  /** Every stored status of the Waiting group: timers, child workflows, external waits. */
  waiting: number;
  completed: number;
  queued: number | null;
  longestRunningMs: number | null;
  oldestFailingMs: number | null;
  oldestWaitingMs: number | null;
  successRate: string;
  successRateDeltaPp: number | null;
  generatedAt: string;
};

function relativeAge(generatedAt: string, now: number, t: I18nContextValue["t"] = fallbackContext.t): string {
  const stamp = Date.parse(generatedAt);
  if (Number.isNaN(stamp)) {
    return "";
  }
  const seconds = Math.max(0, Math.round((now - stamp) / 1000));
  if (seconds < 5) {
    return t("dashboard.time.just_now", "just now");
  }
  if (seconds < 60) {
    return t("react.dashboard.value1_s_ago", "{value1}s ago", { value1: String(seconds) });
  }
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) {
    return t("react.dashboard.value1_m_ago", "{value1}m ago", { value1: String(minutes) });
  }
  return t("react.dashboard.value1_h_ago", "{value1}h ago", { value1: String(Math.round(minutes / 60)) });
}

/** Ticks on its own so the rest of the row does not re-render every second. */
function FreshnessLabel({ generatedAt }: Readonly<{ generatedAt: string }>) {
  const { t } = useI18n();
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = globalThis.setInterval(() => setNow(Date.now()), 1000);
    return () => globalThis.clearInterval(timer);
  }, []);

  const age = relativeAge(generatedAt, now, t);
  if (!age) {
    return null;
  }
  return <span className="fs-ov__live-ago">{t("react.dashboard.updated", "· updated")} {age}</span>;
}

/** No movement is not a signal, so a zero delta renders nothing at all. */
function formatDeltaPp(value: number | null): string {
  if (value === null || !Number.isFinite(value)) {
    return "";
  }
  const rounded = Math.round(value * 10) / 10;
  if (rounded === 0) {
    return "";
  }
  return `${rounded > 0 ? "+" : ""}${rounded}pp`;
}

/** The endpoint renders an em dash when a rate cannot be computed. */
function hasRate(display: string): boolean {
  return /\d/.test(display);
}

function HealthyQueueNotice({ needsAttention }: Readonly<{ needsAttention: number }>) {
  const { t } = useI18n();
  if (needsAttention !== 0) return null;
  return (
    <div className="fs-ov__clear">
      <span aria-hidden="true" className="fs-ov__clear-mark"><Check /></span>
      <div>
        <div className="fs-ov__clear-title">{t("react.dashboard.everything_is_running_clean", "Everything is running clean")}</div>
        <div className="fs-ov__clear-note">{t("react.dashboard.no_failed_blocked_or_interrupted_jobs_in_this_range", "No failed, blocked or interrupted jobs in this range.")}</div>
      </div>
    </div>
  );
}

function InProgressMeta({
  longestRunningLabel,
  queued,
  numberFormat,
}: Readonly<{
  longestRunningLabel: string;
  queued: number | null;
  numberFormat: Intl.NumberFormat;
}>) {
  const { t } = useI18n();
  const hasQueuedJobs = queued !== null && queued > 0;
  if (!longestRunningLabel && !hasQueuedJobs) return null;
  return (
    <>
      {longestRunningLabel ? t("react.dashboard.longest_running_value1", "Longest running {value1}", { value1: String(longestRunningLabel) }) : null}
      {longestRunningLabel && hasQueuedJobs ? " · " : null}
      {hasQueuedJobs ? t("react.dashboard.value1_queued", "{value1} queued", { value1: String(numberFormat.format(queued)) }) : null}
    </>
  );
}

export function OperationalOverview({
  range,
  onRangeChange,
  needsAttention,
  failedOrStuck,
  inProgress,
  awaitingApproval,
  waiting,
  completed,
  queued,
  longestRunningMs,
  oldestFailingMs,
  oldestWaitingMs,
  successRate,
  successRateDeltaPp,
  generatedAt,
}: Readonly<OperationalOverviewProps>) {
  const { t } = useI18n();
  const deltaLabel = formatDeltaPp(successRateDeltaPp);
  const deltaTone = successRateDeltaPp !== null && successRateDeltaPp < 0 ? "attention" : "positive";
  const numberFormat = useMemo(() => new Intl.NumberFormat(), []);
  const longestRunningLabel = formatAge(longestRunningMs, t);
  const oldestWaitingLabel = formatAge(oldestWaitingMs, t);
  const oldestFailingLabel = formatAge(oldestFailingMs, t);

  return (
    <section aria-labelledby="fs-ov-title" className="fs-ov">
      <header className="fs-ov__head">
        <div className="fs-ov__ident">
          <h2 className="fs-ov__title" id="fs-ov-title">
            {t("react.dashboard.operational_overview", "Operational overview")} </h2>
          <p className="fs-ov__live">
            <span aria-hidden="true" className="fs-ov__live-dot" />
            {t("react.dashboard.live", "Live")} <FreshnessLabel generatedAt={generatedAt} />
          </p>
        </div>

        <fieldset aria-label={t("react.dashboard.time_range", "Time range")} className="fs-seg">
          {getRanges(t).map((item) => (
            <button
              aria-pressed={range === item.key}
              className="fs-seg__item"
              key={item.key}
              onClick={() => onRangeChange(item.key)}
              type="button"
            >
              {item.label}
            </button>
          ))}
        </fieldset>
      </header>

      <HealthyQueueNotice needsAttention={needsAttention} />

      <div className="fs-ov__grid">
        {/* One card per status group, with the group's label: red when the run
            broke, orange when a human has to approve. Each card opens the job
            list filtered to its groups. The job list has no time-range filter,
            so the range control does not travel with it. */}
        <DsMetricCard
          ariaLabel={t("react.dashboard.failed_or_stuck_count_show_jobs", "Failed / Stuck: {count}. Show failed and stuck jobs", { count: String(failedOrStuck) })}
          danger={failedOrStuck > 0}
          density="hero"
          figure={numberFormat.format(failedOrStuck)}
          label={`${jobStatusGroupLabel("failed", t)} / ${jobStatusGroupLabel("stuck", t)}`}
          // The range control does not scope this count - it is a live queue
          // snapshot - so the empty copy says "now", not "in range".
          meta={failedOrStuck > 0 && oldestFailingLabel ? t("react.dashboard.oldest_failing_age", "Oldest failing {age}", { age: oldestFailingLabel }) : t("react.dashboard.none_right_now", "None right now")}
          to={jobsListHref(FAILED_OR_STUCK_GROUPS, { hideAcknowledged: true })}
        />

        <DsMetricCard
          ariaLabel={t("react.dashboard.in_progress_value1_open_the_job_queue_filtered_to_running_jobs", "In progress: {value1}. Open the job queue filtered to running jobs", { value1: String(inProgress) })}
          density="hero"
          figure={numberFormat.format(inProgress)}
          label={jobStatusGroupLabel("in_progress", t)}
          meta={<InProgressMeta longestRunningLabel={longestRunningLabel} numberFormat={numberFormat} queued={queued} />}
          to={jobsListHref(["in_progress"])}
        />

        <DsMetricCard
          ariaLabel={t("react.dashboard.waiting_for_approval_count_show_jobs", "Waiting for approval: {count}. Show jobs waiting for approval", { count: String(awaitingApproval) })}
          attention={awaitingApproval > 0}
          density="hero"
          figure={numberFormat.format(awaitingApproval)}
          label={jobStatusGroupLabel("awaiting_approval", t)}
          meta={oldestWaitingLabel ? t("react.dashboard.oldest_waiting_age", "Oldest waiting {age}", { age: oldestWaitingLabel }) : null}
          to={jobsListHref(["awaiting_approval"])}
        />

        <DsMetricCard
          ariaLabel={t("react.dashboard.waiting_count_show_jobs", "Waiting: {count}. Show waiting jobs", { count: String(waiting) })}
          density="hero"
          figure={numberFormat.format(waiting)}
          label={jobStatusGroupLabel("waiting", t)}
          to={jobsListHref(["waiting"])}
        />

        <DsMetricCard
          ariaLabel={t("react.dashboard.completed_value1_open_the_job_queue_filtered_to_completed_jobs", "Completed: {value1}. Open the job queue filtered to completed jobs", { value1: String(completed) })}
          delta={deltaLabel && hasRate(successRate) ? { label: deltaLabel, tone: deltaTone } : undefined}
          density="hero"
          figure={numberFormat.format(completed)}
          label={jobStatusGroupLabel("completed", t)}
          meta={hasRate(successRate) ? t("react.dashboard.success_rate", "{rate} success", { rate: successRate }) : null}
          to={jobsListHref(["completed"])}
        />
      </div>
    </section>
  );
}
