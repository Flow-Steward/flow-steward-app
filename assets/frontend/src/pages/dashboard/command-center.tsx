import { useI18n } from "@/lib/i18n/useI18n";
import { AlertTriangle, ArrowRight, Check, CircleDot, Clock3 } from "lucide-react";
import { Link } from "react-router";

import "@/pages/dashboard/command-center.css";

export type AttentionQueueTone = "danger" | "attention" | "neutral";

export type AttentionQueueItem = {
  id: string;
  title: string;
  /** Project name and age, already formatted; either half may be empty. */
  project: string;
  age: string;
  status: string;
  tone: AttentionQueueTone;
};

export type CommandCenterProps = {
  queue: AttentionQueueItem[];
  queueTotal: number;
  /** The job list filtered to the statuses the queue is built from. */
  queueHref: string;
};

const QUEUE_ICONS: Record<AttentionQueueTone, typeof AlertTriangle> = {
  attention: Clock3,
  danger: AlertTriangle,
  neutral: CircleDot,
};

function QueueRow({ item }: Readonly<{ item: AttentionQueueItem }>) {
  const Icon = QUEUE_ICONS[item.tone];
  const meta = [item.project, item.age].filter(Boolean).join(" · ");
  return (
    <Link className="fs-cc__queue-row" data-tone={item.tone} to={`/jobs/${item.id}`}>
      <span aria-hidden="true" className="fs-cc__queue-icon">
        <Icon />
      </span>
      <span className="fs-cc__queue-body">
        <span className="fs-cc__queue-title">{item.title}</span>
        <span className="fs-cc__queue-meta">
          {meta ? <span className="fs-cc__queue-context">{meta}</span> : null}
          {item.status ? <span className="fs-cc__queue-badge">{item.status}</span> : null}
        </span>
      </span>
    </Link>
  );
}

export function CommandCenter({ queue, queueTotal, queueHref }: Readonly<CommandCenterProps>) {
  const { t } = useI18n();
  const hidden = Math.max(0, queueTotal - queue.length);

  return (
    <section className="fs-cc" data-testid="cmd-center" id="cmd-center">
      <aside aria-label={t("react.dashboard.attention_queue", "Attention queue")} className="fs-cc__queue">
        <h3 className="fs-cc__queue-title-head">{t("react.dashboard.attention_queue", "Attention queue")}</h3>
        <p className="fs-cc__queue-caption">
          {queue.length ? t("react.dashboard.failed_blocked_or_interrupted_jobs_oldest_first", "Failed, blocked or interrupted jobs, oldest first.") : t("react.dashboard.nothing_is_blocked_or_failing", "Nothing is blocked or failing.")}
        </p>

        {queue.length ? (
          <div className="fs-cc__queue-list">
            {queue.map((item) => (
              <QueueRow item={item} key={item.id} />
            ))}
            {hidden ? <p className="fs-cc__queue-more">{hidden} {t("react.dashboard.more_in_the_queue", "more in the queue")}</p> : null}
          </div>
        ) : (
          <div className="fs-cc__queue-clear">
            <span aria-hidden="true" className="fs-cc__queue-mark">
              <Check />
            </span>
            <span className="fs-cc__queue-clear-title">{t("react.dashboard.nothing_needs_you", "Nothing needs you")}</span>
            <span className="fs-cc__queue-clear-note">
              {t("react.dashboard.no_blocked_jobs_or_failed_runs_new_items_land_here_as_they_arrive", "No blocked jobs or failed runs. New items land here as they arrive.")} </span>
          </div>
        )}

        <div className="fs-cc__queue-foot">
          <Link className="fs-cc__queue-link" to={queueHref}>
            {t("react.dashboard.open_queue", "Open queue")} <ArrowRight aria-hidden="true" />
          </Link>
        </div>
      </aside>
    </section>
  );
}
