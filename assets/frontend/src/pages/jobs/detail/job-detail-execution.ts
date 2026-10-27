import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import type { JobIncidentDrilldown } from "@/pages/jobs/detail/api";
import {
  firstReadableText,
  humanizeDiagnosticLabel,
  listFromUnknown,
  normalizedStepSemanticPart,
  type Entity,
} from "@/pages/jobs/detail/job-detail-core";
import { detailExecutionStepRows } from "@/pages/jobs/detail/job-detail-view-model";
import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";

export function buildExecutionSteps({
  detailEntity,
  incident,
  historyRows = [],
  streamRows = [],
}: {
  detailEntity: Entity;
  incident: JobIncidentDrilldown | undefined;
  historyRows?: Entity[];
  streamRows?: Entity[];
}): Entity[] {
  const detailSteps = detailExecutionStepRows(detailEntity);
  if (detailSteps.length) {
    return mergeExecutionStepsChronologically([
      ...historyRows,
      ...detailSteps,
      ...streamRows,
    ]);
  }

  const attemptRows = listFromUnknown(incident?.related_attempts);
  if (attemptRows.length) {
    const attemptSteps = [...attemptRows]
      .sort((left, right) => Number(left.attempt_no || 0) - Number(right.attempt_no || 0))
      .map((row, index) => {
        const label = firstStringFromUnknownValue([row.agent_display_name, row.agent, row.handler, row.target_type, row.step_id, ""]).trim();
        return {
          step_id: row.attempt_id || row.id || row.dispatch_id || index + 1,
          title: label || `Step ${index + 1}`,
          group_id: row.group_id || "",
          status: row.status || row.result || "recorded",
          summary:
            firstStringFromUnknownValue([row.error_text, row.reason_detail, row.message, row.status, ""]).trim() ||
            "Delivery attempt recorded.",
          agent_id: row.agent_id || row.agent || row.handler || "",
          timestamp: row.created_at || row.started_at || row.completed_at || "",
        };
      });
    return mergeExecutionStepsChronologically([
      ...historyRows,
      ...attemptSteps,
      ...streamRows,
    ]);
  }

  return mergeExecutionStepsChronologically([...historyRows, ...streamRows]);
}

function executionStepTimestamp(row: Entity): number {
  const timestamp = firstReadableText(
    row.started_at,
    row.timestamp,
    row.finished_at,
  );
  const parsed = Date.parse(timestamp);
  return Number.isFinite(parsed) ? parsed : 0;
}

export function mergeExecutionStepsChronologically(rows: Entity[]): Entity[] {
  return mergeExecutionSteps(rows, Number.MAX_SAFE_INTEGER)
    .map((row, index) => ({ row, index }))
    .sort(
      (left, right) =>
        executionStepTimestamp(left.row) - executionStepTimestamp(right.row) ||
        left.index - right.index,
    )
    .map(({ row }) => row);
}

export function executionStepKey(row: Entity, index: number, prefix = "step"): string {
  const executionId = firstReadableText(row.step_execution_id);
  if (executionId) {
    return `execution:${executionId}`;
  }
  return (
    firstReadableText(
      row.step_id,
      row.id,
      row.correlation_id,
      row.message_id,
      row.event_id,
      row.entry_id,
      row.attempt_id,
    ) || `${prefix}-${index}`
  );
}

export function executionStepTitleActor(row: Entity): string {
  const title = firstReadableText(row.title, row.display_name, row.step_name, row.step_title);
  if (!title) {
    return "";
  }
  const prefix = title.includes(":") ? title.split(":")[0] : title;
  const normalized = normalizedStepSemanticPart(prefix);
  return normalized.includes(" ") ? "" : normalized;
}

export function uniqueSemanticKeys(keys: string[]): string[] {
  return Array.from(new Set(keys.filter(isTruthy)));
}

function semanticKey(parts: Array<[string, string]>): string {
  if (parts.some(([, value]) => !value)) {
    return "";
  }
  return parts.map(([label, value]) => `${label}:${value}`).join("|");
}

export function executionStepSemanticKeys(row: Entity): { strict: string[]; loose: string[] } {
  const group = normalizedStepSemanticPart(row.group_id);
  const explicitActor = normalizedStepSemanticPart(
    row.agent_id,
    row.from_agent,
    row.producer,
    row.agent,
    row.agent_label,
    row.agent_display_name,
    row.producer_display_name,
  );
  const actor = explicitActor || executionStepTitleActor(row);
  const skill = normalizedStepSemanticPart(row.skill, row.skill_id, row.tool, row.tool_name);
  const title = normalizedStepSemanticPart(row.title, row.display_name, row.step_name, row.step_title);
  const summary = normalizedStepSemanticPart(row.summary, row.result, row.message, row.content_preview);
  return {
    strict: uniqueSemanticKeys([
      semanticKey([["group", group], ["actor", actor], ["skill", skill]]),
      semanticKey([["group", group], ["title", title]]),
    ]),
    loose: uniqueSemanticKeys([
      semanticKey([["actor", actor], ["skill", skill]]),
      semanticKey([["actor", actor], ["summary", summary]]),
      semanticKey([["title", title], ["summary", summary]]),
      semanticKey([["group", group], ["actor", actor]]),
      semanticKey([["actor", actor], ["title", title]]),
      semanticKey([["group", group], ["summary", summary]]),
      actor ? `actor:${actor}` : "",
    ]),
  };
}

export function isStreamExecutionStep(row: Entity): boolean {
  return Boolean(row.source_type);
}

export function mergeExecutionStepRows(primary: Entity, supplemental: Entity, key: string): Entity {
  const executionId = firstReadableText(primary.step_execution_id);
  const supplementalStatus = firstReadableText(supplemental.status);
  const liveStatusUpdate = executionId === firstReadableText(supplemental.step_execution_id) &&
    Boolean(executionId) && !isStreamExecutionStep(primary) && isStreamExecutionStep(supplemental) &&
    isRunningStep(primary) && supplementalStatus && supplementalStatus.toLowerCase() !== "recorded";
  const genericStreamStatus = isStreamExecutionStep(primary) &&
    firstReadableText(primary.status).toLowerCase() === "recorded";
  const statusSource = liveStatusUpdate || genericStreamStatus ? supplemental : primary;
  return {
    ...supplemental,
    ...primary,
    // Keep canonical provenance when a stream only supplements its fields.
    source_type: primary.source_type,
    step_id: firstReadableText(primary.step_id, supplemental.step_id, key),
    title: firstReadableText(primary.title, supplemental.title),
    group_id: firstReadableText(primary.group_id, supplemental.group_id),
    agent_id: firstReadableText(primary.agent_id, supplemental.agent_id, supplemental.from_agent),
    agent_label: firstReadableText(primary.agent_label, primary.agent_display_name, supplemental.agent_label, supplemental.agent_display_name),
    skill: firstReadableText(primary.skill, supplemental.skill),
    tool: firstReadableText(primary.tool, supplemental.tool),
    summary: firstReadableText(primary.summary, supplemental.summary),
    result: firstReadableText(primary.result, supplemental.result),
    status: firstReadableText(statusSource.status, primary.status, supplemental.status),
    status_label: firstReadableText(statusSource.status_label, primary.status_label, supplemental.status_label),
    timestamp: firstReadableText(primary.timestamp, supplemental.timestamp),
  };
}

export function mergeExecutionSteps(rows: Entity[], limit = 80): Entity[] {
  const deduped = new Map<string, Entity>();
  const strictSemanticKeys = new Map<string, string>();
  const looseSemanticKeys = new Map<string, string>();
  const registerKeys = (row: Entity, key: string) => {
    // Semantic matching is a legacy fallback, not proof of execution identity.
    if (firstReadableText(row.step_execution_id)) {
      return;
    }
    const keys = executionStepSemanticKeys(row);
    keys.strict.forEach((semanticKey) => {
      strictSemanticKeys.set(semanticKey, key);
    });
    keys.loose.forEach((semanticKey) => {
      looseSemanticKeys.set(semanticKey, key);
    });
  };
  const matchingSemanticKey = (row: Entity): string => {
    if (firstReadableText(row.step_execution_id)) {
      return "";
    }
    const keys = executionStepSemanticKeys(row);
    for (const semanticKey of keys.strict) {
      const existingKey = strictSemanticKeys.get(semanticKey);
      const existing = existingKey ? deduped.get(existingKey) : null;
      if (existingKey && existing && isStreamExecutionStep(existing) !== isStreamExecutionStep(row)) {
        return existingKey;
      }
    }
    for (const semanticKey of keys.loose) {
      const existingKey = looseSemanticKeys.get(semanticKey);
      const existing = existingKey ? deduped.get(existingKey) : null;
      if (existing && isStreamExecutionStep(existing) !== isStreamExecutionStep(row)) {
        return existingKey || "";
      }
    }
    return "";
  };
  rows.forEach((row, index) => {
    const key = executionStepKey(row, index);
    const existingKey = deduped.has(key) ? key : matchingSemanticKey(row);
    if (existingKey) {
      const existing = deduped.get(existingKey) || {};
      const canonical = isStreamExecutionStep(row) && !isStreamExecutionStep(existing) ? existing : row;
      const supplemental = canonical === row ? existing : row;
      const merged = mergeExecutionStepRows(canonical, supplemental, existingKey);
      deduped.set(existingKey, merged);
      registerKeys(merged, existingKey);
      return;
    }
    const existing = deduped.get(key) || {};
    const merged = mergeExecutionStepRows(row, existing, key);
    deduped.set(key, merged);
    registerKeys(merged, key);
  });
  return Array.from(deduped.values()).slice(-limit);
}

export function isSemanticStreamStep(frameType: string, frameRecord: Entity): boolean {
  const normalizedType = firstStringFromUnknownValue([frameType, ""]).trim().toLowerCase();
  if (normalizedType !== "message") {
    return false;
  }
  const explicitStep = firstReadableText(
    frameRecord.step_id,
    frameRecord.step_name,
    frameRecord.step_title,
    frameRecord.execution_step_id,
    frameRecord.workflow_step_id,
  );
  const summary = firstReadableText(
    frameRecord.summary,
    frameRecord.result,
    frameRecord.message,
    frameRecord.content_preview,
    frameRecord.content,
    frameRecord.detail,
    frameRecord.payload_summary,
  );
  const genericSummary = /^(?:message|event|log|runtime message|event captured|log captured|recorded|done)$/i.test(
    summary.trim(),
  );
  const kind = firstReadableText(
    frameRecord.message_type,
    frameRecord.event_type,
    frameRecord.kind,
    frameRecord.type,
  ).toLowerCase();
  if (!explicitStep && /(?:^|[._\s-])(?:log|event|routing|heartbeat|telemetry)(?:$|[._\s-])/i.test(kind)) {
    return false;
  }
  if (explicitStep) {
    return Boolean(
      summary ||
        firstReadableText(
          frameRecord.title,
          frameRecord.agent_display_name,
          frameRecord.producer_display_name,
          frameRecord.from_agent,
          frameRecord.skill,
        ),
    );
  }
  if (!summary || genericSummary) {
    return false;
  }
  return Boolean(
    firstReadableText(
      frameRecord.title,
      frameRecord.producer_display_name,
      frameRecord.agent_display_name,
      frameRecord.agent_label,
      frameRecord.from_agent,
      frameRecord.skill,
      frameRecord.to_agent,
      frameRecord.agent,
      frameRecord.agent_id,
    ),
  );
}

export function streamFrameToExecutionStep(frameType: string, frameRecord: Entity, index: number): Entity | null {
  const normalizedType = firstStringFromUnknownValue([frameType, ""]).trim().toLowerCase();
  if (!isSemanticStreamStep(normalizedType, frameRecord)) {
    return null;
  }

  const key = executionStepKey(frameRecord, index, normalizedType);
  const title = firstReadableText(
    frameRecord.title,
    frameRecord.producer_display_name,
    frameRecord.agent_display_name,
    frameRecord.agent_label,
    frameRecord.from_agent,
    frameRecord.skill,
    frameRecord.producer,
    frameRecord.agent,
    frameRecord.agent_id,
    frameRecord.step_id,
    frameRecord.event_type,
    frameRecord.type,
    humanizeDiagnosticLabel(normalizedType),
  );
  const status = firstReadableText(
    frameRecord.status_label,
    frameRecord.status,
    frameRecord.level,
    frameRecord.severity,
    frameRecord.message_type,
    frameRecord.event_type,
    "recorded",
  );
  const summary = firstReadableText(
    frameRecord.summary,
    frameRecord.result,
    frameRecord.message,
    frameRecord.content_preview,
    frameRecord.content,
    frameRecord.detail,
    frameRecord.payload_summary,
  );

  return {
    ...frameRecord,
    id: key,
    step_id: firstReadableText(frameRecord.step_id, key),
    title,
    status,
    status_label: humanizeDiagnosticLabel(status),
    summary: summary || `${humanizeDiagnosticLabel(normalizedType)} captured.`,
    agent_id: firstReadableText(frameRecord.agent_id, frameRecord.from_agent, frameRecord.producer, frameRecord.agent),
    timestamp: firstReadableText(frameRecord.timestamp, frameRecord.created_at, frameRecord.occurred_at),
    source_type: normalizedType,
  };
}

function streamEventRows(streamEvents: readonly unknown[]): Entity[] {
  return streamEvents
    .map((frame) => (frame && typeof frame === "object" ? (frame as Entity) : {}))
    .filter((frame) => firstStringFromUnknownValue([frame.type, ""]).toLowerCase() === "event")
    .map((frame) => (frame.data && typeof frame.data === "object" ? (frame.data as Entity) : {}));
}

function eventStepId(event: Entity): string {
  const detail = event.detail && typeof event.detail === "object" ? (event.detail as Entity) : {};
  return firstStringFromUnknownValue([detail.step_id, ""]).trim();
}

/**
 * Give each running step the newest signal that it is alive: its own latest
 * runtime event from the live stream, or else the job heartbeat. Stream frames
 * arrive newest first.
 */
export function withLiveStepActivity(
  steps: Entity[],
  {
    isTerminal,
    liveFiles = [],
    runtime,
    streamEvents,
  }: {
    isTerminal: boolean;
    liveFiles?: readonly Entity[];
    runtime: Entity;
    streamEvents: readonly unknown[];
  },
): Entity[] {
  if (isTerminal) {
    return steps;
  }
  const events = streamEventRows(streamEvents);
  const runningCount = steps.filter(
    (step) => firstStringFromUnknownValue([step.status, ""]).toLowerCase() === "in_progress",
  ).length;
  return steps.map((step) => {
    if (firstStringFromUnknownValue([step.status, ""]).toLowerCase() !== "in_progress") {
      return step;
    }
    const stepId = firstStringFromUnknownValue([step.scope_id, step.step_id, ""]).trim();
    const files = stepId ? liveFilesForStep(liveFiles, stepId) : [];
    if (files.length) {
      step = { ...step, live_files: files };
    }
    const latest = stepId
      ? events.find(
          (event) => eventStepId(event) === stepId && firstStringFromUnknownValue([event.event_type, ""]) !== "artifact_ready",
        )
      : undefined;
    if (latest) {
      const detail = latest.detail && typeof latest.detail === "object" ? (latest.detail as Entity) : {};
      const isProgress = firstStringFromUnknownValue([latest.event_type, ""]) === "step_progress";
      return {
        ...step,
        live_activity: firstReadableText(isProgress ? detail.message : "", latest.title, latest.event_type),
        live_activity_at: firstStringFromUnknownValue([latest.timestamp, ""]),
        live_progress_done: isProgress && typeof detail.done === "number" ? detail.done : undefined,
        live_progress_total: isProgress && typeof detail.total === "number" ? detail.total : undefined,
      };
    }
    // The job heartbeat names the running step; with one step running it is that step's.
    if (runningCount === 1) {
      return {
        ...step,
        live_activity: firstReadableText(runtime.last_response_excerpt),
        live_activity_at: firstStringFromUnknownValue([runtime.heartbeat_at, ""]),
      };
    }
    return step;
  });
}

const MAX_LIVE_FILES = 200;

/** Add one artifact_ready event to the run's file list, once per file. */
function liveFileKey(event: Entity): string {
  const detail = event.detail && typeof event.detail === "object" ? (event.detail as Entity) : {};
  return firstStringFromUnknownValue([detail.artifact_id, detail.dataset_id, event.id, ""]);
}

export function mergeLiveFiles(current: Entity[], event: Entity): Entity[] {
  const key = liveFileKey(event);
  if (key && current.some((row) => liveFileKey(row) === key)) {
    return current;
  }
  return [...current, event].slice(-MAX_LIVE_FILES);
}

function liveFilesForStep(liveFiles: readonly Entity[], stepId: string): Entity[] {
  return liveFiles
    .filter((event) => eventStepId(event) === stepId)
    .map((event) => {
      const detail = event.detail && typeof event.detail === "object" ? (event.detail as Entity) : {};
      return {
        id: firstStringFromUnknownValue([detail.artifact_id, detail.dataset_id, event.id, ""]),
        filename: firstReadableText(detail.filename, "file"),
        size_bytes: typeof detail.size_bytes === "number" ? detail.size_bytes : undefined,
        content_type: firstStringFromUnknownValue([detail.content_type, ""]),
        download_url: firstStringFromUnknownValue([detail.download_url, ""]),
      };
    });
}

export function isRunningStep(step: Entity): boolean {
  return firstStringFromUnknownValue([step.status, ""]).toLowerCase() === "in_progress";
}

export function executionStepStatusLabel(step: Entity, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (firstReadableText(step.status, step.outcome).toLowerCase() === "completed") {
    return t("jobs.status.completed", "Completed");
  }
  return firstReadableText(step.status_label, step.status, step.outcome, "recorded");
}

export function formatElapsed(milliseconds: number, t: I18nContextValue["t"] = fallbackContext.t): string {
  const total = Math.max(0, Math.floor(milliseconds / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours) {
    return t("react.jobs.value1_h_value2_min", "{value1} h {value2} min", { value1: String(hours), value2: String(minutes) });
  }
  if (minutes) {
    return t("react.jobs.value1_min_value2_s", "{value1} min {value2} s", { value1: String(minutes), value2: String(seconds) });
  }
  return t("react.jobs.value1_s", "{value1} s", { value1: String(seconds) });
}
