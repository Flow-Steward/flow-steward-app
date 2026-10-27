from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from typing import Any

from core.application.management.errors import ServiceError
from core.application.management.scope import ScopeContext
from core.application.reports.job_detail_lifecycle import (
    _build_lifecycle_contract,
    _status_terminal,
)
from core.application.reports.job_detail_payloads import (
    _DETAIL_EXECUTION_STEP_PAGE_LIMIT,
    _decode_execution_step_cursor,
    _execution_step_page_limit,
    _execution_step_page_meta,
    _payload_dict,
    _payload_nested_dict,
    _payload_rows,
    _section_warning,
    _workflow_run_payload,
    _workflow_run_step_window,
)
from core.application.reports.job_trigger import job_trigger
from core.infrastructure.security.integration import reuse_resolved_policies

from .job_detail_contract_projection import (
    _TOP_LEVEL_JOB_FIELDS as _TOP_LEVEL_JOB_FIELDS,
)
from .job_detail_contract_projection import (
    _TOP_LEVEL_PROJECTION_FIELDS as _TOP_LEVEL_PROJECTION_FIELDS,
)
from .job_detail_contract_projection import _build_contract_payload
from .job_detail_contract_projection import (
    _build_job_bucket as _build_job_bucket,
)
from .job_detail_contract_projection import (
    _projection_with_canonical_step as _projection_with_canonical_step,
)
from .job_detail_sections import (
    build_execution_steps,
    build_job_detail_sections,
)
from .ui_projection import project_job_detail
from .ui_projection_helpers import _artifact_descriptors

_MISSING = object()
_LOGGER = logging.getLogger("platform.reports.job_detail_contract")
_DETAIL_RUNTIME_JOURNAL_LIMIT = 100
_DETAIL_APPROVAL_HISTORY_LIMIT = 100


async def _invoke_service_method(
    service: Any,
    method_name: str,
    *args: Any,
    scope: ScopeContext | None | object = _MISSING,
    default: Any = None,
    **kwargs: Any,
) -> Any:
    handler = getattr(service, method_name, None)
    if not callable(handler):
        return default

    call_kwargs = dict(kwargs)
    if scope is not _MISSING:
        call_kwargs["scope"] = scope

    try:
        result = handler(*args, **call_kwargs)
    except TypeError as exc:
        message = str(exc)
        if (
            scope is _MISSING
            or "scope" not in call_kwargs
            or "scope" not in message
            or "unexpected keyword argument" not in message
        ):
            raise
        call_kwargs.pop("scope", None)
        result = handler(*args, **call_kwargs)

    if inspect.isawaitable(result):
        return await result
    return result


def _overlay_job_fields(job: dict[str, Any], status_payload: dict[str, Any]) -> dict[str, Any]:
    merged = dict(job)
    for key in (
        "id",
        "status",
        "project_id",
        "thread_id",
        "task_summary",
        "mode",
        "selected_workflow",
        "selected_workflow_display_name",
        "selected_workflow_source",
        "workflow_selection_reason",
        "started_at",
        "finished_at",
        "duration_ms",
        "current_step_id",
        "current_group_id",
        "current_agent",
        "failure_summary",
        "invocation",
    ):
        value = status_payload.get(key)
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def _project_invocation_artifact_downloads(
    job: dict[str, Any],
    *,
    resolve_artifact: Callable[[str], Any] | None,
) -> None:
    """Replace owned invocation artifact summaries with authorized UI descriptors."""
    if not callable(resolve_artifact):
        return
    invocation = _payload_nested_dict(job, "invocation")
    files = invocation.get("files")
    if not isinstance(files, list) or not files:
        return
    descriptors = _artifact_descriptors(
        files,
        role="input",
        project_id=str(job.get("project_id") or "").strip(),
        job_id=str(job.get("id") or "").strip(),
        resolve_artifact=resolve_artifact,
    )
    by_artifact_id = {
        str(row.get("artifact_id") or "").strip(): row
        for row in descriptors
        if str(row.get("artifact_id") or "").strip()
    }
    if not by_artifact_id:
        return
    invocation["files"] = [
        by_artifact_id.get(str(row.get("artifact_id") or "").strip(), dict(row))
        if isinstance(row, dict)
        else row
        for row in files
    ]
    job["invocation"] = invocation


def _merge_runtime_payload(
    runtime_payload: dict[str, Any],
    status_payload: dict[str, Any],
) -> dict[str, Any]:
    runtime = dict(runtime_payload)
    status_runtime = status_payload.get("runtime")
    if isinstance(status_runtime, dict):
        runtime.update(status_runtime)
    current_step_id = str(status_payload.get("current_step_id") or "").strip()
    current_agent = str(status_payload.get("current_agent") or "").strip()
    if current_step_id and not str(runtime.get("current_step_id") or "").strip():
        runtime["current_step_id"] = current_step_id
        runtime["current_group_id"] = status_payload.get("current_group_id") or None
    if current_agent and not str(runtime.get("active_agent") or "").strip():
        runtime["active_agent"] = current_agent
    selected_workflow = str(status_payload.get("selected_workflow") or "").strip()
    if selected_workflow and not str(runtime.get("selected_workflow") or "").strip():
        runtime["selected_workflow"] = selected_workflow
    return runtime


def _set_current_step(
    *,
    step_id: str,
    group_id: str | None,
    status_payload: dict[str, Any],
    runtime: dict[str, Any],
    job: dict[str, Any],
) -> None:
    for bucket in (status_payload, runtime, job):
        bucket["current_step_id"] = step_id
        bucket["current_group_id"] = group_id


def _canonicalize_current_step(
    *,
    status_payload: dict[str, Any],
    runtime: dict[str, Any],
    job: dict[str, Any],
) -> str:
    """Give status, runtime and job one current step and the group it runs in.

    The group travels with the step it came from: a step id from one source
    and a group id from another would name a scope the run was never in.
    """
    for source in (status_payload, runtime, job):
        step_id = str(source.get("current_step_id") or "").strip()
        if step_id:
            group_id = str(source.get("current_group_id") or "").strip() or None
            _set_current_step(
                step_id=step_id,
                group_id=group_id,
                status_payload=status_payload,
                runtime=runtime,
                job=job,
            )
            return step_id
    return ""


def _overlay_workflow_run_fallbacks(
    *,
    workflow_run: dict[str, Any] | None,
    status_payload: dict[str, Any],
    runtime: dict[str, Any],
    job: dict[str, Any],
) -> None:
    """Backfill sparse job-detail rows from the validated durable run view."""
    if not isinstance(workflow_run, dict):
        return

    workflow_id = str(workflow_run.get("workflow_id") or "").strip()
    if workflow_id and not str(status_payload.get("selected_workflow") or "").strip():
        status_payload["selected_workflow"] = workflow_id
        status_payload["selected_workflow_source"] = "workflow_run.workflow_id"
        status_payload["workflow_selection_reason"] = (
            "Resolved from the durable public workflow-run projection."
        )
        job.setdefault("selected_workflow", workflow_id)

    for field in ("started_at", "finished_at"):
        value = workflow_run.get(field)
        if value not in (None, "") and status_payload.get(field) in (None, ""):
            status_payload[field] = value
            job.setdefault(field, value)

    if str(status_payload.get("current_step_id") or "").strip():
        return
    scope: dict[str, Any] = {}
    error = workflow_run.get("error")
    if isinstance(error, dict) and str(error.get("step_id") or "").strip():
        scope = error
    else:
        steps = workflow_run.get("steps")
        if isinstance(steps, list):
            scope = next(
                (
                    step
                    for step in reversed(steps)
                    if isinstance(step, dict) and str(step.get("step_id") or "").strip()
                ),
                {},
            )
    step_id = str(scope.get("step_id") or "").strip()
    if step_id:
        _set_current_step(
            step_id=step_id,
            group_id=str(scope.get("group_id") or "").strip() or None,
            status_payload=status_payload,
            runtime=runtime,
            job=job,
        )


async def build_job_detail_contract(
    *,
    job_id: str,
    scope: ScopeContext | None,
    job_query_service: Any,
    approval_query_service: Any | None = None,
    get_thread_state_values: Callable[[str], dict[str, Any]] | None = None,
    user_role: str = "viewer",
    resolve_artifact: Callable[[str], Any] | None = None,
    execution_step_limit: int | None = None,
    include_workflow_run: bool = True,
) -> dict[str, Any]:
    """Build one job's detail contract without holding the caller's event loop.

    AC17-R155: every store this reads is synchronous and the diagnostics preview is
    CPU-bound, and callers are `async def` routes -- so one finished 200-row job held
    the API's event loop for 1.4-2 s, and /health/live waited behind it. The build runs
    in a worker thread on its own loop, and resolves each security policy once.
    """
    arguments = {
        "job_id": job_id,
        "scope": scope,
        "job_query_service": job_query_service,
        "approval_query_service": approval_query_service,
        "get_thread_state_values": get_thread_state_values,
        "user_role": user_role,
        "resolve_artifact": resolve_artifact,
        "execution_step_limit": execution_step_limit,
        "include_workflow_run": include_workflow_run,
    }
    return await asyncio.to_thread(_build_job_detail_contract_in_thread, arguments)


def _build_job_detail_contract_in_thread(arguments: dict[str, Any]) -> dict[str, Any]:
    with reuse_resolved_policies():
        return asyncio.run(_build_job_detail_contract(**arguments))


async def _build_job_detail_contract(
    *,
    job_id: str,
    scope: ScopeContext | None,
    job_query_service: Any,
    approval_query_service: Any | None = None,
    get_thread_state_values: Callable[[str], dict[str, Any]] | None = None,
    user_role: str = "viewer",
    resolve_artifact: Callable[[str], Any] | None = None,
    execution_step_limit: int | None = None,
    include_workflow_run: bool = True,
) -> dict[str, Any]:
    normalized_job_id = str(job_id or "").strip()
    compact_detail = execution_step_limit is not None and not include_workflow_run
    summary_only = compact_detail and int(execution_step_limit or 0) <= 0
    section_warnings: list[dict[str, str]] = []
    status_payload = _payload_dict(
        await _invoke_service_method(
            job_query_service,
            "get_job_status",
            normalized_job_id,
            scope=scope,
            include_workflow_result=True,
            default={},
        )
    )
    if compact_detail:
        compact_message_view: dict[str, Any] = {}
        if not summary_only:
            compact_message_view = _payload_dict(
                await _invoke_service_method(
                    job_query_service,
                    "list_messages",
                    normalized_job_id,
                    before_id="",
                    newest=True,
                    limit=_execution_step_page_limit(execution_step_limit),
                    scope=scope,
                    default={},
                )
            )
        logs_payload = {
            "job": status_payload,
            "runtime": _payload_nested_dict(status_payload, "runtime"),
            "messages": _payload_rows(compact_message_view, "messages"),
            "message_has_more": bool(compact_message_view.get("has_more")),
            "events": [],
            "decision_overrides": [],
            "job_errors": [],
        }
    else:
        logs_payload = _payload_dict(
            await _invoke_service_method(
                job_query_service,
                "get_logs_page",
                normalized_job_id,
                initial_limit=50,
                scope=scope,
                default={},
            )
        )

    job = _overlay_job_fields(
        _payload_nested_dict(logs_payload, "job"),
        status_payload,
    )
    _project_invocation_artifact_downloads(job, resolve_artifact=resolve_artifact)
    if not job.get("trigger"):
        job["trigger"] = job_trigger(job)
    if isinstance(job.get("invocation"), dict):
        status_payload["invocation"] = dict(job["invocation"])
    runtime = _merge_runtime_payload(
        _payload_nested_dict(logs_payload, "runtime"),
        status_payload,
    )
    _canonicalize_current_step(
        status_payload=status_payload,
        runtime=runtime,
        job=job,
    )
    messages = _payload_rows(logs_payload, "messages")
    events = _payload_rows(logs_payload, "events")
    decision_overrides = _payload_rows(logs_payload, "decision_overrides")
    job_errors = _payload_rows(logs_payload, "job_errors")
    project_name = str(logs_payload.get("project_name") or "").strip()
    failure_reason = str(logs_payload.get("job_failure_reason") or "").strip()
    normalized_status = str(status_payload.get("status") or job.get("status") or "").strip().lower()
    cost_breakdown_raw: dict[str, Any] = {}
    if not compact_detail:
        cost_breakdown_raw = _payload_dict(
            await _invoke_service_method(
                job_query_service,
                "get_cost_breakdown",
                normalized_job_id,
                scope=scope,
                default={},
            )
        )
    runtime_journal: list[dict[str, Any]] = []
    if not compact_detail:
        runtime_journal_payload = _payload_dict(
            await _invoke_service_method(
                job_query_service,
                "get_runtime_journal",
                normalized_job_id,
                limit=_DETAIL_RUNTIME_JOURNAL_LIMIT,
                scope=scope,
                default={},
            )
        )
        runtime_journal = _payload_rows(runtime_journal_payload, "runtime_journal")
    include_full_diagnostics = not compact_detail and (
        normalized_status
        in {
            "awaiting_approval",
            "resume_pending",
        }
        or _status_terminal(normalized_status)
    )

    causal_chain: dict[str, Any] = {}
    incident: dict[str, Any] = {}
    if include_full_diagnostics:
        # The complete causal graph has its own lazy diagnostics endpoint.
        # Embedding it here duplicates raw message artifacts throughout the
        # default job-detail projection and makes successful bulk jobs slow to
        # open. Diagnostics still derive a bounded causal summary from the
        # already-loaded messages until the drawer requests the full graph.
        incident = _payload_dict(
            await _invoke_service_method(
                job_query_service,
                "get_incident_drilldown",
                normalized_job_id,
                scope=scope,
                default={},
            )
        )

    approval_gate: dict[str, Any] = {}
    approval_history: list[dict[str, Any]] = []
    include_approval_context = (
        normalized_status
        in {
            "awaiting_approval",
            "resume_pending",
        }
        or include_full_diagnostics
    )
    if approval_query_service is not None and include_approval_context:
        try:
            gate_view = _payload_dict(
                await _invoke_service_method(
                    approval_query_service,
                    "get_current_gate",
                    normalized_job_id,
                    scope=scope,
                    default={},
                )
            )
            if isinstance(gate_view.get("payload"), dict):
                approval_gate = dict(gate_view["payload"])
            if approval_gate or gate_view.get("thread_id"):
                approval_gate["thread_id"] = str(
                    gate_view.get("thread_id") or job.get("thread_id") or ""
                ).strip()
        except Exception as exc:
            status_code = int(getattr(exc, "status_code", 0) or 0)
            error_payload = getattr(exc, "payload", None)
            resumed_without_gate = (
                normalized_status == "resume_pending"
                and isinstance(error_payload, dict)
                and error_payload.get("code") == "approval_not_awaiting"
            )
            if (
                status_code == 409
                and not _status_terminal(normalized_status)
                and not resumed_without_gate
            ):
                raise
            if status_code == 409:
                # An accepted resume or durable terminal job no longer awaits approval even
                # when its checkpoint still contains the gate that preceded
                # completion/failure.  That expected conflict must not make
                # the entire terminal job detail endpoint unreadable.
                approval_gate = {}
            else:
                _LOGGER.warning(
                    "job_detail_contract: approval gate unavailable for job %s: %s",
                    normalized_job_id,
                    exc,
                )
                section_warnings.append(
                    _section_warning(
                        section="approval_gate",
                        code="approval_gate_unavailable",
                        message="Approval gate details could not be loaded for this job.",
                        error=exc,
                    )
                )
                approval_gate = {}
        try:
            history_view = _payload_dict(
                await _invoke_service_method(
                    approval_query_service,
                    "list_history",
                    normalized_job_id,
                    limit=_DETAIL_APPROVAL_HISTORY_LIMIT,
                    scope=scope,
                    default={},
                )
            )
            approval_history = _payload_rows(history_view, "approvals")
        except Exception as exc:
            _LOGGER.warning(
                "job_detail_contract: approval history unavailable for job %s: %s",
                normalized_job_id,
                exc,
            )
            section_warnings.append(
                _section_warning(
                    section="approval_history",
                    code="approval_history_unavailable",
                    message="Approval history could not be loaded for this job.",
                    error=exc,
                )
            )
            approval_history = []

    try:
        workflow_run_view = None
        if not summary_only:
            workflow_run_view = await _invoke_service_method(
                job_query_service,
                "get_workflow_run",
                normalized_job_id,
                scope=scope,
                approval_gate=approval_gate,
                job_errors=job_errors,
                default=None,
            )
        workflow_run = _workflow_run_payload(workflow_run_view)
    except ServiceError as exc:
        # The public projection is built from the graph checkpoint, which is
        # briefly absent while a step transitions — exactly the moment after an
        # approval decision, when the operator who just decided is looking at
        # this job. Letting that fail the whole response hid status, logs and
        # approvals behind a 503 for a job that plainly exists. Degrade this one
        # section and say so, instead of withholding everything.
        _LOGGER.warning(
            "job_detail_contract: workflow run projection unavailable for job %s: %s",
            normalized_job_id,
            exc,
        )

        workflow_run = None
        section_warnings.append(
            _section_warning(
                section="workflow_run",
                code="workflow_run_unavailable",
                message=(
                    "The public workflow projection is not available yet; "
                    "the rest of this job is current."
                ),
                error=exc,
            )
        )

    _overlay_workflow_run_fallbacks(
        workflow_run=workflow_run,
        status_payload=status_payload,
        runtime=runtime,
        job=job,
    )
    _canonicalize_current_step(
        status_payload=status_payload,
        runtime=runtime,
        job=job,
    )

    thread_state: dict[str, Any] = {}
    if callable(get_thread_state_values):
        try:
            loaded = get_thread_state_values(str(job.get("thread_id") or ""))
            if isinstance(loaded, dict):
                thread_state = dict(loaded)
        except Exception as exc:
            _LOGGER.warning(
                "job_detail_contract: thread state unavailable for job %s: %s",
                normalized_job_id,
                exc,
            )
            section_warnings.append(
                _section_warning(
                    section="thread_state",
                    code="thread_state_unavailable",
                    message="Thread state could not be loaded for this job.",
                    error=exc,
                )
            )
            thread_state = {}

    section_workflow_run = workflow_run
    execution_page_window: dict[str, Any] | None = None
    if execution_step_limit is not None and not summary_only:
        page_limit = _execution_step_page_limit(execution_step_limit)
        section_workflow_run, execution_page_window = _workflow_run_step_window(
            workflow_run,
            limit=page_limit,
        )

    sections = build_job_detail_sections(
        job=job,
        runtime=runtime,
        thread_state=thread_state,
        approval_gate=approval_gate,
        approval_history=approval_history,
        decision_overrides=decision_overrides,
        cost_breakdown=cost_breakdown_raw,
        causal_chain=causal_chain,
        incident=incident,
        job_errors=job_errors,
        messages=messages,
        workflow_run=section_workflow_run,
        events=events,
        runtime_journal=runtime_journal,
        user_role=user_role,
        resolve_artifact=resolve_artifact,
    )
    if execution_page_window is not None:
        execution_steps = sections.get("execution_steps")
        if not isinstance(execution_steps, list):
            execution_steps = []
        sections["execution_steps_page"] = _execution_step_page_meta(
            page_window=execution_page_window,
            messages=messages,
            message_has_more=bool(logs_payload.get("message_has_more")),
            count=len(execution_steps),
        )
    if compact_detail:
        # Advanced diagnostics already load from their dedicated lazy endpoints.
        # Keeping their complete runtime/causal rows in the initial response would
        # duplicate most of the payload and delay the first useful render.
        sections["diagnostics"] = {}
    if summary_only:
        sections["execution_steps"] = []
        sections["selected_step"] = None
    lifecycle = _build_lifecycle_contract(
        normalized_job_id=normalized_job_id,
        status_payload=status_payload,
        runtime=runtime,
        incident=incident,
        approval_gate=approval_gate,
        approval_history=approval_history,
        sections=sections,
    )
    projection = project_job_detail(
        job=job,
        runtime=runtime,
        thread_state=thread_state,
        approval_gate=approval_gate,
        approval_history=approval_history,
        decision_overrides=decision_overrides,
        cost_breakdown=cost_breakdown_raw,
        causal_chain=causal_chain,
        job_errors=job_errors,
        messages=messages,
        events=events,
        project_name=project_name,
        failure_reason=failure_reason,
        user_role=user_role,
        incident=incident,
        runtime_journal=runtime_journal,
        lifecycle=lifecycle,
        sections=sections,
        include_sections=False,
    )

    return _build_contract_payload(
        job=job,
        status_payload=status_payload,
        runtime=runtime,
        projection=projection,
        lifecycle=lifecycle,
        sections=sections,
        approval_gate=approval_gate,
        approval_history=approval_history,
        causal_chain=causal_chain,
        cost_breakdown_raw=cost_breakdown_raw,
        decision_overrides=decision_overrides,
        incident=incident,
        job_errors=job_errors,
        runtime_journal=runtime_journal,
        section_warnings=section_warnings,
        thread_state=thread_state,
        workflow_run=workflow_run if include_workflow_run else None,
    )


async def build_job_execution_steps_page(
    *,
    job_id: str,
    scope: ScopeContext | None,
    job_query_service: Any,
    limit: int = _DETAIL_EXECUTION_STEP_PAGE_LIMIT,
    cursor: str = "",
    resolve_artifact: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Return one bounded durable execution-history page for lazy Job Details rendering."""
    normalized_job_id = str(job_id or "").strip()
    page_limit = _execution_step_page_limit(limit)
    status_payload = _payload_dict(
        await _invoke_service_method(
            job_query_service,
            "get_job_status",
            normalized_job_id,
            scope=scope,
            default={},
        )
    )
    workflow_run_view = await _invoke_service_method(
        job_query_service,
        "get_workflow_run",
        normalized_job_id,
        scope=scope,
        approval_gate={},
        job_errors=[],
        default=None,
    )
    workflow_run = _workflow_run_payload(workflow_run_view)
    total_durable_steps = len((workflow_run or {}).get("steps") or [])
    before_index, message_before_id = _decode_execution_step_cursor(
        cursor,
        total_steps=total_durable_steps,
    )
    section_workflow_run, page_window = _workflow_run_step_window(
        workflow_run,
        limit=page_limit,
        before_index=before_index,
    )

    messages_payload = _payload_dict(
        await _invoke_service_method(
            job_query_service,
            "list_messages",
            normalized_job_id,
            before_id=message_before_id,
            newest=True,
            limit=page_limit,
            scope=scope,
            default={},
        )
    )
    messages = _payload_rows(messages_payload, "messages")
    execution_steps, _selected = build_execution_steps(
        job=status_payload,
        runtime=status_payload,
        messages=messages,
        workflow_run=section_workflow_run,
        resolve_artifact=resolve_artifact,
    )
    page = _execution_step_page_meta(
        page_window=page_window,
        messages=messages,
        message_has_more=bool(messages_payload.get("has_more")),
        count=len(execution_steps),
    )
    # The run's own error (code, message, failing step) from the public
    # workflow-run contract, so Job Details can say why a run broke without the
    # full run projection the compact detail leaves out.
    run_error = (workflow_run or {}).get("error")
    payload = {
        "job_id": normalized_job_id,
        "steps": execution_steps,
        "page": page,
        "run_error": run_error if isinstance(run_error, dict) else None,
        "run_terminal": bool((workflow_run or {}).get("terminal")),
    }
    if not cursor:
        trace = (workflow_run or {}).get("run_trace")
        if (
            isinstance(trace, dict)
            and trace.get("job_id") == normalized_job_id
            and trace.get("workflow_id") == (workflow_run or {}).get("workflow_id")
        ):
            payload["run_trace"] = trace
        containers = (workflow_run or {}).get("container_progress")
        if (
            isinstance(containers, dict)
            and containers.get("job_id") == normalized_job_id
            and containers.get("project_id") == (workflow_run or {}).get("project_id")
            and containers.get("workflow_id") == (workflow_run or {}).get("workflow_id")
        ):
            payload["container_progress"] = containers
        children = _payload_dict(
            await _invoke_service_method(
                job_query_service,
                "get_child_invocations",
                normalized_job_id,
                scope=scope,
                default={},
            )
        )
        payload["child_invocations"] = _payload_rows(children, "invocations")
        payload["child_invocations_truncated"] = bool(children.get("truncated"))
    return payload
