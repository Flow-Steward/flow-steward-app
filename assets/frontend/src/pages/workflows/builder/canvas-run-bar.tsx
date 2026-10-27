import { useId } from "react";
import { CheckCircle2, Clock3, Square, X, XCircle } from "lucide-react";

import { DsAlert } from "@/components/shared/design-system-primitives";
import { DsLoader } from "@/components/shared/ds-loader";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { Button } from "@/components/ui/button";
import { IconButton } from "@/components/ui/icon-button";
import { useI18n } from "@/lib/i18n/useI18n";
import type { RunBarTone } from "@/pages/workflows/detail/detail-run-bar-text";
import "./canvas-test-run.css";

export type CanvasRunBarSelection =
  | Readonly<{ kind: "empty" | "loading" | "error" }>
  | Readonly<{
      kind: "verified";
      run: Readonly<{
        jobId: string;
        tone: RunBarTone;
        /** "Test Run running", "Failed at Create shipment", "Approve order is waiting for your decision". */
        title: string;
        /** One line: where, how far, how long — names, never ids. */
        sub: string;
        /** The run's number among the kept test runs ("Run #1"), shown for an earlier run. */
        runNumber?: number | null;
      }>;
    }>;

export type CanvasRunBarHistoryRow = Readonly<{
  jobId: string;
  /** Run #n, oldest kept run = 1. */
  number: number;
  status: string;
  tone: RunBarTone;
  /** "14 s · 15:07:43". */
  meta: string;
  /** Plain words for assistive technology and the old select callers. */
  label: string;
}>;

export type CanvasRunBarHistory =
  | Readonly<{ kind: "loading" | "error" }>
  | Readonly<{
      kind: "ready";
      rows: readonly CanvasRunBarHistoryRow[];
      latestJobId: string | null;
    }>;

type NavigationOutcome = void | Promise<boolean | void>;
export type CanvasRunBarAction = Readonly<{
  onInvoke: () => NavigationOutcome;
  disabled?: boolean;
  busy?: boolean;
  disabledReason?: string;
}>;

export type CanvasRunBarProps = Readonly<{
  selection: CanvasRunBarSelection;
  /** The kept test runs; the Results panel lists them, the bar only uses the latest pointer. */
  history: CanvasRunBarHistory;
  navigationBusy?: boolean;
  viewResultsDisabled?: boolean;
  historyDisabled?: boolean;
  backToLatestDisabled?: boolean;
  stopAction?: CanvasRunBarAction;
  clearMarksAction?: CanvasRunBarAction;
  runAgainAction?: CanvasRunBarAction;
  reviewRequestAction?: CanvasRunBarAction;
  openFailedStepAction?: CanvasRunBarAction;
  /** The author moved the view: bring the running step back to the middle and follow again. */
  followRunAction?: CanvasRunBarAction;
  actionNotice?: Readonly<{ kind: "error" | "status"; message: string }>;
  onViewResults: () => NavigationOutcome;
  onSelectHistory: (jobId: string) => NavigationOutcome;
  onBackToLatest: () => NavigationOutcome;
}>;

/** The owning controller handles outcomes and supplies subsequent state/authority. */
function invokeNavigation(action: () => NavigationOutcome) {
  try {
    void Promise.resolve(action()).catch(() => undefined);
  } catch {
    // Presentation must not turn a parent's refusal/failure into a new run state.
  }
}

function RunBarAction({ action, label, navigationBusy, variant = "secondary" }: Readonly<{
  action: CanvasRunBarAction; label: string; navigationBusy: boolean; variant?: "secondary" | "ghost";
}>) {
  const descriptionId = useId();
  const busyLoader = useLoaderDelay(Boolean(action.busy));
  return <>
    <Button aria-busy={action.busy || undefined} aria-describedby={action.disabledReason ? descriptionId : undefined}
      disabled={navigationBusy || action.disabled || action.busy}
      onClick={() => invokeNavigation(action.onInvoke)} size="sm" title={action.disabledReason || undefined} type="button" variant={variant}>
      {busyLoader ? <DsLoader variant="orbit-inline" /> : null}{label}
    </Button>
    {action.disabledReason ? <span className="sr-only" id={descriptionId}>{action.disabledReason}</span> : null}
  </>;
}

function ToneIcon({ tone }: Readonly<{ tone: RunBarTone }>) {
  const loader = useLoaderDelay(tone === "running");
  if (tone === "running") return loader ? <DsLoader variant="orbit-inline" /> : null;
  if (tone === "success") return <CheckCircle2 aria-hidden="true" />;
  if (tone === "danger") return <XCircle aria-hidden="true" />;
  if (tone === "attention") return <Clock3 aria-hidden="true" />;
  return <Square aria-hidden="true" />;
}

/**
 * The run bar: a floating card centred above the toolbar. Tone icon, title and
 * one sub line; then the actions the run allows. No observation or placement:
 * the caller positions it in the canvas controls column.
 */
export function CanvasRunBar({
  selection,
  history,
  navigationBusy = false,
  viewResultsDisabled = false,
  backToLatestDisabled = false,
  stopAction,
  clearMarksAction,
  runAgainAction,
  reviewRequestAction,
  openFailedStepAction,
  followRunAction,
  actionNotice,
  onViewResults,
  onBackToLatest,
}: CanvasRunBarProps) {
  const { t } = useI18n();
  const id = useId();
  const run = selection.kind === "verified" ? selection.run : null;
  const rows = history.kind === "ready" ? history.rows : [];
  // A latest pointer without its supplied scoped history witness is not an action target.
  const latestJobId = history.kind === "ready" && rows.some((row) => row.jobId === history.latestJobId) ? history.latestJobId : null;
  const historical = Boolean(run && latestJobId && latestJobId !== run.jobId);
  const title = run ? historical && run.runNumber
    ? t("react.workflows.run_bar_earlier_run", "Run #{number} · {title}", { number: String(run.runNumber), title: run.title }) : run.title : "";
  const tone = run?.tone || "neutral";
  const toneAttr = tone === "danger" ? "danger" : tone === "attention" ? "attention" : undefined;

  return (
    <section
      aria-busy={navigationBusy || selection.kind === "loading" || stopAction?.busy}
      aria-labelledby={`${id}-title`}
      aria-describedby={run?.sub ? `${id}-sub` : undefined}
      className="workflow-runbar nodrag nopan nowheel"
      data-job-id={run?.jobId}
      data-testid="workflow-run-bar"
      data-tone={toneAttr}
    >
      {run ? <>
        <span className="workflow-runbar-icon" data-tone={tone}><ToneIcon tone={tone} /></span>
        <span className="workflow-runbar-text">
          <span className="workflow-runbar-title" id={`${id}-title`} title={title}>{title}</span>
          {run.sub ? <span className="workflow-runbar-sub" id={`${id}-sub`} title={run.sub}>{run.sub}</span> : null}
        </span>
        <span className="workflow-runbar-actions">
          {followRunAction ? <RunBarAction action={followRunAction} label={t("react.workflows.run_bar_follow_run", "Follow run")} navigationBusy={navigationBusy} variant="ghost" /> : null}
          {openFailedStepAction ? <RunBarAction action={openFailedStepAction} label={t("react.workflows.run_bar_open_step", "Open step")} navigationBusy={navigationBusy} /> : null}
          {historical ? <Button aria-disabled={navigationBusy || undefined} disabled={backToLatestDisabled} onClick={() => { if (!navigationBusy) invokeNavigation(onBackToLatest); }} size="sm" type="button" variant="secondary">
            {t("react.workflows.run_bar_back_to_latest", "Back to latest")}</Button> : null}
          {/* While its own guarded navigation runs (a confirmation may be open), an opener stays
              focusable: aria-disabled, not disabled, so the confirmation can return focus to it. */}
          <Button aria-disabled={navigationBusy || undefined} disabled={viewResultsDisabled} onClick={() => { if (!navigationBusy) invokeNavigation(onViewResults); }} size="sm" type="button" variant="secondary">
            {t("react.workflows.run_bar_results", "Results")}</Button>
          {reviewRequestAction ? <RunBarAction action={reviewRequestAction} label={t("react.workflows.run_bar_review_request", "Review request")} navigationBusy={navigationBusy} /> : null}
          {stopAction ? <RunBarAction action={stopAction} label={t("react.workflows.run_bar_stop", "Stop")} navigationBusy={navigationBusy} /> : null}
          {runAgainAction && !historical ? <RunBarAction action={runAgainAction} label={t("react.workflows.run_bar_run_again", "Run again")} navigationBusy={navigationBusy} /> : null}
          {clearMarksAction ? <IconButton icon={X} size="small" label={t("react.workflows.run_bar_clear_marks", "Clear marks")}
            disabled={navigationBusy || clearMarksAction.disabled || clearMarksAction.busy} onClick={() => invokeNavigation(clearMarksAction.onInvoke)} /> : null}
        </span>
      </> : <span className="workflow-runbar-text" role="status">
        <span className="workflow-runbar-title" id={`${id}-title`}>{selection.kind === "loading"
          ? t("react.workflows.run_bar_loading", "Loading Test Run…")
          : selection.kind === "error"
            ? t("react.workflows.run_bar_unavailable", "Test Run could not be verified")
            : t("react.workflows.run_bar_empty", "No Test Run selected")}</span>
      </span>}
      {actionNotice ? <div className="workflow-runbar-notice"><DsAlert title={actionNotice.message} tone={actionNotice.kind === "error" ? "danger" : "neutral"} /></div> : null}
    </section>
  );
}
