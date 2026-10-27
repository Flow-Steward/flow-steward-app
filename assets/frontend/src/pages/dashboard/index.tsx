import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useState } from "react";

import { useQuery } from "@tanstack/react-query";
import { ArrowRight } from "lucide-react";
import { Link } from "react-router";

import {
  CommandCenter,
  type AttentionQueueItem,
  type AttentionQueueTone,
} from "@/pages/dashboard/command-center";
import "@/pages/dashboard/dashboard-page.css";
import {
  DashboardLoadError,
  ModelSpend,
  QuickLinks,
} from "@/pages/dashboard/dashboard-panels";
import { spendRows } from "@/pages/dashboard/cost-display";
import { formatAge } from "@/pages/dashboard/format";
import {
  ATTENTION_QUEUE_GROUPS,
  FAILED_OR_STUCK_GROUPS,
  failedOrStuckRows,
  jobsListHref,
} from "@/pages/dashboard/job-links";
import { jobStatusGroup } from "@/pages/jobs/status-groups";
import { OperationalOverview } from "@/pages/dashboard/operational-overview";
import { PageContainer } from "@/components/shared/design-system";
import { fetchDashboardSummary } from "@/pages/dashboard/api";
import { queryKeys } from "@/lib/query/keys";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { useHasCapability } from "@/lib/auth/capabilities";

type DashboardHeroKpi = {
  value?: number | string;
};

type DashboardGroupMap = Record<string, Array<Record<string, unknown>>>;




function getRangeLabels(t: I18nContextValue["t"] = fallbackContext.t): Record<string, string> {
  return {
  "7d": t("react.dashboard.last_7_days", "Last 7 days"),
  "24h": t("react.dashboard.last_24_hours", "Last 24 hours"),
  "30d": t("react.dashboard.last_30_days", "Last 30 days"),
};
}

/** Five rows fill the rail without turning it into a second job list. */
const QUEUE_LIMIT = 5;

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function asHeroKpis(value: unknown): Record<string, DashboardHeroKpi> {
  const result: Record<string, DashboardHeroKpi> = {};
  for (const [key, item] of Object.entries(asRecord(value))) {
    const record = asRecord(item);
    const metricValue = record.value;
    result[key] = {
      value: typeof metricValue === "number" || typeof metricValue === "string" ? metricValue : undefined,
    };
  }
  return result;
}

function asGroupMap(value: unknown): DashboardGroupMap {
  const result: DashboardGroupMap = {};
  for (const [key, item] of Object.entries(asRecord(value))) {
    result[key] = Array.isArray(item) ? item.map(asRecord) : [];
  }
  return result;
}

/**
 * Age of the oldest row in a group, measured against the snapshot's own
 * generated_at rather than the wall clock, so the number matches the data.
 */
function oldestAgeMs(
  rows: Array<Record<string, unknown>>,
  fields: string[],
  referenceIso: string,
): number | null {
  const reference = Date.parse(referenceIso);
  if (Number.isNaN(reference)) {
    return null;
  }
  let oldest: number | null = null;
  for (const row of rows) {
    const raw = firstStringFromUnknownValue(fields.map((field) => row[field]).concat(""));
    const stamp = Date.parse(raw);
    if (Number.isNaN(stamp)) {
      continue;
    }
    if (oldest === null || stamp < oldest) {
      oldest = stamp;
    }
  }
  return oldest === null ? null : Math.max(0, reference - oldest);
}

function jobsWithStatus(groups: DashboardGroupMap, status: string): Array<Record<string, unknown>> {
  return Object.values(groups)
    .flat()
    .filter((row) => firstStringFromUnknownValue([row.status, ""]).trim() === status);
}

/** Red when the run broke (Failed, Stuck); orange when a human has to act. */
function attentionTone(status: string): AttentionQueueTone {
  const group = jobStatusGroup(status);
  if (group && FAILED_OR_STUCK_GROUPS.includes(group)) {
    return "danger";
  }
  return group === "awaiting_approval" || group === "interrupted" ? "attention" : "neutral";
}

function attentionQueue(
  rows: Array<Record<string, unknown>>,
  generatedAt: string, t: I18nContextValue["t"] = fallbackContext.t,
): AttentionQueueItem[] {
  return rows.map((row, index) => {
    const id = firstStringFromUnknownValue([row.id, row.job_id, index]);
    const status = firstStringFromUnknownValue([row.status, ""]);
    return {
      age: formatAge(oldestAgeMs([row], ["finished_at", "started_at", "queued_at"], generatedAt), t),
      id,
      project: firstStringFromUnknownValue([row.project_name, ""]),
      status: firstStringFromUnknownValue([row.status_label, row.status, ""]),
      title: firstStringFromUnknownValue([row.task_summary, row.display_summary, row.title, t("react.dashboard.job_value1", "Job {value1}", { value1: String(id) })]),
      tone: attentionTone(status),
    };
  });
}

export function DashboardPage() {
  const { t } = useI18n();
  const canAdministerPlatform = useHasCapability("administer_platform");
  const canReadRegistry = useHasCapability("read_registry");
  const [range, setRange] = useState("24h");
  const summaryQuery = useQuery({
    queryKey: queryKeys.dashboardSummary(range),
    queryFn: () => fetchDashboardSummary(range),
    refetchInterval: 15_000,
    // Changing the range changes the query key, so without this the cache miss
    // renders `undefined` for a beat: every counter drops to zero and the
    // attention card loses its colour until the new payload lands.
    placeholderData: (previous) => previous,
  });

  const summary = summaryQuery.data || {};
  const hero = asHeroKpis(summary.hero_kpis);
  const groups = asGroupMap(summary.groups);
  const trends = asRecord(summary.trends);
  const spendBreakdown = asRecord(trends.spend_breakdown);
  const needsAttention = Number(hero.needs_attention?.value ?? groups.needs_attention?.length ?? 0);
  const inProgress = Number(hero.in_progress?.value ?? groups.running?.length ?? 0);
  const awaitingApproval = Number(
    hero.awaiting_approval?.value ?? groups.awaiting_approval?.length ?? 0,
  );
  // Counted over every stored status of the Waiting group by the server.
  const waiting = Number(hero.waiting?.value ?? 0);
  // The red card counts the queued runs in the Failed and Stuck groups; the
  // server already leaves acknowledged runs out of needs_attention.
  const failedOrStuck = failedOrStuckRows(groups.needs_attention || []).length;
  const completed = Number(hero.done?.value ?? hero.completed?.value ?? groups.done?.length ?? 0);
  // The overview shows only fields the summary endpoint actually returns; the
  // remaining KPI shapes are read raw because asHeroKpis narrows to `value`.
  const heroRaw = asRecord(summary.hero_kpis);
  const queueWorking = asRecord(heroRaw.queue_working);
  const successRateKpi = asRecord(heroRaw.success_rate);
  const spendKpi = asRecord(heroRaw.spend);
  const queued = Number.isFinite(Number(queueWorking.queue)) ? Number(queueWorking.queue) : null;
  const successRateDisplay = firstStringFromUnknownValue([successRateKpi.display, ""]).trim();
  const successRateDeltaPp = Number.isFinite(Number(successRateKpi.delta_pp))
    ? Number(successRateKpi.delta_pp)
    : null;
  const spendDisplay = firstStringFromUnknownValue([spendKpi.display, ""]).trim();
  const generatedAt = firstStringFromUnknownValue([summary.generated_at, ""]).trim();
  // The triage query selects queued_at/started_at/finished_at, so the age of the
  // oldest item in a group is derivable here without a new endpoint.
  const longestRunningMs = oldestAgeMs(groups.running || [], ["started_at", "queued_at"], generatedAt);
  const oldestFailingMs = oldestAgeMs(
    failedOrStuckRows(groups.needs_attention || []),
    ["finished_at", "started_at", "queued_at"],
    generatedAt,
  );
  const oldestWaitingMs = oldestAgeMs(
    jobsWithStatus(groups, "awaiting_approval"),
    ["started_at", "queued_at"],
    generatedAt,
  );

  const queue = attentionQueue(groups.needs_attention || [], generatedAt, t);
  const visibleQueue = queue.slice(0, QUEUE_LIMIT);
  // The button and the queue's own link open the groups the queue is built
  // from, without acknowledged runs, which the queue leaves out as well.
  const attentionHref = jobsListHref(ATTENTION_QUEUE_GROUPS, { hideAcknowledged: true });

  return (
    <PageContainer width="wide">
    <div className="fs-dash">
      <header className="fs-dash__header">
        <div className="fs-dash__ident">
          <p className="fs-dash__eyebrow">{t("react.dashboard.command_center", "Command center")}</p>
          <h1 className="fs-dash__title">{t("react.dashboard.start_from_the_highest_priority_work", "Start from the highest-priority work")}</h1>
          <p className="fs-dash__description">
            {t("react.dashboard.a_quieter_command_surface_for_projects_jobs_approvals_and_ai_assisted_stewardship", "A quieter command surface for projects, jobs, approvals, and AI-assisted stewardship.")} </p>
        </div>

        <div className="fs-dash__actions">
          <Link className="fs-dash__action" to="/projects">
            {t("react.dashboard.browse_projects", "Browse projects")} </Link>
          <Link className="fs-dash__action" to="/jobs">
            {t("react.dashboard.review_jobs", "Review jobs")} </Link>
          {/* Accent is reserved for work that genuinely needs a human, so this
              call to action exists only while there is something to resolve.
              At zero it would lead into an empty queue. */}
          {queue.length ? (
            <>
              <span aria-hidden="true" className="fs-dash__action-divider" />
              <Link className="fs-dash__action fs-dash__action--accent" to={attentionHref}>
                {t("react.dashboard.resolve_attention", "Resolve attention")} <ArrowRight aria-hidden="true" />
              </Link>
            </>
          ) : null}
        </div>
      </header>

      <CommandCenter queue={visibleQueue} queueHref={attentionHref} queueTotal={queue.length} />

      <OperationalOverview
        awaitingApproval={awaitingApproval}
        waiting={waiting}
        completed={completed}
        failedOrStuck={failedOrStuck}
        generatedAt={generatedAt}
        inProgress={inProgress}
        longestRunningMs={longestRunningMs}
        needsAttention={needsAttention}
        oldestFailingMs={oldestFailingMs}
        oldestWaitingMs={oldestWaitingMs}
        onRangeChange={setRange}
        queued={queued}
        range={range}
        successRate={successRateDisplay}
        successRateDeltaPp={successRateDeltaPp}
      />

      {summaryQuery.isError ? (
        <DashboardLoadError
          message={summaryQuery.error instanceof Error ? summaryQuery.error.message : t("react.dashboard.unknown_error", "Unknown error")}
        />
      ) : null}

      {/* The attention queue lives in the Command center rail and nowhere else.
          The mockup's bottom-left panel was an activity feed, which has no
          backing data; filling it with the same queue would print the same jobs
          twice on one page. */}
      <div className="fs-dash__row">
        <ModelSpend
          breakdown={{
            agent: spendRows(spendBreakdown.agent),
            project: spendRows(spendBreakdown.project),
            workflow: spendRows(spendBreakdown.workflow),
          }}
          rangeLabel={getRangeLabels(t)[range] || ""}
          total={spendDisplay}
        />
        <QuickLinks
          canAdministerPlatform={canAdministerPlatform}
          canReadRegistry={canReadRegistry}
        />
      </div>
    </div>
    </PageContainer>
  );
}
