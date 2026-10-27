import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import type { I18nContextValue } from "@/lib/i18n/context";
import { queryKeys } from "@/lib/query/keys";
import { fetchJobDefinition, fetchJobExecutionStepsPage, type JobDefinitionPayload, type JobExecutionSnapshot } from "@/pages/jobs/detail/api";
import { humanizeDiagnosticLabel } from "@/pages/jobs/detail/job-detail-core";
import { mergeExecutionStepsChronologically } from "@/pages/jobs/detail/job-detail-execution";
import { jobStatusGroup } from "@/pages/jobs/status-groups";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

type T = I18nContextValue["t"];
type Entity = Record<string, unknown>;

export { formatRunDuration } from "@/pages/workflows/builder/run-duration";

/** How one pass of a step ended, in the words the canvas and the results use. */
export type RunPassState = "done" | "failed" | "running" | "waiting" | "skipped" | "kept";

export type RunPass = {
  key: string;
  stepId: string;
  stepExecutionId: string;
  label: string;
  state: RunPassState;
  durationMs: number | null;
  /** Where the pass ran: "Workflow", "Fulfilment · item 1 of 2", "Notify customer · lane email". */
  place: string;
  /** Header of the loop item this pass opens, when it is the item's first pass. */
  itemHeader: string;
  /** The step's kind word for kinds that say more than "Workflow" (Approval). */
  kindLabel: string;
  stepKind: string;
  inputs: Entity;
  outputs: Entity;
  /** What the step reported about its pass, when it said something. */
  message: string;
  error: string;
  /** The value a decision step reports (Approved / Rejected, a branch outcome). */
  decision: string;
  groupId: string;
  forEachId: string;
  laneId: string;
  iteration: number | null;
  /** Outputs carry a bounded preview marker: the run record is not the whole value. */
  outputsCut: boolean;
};

export type RunCounts = { done: number; skipped: number; failed: number };

const PROJECTION_MARKER = "_flow_steward_projection";

function record(value: unknown): Entity {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Entity : {};
}
function text(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value).trim() : "";
}

export function runPassState(status: unknown, kept = false): RunPassState {
  const value = text(status).toLowerCase();
  if (["failed", "error", "fail", "interrupted", "max_iterations", "stuck"].includes(value)) return "failed";
  if (["skipped", "cancelled", "canceled", "not_run"].includes(value)) return "skipped";
  if (value.startsWith("awaiting") || ["waiting", "blocked", "external_wait"].includes(value)) return "waiting";
  if (["in_progress", "running", "pending", "queued", "resume_pending", "dispatched"].includes(value)) return "running";
  return kept ? "kept" : "done";
}

/** A run's state in words: Running, Finished, Failed, Stopped, Waiting… */
export function runStatusWord(status: unknown, t: T): string {
  const group = jobStatusGroup(status);
  if (group === "completed") return t("react.workflows.test_run_finished", "Finished");
  if (group === "failed" || group === "stuck") return t("react.workflows.test_run_failed", "Failed");
  if (group === "cancelled") return t("react.workflows.test_run_stopped", "Stopped");
  if (group === "awaiting_approval") return t("react.workflows.test_run_waiting_for_decision", "Waiting for your decision");
  if (group === "waiting" || group === "interrupted") return t("react.workflows.test_run_waiting", "Waiting");
  return t("react.workflows.run_bar_running", "Running");
}

function hasProjectionMarker(value: unknown, depth = 0): boolean {
  if (depth > 6 || !value || typeof value !== "object") return false;
  if (Array.isArray(value)) return value.some((item) => hasProjectionMarker(item, depth + 1));
  const entries = value as Entity;
  if (PROJECTION_MARKER in entries) return true;
  return Object.values(entries).some((item) => hasProjectionMarker(item, depth + 1));
}

export type RunDefinitionNames = {
  steps: Map<string, { label: string; kind: string }>;
  groups: Map<string, string>;
  forEach: Map<string, string>;
};

export function definitionNames(definition: JobDefinitionPayload | undefined, snapshot?: JobExecutionSnapshot | null): RunDefinitionNames {
  const steps = new Map<string, { label: string; kind: string }>();
  for (const step of definition?.steps || []) {
    const id = text(step.step_id);
    if (id) steps.set(id, { label: text(step.display_name) || humanizeDiagnosticLabel(id), kind: text(step.step_kind) });
  }
  for (const step of snapshot?.definitionSteps || []) {
    if (!steps.has(step.stepId)) steps.set(step.stepId, { label: step.label || humanizeDiagnosticLabel(step.stepId), kind: "" });
  }
  const groups = new Map<string, string>();
  for (const group of definition?.groups || []) {
    const id = text(group.group_id);
    if (id) groups.set(id, text(group.display_name) || id);
  }
  const forEach = new Map<string, string>();
  for (const item of definition?.for_each || []) {
    const id = text(item.for_each_id);
    if (id) forEach.set(id, text(item.display_name) || id);
  }
  return { steps, groups, forEach };
}

/** Items a loop selected, from the committed container progress, else from the passes seen. */
function itemTotals(snapshot: JobExecutionSnapshot | null | undefined, rows: Entity[]): Map<string, number> {
  const totals = new Map<string, number>();
  for (const pass of snapshot?.containerProgress?.progress.passes || []) {
    if (pass.container.kind === "for_each" && pass.for_each) {
      totals.set(pass.container.id, Math.max(totals.get(pass.container.id) || 0, pass.for_each.selected_count));
    }
  }
  const committed = new Set(totals.keys());
  for (const row of rows) {
    const id = text(row.for_each_id);
    const iteration = typeof row.iteration === "number" ? row.iteration : null;
    if (id && iteration !== null && !committed.has(id)) totals.set(id, Math.max(totals.get(id) || 0, iteration + 1));
  }
  return totals;
}

export function itemPlace(name: string, iteration: number, total: number, t: T): string {
  return t("react.workflows.test_run_item_place", "{name} · item {item} of {total}", { name, item: String(iteration + 1), total: String(Math.max(total, iteration + 1)) });
}

export function lanePlace(name: string, lane: string, t: T): string {
  return t("react.workflows.test_run_lane_place", "{name} · lane {lane}", { name, lane });
}

/** One row per pass of a step, oldest first, with where it ran. */
export function buildRunPasses(rawRows: Entity[], names: RunDefinitionNames, snapshot: JobExecutionSnapshot | null | undefined, t: T): RunPass[] {
  const rows = mergeExecutionStepsChronologically(rawRows).filter((row) => text(row.workflow_step_id) || text(row.step_id));
  const totals = itemTotals(snapshot, rows);
  const seenItems = new Set<string>();
  return rows.map((row, index) => {
    const stepId = text(row.workflow_step_id) || text(row.step_id);
    const definition = names.steps.get(stepId);
    const forEachId = text(row.for_each_id);
    const groupId = text(row.group_id);
    const laneId = text(row.lane_id);
    const iteration = typeof row.iteration === "number" && Number.isInteger(row.iteration) && row.iteration >= 0 ? row.iteration : null;
    const kept = text(row.skill) === "kept_test_output";
    const state = runPassState(row.status, kept);
    let place = t("react.workflows.test_run_place_workflow", "Workflow");
    let itemHeader = "";
    if (forEachId && iteration !== null) {
      const name = names.forEach.get(forEachId) || forEachId;
      place = itemPlace(name, iteration, totals.get(forEachId) || 0, t);
      const itemKey = `${forEachId}:${iteration}`;
      if (!seenItems.has(itemKey)) {
        seenItems.add(itemKey);
        itemHeader = t("react.workflows.test_run_item_header", "{name}: item {item} of {total}", {
          name, item: String(iteration + 1), total: String(Math.max(totals.get(forEachId) || 0, iteration + 1)),
        });
      }
    } else if (laneId) {
      place = lanePlace(names.groups.get(groupId) || groupId, laneId, t);
    } else if (groupId) {
      place = names.groups.get(groupId) || groupId;
    }
    const kind = definition?.kind || "";
    // The durable rows name them outputs; older recorded rows say output or output_payload.
    const outputs = record(row.outputs ?? row.output ?? row.output_payload);
    const error = text(row.error) || text(record(row.error).message) || (state === "failed" ? text(row.summary) || text(row.reason) : "");
    const decisionRaw = text(record(row.outputs).decision) || text(record(row.artifacts).decision);
    const decision = decisionRaw === "approved" ? t("react.workflows.test_run_approved", "Approved")
      : decisionRaw === "rejected" ? t("react.workflows.test_run_rejected", "Rejected") : "";
    const durationMs = typeof row.duration_ms === "number" && Number.isFinite(row.duration_ms) && row.duration_ms >= 0 ? row.duration_ms : null;
    return {
      key: text(row.step_execution_id) || `${stepId}:${index}`,
      stepId,
      stepExecutionId: text(row.step_execution_id),
      label: definition?.label || text(row.title) || text(row.skill_label) || humanizeDiagnosticLabel(stepId),
      state,
      durationMs,
      place,
      itemHeader,
      kindLabel: kind === "approval" ? t("react.workflows.test_run_kind_approval", "Approval") : "",
      stepKind: kind,
      inputs: record(row.inputs ?? row.input ?? row.input_payload),
      outputs,
      message: text(row.message),
      error,
      decision,
      groupId,
      forEachId,
      laneId,
      iteration,
      outputsCut: hasProjectionMarker(outputs),
    };
  });
}

export function countPasses(passes: readonly RunPass[]): RunCounts {
  return passes.reduce<RunCounts>((counts, pass) => ({
    done: counts.done + (pass.state === "done" || pass.state === "kept" ? 1 : 0),
    skipped: counts.skipped + (pass.state === "skipped" ? 1 : 0),
    failed: counts.failed + (pass.state === "failed" ? 1 : 0),
  }), { done: 0, skipped: 0, failed: 0 });
}

/** The long-value rule: more than 8 lines or 240 characters shows its start. */
export const VALUE_PREVIEW_LINES = 8;
export const VALUE_PREVIEW_CHARS = 240;

/** A value the gate sent as text: JSON reads back as what it was, plain text stays text. */
function gateValue(raw: string): unknown {
  if (!/^\s*[[{"]/.test(raw)) return raw;
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

/**
 * The outputs an Approval shows with its question, from its gate: one shown
 * output is the gate content itself, several are one JSON object keyed by name.
 */
export function approvalShownValues(showOutputs: unknown, content: string): Array<{ name: string; value: unknown }> {
  const names = Array.isArray(showOutputs) ? showOutputs.map(text).filter(Boolean) : [];
  if (names.length === 1) return [{ name: names[0], value: gateValue(content) }];
  if (names.length < 2) return [];
  const parsed = gateValue(content);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return [];
  return names.map((name) => ({ name, value: (parsed as Entity)[name] }));
}

export function valueText(value: unknown): string {
  if (typeof value === "string") return JSON.stringify(value);
  if (value === undefined) return "";
  try {
    return JSON.stringify(stripMarker(value), null, 2) ?? stringFromUnknownValue(value);
  } catch {
    return stringFromUnknownValue(value);
  }
}

function stripMarker(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stripMarker);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Entity).filter(([key]) => key !== PROJECTION_MARKER).map(([key, item]) => [key, stripMarker(item)]));
  }
  return value;
}

export function valuePreview(full: string): { long: boolean; lines: number; preview: string } {
  const lines = full.split("\n");
  const long = lines.length > VALUE_PREVIEW_LINES || full.length > VALUE_PREVIEW_CHARS;
  if (!long) return { long, lines: lines.length, preview: full };
  const head = lines.slice(0, VALUE_PREVIEW_LINES).join("\n");
  return { long, lines: lines.length, preview: head.length > VALUE_PREVIEW_CHARS ? `${head.slice(0, VALUE_PREVIEW_CHARS)}…` : head };
}

/** Values that look like masked secrets stay hidden. */
export function isHiddenValue(value: unknown): boolean {
  const raw = text(value).toLowerCase();
  return raw === "[redacted]" || raw === "***" || raw === "hidden" || raw.startsWith("[secret") || raw === "••••••";
}

/** The kept output a pass would stand in with: its output values under their targets. */
export function keptOutputsFromPass(pass: RunPass, declaredTargets: readonly string[]): Record<string, unknown> {
  const outputs = stripMarker(pass.outputs) as Entity;
  const keys = Object.keys(outputs);
  if (!keys.length) return {};
  if (keys.every((key) => key.startsWith("state."))) return outputs;
  // A built-in step (Transform, Template, Tabular feed…) reports what it wrote as its value and the target it went to.
  const resultTarget = typeof outputs.result_target === "string" ? outputs.result_target : "";
  if (resultTarget.startsWith("state.") && "value" in outputs) return { [resultTarget]: outputs.value };
  if (declaredTargets.length === 1 && !declaredTargets.includes(keys[0])) {
    const target = declaredTargets[0];
    const own = outputs[target.replace(/^state\./, "")];
    return { [target]: keys.length === 1 && own !== undefined ? own : outputs };
  }
  return Object.fromEntries(keys.map((key) => [key.startsWith("state.") ? key : `state.${key}`, outputs[key]]));
}

/** Raw durable step rows and the archived definition of one run (shared caches). */
export function useTestRunPassData(jobId: string, enabled: boolean) {
  const active = enabled && Boolean(jobId);
  const definitionQuery = useQuery({
    queryKey: ["jobs", "detail", jobId, "definition"],
    queryFn: () => fetchJobDefinition(jobId),
    enabled: active,
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
  });
  // Same key, page function and polling as the canvas observer: one cache, one poll.
  const stepsQuery = useInfiniteQuery({
    queryKey: queryKeys.jobExecutionSteps(jobId, ""),
    queryFn: ({ pageParam }) => fetchJobExecutionStepsPage(jobId, pageParam),
    initialPageParam: "",
    getNextPageParam: (page) => page.page?.has_more && page.page.next_cursor ? page.page.next_cursor : undefined,
    enabled: active,
    retry: false,
    refetchInterval: (query) => query.state.data?.pages[0]?.run_terminal ? false : 3_000,
  });
  const pages = stepsQuery.data?.pages || [];
  const rows = pages.filter((page) => page.job_id === jobId).flatMap((page) => page.steps || []);
  return {
    definition: definitionQuery.data?.job_id === jobId ? definitionQuery.data : undefined,
    rows,
    /** The step history was read after the run ended: it holds every step. */
    terminal: pages[0]?.job_id === jobId && pages[0]?.run_terminal === true,
    pending: active && (definitionQuery.isPending || stepsQuery.isPending),
    error: active ? definitionQuery.error || stepsQuery.error : null,
  };
}

/** "15:07:43" in the viewer's zone; `clock: "locale"` keeps the locale's 12/24-hour habit ("03:08:51 PM"). */
export function runClockTime(value: unknown, timezone: string, clock: "24h" | "locale" = "24h"): string {
  const raw = text(value);
  if (!raw) return "";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return "";
  try {
    return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", ...(clock === "24h" ? { hour12: false } : {}), ...(timezone ? { timeZone: timezone } : {}) }).format(date);
  } catch {
    return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", ...(clock === "24h" ? { hour12: false } : {}) }).format(date);
  }
}

/** The output targets a step declares ("state.shipment"), from its contract preview. */
export function declaredStepOutputTargets(preview: unknown): string[] {
  const outputs = record(preview).outputs;
  if (!Array.isArray(outputs)) return [];
  return outputs.flatMap((row) => {
    const entry = record(row);
    const target = text(entry.target) || text(entry.state_target) || text(entry.path) || text(entry.name) || text(entry.key);
    if (!target) return [];
    return [target.startsWith("state.") ? target : `state.${target}`];
  });
}
