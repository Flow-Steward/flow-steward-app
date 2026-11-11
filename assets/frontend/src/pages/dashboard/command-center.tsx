import { useI18n } from "@/lib/i18n/useI18n";
import { AlertTriangle, ArrowRight, Check, ChevronRight, CircleDot, Clock3 } from "lucide-react";
import { Link } from "react-router";

import type { DsMetricCardLoading } from "@/components/shared/design-system";
import "@/pages/dashboard/dashboard-panels.css";
import "@/pages/dashboard/command-center.css";

export type AttentionQueueTone = "danger" | "attention" | "neutral";

export type AttentionQueueItem = {
  id: string;
  /** The workflow's name, or the task summary of a run without a workflow. */
  title: string;
  /** Where the run came from and how long ago it stopped, already formatted. */
  meta: string;
  status: string;
  tone: AttentionQueueTone;
};

export type CommandCenterProps = {
  queue: AttentionQueueItem[];
  queueTotal: number;
  /** The job list filtered to the statuses the queue is built from. */
  queueHref: string;
  /**
   * The first answer has not arrived: "reserved" keeps the rows empty for the
   * first 400ms, "skeleton" holds three stand-in rows. Neither shows the
   * all-clear, which is an answer, not a wait.
   */
  loading?: DsMetricCardLoading;
};

/** skeleton.md "Fix the count": three rows stand in for an unknown number. */
const QUEUE_SKELETON_ROWS = ["queue-skeleton-1", "queue-skeleton-2", "queue-skeleton-3"];

/** Title line over a shorter context line; no icon, badge or chevron is known yet. */
function QueueSkeletonRow() {
  return (
    <div aria-hidden="true" className="fs-cc__queue-row fs-cc__queue-row--skeleton">
      <span className="fs-cc__queue-icon" />
      <span className="fs-cc__queue-body">
        <span className="fs-cc__queue-title"><span className="fs-skeleton fs-skeleton-line" /></span>
        <span className="fs-cc__queue-context"><span className="fs-skeleton fs-skeleton-line" /></span>
      </span>
    </div>
  );
}

const QUEUE_ICONS: Record<AttentionQueueTone, typeof AlertTriangle> = {
  attention: Clock3,
  danger: AlertTriangle,
  neutral: CircleDot,
};

/** The whole row opens the run, so its chevron shows at rest, not on hover. */
function QueueRow({ item }: Readonly<{ item: AttentionQueueItem }>) {
  const Icon = QUEUE_ICONS[item.tone];
  return (
    <Link className="fs-cc__queue-row" data-tone={item.tone} to={`/jobs/${item.id}`}>
      <span aria-hidden="true" className="fs-cc__queue-icon">
        <Icon />
      </span>
      <span className="fs-cc__queue-body">
        <span className="fs-cc__queue-title">{item.title}</span>
        {item.meta ? <span className="fs-cc__queue-context">{item.meta}</span> : null}
      </span>
      {item.status ? <span className="fs-cc__queue-badge">{item.status}</span> : null}
      <ChevronRight aria-hidden="true" className="fs-cc__queue-chevron" />
    </Link>
  );
}

function QueueBody({ queue, loading }: Readonly<{ queue: AttentionQueueItem[]; loading?: DsMetricCardLoading }>) {
  const { t } = useI18n();
  if (loading) {
    return (
      <div aria-busy="true" className="fs-cc__queue-list">
        {loading === "skeleton" ? QUEUE_SKELETON_ROWS.map((key) => <QueueSkeletonRow key={key} />) : null}
      </div>
    );
  }
  if (queue.length) {
    return (
      <div className="fs-cc__queue-list">
        {queue.map((item) => (
          <QueueRow item={item} key={item.id} />
        ))}
      </div>
    );
  }
  return (
    <div className="fs-cc__queue-clear">
      <span aria-hidden="true" className="fs-cc__queue-mark">
        <Check />
      </span>
      <span className="fs-cc__queue-clear-title">{t("react.dashboard.nothing_needs_you", "Nothing needs you")}</span>
      <span className="fs-cc__queue-clear-note">
        {t("react.dashboard.no_blocked_jobs_or_failed_runs_new_items_land_here_as_they_arrive", "No blocked jobs or failed runs. New items land here as they arrive.")} </span>
    </div>
  );
}

export function CommandCenter({ queue, queueTotal, queueHref, loading }: Readonly<CommandCenterProps>) {
  const { t } = useI18n();
  const hidden = loading ? 0 : Math.max(0, queueTotal - queue.length);

  return (
    <section aria-labelledby="fs-cc-title" className="fs-dp fs-cc" data-testid="cmd-center" id="cmd-center">
      <header className="fs-dp__head">
        <h2 className="fs-dp__title" id="fs-cc-title">
          <span>{t("react.dashboard.attention_queue", "Attention queue")}</span>
        </h2>
      </header>
      <p className="fs-dp__note">
        {/* While loading the caption describes the panel, not an answer. */}
        {loading || queue.length ? t("react.dashboard.failed_blocked_or_interrupted_jobs_oldest_first", "Failed, blocked or interrupted jobs, oldest first.") : t("react.dashboard.nothing_is_blocked_or_failing", "Nothing is blocked or failing.")}
      </p>

      <QueueBody loading={loading} queue={queue} />

      <div className="fs-cc__queue-foot">
        <Link className="fs-cc__queue-link" to={queueHref}>
          {hidden
            ? t("react.dashboard.open_queue_count_more", "Open queue · {count} more", { count: String(hidden) })
            : t("react.dashboard.open_queue", "Open queue")}
          <ArrowRight aria-hidden="true" />
        </Link>
      </div>
    </section>
  );
}
