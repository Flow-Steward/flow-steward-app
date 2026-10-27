"""Application service for atomic durable public workflow progress."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from core.application.jobs.workflow_run_models import (
    PublicApprovalGateV1,
    PublicSnapshotRevision,
    PublicWorkflowErrorV1,
    PublicWorkflowResultV1,
    PublicWorkflowStepV1,
    PublicWorkflowTraceV1,
    WorkflowRunStateV1,
)
from core.application.jobs.workflow_wait_contract import DURABLE_WAIT_STATUSES
from core.application.observability_vocabulary import job_status_meta, normalize_job_status
from core.application.orchestration.task_envelope import TEST_RUN_SOURCES

_UTC_OFFSET = "+00:00"

#: A step paused on a durable wait (approval, timer, child) is still active.
_ACTIVE_STEP_STATUSES = frozenset({"in_progress", "external_wait", *DURABLE_WAIT_STATUSES})
#: Graph nodes that route the run rather than execute an authored step. While
#: the cursor names a step their passes are not projected as steps of their own.
_RUN_ROUTING_NODES = frozenset(
    {
        "task_intake",
        "kb_intake",
        "workflow_routing",
        "orchestrator",
        "orchestrator_validation",
        "operation_retry_wait",
        "parallel_executor",
    }
)
#: How the step hub marks the execution of a disabled step it skipped instead of
#: dispatching (``step_hub_support.STEP_SKIPPED_REASON``).
_SKIPPED_DISPATCH = "disabled"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _identity(step: PublicWorkflowStepV1) -> str:
    return json.dumps(
        [step.step_id, step.step_execution_id or "", step.occurrence],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _fingerprint(snapshot: WorkflowRunStateV1) -> str:
    payload = snapshot.model_dump(mode="json")
    payload.pop("updated_at", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _model(value: Any, model_type):
    if value is None or isinstance(value, model_type):
        return value
    return model_type.model_validate(value)


#: Finished step executions the stored progress snapshot keeps. A For each
#: over thousands of items finishes thousands of executions; the snapshot is
#: rewritten on every transition, so it keeps the newest ones (and every active
#: one). The complete history is the job's messages and lifecycle events.
RETAINED_FINISHED_STEPS = 200


def _retained_steps(ordered: list[PublicWorkflowStepV1]) -> list[PublicWorkflowStepV1]:
    active = [item for item in ordered if item.status in _ACTIVE_STEP_STATUSES]
    finished = [item for item in ordered if item.status not in _ACTIVE_STEP_STATUSES]
    if len(finished) <= RETAINED_FINISHED_STEPS:
        return ordered
    keep = {id(item) for item in [*active, *finished[-RETAINED_FINISHED_STEPS:]]}
    return [item for item in ordered if id(item) in keep]


def completed_stop_operation(
    snapshot: WorkflowRunStateV1, step_id: str | None, execution_id: str | None
) -> PublicWorkflowStepV1 | None:
    if not step_id or not execution_id:
        return None
    matches = [
        step
        for step in snapshot.steps
        if step.step_id == step_id and step.step_execution_id == execution_id
    ]
    if len(matches) != 1:
        return None
    operation = matches[0]
    return (
        operation
        if operation.status == "completed" and operation.step_kind == "stop_fail"
        else None
    )


def _generic_job_failure(error: PublicWorkflowErrorV1 | None) -> bool:
    return bool(
        error is not None
        and error.code == "job_failed"
        and error.category == "unknown"
        and not error.retryable
        and not error.details
        and not any((error.step_id, error.step_execution_id, error.group_id, error.occurrence))
    )


def _retain_terminal_stop_error(
    previous: WorkflowRunStateV1 | None,
    incoming: PublicWorkflowErrorV1 | None,
    *,
    job_id: str,
    project_id: str,
    workflow_id: str,
    status: str,
) -> PublicWorkflowErrorV1 | None:
    if (
        previous is not None
        and previous.terminal
        and previous.status == status == "failed"
        and (previous.job_id, previous.project_id, previous.workflow_id)
        == (job_id, project_id, workflow_id)
        and previous.error is not None
        and previous.error.code == "workflow_stop_fail"
        and completed_stop_operation(
            previous, previous.error.step_id, previous.error.step_execution_id
        )
        and _generic_job_failure(incoming)
    ):
        return previous.error
    return incoming


def _stop_matches_current(
    snapshot: WorkflowRunStateV1, identity: dict[str, Any], error: PublicWorkflowErrorV1
) -> bool:
    if (
        not snapshot.terminal
        or snapshot.status != "failed"
        or any(
            getattr(snapshot, key) != identity[key]
            for key in ("job_id", "project_id", "workflow_id")
        )
        or error.code != "workflow_stop_fail"
        or (snapshot.error != error and not _generic_job_failure(snapshot.error))
    ):
        return False
    operation = completed_stop_operation(snapshot, error.step_id, error.step_execution_id)
    if operation is None or (error.group_id, error.occurrence) != (
        operation.group_id,
        operation.occurrence,
    ):
        return False
    if (
        snapshot.container_progress is not None
        and snapshot.container_progress.project_id != identity["project_id"]
    ):
        return False
    for evidence in (snapshot.run_trace, snapshot.container_progress):
        if evidence is not None and any(
            getattr(evidence, key) != identity[key]
            for key in ("job_id", "workflow_id", "snapshot_id", "revision", "digest")
        ):
            return False
    return True


def _stop_metadata_update(
    current: dict[str, Any],
    previous: WorkflowRunStateV1,
    error: PublicWorkflowErrorV1,
    occurred_at: datetime,
) -> dict[str, Any] | None:
    if previous.error == error:
        return None
    candidate = previous.model_copy(update={"error": error, "updated_at": _utc(occurred_at)})
    revision = int(current["revision"]) + 1
    fingerprint = _fingerprint(candidate)
    timestamp = candidate.updated_at.isoformat().replace(_UTC_OFFSET, "Z")
    return {
        **current,
        "revision": revision,
        "fingerprint": fingerprint,
        "snapshot": candidate.model_dump(mode="json"),
        "updated_at": timestamp,
        "marker_detail": {
            "schema_version": "1",
            "revision": revision,
            "job_status": previous.status,
            "step": None,
            "projection_fingerprint": fingerprint,
            "occurred_at": timestamp,
        },
    }


class PublicProgressService:
    def __init__(self, repository) -> None:
        self.repository = repository

    def current_snapshot(self, *, job_id: str) -> PublicSnapshotRevision:
        stored = self.repository.load(job_id=job_id)
        if not stored:
            raise LookupError(f"No public progress snapshot for job {job_id}")
        return PublicSnapshotRevision(
            snapshot=WorkflowRunStateV1.model_validate(stored["snapshot"]),
            revision=int(stored["revision"]),
            marker_id=str(stored.get("marker_id") or "").strip() or None,
            changed=False,
        )

    def record_transition(
        self,
        *,
        job_id: str,
        project_id: str,
        workflow_id: str,
        job_status: str,
        occurred_at: datetime,
        queued_at: datetime | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        step: PublicWorkflowStepV1 | dict[str, Any] | None = None,
        approval: PublicApprovalGateV1 | dict[str, Any] | None = None,
        result: PublicWorkflowResultV1 | dict[str, Any] | None = None,
        error: PublicWorkflowErrorV1 | dict[str, Any] | None = None,
        force: bool = False,
    ) -> PublicSnapshotRevision:
        canonical_status = normalize_job_status(job_status)
        status_meta = job_status_meta(canonical_status)
        terminal = bool(status_meta["terminal"])
        occurred_at = _utc(occurred_at)
        public_step = _model(step, PublicWorkflowStepV1)
        public_approval = _model(approval, PublicApprovalGateV1)
        public_result = _model(result, PublicWorkflowResultV1)
        public_error = _model(error, PublicWorkflowErrorV1)

        def update(current: dict[str, Any] | None) -> dict[str, Any] | None:
            previous_snapshot = (
                WorkflowRunStateV1.model_validate(current["snapshot"]) if current else None
            )
            if (
                previous_snapshot
                and previous_snapshot.terminal
                and (not terminal or previous_snapshot.status != canonical_status)
            ):
                return None
            previous_revision = int(current.get("revision") or 0) if current else 0
            order = dict(current.get("step_order") or {}) if current else {}
            by_identity = {
                _identity(item): item
                for item in (previous_snapshot.steps if previous_snapshot else [])
            }
            next_revision = previous_revision + 1
            if public_step is not None:
                key = _identity(public_step)
                by_identity[key] = public_step
                order.setdefault(key, next_revision)

            ordered_steps = _retained_steps(
                sorted(
                    by_identity.values(),
                    key=lambda item: (
                        int(order.get(_identity(item), next_revision)),
                        _identity(item),
                    ),
                )
            )
            retained = {_identity(item) for item in ordered_steps}
            order = {key: value for key, value in order.items() if key in retained}
            active_steps = (
                []
                if terminal
                else [item for item in ordered_steps if item.status in _ACTIVE_STEP_STATUSES]
            )
            serial = [item for item in active_steps if not item.lane_id]
            serial_step = serial[-1] if len(serial) == len(active_steps) and serial else None
            effective_finished_at = finished_at if terminal else None
            effective_result = public_result if canonical_status == "completed" else None
            effective_error = public_error if terminal and canonical_status != "completed" else None
            if public_step is None:
                effective_error = _retain_terminal_stop_error(
                    previous_snapshot,
                    effective_error,
                    job_id=job_id,
                    project_id=project_id,
                    workflow_id=workflow_id,
                    status=canonical_status,
                )
            candidate = WorkflowRunStateV1(
                job_id=job_id,
                project_id=project_id,
                workflow_id=workflow_id,
                status=canonical_status,
                terminal=terminal,
                current_step_id=serial_step.step_id if serial_step is not None else None,
                current_group_id=(
                    serial_step.group_id
                    if serial_step is not None
                    else next((item.group_id for item in active_steps if item.group_id), None)
                ),
                active_steps=active_steps,
                steps=ordered_steps,
                run_trace=previous_snapshot.run_trace if previous_snapshot else None,
                container_progress=previous_snapshot.container_progress
                if previous_snapshot
                else None,
                approval=public_approval,
                result=effective_result,
                error=effective_error,
                queued_at=queued_at or (previous_snapshot.queued_at if previous_snapshot else None),
                started_at=started_at
                or (previous_snapshot.started_at if previous_snapshot else None),
                updated_at=previous_snapshot.updated_at if previous_snapshot else occurred_at,
                finished_at=effective_finished_at,
            )
            fingerprint = _fingerprint(candidate)
            if current and fingerprint == str(current.get("fingerprint") or "") and not force:
                return None
            candidate = candidate.model_copy(update={"updated_at": occurred_at})
            marker_step = None
            if public_step is not None:
                marker_step = {
                    "step_id": public_step.step_id,
                    "step_execution_id": public_step.step_execution_id,
                    "group_id": public_step.group_id,
                    "occurrence": public_step.occurrence,
                    "status": public_step.status,
                }
            marker_detail = {
                "schema_version": "1",
                "revision": next_revision,
                "job_status": canonical_status,
                "step": marker_step,
                "projection_fingerprint": fingerprint,
                "occurred_at": occurred_at.isoformat().replace(_UTC_OFFSET, "Z"),
            }
            return {
                "schema_version": "1",
                "revision": next_revision,
                "fingerprint": fingerprint,
                "updated_at": occurred_at.isoformat().replace(_UTC_OFFSET, "Z"),
                "snapshot": candidate.model_dump(mode="json"),
                "step_order": order,
                "marker_detail": marker_detail,
            }

        stored, marker_id, changed = self.repository.atomic_update(job_id=job_id, update=update)
        return PublicSnapshotRevision(
            snapshot=WorkflowRunStateV1.model_validate(stored["snapshot"]),
            revision=int(stored["revision"]),
            marker_id=marker_id,
            changed=changed,
        )

    def record_terminal_stop(
        self,
        *,
        job_id: str,
        identity: dict[str, Any],
        error: PublicWorkflowErrorV1,
        occurred_at: datetime,
    ) -> PublicSnapshotRevision:
        """Recheck proof against the locked row and change only public error metadata."""

        def update(current: dict[str, Any] | None) -> dict[str, Any] | None:
            if not current:
                raise LookupError(f"No public progress snapshot for job {job_id}")
            previous = WorkflowRunStateV1.model_validate(current["snapshot"])
            if identity.get("job_id") != job_id or not _stop_matches_current(
                previous, identity, error
            ):
                return None
            return _stop_metadata_update(current, previous, error, occurred_at)

        stored, marker_id, changed = self.repository.atomic_update(job_id=job_id, update=update)
        return PublicSnapshotRevision(
            snapshot=WorkflowRunStateV1.model_validate(stored["snapshot"]),
            revision=int(stored["revision"]),
            marker_id=marker_id,
            changed=changed,
        )

    def record_committed_trace(
        self, *, job_id: str, trace: Any, occurred_at: datetime, container_progress: Any = None
    ) -> PublicSnapshotRevision:
        """Catch up structural evidence without mutating the run's lifecycle."""
        from core.application.jobs.workflow_run_models import PublicContainerProgressV1
        from core.application.orchestration.container_progress import container_progress_advances

        incoming = PublicWorkflowTraceV1.model_validate(trace) if trace is not None else None
        containers = (
            PublicContainerProgressV1.model_validate(container_progress)
            if container_progress is not None
            else None
        )

        def update(current: dict[str, Any] | None) -> dict[str, Any] | None:
            if not current:
                raise LookupError(f"No public progress snapshot for job {job_id}")
            previous = WorkflowRunStateV1.model_validate(current["snapshot"])
            if (
                previous.job_id != job_id
                or (
                    incoming is not None
                    and (incoming.job_id != job_id or incoming.workflow_id != previous.workflow_id)
                )
                or (
                    containers is not None
                    and (
                        containers.job_id != job_id
                        or containers.project_id != previous.project_id
                        or containers.workflow_id != previous.workflow_id
                    )
                )
            ):
                return None
            old = previous.run_trace
            if old is not None and incoming is not None:
                if any(
                    getattr(incoming, key) != getattr(old, key)
                    for key in ("snapshot_id", "revision", "digest")
                ):
                    return None
                if (
                    incoming.last_sequence < old.last_sequence
                    or old.covered_from_start != incoming.covered_from_start
                    or (old.truncated and not incoming.truncated)
                    or (old.terminal and not incoming.terminal)
                ):
                    return None
                overlap = {fact.sequence: fact for fact in old.transitions}
                if any(
                    fact.sequence in overlap and fact != overlap[fact.sequence]
                    for fact in incoming.transitions
                ):
                    return None
                if (
                    incoming.last_sequence == old.last_sequence
                    and incoming.transitions != old.transitions
                ):
                    return None
            old_containers = previous.container_progress
            if (
                containers is not None
                and old_containers is not None
                and not container_progress_advances(old_containers, containers)
            ):
                return None
            next_trace = incoming if incoming is not None else old
            next_containers = containers if containers is not None else old_containers
            if (
                next_trace is not None
                and next_containers is not None
                and any(
                    getattr(next_trace, key) != getattr(next_containers, key)
                    for key in ("job_id", "workflow_id", "snapshot_id", "revision", "digest")
                )
            ):
                return None
            if next_trace == old and next_containers == old_containers:
                return None
            candidate = previous.model_copy(
                update={
                    "run_trace": next_trace,
                    "container_progress": next_containers,
                    "updated_at": _utc(occurred_at),
                }
            )
            revision = int(current.get("revision") or 0) + 1
            fingerprint = _fingerprint(candidate)
            return {
                **current,
                "revision": revision,
                "fingerprint": fingerprint,
                "snapshot": candidate.model_dump(mode="json"),
                "updated_at": candidate.updated_at.isoformat().replace(_UTC_OFFSET, "Z"),
                "marker_detail": {
                    "schema_version": "1",
                    "revision": revision,
                    "job_status": previous.status,
                    "step": None,
                    "projection_fingerprint": fingerprint,
                    "occurred_at": candidate.updated_at.isoformat().replace(_UTC_OFFSET, "Z"),
                },
            }

        stored, marker_id, changed = self.repository.atomic_update(job_id=job_id, update=update)
        return PublicSnapshotRevision(
            snapshot=WorkflowRunStateV1.model_validate(stored["snapshot"]),
            revision=int(stored["revision"]),
            marker_id=marker_id,
            changed=changed,
        )


class PublicStepProgressRecorder:
    """Project callback events into canonical public step transitions."""

    def __init__(
        self,
        *,
        gateway: Any,
        project_id: str,
        job_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._gateway = gateway
        self._project_id = str(project_id)
        self._job_id = str(job_id)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = Lock()
        self._step_counts: dict[str, int] = {}
        self._execution_occurrences: dict[tuple[str, str], int] = {}
        #: Per callback run: its step identity, occurrence, start time and the
        #: cursor detail captured at its start. A node's end and error events
        #: carry only its partial update (no run cursor), so they reuse it.
        self._run_steps: dict[str, tuple[str, str, str, int, datetime, dict[str, Any]]] = {}
        self._ignored_runs: set[str] = set()
        self._test_approval_runs: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _cursor_detail(payload: dict[str, Any]) -> dict[str, Any]:
        """Attempt, For each, item position and lane of the run cursor in *payload*."""
        cursor = payload.get("run_cursor")
        if not isinstance(cursor, dict):
            return {}
        frames = cursor.get("frames")
        frame = next(
            (
                item
                for item in reversed(frames if isinstance(frames, list) else [])
                if isinstance(item, dict) and item.get("frame") == "for_each"
            ),
            {},
        )
        innermost = next(iter(reversed(frames if isinstance(frames, list) else [])), {})
        immediate_for_each = (
            str(frame.get("for_each_id") or "").strip() or None
            if isinstance(innermost, dict) and innermost.get("frame") == "for_each"
            else None
        )
        try:
            attempt = max(1, int(cursor.get("attempt") or 1))
        except (TypeError, ValueError):
            attempt = 1
        item_index = frame.get("item_index")
        return {
            "attempt": attempt,
            "for_each_id": immediate_for_each,
            "iteration": item_index if isinstance(item_index, int) else None,
            "lane_id": str(payload.get("current_lane_id") or "").strip() or None,
        }

    @staticmethod
    def _cursor_scope(payload: dict[str, Any]) -> tuple[str, str, str]:
        """(step_id, step_execution_id, group_id) of the run cursor in *payload*.

        ``group_id`` is the immediate Group only: a For each step names its For
        each (in the cursor detail), never the Group around it.
        """
        cursor = payload.get("run_cursor")
        if not isinstance(cursor, dict):
            return "", "", ""
        frames = cursor.get("frames")
        frame = next(
            iter(reversed(frames if isinstance(frames, list) else [])),
            {},
        )
        frame = frame if isinstance(frame, dict) and frame.get("frame") == "group" else {}
        return (
            str(cursor.get("step_id") or "").strip(),
            str(cursor.get("step_execution_id") or "").strip(),
            str(frame.get("group_id") or "").strip(),
        )

    @staticmethod
    def _dispatched(payload: dict[str, Any]) -> bool:
        """Whether the hub dispatched exactly the cursor's execution and attempt.

        The projection never loads the pinned definition: the hub's dispatch
        record says which execution is in flight, and the node that runs while
        it is in flight is its executor.
        """
        cursor = payload.get("run_cursor")
        dispatched = payload.get("dispatched_step_execution")
        if not isinstance(cursor, dict) or not isinstance(dispatched, dict):
            return False
        execution_id = str(cursor.get("step_execution_id") or "").strip()
        return bool(execution_id) and (
            str(dispatched.get("step_execution_id") or "").strip(),
            str(dispatched.get("attempt") or ""),
        ) == (execution_id, str(cursor.get("attempt") or 1))

    def _record_skipped_step(self, payload: dict[str, Any]) -> None:
        """Record the disabled step a routing pass skipped: it never runs, so no
        executor reports it, and its row is ``skipped`` from the start."""
        dispatched = payload.get("dispatched_step_execution")
        if (
            not isinstance(dispatched, dict)
            or dispatched.get("skipped") != _SKIPPED_DISPATCH
            or not self._dispatched(payload)
        ):
            return
        step_id, step_execution_id, group_id = self._cursor_scope(payload)
        if not step_id or str(dispatched.get("step_id") or "").strip() != step_id:
            return
        job = self._gateway.get_job(self._job_id)
        if not isinstance(job, dict):
            return
        now = _utc(self._clock())
        with self._lock:
            occurrence = self._next_occurrence(step_id=step_id, step_execution_id=step_execution_id)
        self._gateway.record_public_progress_transition(
            job_id=self._job_id,
            project_id=str(job.get("project_id") or self._project_id),
            workflow_id=str(
                payload.get("workflow_id")
                or job.get("selected_workflow")
                or job.get("selected_workflow_key")
                or "unknown"
            ),
            job_status=job.get("status"),
            occurred_at=now,
            queued_at=job.get("queued_at"),
            started_at=job.get("started_at"),
            step={
                "step_id": step_id,
                "step_execution_id": step_execution_id,
                **self._cursor_detail(payload),
                "group_id": group_id or None,
                "occurrence": occurrence,
                "step_kind": str(dispatched.get("step_kind") or "node"),
                "status": "skipped",
                "started_at": None,
                "finished_at": now,
            },
        )

    def _persisted_occurrences(self, *, step_id: str) -> tuple[int, dict[str, int]]:
        """Highest recorded occurrence of *step_id* and each execution's occurrence."""
        try:
            revision = self._gateway.get_public_progress_snapshot(self._job_id)
            snapshot = getattr(revision, "snapshot", revision)
            steps = [
                item
                for item in getattr(snapshot, "steps", None) or []
                if str(getattr(item, "step_id", "")).strip() == step_id
            ]
        except Exception:
            return 0, {}
        executions = {
            str(item.step_execution_id): int(item.occurrence)
            for item in steps
            if getattr(item, "step_execution_id", None)
        }
        return max((int(item.occurrence) for item in steps), default=0), executions

    def _next_occurrence(self, *, step_id: str, step_execution_id: str) -> int:
        """One occurrence per logical execution; retries of it keep the same row.

        Nodes that are not authored steps have no execution id, so each start
        is a new occurrence.
        """
        if step_execution_id:
            known = self._execution_occurrences.get((step_id, step_execution_id))
            if known is not None:
                return known
        persisted_max, persisted_executions = self._persisted_occurrences(step_id=step_id)
        if step_execution_id and step_execution_id in persisted_executions:
            occurrence = persisted_executions[step_execution_id]
        else:
            occurrence = max(self._step_counts.get(step_id, 0), persisted_max) + 1
        self._step_counts[step_id] = max(self._step_counts.get(step_id, 0), occurrence)
        if step_execution_id:
            self._execution_occurrences[(step_id, step_execution_id)] = occurrence
        return occurrence

    def record(
        self,
        *,
        run_id: Any,
        node_name: str,
        payload: dict[str, Any],
        status: str,
        starting: bool = False,
    ) -> dict[str, Any] | None:
        if node_name in {"", "unknown", "LangGraph"}:
            return None
        if not starting and node_name in _RUN_ROUTING_NODES:
            self._record_skipped_step(payload)
        run_key = str(run_id) if run_id else ""
        cursor_step, cursor_execution, cursor_group = self._cursor_scope(payload)
        routing_node = node_name in _RUN_ROUTING_NODES
        with self._lock:
            if starting and cursor_step and routing_node:
                if run_key:
                    self._ignored_runs.add(run_key)
                return None
            if not starting and run_key in self._ignored_runs:
                self._ignored_runs.discard(run_key)
                return None
        job = self._gateway.get_job(self._job_id)
        if not isinstance(job, dict):
            return None

        # Only the dispatched execution's executor runs as the active step;
        # every other node is recorded under its own name without one.
        on_cursor_step = bool(cursor_step) and not routing_node and self._dispatched(payload)
        step_id = str(
            payload.get("step_id") or (cursor_step if on_cursor_step else "") or node_name
        ).strip()
        step_execution_id = cursor_execution if on_cursor_step and step_id == cursor_step else ""
        group_id = cursor_group if step_execution_id else ""
        run_key = run_key or f"{step_id}:{step_execution_id}"
        now = _utc(self._clock())
        with self._lock:
            occurrence_row = self._run_steps.get(run_key)
            if occurrence_row is not None and not starting:
                step_id, step_execution_id, group_id, *_ = occurrence_row
            if starting or occurrence_row is None:
                occurrence = self._next_occurrence(
                    step_id=step_id, step_execution_id=step_execution_id
                )
                occurrence_row = (
                    step_id,
                    step_execution_id,
                    group_id,
                    occurrence,
                    now,
                    self._cursor_detail(payload) if step_execution_id else {},
                )
                self._run_steps[run_key] = occurrence_row
            _, _, _, occurrence, step_started_at, cursor_detail = occurrence_row
            if (
                starting
                and node_name == "human_approval_gate"
                and step_execution_id
                and str(payload.get("task_source") or "").strip().lower() in TEST_RUN_SOURCES
            ):
                self._test_approval_runs[run_key] = self._cursor_detail(payload)
            approval_detail = self._test_approval_runs.get(run_key)
            test_approval_run = approval_detail is not None
            if not starting:
                self._test_approval_runs.pop(run_key, None)
                if (
                    test_approval_run
                    and node_name == "human_approval_gate"
                    and status == "completed"
                    and payload.get("status") == "awaiting_approval"
                    and payload.get("needs_user_approval") is True
                ):
                    # Command.update is partial. The matching start captured the
                    # dispatched execution; the gate's return supplies only its pause.
                    status = "awaiting_approval"
            if status not in _ACTIVE_STEP_STATUSES or (not starting and test_approval_run):
                self._run_steps.pop(run_key, None)

        workflow_id = str(
            payload.get("workflow_id")
            or job.get("selected_workflow")
            or job.get("selected_workflow_key")
            or "unknown"
        )
        self._gateway.record_public_progress_transition(
            job_id=self._job_id,
            project_id=str(job.get("project_id") or self._project_id),
            workflow_id=workflow_id,
            # The step's pause is the run's pause, before the worker parks the job.
            job_status=status if status in DURABLE_WAIT_STATUSES else job.get("status"),
            occurred_at=now,
            queued_at=job.get("queued_at"),
            started_at=job.get("started_at"),
            step={
                "step_id": step_id,
                "step_execution_id": step_execution_id or None,
                **(approval_detail or cursor_detail),
                "group_id": group_id or None,
                "occurrence": occurrence,
                "display_name": payload.get("display_name"),
                "step_kind": str(payload.get("step_kind") or "node"),
                "status": status,
                "started_at": step_started_at,
                "finished_at": None if status in _ACTIVE_STEP_STATUSES else now,
                "child_job_id": payload.get("child_job_id"),
            },
        )
        return {
            "step_id": step_id,
            "step_execution_id": step_execution_id or None,
            "group_id": group_id or None,
            "occurrence": occurrence,
        }


__all__ = ["PublicProgressService", "PublicStepProgressRecorder"]
