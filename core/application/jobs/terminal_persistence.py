"""One atomic terminal persistence path for initial and resumed workflow runs."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.application.jobs.invoke_child_link_repository import request_child_cancels
from core.application.jobs.public_progress_repository import PublicProgressRepository
from core.application.jobs.public_progress_service import PublicProgressService
from core.application.jobs.workflow_run_models import (
    PublicWorkflowStepV1,
    WorkflowRunStateV1,
)
from core.application.jobs.workflow_run_projection import (
    build_public_error,
    build_public_result,
)
from core.application.observability_vocabulary import job_status_meta, normalize_job_status
from core.infrastructure.db.db_pool import transaction_connection

_MESSAGE_WORKFLOW_EXECUTION_DID_NOT_COMPLETE_SUCCESSFULLY = (
    "Workflow execution did not complete successfully"
)

# Two vocabularies, one total translation at the boundary — the decision this
# epic settled rather than letting it settle itself.
#
# Call sites speak of WHERE a run failed: queue, submission,
# execution, cancellation. That is the useful word at the raise point and it is
# kept. Consumers read a small, stable set of WHAT-KIND values, defined by
# _normalize_error_category: transient, contract, tool, validation, provider,
# human_blocked, unknown. Collapsing the two would either force raise sites to
# describe themselves in consumer words or grow the public vocabulary; both are
# worse than translating.
#
# What was wrong was that the translation was PARTIAL. `cancellation` and
# `queue` had no entry, fell through untranslated, and were normalised to
# `unknown` downstream — losing a kind the call site knew exactly. The table is
# now total over the vocabulary actually used, and a test holds it that way.
_DURABLE_CATEGORY_BY_CALL_SITE_KIND = {
    "execution": "contract",
    "output_contract": "contract",
    "submission": "contract",
    # An operator cancelling a run is not a malfunction; it is the one canonical
    # value that means a human stopped it.
    "cancellation": "human_blocked",
}


def _durable_category(raw_category: str, *, retryable: bool) -> str:
    """Translate a call-site failure kind onto the canonical durable vocabulary."""
    kind = str(raw_category or "").strip().lower()
    if kind == "queue":
        # `queue` legitimately spans two canonical kinds and cannot take a single
        # mapping: an unreadable dispatch envelope is a contract failure and is
        # raised non-retryable, while a failed publish is infrastructure and is
        # raised retryable. Let the raise site's own retryability decide.
        return "transient" if retryable else "contract"
    return _DURABLE_CATEGORY_BY_CALL_SITE_KIND.get(kind, kind)


@dataclass(frozen=True)
class TerminalPersistenceResult:
    status: str
    revision: int
    marker_id: str
    error_code: str | None = None


class TerminalPersistenceConflict(RuntimeError):
    """A competing writer already made the job terminal or changed its owner."""


class ArtifactIntegrityError(RuntimeError):
    """A declared public artifact cannot be authenticated for this job."""


class TerminalPersistenceService:
    def __init__(
        self,
        logger: Any,
        *,
        transaction_factory: Callable[..., Any] = transaction_connection,
        progress_repository_factory: Callable[[Any], Any] = PublicProgressRepository,
        now: Callable[[], datetime] | None = None,
        resolve_artifact: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self.logger = logger
        self.transaction_factory = transaction_factory
        self.progress_repository_factory = progress_repository_factory
        self.now = now or (lambda: datetime.now(UTC))
        self.resolve_artifact = resolve_artifact

    @staticmethod
    def _workflow_id(job: dict[str, Any]) -> str:
        raw_meta = job.get("meta")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        for value in (
            job.get("selected_workflow"),
            job.get("selected_workflow_key"),
            job.get("selected_workflow_id"),
            meta.get("selected_workflow_id"),
        ):
            normalized = str(value or "").strip()
            if normalized:
                return normalized
        return "unknown"

    def complete_success(
        self,
        *,
        job_id: str,
        workflow_outputs: dict[str, Any] | None,
        final_report: str | None = None,
        root_run_id: str | None = None,
        expected_worker_id: str | None = None,
    ) -> TerminalPersistenceResult:
        normalized_outputs = workflow_outputs if isinstance(workflow_outputs, dict) else {}
        try:
            json.dumps(normalized_outputs, allow_nan=False)
        except (TypeError, ValueError):
            return self.complete_failure(
                job_id=job_id,
                status="failed",
                error_message="Validated workflow outputs could not be persisted",
                error_code="output_persistence_failed",
                category="output_contract",
                expected_worker_id=expected_worker_id,
            )
        try:
            return self._persist(
                job_id=job_id,
                status="completed",
                workflow_outputs=normalized_outputs,
                final_report=final_report,
                root_run_id=root_run_id,
                expected_worker_id=expected_worker_id,
            )
        except ArtifactIntegrityError:
            return self.complete_failure(
                job_id=job_id,
                status="failed",
                error_message="Declared workflow artifact could not be authenticated",
                error_code="artifact_integrity_error",
                category="output_contract",
                expected_worker_id=expected_worker_id,
            )

    def complete_failure(
        self,
        *,
        job_id: str,
        status: str,
        error_message: str | None = None,
        error_code: str | None = None,
        category: str = "execution",
        retryable: bool = False,
        step_id: str | None = None,
        step_execution_id: str | None = None,
        group_id: str | None = None,
        occurrence: int | None = None,
        selector: str | None = None,
        diagnostic_kind: str | None = None,
        diagnostic_path: str | None = None,
        correlation_id: str | None = None,
        final_report: str | None = None,
        root_run_id: str | None = None,
        expected_worker_id: str | None = None,
        require_pending_unowned: bool = False,
        meta_patch: dict[str, Any] | None = None,
        cancel_children: bool = False,
    ) -> TerminalPersistenceResult:
        structured_error = {
            # No status substitution. `status` is what the job ended as, not why;
            # writing it here produced code="failed", a tautology that displaced
            # the projection's own fallback ladder (job_failed / cancelled /
            # retry_exhausted). An absent code is left absent so that ladder runs.
            "error_code": error_code or "",
            "error_message": error_message
            or _MESSAGE_WORKFLOW_EXECUTION_DID_NOT_COMPLETE_SUCCESSFULLY,
            "category": category,
            "retryable": retryable,
            "step_id": step_id,
            "step_execution_id": step_execution_id,
            "group_id": group_id,
            "occurrence": occurrence,
            "selector": selector,
            "diagnostic_kind": diagnostic_kind,
            "path": diagnostic_path,
            "correlation_id": correlation_id,
        }
        return self._persist(
            job_id=job_id,
            status=status,
            error_message=error_message,
            structured_error=structured_error,
            final_report=final_report,
            root_run_id=root_run_id,
            expected_worker_id=expected_worker_id,
            require_pending_unowned=require_pending_unowned,
            meta_patch=meta_patch,
            cancel_children=cancel_children,
        )

    @staticmethod
    def _failed_step(
        repository: Any,
        *,
        job_id: str,
        structured_error: dict[str, Any] | None,
        occurred_at: datetime,
    ) -> PublicWorkflowStepV1 | None:
        if not structured_error:
            return None
        step_id = str(structured_error.get("step_id") or "").strip()
        if not step_id:
            return None
        step_execution_id = str(structured_error.get("step_execution_id") or "").strip() or None
        group_id = str(structured_error.get("group_id") or "").strip() or None

        requested_occurrence = structured_error.get("occurrence")
        if isinstance(requested_occurrence, bool) or not isinstance(
            requested_occurrence, (int, str)
        ):
            occurrence = 0
        else:
            try:
                occurrence = int(requested_occurrence)
            except ValueError:
                occurrence = 0
        matching_steps: list[PublicWorkflowStepV1] = []
        stored = repository.load(job_id=job_id)
        if stored:
            snapshot = WorkflowRunStateV1.model_validate(stored["snapshot"])
            # The failed execution when it is known; otherwise every entry of the step.
            matching_steps = [
                step
                for step in snapshot.steps
                if step.step_id == step_id
                and (step_execution_id is None or step.step_execution_id == step_execution_id)
            ]
        if occurrence <= 0 and matching_steps:
            occurrence = max(step.occurrence for step in matching_steps)
        occurrence = max(1, occurrence)
        previous = next(
            (step for step in matching_steps if step.occurrence == occurrence),
            None,
        )
        structured_error["occurrence"] = occurrence
        if previous is not None:
            return previous.model_copy(update={"status": "failed", "finished_at": occurred_at})
        return PublicWorkflowStepV1(
            step_id=step_id,
            step_execution_id=step_execution_id,
            group_id=group_id,
            occurrence=occurrence,
            step_kind="node",
            status="failed",
            started_at=occurred_at,
            finished_at=occurred_at,
        )

    def _persist(
        self,
        *,
        job_id: str,
        status: str,
        workflow_outputs: dict[str, Any] | None = None,
        error_message: str | None = None,
        structured_error: dict[str, Any] | None = None,
        final_report: str | None = None,
        root_run_id: str | None = None,
        expected_worker_id: str | None = None,
        require_pending_unowned: bool = False,
        meta_patch: dict[str, Any] | None = None,
        cancel_children: bool = False,
    ) -> TerminalPersistenceResult:
        occurred_at = self.now().astimezone(UTC)
        with self.transaction_factory(self.logger.conn) as conn:
            self._assert_terminal_write_allowed(
                conn,
                job_id=job_id,
                expected_worker_id=expected_worker_id,
                require_pending_unowned=require_pending_unowned,
            )
            if status == "cancelled" or cancel_children:
                # Under the same row lock as the cancel: every unfinished child
                # invocation gets a durable cancel request before we answer.
                request_child_cancels(conn, parent_job_id=job_id)
            job = self.logger.get_job(job_id)
            if not isinstance(job, dict):
                raise LookupError(f"Job {job_id} does not exist")
            if meta_patch:
                self.logger.merge_job_meta(job_id, meta_patch)
                job = {
                    **job,
                    "meta": {
                        **(job.get("meta") if isinstance(job.get("meta"), dict) else {}),
                        **meta_patch,
                    },
                }
            if workflow_outputs is not None:
                self.logger.merge_job_meta(job_id, {"workflow_outputs": workflow_outputs})
                job = {
                    **job,
                    "meta": {
                        **(job.get("meta") if isinstance(job.get("meta"), dict) else {}),
                        "workflow_outputs": workflow_outputs,
                    },
                }
            progress_repository = self.progress_repository_factory(conn)
            terminal_step = self._failed_step(
                progress_repository,
                job_id=str(job_id),
                structured_error=structured_error,
                occurred_at=occurred_at,
            )
            public_result = build_public_result(
                job,
                status,
                resolve_artifact=self.resolve_artifact,
            )
            if workflow_outputs is not None and status == "completed":
                declared_artifacts = workflow_outputs.get("artifacts")
                declared_names = (
                    set(declared_artifacts) if isinstance(declared_artifacts, dict) else set()
                )
                projected_names = (
                    set(public_result.artifacts) if public_result is not None else set()
                )
                if declared_names != projected_names:
                    raise ArtifactIntegrityError(
                        "Declared workflow artifact could not be authenticated"
                    )
            public_error = build_public_error(
                {**job, "status": status, "last_error": error_message or ""},
                status,
                [structured_error] if structured_error else [],
            )
            if structured_error:
                log_job_error = getattr(self.logger, "log_job_error", None)
                if callable(log_job_error):
                    raw_category = str(structured_error.get("category") or "execution")
                    durable_category = _durable_category(
                        raw_category,
                        retryable=bool(structured_error.get("retryable", False)),
                    )
                    context_jsonb: dict[str, Any] = {}
                    stored_error_code = str(structured_error.get("error_code") or "").strip()
                    if stored_error_code:
                        context_jsonb["error_code"] = stored_error_code
                    if structured_error.get("occurrence") is not None:
                        context_jsonb["occurrence"] = structured_error["occurrence"]
                    if structured_error.get("selector"):
                        context_jsonb["selector"] = structured_error["selector"]
                    if structured_error.get("diagnostic_kind"):
                        context_jsonb["diagnostic_kind"] = structured_error["diagnostic_kind"]
                    if structured_error.get("path"):
                        context_jsonb["path"] = structured_error["path"]
                    if structured_error.get("correlation_id"):
                        context_jsonb["correlation_id"] = structured_error["correlation_id"]
                    log_job_error(
                        job_id,
                        source="system",
                        category=durable_category,
                        retryable=bool(structured_error.get("retryable", False)),
                        error_message=str(
                            structured_error.get("error_message")
                            or _MESSAGE_WORKFLOW_EXECUTION_DID_NOT_COMPLETE_SUCCESSFULLY
                        ),
                        context_jsonb=context_jsonb,
                        group_id=structured_error.get("group_id"),
                        step_id=structured_error.get("step_id"),
                        step_execution_id=structured_error.get("step_execution_id"),
                    )
                log_action = getattr(self.logger, "log_action", None)
                if callable(log_action):
                    log_action(
                        str(job.get("project_id") or ""),
                        "",
                        "",
                        agent="workflow_runtime",
                        action_type="workflow_terminal",
                        status=status,
                        error=str(
                            structured_error.get("error_message")
                            or _MESSAGE_WORKFLOW_EXECUTION_DID_NOT_COMPLETE_SUCCESSFULLY
                        ),
                        meta={
                            "error_code": str(structured_error.get("error_code") or status),
                            "category": str(structured_error.get("category") or "execution"),
                            "retryable": bool(structured_error.get("retryable", False)),
                            "step_id": structured_error.get("step_id"),
                            "step_execution_id": structured_error.get("step_execution_id"),
                            "group_id": structured_error.get("group_id"),
                            "occurrence": structured_error.get("occurrence"),
                            "selector": structured_error.get("selector"),
                            "diagnostic_kind": structured_error.get("diagnostic_kind"),
                            "path": structured_error.get("path"),
                            "correlation_id": structured_error.get("correlation_id"),
                        },
                        job_id=job_id,
                    )
            progress = PublicProgressService(progress_repository).record_transition(
                job_id=str(job_id),
                project_id=str(job.get("project_id") or ""),
                workflow_id=self._workflow_id(job),
                job_status=status,
                occurred_at=occurred_at,
                queued_at=job.get("queued_at"),
                started_at=job.get("started_at"),
                finished_at=occurred_at,
                step=terminal_step,
                result=public_result,
                error=public_error,
            )
            self.logger.finish_job(
                job_id,
                status=status,
                final_report=final_report,
                error=error_message,
                root_run_id=root_run_id,
            )
        return TerminalPersistenceResult(
            status=status,
            revision=progress.revision,
            marker_id=str(progress.marker_id or ""),
            error_code=public_error.code if public_error else None,
        )

    @staticmethod
    def _assert_terminal_write_allowed(
        conn: Any,
        *,
        job_id: str,
        expected_worker_id: str | None,
        require_pending_unowned: bool = False,
    ) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, worker_id FROM jobs WHERE id = %s FOR UPDATE",
                (str(job_id),),
            )
            row = cur.fetchone()
        if not row:
            raise LookupError(f"Job {job_id} does not exist")
        status = normalize_job_status(row.get("status") if isinstance(row, dict) else row[0])
        worker_id = str((row.get("worker_id") if isinstance(row, dict) else row[1]) or "").strip()
        if require_pending_unowned and (status != "pending" or worker_id):
            raise TerminalPersistenceConflict(f"Job {job_id} is no longer pending and unowned")
        if bool(job_status_meta(status)["terminal"]):
            raise TerminalPersistenceConflict(f"Job {job_id} is already terminal")
        expected = str(expected_worker_id or "").strip()
        if expected and worker_id != expected:
            raise TerminalPersistenceConflict(f"Job {job_id} worker ownership changed")


def cancel_job_terminally(logger: Any, job_id: str, *, reason: str, error_code: str) -> bool:
    """Cancel a job through the one terminal path; ``False`` if it already ended.

    Operator actions (watchdog kill, watchdog retry of a stuck job) keep their
    own error code and reason, and get what every cancel gets: the terminal
    fence and durable cancel requests for unfinished child invocations.
    """
    try:
        TerminalPersistenceService(logger).complete_failure(
            job_id=str(job_id),
            status="cancelled",
            error_message=reason,
            error_code=error_code,
            category="cancellation",
        )
    except TerminalPersistenceConflict:
        return False
    return True


__all__ = [
    "TerminalPersistenceConflict",
    "TerminalPersistenceResult",
    "TerminalPersistenceService",
    "cancel_job_terminally",
]
