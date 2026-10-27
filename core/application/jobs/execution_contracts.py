"""Application contracts for durable worker job execution."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

GraphOutcomeKind = Literal["result", "cancelled", "generator_exit", "error"]


def _positive_occurrence(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return 0
    try:
        return max(0, int(value))
    except ValueError:
        return 0


def terminal_failure_context(
    result: Mapping[str, Any],
    *,
    observed: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve safe terminal failure identity from callbacks or handled graph state.

    Some graph nodes intentionally convert contract exceptions into state.  In that
    case LangGraph never emits ``on_chain_error``, so callback-only context is empty.
    The hub's ``unresolved_step_failure`` is the durable fallback for that path.
    """

    raw_handled = result.get("unresolved_step_failure")
    handled = raw_handled if isinstance(raw_handled, Mapping) else {}
    context: dict[str, Any] = {}
    for key in ("step_id", "step_execution_id", "group_id", "error_code"):
        value = str(handled.get(key) or "").strip()
        if value:
            context[key] = value
    occurrence = _positive_occurrence(handled.get("occurrence"))
    if occurrence > 0:
        context["occurrence"] = occurrence
    diagnostic_kind = str(handled.get("diagnostic_kind") or "").strip()
    selector = str(handled.get("selector") or "").strip()
    if diagnostic_kind in {"transform", "tabular_feed", "dataset_query"}:
        context["diagnostic_kind"] = diagnostic_kind
        if len(selector) <= 256 and (selector == "state" or selector.startswith("state.")):
            context["selector"] = selector
    if diagnostic_kind == "dataset_query":
        path = str(handled.get("path") or "").strip()
        correlation_id = str(handled.get("correlation_id") or "").strip()
        if (
            len(path) <= 256
            and re.fullmatch(r"[A-Za-z0-9_.\[\]-]+", path)
            and not any(token in path.lower() for token in ("password", "secret", "token"))
        ):
            context["path"] = path
        if len(correlation_id) <= 128 and re.fullmatch(r"[A-Za-z0-9_.:-]+", correlation_id):
            context["correlation_id"] = correlation_id

    # A callback observes the precise runtime occurrence, so it wins over the
    # state fallback when both are available.  Keep this surface deliberately
    # bounded: exception text and arbitrary state must not enter public errors.
    for key in ("step_id", "step_execution_id", "group_id"):
        value = str((observed or {}).get(key) or "").strip()
        if value:
            context[key] = value
    observed_occurrence = _positive_occurrence((observed or {}).get("occurrence"))
    if observed_occurrence > 0:
        context["occurrence"] = observed_occurrence
    return context


@dataclass(frozen=True)
class TaskClaimResult:
    """Result of the infrastructure-owned atomic job claim."""

    claimed: bool
    current_job: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class TaskExecutionRequest:
    """Normalized normal-delivery input passed to the graph adapter."""

    message: Mapping[str, Any]
    job_id: str
    task_id: str
    thread_id: str
    task_summary: str
    task_source: str
    project_name: str
    project_config: Mapping[str, Any]
    extra_state: Mapping[str, Any]
    workflow_id: str
    test_mode: bool


@dataclass(frozen=True)
class TaskGraphOutcome:
    """Technical graph observation interpreted by application policy."""

    kind: GraphOutcomeKind
    result: Mapping[str, Any] = field(default_factory=dict)
    pending_interrupt: bool = False
    interrupt_state: Any | None = None
    terminal_error_context: Mapping[str, Any] = field(default_factory=dict)
    error: Exception | None = None
    root_run_id: str | None = None

    @classmethod
    def from_result(
        cls,
        result: Mapping[str, Any],
        *,
        pending_interrupt: bool = False,
        interrupt_state: Any | None = None,
        terminal_error_context: Mapping[str, Any] | None = None,
        root_run_id: str | None = None,
    ) -> TaskGraphOutcome:
        return cls(
            kind="result",
            result=dict(result),
            pending_interrupt=bool(pending_interrupt),
            interrupt_state=interrupt_state,
            terminal_error_context=dict(terminal_error_context or {}),
            root_run_id=root_run_id,
        )

    @classmethod
    def cancelled(cls, *, root_run_id: str | None = None) -> TaskGraphOutcome:
        return cls(kind="cancelled", root_run_id=root_run_id)

    @classmethod
    def generator_exit(cls, *, root_run_id: str | None = None) -> TaskGraphOutcome:
        return cls(kind="generator_exit", root_run_id=root_run_id)

    @classmethod
    def failed(
        cls,
        error: Exception,
        *,
        root_run_id: str | None = None,
    ) -> TaskGraphOutcome:
        return cls(kind="error", error=error, root_run_id=root_run_id)


@dataclass(frozen=True)
class TaskNotification:
    project_id: str
    event_type: str
    severity: str
    workflow_id: str | None
    job_id: str
    payload: Mapping[str, Any]
    #: False for a moment only workflow reactions observe (a Wait timer
    #: starting): the project's notification routes are not sent it.
    dispatch_routes: bool = True


class TaskExecutionRepository(Protocol):
    """Persistence port for normal queue execution."""

    @property
    def available(self) -> bool: ...

    @property
    def project_lookup_available(self) -> bool: ...

    def get_job(self, job_id: str) -> Mapping[str, Any] | None: ...

    def get_project(self, project_id: str) -> Mapping[str, Any] | None: ...

    def persist_meta(self, job_id: str, meta: dict[str, Any]) -> None: ...

    def mark_delivery_terminal(self, job_id: str, *, status: str, error: str) -> None: ...

    def interrupted_step_snapshot(self, job_row: dict[str, Any]) -> dict[str, Any] | None: ...

    def mark_retryable_interrupted_step(
        self,
        job_id: str,
        job_row: dict[str, Any],
        snapshot: dict[str, Any] | None,
    ) -> bool: ...

    def in_progress_owner_has_active_lease(
        self,
        job_row: Mapping[str, Any],
    ) -> bool | None: ...

    def recover_in_progress(
        self,
        job_id: str,
        *,
        expected_worker_id: str,
        reason: str,
    ) -> bool: ...

    def claim(
        self,
        job_id: str,
        *,
        worker_id: str,
        allow_in_progress: bool,
    ) -> TaskClaimResult: ...

    def is_cancelled(self, job_id: str) -> bool: ...

    def merge_meta(self, job_id: str, patch: dict[str, Any]) -> None: ...

    def finish(
        self,
        job_id: str,
        *,
        status: str,
        final_report: Any,
        error: str | None,
        root_run_id: str | None,
    ) -> None: ...

    def release_lock(self, job_id: str, *, reason: str, worker_id: str) -> None: ...

    def log_execution_error(
        self,
        job_id: str,
        error: Exception,
        *,
        worker_id: str,
        task_id: str,
        thread_id: str,
        root_run_id: str | None,
    ) -> None: ...


class TaskGraphPort(Protocol):
    def run(
        self,
        request: TaskExecutionRequest,
        *,
        job_row: Mapping[str, Any] | None,
        project_id: str | None,
    ) -> TaskGraphOutcome: ...


class TaskTerminalEffectsPort(Protocol):
    def persist(
        self,
        request: TaskExecutionRequest,
        *,
        job_row: Mapping[str, Any] | None,
        project_id: str | None,
        final_status: str,
        result: dict[str, Any],
        graph_outcome: TaskGraphOutcome | None,
    ) -> str | None: ...

    def sync_provider(
        self,
        job_id: str,
        *,
        final_status: str,
        repository: TaskExecutionRepository,
    ) -> None: ...

    def notify(
        self,
        notification: TaskNotification,
        *,
        repository: TaskExecutionRepository,
    ) -> None: ...


class RuntimeModePort(Protocol):
    def activate(self, enabled: bool) -> AbstractContextManager[None]: ...


@dataclass(frozen=True)
class WorkflowResumeRequest:
    message: Mapping[str, Any]
    job_id: str
    thread_id: str
    resume_revision: int
    approval_id: str
    resume_value: Mapping[str, Any]
    approved: bool
    action: str | None
    comment: str
    interrupt_payload: Mapping[str, Any]
    actor_username: str


@dataclass(frozen=True)
class WorkflowResumeGraphOutcome:
    started: bool
    status: str
    retryable: bool
    reason: str
    result: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
    terminal: bool = False

    @classmethod
    def accepted(
        cls,
        *,
        status: str,
        result: Mapping[str, Any],
        terminal: bool = False,
    ) -> WorkflowResumeGraphOutcome:
        return cls(
            started=True,
            status=str(status or ""),
            retryable=False,
            reason="",
            result=dict(result),
            terminal=bool(terminal),
        )

    @classmethod
    def not_started(cls, *, reason: str) -> WorkflowResumeGraphOutcome:
        return cls(
            started=False,
            status="not_started",
            retryable=True,
            reason=str(reason or ""),
            error=str(reason or ""),
        )

    @classmethod
    def for_retry(cls, *, reason: str) -> WorkflowResumeGraphOutcome:
        return cls(
            started=True,
            status="retryable",
            retryable=True,
            reason=str(reason or ""),
            error=str(reason or ""),
        )


@dataclass(frozen=True)
class WorkflowResumeTerminalContext:
    job_id: str
    job_row: Mapping[str, Any]
    final_status: str
    result: Mapping[str, Any]
    resume_revision: int
    finalizer_token: str
    workflow_id: str = ""
    notification_event_type: str = ""
    notification_severity: str = "info"


class WorkflowResumeGraphPort(Protocol):
    def resume(
        self,
        request: WorkflowResumeRequest,
        *,
        cancelled: Callable[[], bool],
    ) -> WorkflowResumeGraphOutcome: ...


class LeaseHandle(Protocol):
    def cancelled(self) -> bool: ...

    def stop(self) -> None: ...


class WorkflowResumeLeasePort(Protocol):
    def start_execution(
        self,
        *,
        job_id: str,
        resume_revision: int,
        lease_token: str,
    ) -> LeaseHandle: ...

    def start_finalizer(
        self,
        *,
        job_id: str,
        resume_revision: int,
        finalizer_token: str,
    ) -> LeaseHandle: ...


class WorkflowResumeTerminalEffectsPort(Protocol):
    def release_lock(self, context: WorkflowResumeTerminalContext) -> None: ...

    def notify(self, context: WorkflowResumeTerminalContext) -> None: ...

    def sync_provider(self, context: WorkflowResumeTerminalContext) -> None: ...


ApprovalPublisher = Callable[..., Any]
TokenFactory = Callable[[], str]
PendingRows = Iterator[Mapping[str, Any]]
