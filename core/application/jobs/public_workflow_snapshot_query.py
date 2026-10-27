"""Application query for reconciling the durable public workflow projection."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from core.application.jobs.workflow_identity import resolve_job_workflow_id
from core.application.jobs.workflow_run_models import (
    PublicContainerProgressV1,
    PublicWorkflowStepV1,
    PublicWorkflowTraceV1,
    WorkflowRunStateV1,
)
from core.application.jobs.workflow_run_projection import (
    ArtifactResolverUnavailable,
    WorkflowRunProjectionService,
    approval_notification_payload_from_state,
    build_terminal_stop_error,
    ensure_single_approval_interrupt,
)
from core.application.management import ServiceError
from core.application.observability_vocabulary import job_status_meta, normalize_job_status

if TYPE_CHECKING:
    from core.application.orchestration.run_cursor import FrozenDefinition

_MESSAGE_PUBLIC_WORKFLOW_STATE_IS_UNAVAILABLE = "Public workflow state is unavailable"


@dataclass(frozen=True)
class _CommittedEvidence:
    values: Mapping[str, Any]
    definition: FrozenDefinition
    reference: dict[str, Any]


def _pinned_evidence(
    values: Mapping[str, Any], archive: Mapping[str, Any]
) -> _CommittedEvidence | None:
    from core.application.orchestration.run_cursor import FrozenDefinition
    from core.application.workflows.workflow_definition_snapshot import definition_digest

    reference = {
        "snapshot_id": str(archive.get("id") or ""),
        "revision": archive.get("revision"),
        "digest": archive.get("digest"),
    }
    definition = archive.get("definition_jsonb")
    if (
        not reference["snapshot_id"]
        or type(reference["revision"]) is not int
        or reference["revision"] < 1
        or values.get("workflow_definition_ref") != reference
        or not isinstance(definition, dict)
        or definition_digest(definition) != reference["digest"]
    ):
        return None
    return _CommittedEvidence(values, FrozenDefinition(definition), reference)


def _evidence_identity(
    evidence: _CommittedEvidence, snapshot: WorkflowRunStateV1
) -> dict[str, Any]:
    return {
        "job_id": snapshot.job_id,
        "project_id": snapshot.project_id,
        "workflow_id": snapshot.workflow_id,
        **evidence.reference,
    }


def _validated_trace(
    evidence: _CommittedEvidence, snapshot: WorkflowRunStateV1
) -> PublicWorkflowTraceV1 | None:
    from core.application.workflows.execution_graph import NodeRef, Outcome

    raw = evidence.values.get("workflow_run_trace")
    if raw is None:
        return None
    trace = PublicWorkflowTraceV1.model_validate(raw)
    if (
        trace.job_id != snapshot.job_id
        or trace.workflow_id != snapshot.workflow_id
        or any(getattr(trace, key) != value for key, value in evidence.reference.items())
    ):
        raise ValueError("Committed trace identity does not match the pinned job")
    for fact in trace.transitions:
        edge = evidence.definition.graph.edge_for(
            NodeRef(fact.source.kind, fact.source.id),
            Outcome(fact.outcome["name"], fact.outcome.get("case_id")),
        )
        if edge is None or edge.target.dump() != fact.target.model_dump(mode="json"):
            raise ValueError("Committed trace edge does not match the pinned definition")
    return trace


def _committed_containers(
    evidence: _CommittedEvidence,
    snapshot: WorkflowRunStateV1,
    trace: PublicWorkflowTraceV1 | None,
) -> PublicContainerProgressV1 | None:
    from core.application.orchestration.container_progress import committed_container_progress
    from core.application.orchestration.run_cursor import RunCursorError

    try:
        return committed_container_progress(
            evidence.values,
            evidence.definition,
            identity=_evidence_identity(evidence, snapshot),
            trace=trace,
        )
    except (TypeError, ValueError, RunCursorError):
        # A malformed optional aggregate must not stop D061 trace catch-up.
        return None


def _terminal_stop_operation(
    evidence: _CommittedEvidence, snapshot: WorkflowRunStateV1
) -> PublicWorkflowStepV1 | None:
    from core.application.jobs.public_progress_service import completed_stop_operation

    values = evidence.values
    terminal = values.get("workflow_terminal")
    cursor = values.get("run_cursor")
    if (
        not snapshot.terminal
        or snapshot.status != "failed"
        or values.get("status") != "failed"
        or values.get("unresolved_step_failure")
        or not isinstance(terminal, Mapping)
        or not isinstance(cursor, Mapping)
        or terminal.get("kind") != "stop_fail"
        or terminal.get("status") != "fail"
    ):
        return None
    step_id, execution_id = terminal.get("step_id"), terminal.get("step_execution_id")
    if (
        not isinstance(step_id, str)
        or not step_id.strip()
        or not isinstance(execution_id, str)
        or not execution_id.strip()
        or cursor.get("step_id") != step_id
        or cursor.get("step_execution_id") != execution_id
        or evidence.definition.step(step_id).get("step_kind") != "stop_fail"
    ):
        return None
    return completed_stop_operation(snapshot, step_id, execution_id)


class PublicWorkflowSnapshotQuery:
    def __init__(
        self,
        bl: Any,
        *,
        get_thread_state_snapshot: Callable[[str], Any],
        to_json_obj: Callable[[Any], Any],
        resolve_artifact: Callable[[str], dict[str, Any] | None] | None = None,
        get_committed_checkpoint_values: Callable[[str], Mapping[str, Any]] | None = None,
        load_definition_snapshot: Callable[[str], Mapping[str, Any] | None] | None = None,
    ) -> None:
        self._bl = bl
        self._get_thread_state_snapshot = get_thread_state_snapshot
        self._to_json_obj = to_json_obj
        self._resolve_artifact = resolve_artifact
        self._committed_values = get_committed_checkpoint_values
        self._load_definition = load_definition_snapshot

    def _checkpoint(self, job: dict[str, Any]) -> Any:
        thread_id = str(job.get("thread_id") or "").strip()
        state = self._get_thread_state_snapshot(thread_id) if thread_id else None
        if not state:
            raise ServiceError(_MESSAGE_PUBLIC_WORKFLOW_STATE_IS_UNAVAILABLE, status_code=503)
        return state

    def _approval_gate(self, state: Any) -> dict[str, Any] | None:
        return approval_notification_payload_from_state(state) or None

    def _committed_evidence(
        self, job: dict[str, Any], snapshot: WorkflowRunStateV1
    ) -> _CommittedEvidence | None:
        if self._committed_values is None:
            return None
        try:
            from core.application.jobs.workflow_definition_snapshot_repository import (
                WorkflowDefinitionSnapshotRepository,
            )

            values = deepcopy(self._committed_values(str(job.get("thread_id") or "")))
            if (
                not isinstance(values, Mapping)
                or values.get("job_id") != snapshot.job_id
                or values.get("workflow_id") != snapshot.workflow_id
                or ("project_id" in values and values.get("project_id") != snapshot.project_id)
            ):
                return None
            loader = (
                self._load_definition
                or WorkflowDefinitionSnapshotRepository(self._bl.conn).load_for_job
            )
            archive = deepcopy(loader(snapshot.job_id))
            if (
                not isinstance(archive, Mapping)
                or archive.get("workflow_id") != snapshot.workflow_id
            ):
                return None
            return _pinned_evidence(values, archive)
        except Exception:
            # Optional evidence cannot make an unavailable committed reader fail lifecycle reads.
            return None

    def _reconcile_trace(
        self, evidence: _CommittedEvidence | None, snapshot: WorkflowRunStateV1
    ) -> WorkflowRunStateV1:
        if evidence is None:
            return snapshot
        try:
            trace = _validated_trace(evidence, snapshot)
            containers = _committed_containers(evidence, snapshot, trace)
            if trace is not None:
                trace = trace.model_copy(
                    update={
                        "terminal": bool(job_status_meta(evidence.values.get("status"))["terminal"])
                    }
                )
            if trace is None and containers is None:
                return snapshot
            recorded = self._bl.reconcile_public_run_trace(
                job_id=snapshot.job_id,
                trace=trace,
                occurred_at=datetime.now(UTC),
                **({"container_progress": containers} if containers is not None else {}),
            )
            return recorded.snapshot
        except Exception:
            # A malformed optional trace cannot veto independent Stop proof.
            return snapshot

    def _reconcile_stop(
        self, evidence: _CommittedEvidence | None, snapshot: WorkflowRunStateV1
    ) -> WorkflowRunStateV1:
        if evidence is None:
            return snapshot
        try:
            operation = _terminal_stop_operation(evidence, snapshot)
            if operation is None:
                return snapshot
            error = build_terminal_stop_error(
                evidence.definition.step(operation.step_id), operation
            )
            recorded = self._bl.reconcile_public_terminal_stop(
                job_id=snapshot.job_id,
                error=error,
                occurred_at=datetime.now(UTC),
                identity=_evidence_identity(evidence, snapshot),
            )
            return recorded.snapshot
        except Exception:
            # Failed validation or atomic persistence leaves the durable lifecycle untouched.
            return snapshot

    def _current_snapshot(self, job: dict[str, Any]) -> WorkflowRunStateV1:
        job_id = str(job.get("id") or "").strip()
        try:
            current = self._bl.get_public_progress_snapshot(job_id)
        except LookupError:
            current = None
        except Exception as exc:
            raise ServiceError(
                _MESSAGE_PUBLIC_WORKFLOW_STATE_IS_UNAVAILABLE, status_code=503
            ) from exc

        snapshot = getattr(current, "snapshot", current) if current is not None else None
        marker_id = str(getattr(current, "marker_id", "") or "").strip()
        if (
            current is None
            or not isinstance(snapshot, WorkflowRunStateV1)
            or snapshot.job_id != job_id
            or not marker_id
        ):
            raise ServiceError(_MESSAGE_PUBLIC_WORKFLOW_STATE_IS_UNAVAILABLE, status_code=503)

        workflow_id = resolve_job_workflow_id(job)
        project_id = job.get("project_id")
        if isinstance(project_id, UUID):
            project_id = str(project_id)
        if ("project_id" in job and project_id != snapshot.project_id) or (
            workflow_id and workflow_id != snapshot.workflow_id
        ):
            raise ServiceError(_MESSAGE_PUBLIC_WORKFLOW_STATE_IS_UNAVAILABLE, status_code=503)
        return snapshot

    def get(
        self,
        job: dict[str, Any],
        *,
        approval_gate: dict[str, Any] | None = None,
        job_errors: list[dict[str, Any]] | None = None,
    ) -> WorkflowRunStateV1:
        snapshot = self._current_snapshot(job)
        evidence = self._committed_evidence(job, snapshot)
        snapshot = self._reconcile_trace(evidence, snapshot)
        snapshot = self._reconcile_lifecycle(job, snapshot, approval_gate, job_errors)
        return self._reconcile_stop(evidence, snapshot)

    def _reconcile_lifecycle(
        self,
        job: dict[str, Any],
        snapshot: WorkflowRunStateV1,
        approval_gate: dict[str, Any] | None,
        job_errors: list[dict[str, Any]] | None,
    ) -> WorkflowRunStateV1:
        resolved_approval_gate = dict(approval_gate) if approval_gate else None
        state_checked_for_approval = False
        job_status = normalize_job_status(job.get("status"))
        if snapshot.terminal and (snapshot.status != job_status or not job_errors):
            return snapshot
        if (
            not snapshot.terminal
            and job_status != "awaiting_approval"
            and snapshot.status == job_status
        ):
            if (
                job_status == "in_progress"
                and ensure_single_approval_interrupt(self._checkpoint(job)) is not None
            ):
                raise ServiceError(_MESSAGE_PUBLIC_WORKFLOW_STATE_IS_UNAVAILABLE, status_code=503)
            return snapshot
        if job_status == "awaiting_approval":
            state_checked_for_approval = True
            resolved_approval_gate = resolved_approval_gate or self._approval_gate(
                self._checkpoint(job)
            )

        if job_status == "awaiting_approval" and not state_checked_for_approval:
            resolved_approval_gate = resolved_approval_gate or self._approval_gate(
                self._checkpoint(job)
            )

        return self._project_lifecycle(job, snapshot, resolved_approval_gate, job_errors)

    def _project_lifecycle(
        self,
        job: dict[str, Any],
        snapshot: WorkflowRunStateV1,
        approval_gate: dict[str, Any] | None,
        job_errors: list[dict[str, Any]] | None,
    ) -> WorkflowRunStateV1:
        try:
            return WorkflowRunProjectionService(
                self._bl,
                resolve_artifact=self._resolve_artifact,
            ).get(
                job={
                    **job,
                    "project_id": snapshot.project_id,
                    "selected_workflow": snapshot.workflow_id,
                },
                approval_gate=approval_gate,
                job_errors=job_errors,
                force=snapshot.status != normalize_job_status(job.get("status")),
            )
        except (LookupError, ArtifactResolverUnavailable) as exc:
            raise ServiceError(
                _MESSAGE_PUBLIC_WORKFLOW_STATE_IS_UNAVAILABLE, status_code=503
            ) from exc


def reconcile_public_job_snapshot(
    repository: Any,
    job: dict[str, Any],
    *,
    get_thread_state_snapshot: Callable[[str], Any],
    to_json_obj: Callable[[Any], Any],
    resolve_artifact: Callable[[str], dict[str, Any] | None] | None = None,
    get_committed_checkpoint_values: Callable[[str], Mapping[str, Any]] | None = None,
) -> WorkflowRunStateV1:
    """Load supporting records and reconcile one durable public snapshot."""
    job_errors = None
    list_job_errors = getattr(repository, "list_job_errors", None)
    if callable(list_job_errors):
        try:
            rows = list_job_errors(str(job.get("id") or ""), limit=200)
            job_errors = rows or None if isinstance(rows, list) else None
        except Exception as exc:
            raise ServiceError(
                _MESSAGE_PUBLIC_WORKFLOW_STATE_IS_UNAVAILABLE, status_code=503
            ) from exc

    return PublicWorkflowSnapshotQuery(
        repository,
        get_thread_state_snapshot=get_thread_state_snapshot,
        to_json_obj=to_json_obj,
        resolve_artifact=resolve_artifact,
        get_committed_checkpoint_values=get_committed_checkpoint_values,
    ).get(dict(job), job_errors=job_errors)


__all__ = ["PublicWorkflowSnapshotQuery", "reconcile_public_job_snapshot"]
