import { DsLoader } from "@/components/shared/ds-loader";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { useI18n } from "@/lib/i18n/useI18n";
import type { HookExecutionMark } from "@/pages/workflows/detail/detail-hook-execution-marks";
import { formatRunDuration } from "./run-duration";
import { runtimeMarkState, type RuntimeMarkState } from "./runtime-mark-state";
import "./canvas-test-run.css";

/**
 * Recorded execution evidence on the card's top edge, so nothing reflows:
 * the time when done ("0.4 s", "< 0.1 s"), a loader and "Running", "Waiting",
 * "Failed", "Kept"; inside a loop how often the step ran ("2× · 0.6 s").
 * The owning definition is verified before projection.
 */
export function CanvasRuntimeMark({ mark }: Readonly<{ mark?: HookExecutionMark }>) {
  const { t } = useI18n();
  const state = runtimeMarkState(mark);
  const loader = useLoaderDelay(state === "running");
  if (!mark || !state) return null;
  const repeated = Boolean(mark.passes && mark.passes > 1);
  const passes = repeated ? t("react.workflows.run_mark_passes", "{count}×", { count: String(mark.passes) }) : "";
  const time = formatRunDuration(repeated ? mark.totalDurationMs ?? mark.durationMs : mark.durationMs, t);
  const labels: Record<RuntimeMarkState, string> = {
    running: t("react.workflows.run_bar_running", "Running"),
    waiting: t("react.workflows.test_run_waiting", "Waiting"),
    failed: t("react.workflows.test_run_failed", "Failed"),
    skipped: t("react.workflows.test_run_skipped", "Skipped"),
    kept: t("react.workflows.run_mark_kept", "Kept"),
    // The time when it was recorded; "Done" when the run recorded none.
    done: time || t("react.workflows.container_done", "Done"),
  };
  const text = [passes, labels[state]].filter(Boolean).join(" · ");
  if (!text) return null;
  return (
    <span className="workflow-run-mark" data-run={state} data-testid="workflow-runtime-mark" data-job-id={mark.jobId}
      title={`${t("registry.workflow_detail.run_lab.tab.test_run", "Test Run")} · ${text}`}>
      {state === "running" && loader ? <DsLoader variant="orbit-inline" /> : null}
      <span className="workflow-run-mark-text">{text}</span>
    </span>
  );
}
