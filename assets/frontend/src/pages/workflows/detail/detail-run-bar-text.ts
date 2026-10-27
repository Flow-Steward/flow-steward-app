import type { I18nContextValue } from "@/lib/i18n/context";
import type { JobDefinitionPayload, JobExecutionSnapshot } from "@/pages/jobs/detail/api";
import { jobStatusGroup } from "@/pages/jobs/status-groups";
import { formatRunDuration } from "@/pages/workflows/builder/run-duration";
import { definitionNames, itemPlace, lanePlace, runClockTime, runPassState } from "./detail-test-run-results-model";

type T = I18nContextValue["t"];
type Entity = Record<string, unknown>;

export type RunBarTone = "running" | "success" | "danger" | "attention" | "neutral";
export type RunBarText = { tone: RunBarTone; title: string; sub: string };

function record(value: unknown): Entity {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Entity : {};
}
function text(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value).trim() : "";
}

type Step = JobExecutionSnapshot["steps"][number];

/** Latest record of each execution, chronological. */
function executions(snapshot: JobExecutionSnapshot | null | undefined): Step[] {
  const latest = new Map<string, Step>();
  (snapshot?.steps || []).forEach((step, index) => latest.set(step.stepExecutionId || `${step.stepId}:${index}`, step));
  return [...latest.values()];
}

export function stepsDone(snapshot: JobExecutionSnapshot | null | undefined): number {
  return executions(snapshot).filter((step) => {
    const state = runPassState(step.status, step.kept);
    return state === "done" || state === "kept";
  }).length;
}

function itemTotal(snapshot: JobExecutionSnapshot | null | undefined, forEachId: string, iteration: number): number {
  let total = 0;
  for (const pass of snapshot?.containerProgress?.progress.passes || []) {
    if (pass.container.kind === "for_each" && pass.container.id === forEachId && pass.for_each) total = Math.max(total, pass.for_each.selected_count);
  }
  if (!total) {
    for (const step of snapshot?.steps || []) if (step.forEachId === forEachId && typeof step.iteration === "number") total = Math.max(total, step.iteration + 1);
  }
  return Math.max(total, iteration + 1);
}

/** "Fulfilment · item 1 of 2" or "Notify customer · lane email"; empty at the workflow root. */
export function stepPlace(step: Step, snapshot: JobExecutionSnapshot | null | undefined, definition: JobDefinitionPayload | undefined, t: T): string {
  const names = definitionNames(definition, snapshot);
  if (step.forEachId && typeof step.iteration === "number") {
    return itemPlace(names.forEach.get(step.forEachId) || step.forEachId, step.iteration, itemTotal(snapshot, step.forEachId, step.iteration), t);
  }
  if (step.laneId) return lanePlace(names.groups.get(step.groupId || "") || step.groupId || "", step.laneId, t);
  return "";
}

function stepName(stepId: string, snapshot: JobExecutionSnapshot | null | undefined, definition: JobDefinitionPayload | undefined): string {
  return definitionNames(definition, snapshot).steps.get(stepId)?.label || stepId;
}

function approvalMessage(stepId: string, definition: JobDefinitionPayload | undefined): string {
  const step = (definition?.steps || []).find((row) => text(row.step_id) === stepId);
  return text(record(record(record(step).override).approval).message);
}

export type RunBarTextInput = {
  statusCode: string | null;
  isTerminal: boolean;
  snapshot: JobExecutionSnapshot | null | undefined;
  definition: JobDefinitionPayload | undefined;
  detail: Entity | null | undefined;
  durationMs: number | null;
  now: number;
  timezone: string;
  /** The Approval step's Test Run tab is open: the decision is made there. */
  decisionPanelOpen?: boolean;
};

/** What the run bar says: names, never ids (TestRun board). */
export function runBarText(input: RunBarTextInput, t: T): RunBarText {
  const { snapshot, definition, detail } = input;
  const group = jobStatusGroup(input.statusCode);
  const done = stepsDone(snapshot);
  // A step history read before the run ended undercounts it: say nothing until it catches up.
  const stale = input.isTerminal && !snapshot?.terminal;
  const doneText = stale ? "" : done === 1 ? t("react.workflows.run_bar_one_step_done", "1 step done")
    : t("react.workflows.run_bar_steps_done", "{count} steps done", { count: String(done) });
  const started = Date.parse(text(record(detail).started_at) || text(record(record(detail).job).started_at));
  const elapsedMs = input.durationMs ?? (Number.isFinite(started) ? Math.max(0, input.now - started) : null);
  const elapsed = formatRunDuration(elapsedMs, t);
  const all = executions(snapshot);
  if (group === "completed") {
    return { tone: "success", title: elapsed ? t("react.workflows.run_bar_finished_in", "Test Run finished in {duration}", { duration: elapsed }) : t("react.workflows.run_bar_finished", "Test Run finished"), sub: doneText };
  }
  if (group === "cancelled") {
    return { tone: "neutral", title: t("react.workflows.run_bar_stopped", "Test Run stopped"), sub: [doneText, elapsed].filter(Boolean).join(" · ") };
  }
  if (group === "failed" || group === "stuck" || (input.isTerminal && group !== null)) {
    const errorStepId = text(snapshot?.runError?.stepId);
    const failed = all.filter((step) => runPassState(step.status) === "failed");
    const step = (snapshot?.runError?.stepExecutionId && all.find((row) => row.stepExecutionId === snapshot.runError?.stepExecutionId))
      || (errorStepId && [...all].reverse().find((row) => row.stepId === errorStepId)) || failed.at(-1);
    const reason = text(snapshot?.runError?.message) || text(record(record(detail).failure_summary).summary);
    if (!step) return { tone: "danger", title: t("react.workflows.run_bar_failed", "Test Run failed"), sub: reason };
    const place = stepPlace(step, snapshot, definition, t);
    return {
      tone: "danger",
      title: t("react.workflows.run_bar_failed_at", "Failed at {step}", { step: stepName(step.stepId, snapshot, definition) }),
      sub: [place, reason].filter(Boolean).join(" · "),
    };
  }
  const waitingSteps = all.filter((step) => runPassState(step.status) === "waiting");
  if (group === "awaiting_approval") {
    const step = waitingSteps.at(-1);
    const name = step ? stepName(step.stepId, snapshot, definition) : "";
    const message = input.decisionPanelOpen ? t("react.workflows.run_bar_decide_in_panel", "Decide in the panel") : step ? approvalMessage(step.stepId, definition) : "";
    return {
      tone: "attention",
      title: name ? t("react.workflows.run_bar_waiting_for_decision", "{step} is waiting for your decision", { step: name })
        : t("react.workflows.run_bar_waiting_for_your_decision", "The run is waiting for your decision"),
      sub: [message, doneText].filter(Boolean).join(" · "),
    };
  }
  if (group === "waiting") {
    const due = text(record(record(record(detail).runtime).workflow_wait).due_at);
    const step = waitingSteps.at(-1);
    const time = due ? runClockTime(due, input.timezone, "locale") : "";
    return {
      tone: "attention",
      title: time ? t("react.workflows.run_bar_waiting_until", "Waiting until {time}", { time }) : t("react.workflows.test_run_waiting", "Waiting"),
      sub: [step ? stepName(step.stepId, snapshot, definition) : "", doneText, elapsed].filter(Boolean).join(" · "),
    };
  }
  const running = all.filter((step) => runPassState(step.status) === "running");
  const names = running.map((step) => {
    const place = stepPlace(step, snapshot, definition, t);
    const name = stepName(step.stepId, snapshot, definition);
    return place ? `${name} (${place})` : name;
  });
  return {
    tone: "running",
    title: t("react.workflows.run_bar_test_run_running", "Test Run running"),
    sub: [names.join(" + "), doneText, elapsed].filter(Boolean).join(" · "),
  };
}
