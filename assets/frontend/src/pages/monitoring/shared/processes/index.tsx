import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useCallback, useEffect, useMemo, useState } from "react";

import { useQuery } from "@tanstack/react-query";

import {
  cancelProcessJob,
  fetchProcessesPayload,
  killWorkerProcess,
  type ProcessesPayload,
} from "@/pages/monitoring/shared/processes/api";
import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";
import {
  BulkActionBar,
  DsAlert,
  DsBadge,
  DsCard,
  DsTable,
  RowActionMenu,
  type DsBulkAction,
} from "@/components/shared/design-system";
import { columnsFromGridConfig, visibleRowActions } from "@/components/shared/data-grid-adapter";
import { ConfirmActionDialog } from "@/components/shared/confirm-action-dialog";

import "../../monitoring.css";

type BulkHandler = (action: string, itemIds: string[]) => void | Promise<void>;

type ProcessesWindow = Window & {
  bulkProcessJobAction?: BulkHandler;
  bulkWorkerAction?: BulkHandler;
};

function listFromUnknown(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object");
}

function statusLabel(value: unknown): string {
  return firstStringFromUnknownValue([value, "unknown"]).replaceAll("_", " ");
}

function formatDuration(value: unknown, t: I18nContextValue["t"] = fallbackContext.t): string {
  const text = firstStringFromUnknownValue([value, ""]).trim();
  if (!text) {
    return "—";
  }
  const parsed = new Date(text);
  if (Number.isNaN(parsed.getTime())) {
    return text;
  }
  const diffSeconds = Math.max(0, Math.floor((Date.now() - parsed.getTime()) / 1000));
  if (diffSeconds < 60) {
    return `${diffSeconds}s`;
  }
  if (diffSeconds < 3600) {
    return t("react.monitoring.value1_m_value2_s", "{value1}m {value2}s", { value1: String(Math.floor(diffSeconds / 60)), value2: String(diffSeconds % 60) });
  }
  return t("react.monitoring.value1_h_value2_m", "{value1}h {value2}m", { value1: String(Math.floor(diffSeconds / 3600)), value2: String(Math.floor((diffSeconds % 3600) / 60)) });
}

function normalizeState(config: Record<string, unknown>, key: "title" | "description"): string {
  const empty = (config.empty_state || {}) as Record<string, unknown>;
  return firstStringFromUnknownValue([empty[key], ""]);
}

function bulkActionsFromConfig(config: Record<string, unknown>): DsBulkAction[] {
  const declared = Array.isArray(config.bulk_actions) ? config.bulk_actions : [];
  return declared.map((entry) => {
    const action = (entry || {}) as Record<string, unknown>;
    return {
      id: firstStringFromUnknownValue([action.action_id, ""]),
      label: firstStringFromUnknownValue([action.label, ""]),
      variant: action.variant === "danger" ? ("danger" as const) : ("default" as const),
    };
  });
}

function buildJobsGridConfig(t: I18nContextValue["t"] = fallbackContext.t): Record<string, unknown> {
  return {
    columns: [
      { key: "id", label: "#", type: "id", width: "70px" },
      { key: "project_name", label: t("events.columns.project", "Project"), type: "text", width: "150px" },
      {
        key: "display_summary",
        label: t("processes.columns.task", "Task"),
        type: "text",
        truncate: true,
        truncate_width: "clamp(18rem, 36vw, 34rem)",
        tooltip_key: "task_summary",
      },
      {
        key: "_status_label",
        label: t("account_settings.card.status", "Status"),
        type: "badge",
        width: "130px",
        badge_map: {
          "awaiting approval": "red",
          "in progress": "accent",
          pending: "accent",
          running: "accent",
          blocked: "amber",
          unknown: "neutral",
        },
      },
      { key: "current_step_id", label: t("registry.workflow_detail.step.drawer_title", "Step"), type: "text", width: "110px" },
      { key: "current_agent", label: t("events.columns.agent", "Agent"), type: "text", width: "110px" },
      { key: "_duration", label: t("jobs.columns.duration", "Duration"), type: "text", width: "90px" },
      { key: "worker_id", label: t("processes.columns.worker", "Worker"), type: "text", width: "110px" },
    ],
    row_actions: [{ action_id: "cancel", label: t("common.cancel", "Cancel"), variant: "danger" }],
    bulk_actions: [{ action_id: "bulk_cancel", label: t("jobs.actions.cancel_selected", "Cancel Selected"), variant: "danger" }],
    id_field: "id",
    pagination: { page_size_default: 25 },
    empty_state: { title: t("processes.empty.jobs_title", "No active jobs"), description: t("react.monitoring.jobs_will_appear_here_while_running", "Jobs will appear here while running.") },
  };
}

function buildWorkersGridConfig(t: I18nContextValue["t"] = fallbackContext.t): Record<string, unknown> {
  return {
    columns: [
      { key: "pid", label: "PID", type: "id", width: "90px" },
      {
        key: "cmdline",
        label: t("processes.columns.command", "Command"),
        type: "text",
        truncate: true,
        truncate_width: "clamp(18rem, 40vw, 38rem)",
      },
      { key: "_rss_label", label: t("react.monitoring.memory", "Memory"), type: "text", width: "130px" },
    ],
    row_actions: [{ action_id: "kill", label: t("monitoring.action.kill", "Kill"), variant: "danger" }],
    bulk_actions: [{ action_id: "bulk_kill", label: t("processes.actions.kill_selected", "Kill Selected"), variant: "danger" }],
    id_field: "pid",
    pagination: { page_size_default: 25 },
    empty_state: { title: t("react.monitoring.no_worker_subprocesses", "No worker subprocesses"), description: t("react.monitoring.subprocesses_appear_here_when_active", "Subprocesses appear here when active.") },
  };
}

function processPayloadToGridRows(payload: ProcessesPayload | undefined, t: I18nContextValue["t"] = fallbackContext.t) {
  const jobs = listFromUnknown(payload?.jobs).map((job) => {
    const id = Number(job.id || job.job_id || 0);
    return {
      ...job,
      id: firstStringFromUnknownValue([id, job.id, job.job_id, ""]),
      display_summary: firstStringFromUnknownValue([job.display_summary, job.task_summary, ""]),
      _status_label: statusLabel(job.status),
      _duration: formatDuration(job.started_at, t),
    };
  });
  // AC17-R69: the worker processes themselves. `subprocesses` below are their children
  // during an active job, so it is empty on a healthy idle install and cannot answer
  // "did FS_WORKER_PROCESSES take effect?". Absent on a runtime that predates this field.
  const workerProcesses = listFromUnknown(payload?.worker_processes).map((process) => ({
    ...process,
    pid: firstStringFromUnknownValue([process.pid, ""]).trim(),
  })).filter((process) => Boolean(process.pid));
  const subprocesses = listFromUnknown(payload?.subprocesses);
  const workerRows = subprocesses
    .map((process) => ({
      ...process,
      pid: firstStringFromUnknownValue([process.pid, process.process_id, ""]).trim(),
      cmdline: firstStringFromUnknownValue([process.cmdline, process.command, process.cmd, ""]),
      _rss_label: `${firstStringFromUnknownValue([process.rss_mb, 0])} MB`,
    }))
    .filter((process) => Boolean(process.pid));
  return { jobs, workers: workerRows, workerProcesses };
}

/**
 * After a destructive action the row, and its action menu, may be gone. Canon
 * sends focus to the nearest surviving row, or to the region's heading when
 * none is left, and never to body.
 */
function focusAfterAction(selector: string, headingId: string) {
  requestAnimationFrame(() =>
    requestAnimationFrame(() => {
      const target = (selector && document.querySelector<HTMLElement>(selector)) || document.getElementById(headingId);
      target?.focus();
    }),
  );
}

export function ProcessesControlPanel() {
  const { t } = useI18n();
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState(false);
  const [selectedJobs, setSelectedJobs] = useState<ReadonlySet<string>>(new Set());
  const [selectedWorkers, setSelectedWorkers] = useState<ReadonlySet<string>>(new Set());
  const [jobMenu, setJobMenu] = useState<string | null>(null);
  const [workerMenu, setWorkerMenu] = useState<string | null>(null);

  const processesQuery = useQuery({
    queryKey: ["processes"],
    queryFn: fetchProcessesPayload,
    refetchInterval: 10_000,
  });

  const { jobs, workers, workerProcesses } = useMemo(
    () => processPayloadToGridRows(processesQuery.data, t),
    [processesQuery.data, t],
  );

  // Cancelling a run and killing a process cannot be undone, so each is a
  // blocking Modal / Dialog under canon's routing, one object or many.
  const [confirmation, setConfirmation] = useState<
    | { kind: "cancel-jobs"; ids: string[] }
    | { kind: "kill-workers"; ids: string[] }
    | null
  >(null);
  const [confirmationError, setConfirmationError] = useState("");
  const [actionMessage, setActionMessage] = useState("");

  const rowsOf = (kind: "cancel-jobs" | "kill-workers"): Array<Record<string, unknown>> =>
    kind === "cancel-jobs" ? jobs : workers;

  const requestCancelJobs = useCallback((ids: string[]) => {
    const clean = ids.map((id) => firstStringFromUnknownValue([id, ""]).trim()).filter(isTruthy);
    if (!clean.length) return;
    setConfirmationError("");
    setConfirmation({ kind: "cancel-jobs", ids: clean });
  }, []);

  const requestKillWorkers = useCallback((ids: string[]) => {
    const clean = ids.map((id) => firstStringFromUnknownValue([id, ""]).trim()).filter(isTruthy);
    if (!clean.length) return;
    setConfirmationError("");
    setConfirmation({ kind: "kill-workers", ids: clean });
  }, []);

  // The bridge parked bulk handlers on window so a web component could reach
  // them, and tests/ui still addresses them. The fallbacks now ask first.
  const fallbackBulkProcessJobAction = useCallback(
    (action: string, itemIds: string[]) => {
      if (action === "bulk_cancel") requestCancelJobs(itemIds);
    },
    [requestCancelJobs],
  );

  const fallbackBulkWorkerAction = useCallback(
    (action: string, itemIds: string[]) => {
      if (action === "bulk_kill") requestKillWorkers(itemIds);
    },
    [requestKillWorkers],
  );

  useEffect(() => {
    const activeWindow = globalThis as unknown as ProcessesWindow;
    const ownJob = typeof activeWindow.bulkProcessJobAction !== "function";
    const ownWorker = typeof activeWindow.bulkWorkerAction !== "function";
    if (ownJob) activeWindow.bulkProcessJobAction = fallbackBulkProcessJobAction;
    if (ownWorker) activeWindow.bulkWorkerAction = fallbackBulkWorkerAction;
    // Remove only what this instance installed. A handler left behind would
    // point at an unmounted panel, and the next panel's bulk action would open
    // its confirmation in a component that no longer renders.
    return () => {
      if (ownJob && activeWindow.bulkProcessJobAction === fallbackBulkProcessJobAction) {
        delete activeWindow.bulkProcessJobAction;
      }
      if (ownWorker && activeWindow.bulkWorkerAction === fallbackBulkWorkerAction) {
        delete activeWindow.bulkWorkerAction;
      }
    };
  }, [fallbackBulkProcessJobAction, fallbackBulkWorkerAction]);

  function cancelConfirmation() {
    if (busy) return;
    setConfirmation(null);
    setConfirmationError("");
  }

  async function confirmPendingAction() {
    if (!confirmation) return;
    const { kind, ids } = confirmation;
    const jobsKind = kind === "cancel-jobs";
    const act = jobsKind ? cancelProcessJob : killWorkerProcess;
    const rowIds = rowsOf(kind).map((row) =>
      firstStringFromUnknownValue([jobsKind ? row.id : row.pid, ""]),
    );
    setBusy(true);
    setConfirmationError("");
    setActionError("");
    setActionMessage("");
    const results = await Promise.allSettled(ids.map((id) => act(id)));
    const failed = results.flatMap((result, index) =>
      result.status === "rejected"
        ? [{ id: ids[index] ?? "", reason: result.reason instanceof Error ? result.reason.message : t("react.monitoring.no_reason_given", "No reason given.") }]
        : [],
    );
    setBusy(false);
    // Nothing went through: the dialog stays open and says why.
    if (failed.length === ids.length) {
      setConfirmationError(
        ids.length === 1
          ? failed[0]?.reason || t("react.monitoring.action_did_not_complete", "The action did not complete.")
          : (jobsKind ? t("react.monitoring.jobs_cancel_failed_count", "None of the {count} jobs were cancelled. {reason}", { count: ids.length, reason: failed[0]?.reason ?? "" }) : t("react.monitoring.processes_kill_failed_count", "None of the {count} processes were killed. {reason}", { count: ids.length, reason: failed[0]?.reason ?? "" })).trim(),
      );
      return;
    }
    setConfirmation(null);
    const done = ids.length - failed.length;
    if (!failed.length) {
      setActionMessage(ids.length === 1
        ? (jobsKind ? t("react.monitoring.job_cancelled_feedback", "Job #{id} cancelled.", { id: ids[0] }) : t("react.monitoring.process_killed_feedback", "Process {id} killed.", { id: ids[0] }))
        : (jobsKind ? t("react.monitoring.jobs_cancelled_feedback", "{count} jobs cancelled.", { count: ids.length }) : t("react.monitoring.processes_killed_feedback", "{count} processes killed.", { count: ids.length })));
    } else {
      // Per-object results: name the split, keep the failed objects selected
      // for a retry, and give each reason beside the table.
      setActionMessage(jobsKind ? t("react.monitoring.jobs_cancelled_partial", "{count} of {total} jobs cancelled.", { count: done, total: ids.length }) : t("react.monitoring.processes_killed_partial", "{count} of {total} processes killed.", { count: done, total: ids.length }));
      setActionError(failed.map((item) => `${jobsKind ? "#" : "PID "}${item.id}: ${item.reason}`).join(" "));
    }
    const failedIds = new Set(failed.map((item) => item.id));
    if (jobsKind) setSelectedJobs(failedIds);
    else setSelectedWorkers(failedIds);
    // Focus moves before the refetch, to a row that survives it, so it never
    // waits on body while the table reloads.
    const firstIndex = rowIds.indexOf(ids[0] ?? "");
    const survivors = new Set(rowIds.filter((id) => !ids.includes(id) || failedIds.has(id)));
    const next = rowIds.slice(firstIndex + 1).find((id) => survivors.has(id))
      ?? rowIds.slice(0, Math.max(firstIndex, 0)).reverse().find((id) => survivors.has(id));
    focusAfterAction(
      next ? `[aria-label="${CSS.escape(jobsKind ? t("react.monitoring.actions_for_job_value1", "Actions for job {value1}", { value1: next }) : t("react.monitoring.actions_for_process_value1", "Actions for process {value1}", { value1: next }))}"]` : "",
      jobsKind ? "processes-jobs-heading" : "processes-workers-heading",
    );
    await processesQuery.refetch();
  }

  function handleJobAction(actionId: string, row: Record<string, unknown>) {
    if (actionId === "cancel") {
      requestCancelJobs([firstStringFromUnknownValue([row.id, row.job_id, ""])]);
    }
  }

  function handleWorkerAction(actionId: string, row: Record<string, unknown>) {
    if (actionId === "kill") {
      requestKillWorkers([firstStringFromUnknownValue([row.pid, ""])]);
    }
  }

  function runBulkJobAction(actionId: string) {
    const handler =
      (globalThis as unknown as ProcessesWindow).bulkProcessJobAction ?? fallbackBulkProcessJobAction;
    ignoreAsyncResult(Promise.resolve(handler(actionId, [...selectedJobs])));
  }

  function runBulkWorkerAction(actionId: string) {
    const handler =
      (globalThis as unknown as ProcessesWindow).bulkWorkerAction ?? fallbackBulkWorkerAction;
    ignoreAsyncResult(Promise.resolve(handler(actionId, [...selectedWorkers])));
  }

  const confirmationRows = confirmation
    ? rowsOf(confirmation.kind).filter((row) =>
        confirmation.ids.includes(
          firstStringFromUnknownValue([confirmation.kind === "cancel-jobs" ? row.id : row.pid, ""]),
        ),
      )
    : [];
  const confirmationObjects = confirmation
    ? confirmation.ids.map((id) => {
        const row = confirmationRows.find(
          (candidate) => firstStringFromUnknownValue([confirmation.kind === "cancel-jobs" ? candidate.id : candidate.pid, ""]) === id,
        );
        if (confirmation.kind === "cancel-jobs") {
          const project = firstStringFromUnknownValue([row?.project_name, ""]);
          return project ? `#${id} · ${project}` : `#${id}`;
        }
        const command = firstStringFromUnknownValue([row?.cmdline, ""]);
        return command ? `PID ${id} · ${command.slice(0, 48)}` : `PID ${id}`;
      })
    : [];
  const confirmationCount = confirmation?.ids.length ?? 0;
  const confirmationCopy = confirmation
    ? confirmation.kind === "cancel-jobs"
      ? {
          title: confirmationCount === 1 ? t("react.monitoring.cancel_job_value1", "Cancel job #{value1}?", { value1: String(confirmation.ids[0]) }) : t("react.monitoring.cancel_value1_jobs", "Cancel {value1} jobs?", { value1: String(confirmationCount) }),
          consequence:
            confirmationCount === 1
              ? t("react.monitoring.the_run_stops_where_it_is_and_its_progress_is_lost", "The run stops where it is, and its progress is lost.")
              : t("react.monitoring.each_run_stops_where_it_is_and_its_progress_is_lost", "Each run stops where it is, and its progress is lost."),
          confirmLabel: confirmationCount === 1 ? t("registry.workflow_detail.automation.action.cancel_job", "Cancel job") : t("react.monitoring.cancel_value1_jobs_2", "Cancel {value1} jobs", { value1: String(confirmationCount) }),
          pendingLabel: "Cancelling…",
        }
      : {
          title: confirmationCount === 1 ? t("react.monitoring.kill_process_value1", "Kill process {value1}?", { value1: String(confirmation.ids[0]) }) : t("react.monitoring.kill_value1_processes", "Kill {value1} processes?", { value1: String(confirmationCount) }),
          consequence:
            confirmationCount === 1
              ? t("react.monitoring.the_worker_subprocess_is_terminated_immediately_and_the_step_it_was_running_fails", "The worker subprocess is terminated immediately, and the step it was running fails.")
              : t("react.monitoring.each_worker_subprocess_is_terminated_immediately_and_the_step_it_was_running_fails", "Each worker subprocess is terminated immediately, and the step it was running fails."),
          confirmLabel: confirmationCount === 1 ? t("react.monitoring.kill_process", "Kill process") : t("react.monitoring.kill_value1_processes_2", "Kill {value1} processes", { value1: String(confirmationCount) }),
          pendingLabel: "Killing…",
        }
    : null;

  const jobsConfig = useMemo(() => buildJobsGridConfig(t), [t]);
  const workersConfig = useMemo(() => buildWorkersGridConfig(t), [t]);

  const jobColumns = useMemo(
    () =>
      columnsFromGridConfig(jobsConfig, (row) => {
        const jobId = firstStringFromUnknownValue([row.id, row.job_id, ""]);
        return (
          <RowActionMenu
            label={t("react.monitoring.actions_for_job_value1", "Actions for job {value1}", { value1: String(jobId) })}
            onOpenChange={(open) => setJobMenu(open ? jobId : null)}
            open={jobMenu === jobId}
          >
            {visibleRowActions(jobsConfig, row).map((action) => (
              <div className="fs-row-menu-group" key={action.action_id}>
                <button
                  className={action.variant === "danger" ? "danger" : undefined}
                  onClick={() => {
                    setJobMenu(null);
                    handleJobAction(action.action_id, row);
                  }}
                  role="menuitem"
                  type="button"
                >
                  {action.label}
                </button>
              </div>
            ))}
          </RowActionMenu>
        );
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [jobMenu, jobsConfig, t],
  );

  const workerColumns = useMemo(
    () =>
      columnsFromGridConfig(workersConfig, (row) => {
        const pid = firstStringFromUnknownValue([row.pid, ""]);
        return (
          <RowActionMenu
            label={t("react.monitoring.actions_for_process_value1", "Actions for process {value1}", { value1: String(pid) })}
            onOpenChange={(open) => setWorkerMenu(open ? pid : null)}
            open={workerMenu === pid}
          >
            {visibleRowActions(workersConfig, row).map((action) => (
              <div className="fs-row-menu-group" key={action.action_id}>
                <button
                  className={action.variant === "danger" ? "danger" : undefined}
                  onClick={() => {
                    setWorkerMenu(null);
                    handleWorkerAction(action.action_id, row);
                  }}
                  role="menuitem"
                  type="button"
                >
                  {action.label}
                </button>
              </div>
            ))}
          </RowActionMenu>
        );
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [workerMenu, workersConfig, t],
  );

  return (
    <section className="fs-panel-stack">
      <DsCard
        description={t("react.monitoring.manage_active_jobs_and_worker_subprocesses_with_row_and_bulk_control_actions", "Manage active jobs and worker subprocesses with row and bulk control actions.")}
        title={t("react.monitoring.runtime_process_control", "Runtime process control")}
      >
        <div className="fs-panel-heading">
          <div className="fs-monitoring-status">
            <DsBadge>{t("react.monitoring.active_jobs", "Active jobs:")} {jobs.length}</DsBadge>
            {/*
              AC17-R69: this chip said "Worker processes" and counted subprocesses, so a Compact
              install with two healthy workers reported 0. The two are different things and now
              say so.
            */}
            <span className="fs-badge" data-testid="proc-worker-process-count">
              {t("react.monitoring.worker_processes", "Worker processes:")} {workerProcesses.length}
            </span>
            <DsBadge>{t("react.monitoring.worker_subprocesses", "Worker subprocesses:")} {workers.length}</DsBadge>
          </div>
          <button
            className="fs-button"
            disabled={busy}
            onClick={() => {
              ignoreAsyncResult(processesQuery.refetch());
            }}
            type="button"
          >
            {t("account.tokens.refresh", "Refresh")} </button>
        </div>
      </DsCard>

      {actionMessage ? <DsAlert title={actionMessage} tone={actionError ? "warning" : "success"} /> : null}
      {actionError ? (
        <DsAlert title={t("react.monitoring.not_completed", "Not completed")} tone="danger">
          {actionError}
        </DsAlert>
      ) : null}

      <div className="fs-panel-group">
        <div className="fs-panel-heading">
          <div>
            <h2 className="fs-panel-title" id="processes-jobs-heading" tabIndex={-1}>
              {t("react.monitoring.active_jobs_2", "Active jobs")} </h2>
          </div>
        </div>
        <article className="fs-card fs-table-card">
          <DsTable
            ariaLabel={t("react.monitoring.active_jobs_2", "Active jobs")}
            columns={jobColumns}
            onSelectionChange={setSelectedJobs}
            rowKey={(row) => firstStringFromUnknownValue([row.id, ""])}
            rows={jobs}
            selectable
            selectedKeys={selectedJobs}
            state={
              jobs.length ? null : (
                <div className="fs-table-state" data-testid="proc-jobs-grid">
                  <strong>{normalizeState(jobsConfig, "title")}</strong>
                  <span>{normalizeState(jobsConfig, "description")}</span>
                </div>
              )
            }
          />
          <BulkActionBar
            actions={bulkActionsFromConfig(jobsConfig)}
            busy={busy}
            count={selectedJobs.size}
            onAction={runBulkJobAction}
            onClear={() => setSelectedJobs(new Set())}
          />
        </article>
      </div>

      <div className="fs-panel-group">
        <div className="fs-panel-heading">
          <div>
            <h2 className="fs-panel-title" id="processes-workers-heading" tabIndex={-1}>
              {t("react.monitoring.worker_subprocesses_2", "Worker subprocesses")} </h2>
          </div>
        </div>
        <article className="fs-card fs-table-card">
          <DsTable
            ariaLabel={t("react.monitoring.worker_subprocesses_2", "Worker subprocesses")}
            columns={workerColumns}
            onSelectionChange={setSelectedWorkers}
            rowKey={(row) => firstStringFromUnknownValue([row.pid, ""])}
            rows={workers}
            selectable
            selectedKeys={selectedWorkers}
            state={
              workers.length ? null : (
                <div className="fs-table-state" data-testid="proc-workers-grid">
                  <strong>{normalizeState(workersConfig, "title")}</strong>
                  <span>
                    {workerProcesses.length
                      ? (workerProcesses.length === 1
                        ? t("react.monitoring.one_running_idle_worker", "1 worker process is running and idle. Subprocesses appear here while a job executes.")
                        : t("react.monitoring.running_idle_workers_count", "{count} worker processes are running and idle. Subprocesses appear here while a job executes.", { count: workerProcesses.length }))
                      : normalizeState(workersConfig, "description")}
                  </span>
                </div>
              )
            }
          />
          <BulkActionBar
            actions={bulkActionsFromConfig(workersConfig)}
            busy={busy}
            count={selectedWorkers.size}
            onAction={runBulkWorkerAction}
            onClear={() => setSelectedWorkers(new Set())}
          />
        </article>
      </div>
      <ConfirmActionDialog
        closeLabel={t("react.monitoring.close_confirmation", "Close confirmation")}
        confirmLabel={confirmationCopy?.confirmLabel || ""}
        consequence={confirmationCopy?.consequence || ""}
        error={confirmationError}
        eyebrow={t("settings.watchdog.processes_link", "Processes")}
        objects={confirmationObjects}
        onCancel={cancelConfirmation}
        onConfirm={() => ignoreAsyncResult(confirmPendingAction())}
        open={Boolean(confirmation)}
        pending={busy && Boolean(confirmation)}
        pendingLabel={confirmationCopy?.pendingLabel || ""}
        title={confirmationCopy?.title || ""}
      />
    </section>
  );
}
