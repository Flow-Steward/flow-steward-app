"""
Graph Nodes — adapters that convert BaseAgent.execute() → LangGraph node functions.

All agents run through PromptAgent — no custom Python implementations.
"""

from __future__ import annotations

import contextlib
import logging
import multiprocessing as mp
import queue
import time
import traceback
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from core.application.orchestration.graph import nodes_agent_runtime as _agent_runtime
from core.application.orchestration.graph import nodes_contracts as _contracts
from core.application.orchestration.graph.nodes_agent_runtime import (
    _agent_process_entry as _agent_process_entry,
)
from core.application.orchestration.graph.nodes_agent_runtime import (
    _cancel_poll_interval_seconds as _cancel_poll_interval_seconds,
)
from core.application.orchestration.graph.nodes_agent_runtime import (
    _coerce_float as _coerce_float,
)
from core.application.orchestration.graph.nodes_agent_runtime import (
    _get_observability_logger as _get_observability_logger,
)
from core.application.orchestration.graph.nodes_agent_runtime import (
    _heartbeat_interval_seconds as _heartbeat_interval_seconds,
)
from core.application.orchestration.graph.nodes_agent_runtime import (
    _process_join_grace_seconds as _process_join_grace_seconds,
)
from core.application.orchestration.graph.nodes_agent_runtime import (
    _serialize_agent_output as _serialize_agent_output,
)
from core.application.orchestration.graph.nodes_agent_runtime import (
    _terminate_agent_process as _terminate_agent_process,
)
from core.application.orchestration.graph.nodes_agent_runtime import (
    _to_builtin as _to_builtin,
)
from core.application.orchestration.graph.nodes_contracts import (
    _active_agent_step_skill_id as _active_agent_step_skill_id,
)
from core.application.orchestration.graph.nodes_contracts import (
    _agent_primary_skill_id as _agent_primary_skill_id,
)
from core.application.orchestration.graph.nodes_contracts import (
    _has_value as _has_value,
)
from core.application.orchestration.graph.nodes_contracts import (
    _load_skill_schema as _load_skill_schema,
)
from core.application.orchestration.graph.nodes_contracts import (
    _normalize_message_status as _normalize_message_status,
)
from core.application.orchestration.graph.nodes_contracts import (
    _required_fields_from_schema as _required_fields_from_schema,
)
from core.application.orchestration.graph.nodes_contracts import (
    _resolve_dotted as _resolve_dotted,
)
from core.application.orchestration.graph.nodes_contracts import (
    _resolve_required_input as _resolve_required_input,
)
from core.application.orchestration.graph.nodes_contracts import (
    _set_agent_step_output_state_path as _set_agent_step_output_state_path,
)
from core.application.orchestration.graph.nodes_contracts import (
    _skill_requires_request_input as _skill_requires_request_input,
)
from core.application.orchestration.graph.nodes_contracts import (
    _synthesized_request_from_materialized_inputs as _synthesized_request_from_materialized_inputs,
)
from core.application.orchestration.graph.nodes_step_context import (
    extract_step_context as _extract_step_context,
)
from core.application.orchestration.graph.nodes_step_context import (
    extract_task_context as _extract_task_context,
)
from core.application.orchestration.graph.nodes_step_context import (
    get_current_skill as _get_current_skill,
)
from core.application.orchestration.graph.nodes_step_context import (
    resolve_agent_timeout_seconds as _resolve_agent_timeout_seconds,
)
from core.application.orchestration.graph.nodes_step_dispatch import (
    execute_step_node as _execute_step_node,
)
from core.application.orchestration.models import is_pass_phase_status, normalize_phase_status

if TYPE_CHECKING:
    from core.application.orchestration.agents.base import BaseAgent
    from core.application.orchestration.state import OrchestratorState
    from core.application.registry.agent_manifest import AgentManifest

STATE_PREFIX = "state."


logger = logging.getLogger(__name__)


class AgentHardCancelled(Exception):
    """Raised when running agent execution is hard-cancelled."""


class AgentExecutionError(RuntimeError):
    """RuntimeError that preserves child stderr payload for callers."""

    def __init__(self, message: str, *, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


def load_agent_implementation(
    manifest: AgentManifest, tool_registry=None, skill_registry=None
) -> BaseAgent:
    """Load a PromptAgent for the given manifest.

    All agents are YAML-driven — no custom Python implementations.
    """
    from core.application.orchestration.agents.prompt_agent import PromptAgent

    logger.info("Loading PromptAgent for '%s'", manifest.agent_id)
    return PromptAgent(manifest, tool_registry=tool_registry, skill_registry=skill_registry)


def wrap_agent_as_node(agent: BaseAgent, manifest: AgentManifest):
    """Convert a BaseAgent.execute() method into a LangGraph node function.

    The returned function takes OrchestratorState and returns a state update dict.
    """
    from core.application.orchestration.active_step import step_message_fields
    from core.application.orchestration.agent_messages import build_agent_message
    from core.application.orchestration.agents.base import AgentInput
    from core.application.orchestration.agents.common import (
        emit_event,
        publish_agent_message,
        update_runtime_truth,
    )

    def node_fn(state: OrchestratorState) -> dict:
        # The active step of the run cursor, read from the pinned definition.
        step_context = _extract_step_context(state)
        task_context = _extract_task_context(state)
        active_step = step_context.get("_step") if isinstance(step_context, dict) else None
        active_step = active_step if isinstance(active_step, dict) else {}
        message_identity = step_message_fields(state)
        started_at = datetime.now(UTC).isoformat()
        message_correlation_id = uuid.uuid4().hex
        caused_by = _build_message_causal_links(state)
        from core.application.orchestration.workflow_state_selector_contract import (
            RUNTIME_ERROR_CODE,
            validate_step_state_selectors,
        )

        selector_issues = validate_step_state_selectors(active_step)
        if selector_issues:
            issue = selector_issues[0]
            completed_at = datetime.now(UTC).isoformat()
            message = build_agent_message(
                from_agent=manifest.agent_id,
                to_agent="orchestrator",
                **message_identity,
                skill=str(
                    active_step.get("skill") or active_step.get("skill_id") or "unknown"
                ).strip(),
                status="fail",
                correlation_id=message_correlation_id,
                caused_by=caused_by,
                artifacts={
                    "error": RUNTIME_ERROR_CODE,
                    "selector": issue["selector"],
                    "path": issue["path"].removeprefix("step."),
                    "step_kind": str(active_step.get("step_kind") or "agent"),
                },
                summary="Agent step has an invalid input selector",
                errors=[RUNTIME_ERROR_CODE],
                started_at=started_at,
                completed_at=completed_at,
                published_at=completed_at,
            )
            return {"agent_messages": [*(state.get("agent_messages") or []), message]}
        _materialize_agent_step_input_mapping(state)
        state_ref = dict(state)
        state_ref["_message_correlation_id"] = message_correlation_id
        if caused_by:
            state_ref["_message_caused_by"] = [dict(x) for x in caused_by]

        agent_input = AgentInput(
            task_context=task_context,
            step_context=step_context,
            agent_config=manifest.config or {},
            skill=_get_current_skill(state, manifest),
            feedback=state.get("feedback"),
            state_ref=state_ref,
        )

        update_runtime_truth(
            state,
            current_agent=manifest.agent_id,
            selected_workflow=state.get("workflow_id", ""),
        )
        emit_event(
            state,
            manifest.agent_id,
            "iteration_start",
            f"{manifest.display_name} started",
            {"skill": agent_input.skill},
        )

        input_contract_errors = _validate_skill_input_contract(
            state=state,
            skill=agent_input.skill,
        )
        if input_contract_errors:
            completed_at = datetime.now(UTC).isoformat()
            summary = (
                f"Missing or invalid required inputs for skill '{agent_input.skill}': "
                f"{'; '.join(input_contract_errors[:5])}"
            )
            emit_event(
                state,
                manifest.agent_id,
                "error",
                f"{manifest.display_name} input contract validation failed",
                {"errors": input_contract_errors[:20], "skill": agent_input.skill},
                severity="error",
            )
            update_runtime_truth(
                state,
                current_agent=manifest.agent_id,
                selected_workflow=state.get("workflow_id", ""),
                last_error=summary[:16000],
            )
            message = build_agent_message(
                from_agent=manifest.agent_id,
                to_agent="orchestrator",
                **message_identity,
                skill=agent_input.skill or "unknown",
                status="fail",
                correlation_id=message_correlation_id,
                caused_by=caused_by,
                artifacts={
                    "_contract_errors": input_contract_errors,
                    "_input_validation_failed": True,
                    "step_kind": str(active_step.get("step_kind") or "agent"),
                    "step_id": str(
                        active_step.get("step_id") or active_step.get("id") or ""
                    ).strip(),
                    "step_display_name": str(active_step.get("display_name") or "").strip(),
                },
                summary=summary,
                errors=input_contract_errors,
                started_at=started_at,
                completed_at=completed_at,
                published_at=completed_at,
            )
            publish_agent_message(state, message)
            return {"agent_messages": [*(state.get("agent_messages") or []), message]}

        captured_contract = None
        producer_context = None
        try:
            if state.get("workflow_definition_ref"):
                from core.application.orchestration.output_contract_receipt import (
                    OutputProducerContext,
                    capture_agent_output_contract,
                )

                producer_context = OutputProducerContext.for_state(state)
                captured_contract, selected_skill = capture_agent_output_contract(
                    agent, manifest, agent_input
                )
                agent_input.output_contract = captured_contract
                agent_input.selected_skill = selected_skill
            timeout_seconds = _resolve_agent_timeout_seconds(manifest)
            output = _execute_agent_with_timeout(
                agent=agent,
                agent_input=agent_input,
                timeout_seconds=timeout_seconds,
                agent_id=manifest.agent_id,
                state=state,
            )
        except AgentHardCancelled as e:
            # Propagate cancellation to the worker so job status becomes cancelled,
            # not a normal needs_rework retry.
            from core.application.orchestration.agents.common import JobCancelledException

            raise JobCancelledException(str(e)) from e
        except Exception as e:
            tb = traceback.format_exc()
            stderr_text = ""
            stderr_from_error = getattr(e, "stderr", "")
            if stderr_from_error:
                stderr_text = str(stderr_from_error)
            else:
                cause = e.__cause__
                if cause is not None:
                    stderr_from_cause = getattr(cause, "stderr", "")
                    if stderr_from_cause:
                        stderr_text = str(stderr_from_cause)

            raw_output_ref = ""
            if state.get("job_id"):
                raw_output_ref = f"job://{state.get('job_id')}/last_response_excerpt"

            logger.exception("Agent '%s' raised exception", manifest.agent_id)
            emit_event(
                state,
                manifest.agent_id,
                "error",
                f"{manifest.display_name} failed with exception",
                {
                    "error": str(e),
                    "traceback": tb,
                    "stderr_excerpt": stderr_text[:2000] if stderr_text else "",
                    "raw_output_ref": raw_output_ref,
                },
                severity="error",
            )
            update_runtime_truth(
                state,
                current_agent=manifest.agent_id,
                selected_workflow=state.get("workflow_id", ""),
                last_error=(tb if tb else str(e))[:16000],
            )
            message = build_agent_message(
                from_agent=manifest.agent_id,
                to_agent="orchestrator",
                **message_identity,
                skill=agent_input.skill or "unknown",
                status="fail",
                correlation_id=message_correlation_id,
                caused_by=caused_by,
                artifacts={
                    "error": str(e),
                    "traceback": tb,
                    "stderr": stderr_text,
                    "raw_output_ref": raw_output_ref,
                    "step_kind": str(active_step.get("step_kind") or "agent"),
                    "step_id": str(
                        active_step.get("step_id") or active_step.get("id") or ""
                    ).strip(),
                    "step_display_name": str(active_step.get("display_name") or "").strip(),
                },
                summary=str(e),
                errors=[str(e)],
                started_at=started_at,
                completed_at=datetime.now(UTC).isoformat(),
                published_at=datetime.now(UTC).isoformat(),
            )
            publish_agent_message(state, message)
            return {"agent_messages": [*(state.get("agent_messages") or []), message]}

        # Estimate cost (heuristic — will be 0 if no usage data)
        cost_usd = output.metadata.get("cost_usd", 0.0)
        tokens = output.metadata.get("tokens", 0)

        emit_event(
            state,
            manifest.agent_id,
            "phase_complete",
            f"{manifest.display_name} completed: {output.status}",
            {
                "status": output.status,
                "confidence": output.confidence,
                "summary": output.summary[:200],
                "cost_usd": cost_usd,
                "tokens": tokens,
            },
        )

        output_contract_errors: list[str] = []
        if is_pass_phase_status(output.status):
            output_contract_errors = (
                captured_contract.validate(output.artifacts)
                if captured_contract is not None
                else _validate_skill_output_contract(
                    skill=agent_input.skill, artifacts=output.artifacts
                )
            )
            if output_contract_errors:
                existing_contract_errors = output.artifacts.get("_contract_errors")
                merged_contract_errors: list[str] = []
                if isinstance(existing_contract_errors, list):
                    merged_contract_errors.extend(
                        [str(x) for x in existing_contract_errors if str(x).strip()]
                    )
                merged_contract_errors.extend(output_contract_errors)
                output.artifacts["_contract_errors"] = list(dict.fromkeys(merged_contract_errors))

        message_status = _normalize_message_status(output.status)
        completed_at = datetime.now(UTC).isoformat()
        schema_errors = output.artifacts.get("_schema_errors")
        contract_errors = output.artifacts.get("_contract_errors")
        merged_errors: list[str] = []
        if isinstance(schema_errors, list):
            merged_errors.extend([str(x) for x in schema_errors if str(x).strip()])
        if isinstance(contract_errors, list):
            merged_errors.extend([str(x) for x in contract_errors if str(x).strip()])
        if message_status != "pass":
            execution_error = str(output.artifacts.get("error") or "").strip()
            if execution_error:
                merged_errors.append(execution_error)
        merged_errors = list(dict.fromkeys(merged_errors))
        if merged_errors:
            message_status = "fail"
        output.artifacts.setdefault("step_kind", str(active_step.get("step_kind") or "agent"))
        if not output.artifacts.get("input_payload"):
            mapped_inputs = _contracts.agent_step_input_preview(state, active_step)
            if mapped_inputs:
                output.artifacts["input_payload"] = mapped_inputs
        output.artifacts.setdefault(
            "step_id",
            str(active_step.get("step_id") or active_step.get("id") or "").strip(),
        )
        output.artifacts.setdefault(
            "step_display_name",
            str(active_step.get("display_name") or "").strip(),
        )

        state_updates: dict[str, Any] = {}
        if message_status == "pass":
            _apply_agent_step_output_mapping(
                step=active_step,
                artifacts=output.artifacts,
                state=state,
                state_updates=state_updates,
            )

        receipt = None
        if (
            message_status == "pass"
            and captured_contract is not None
            and producer_context is not None
        ):
            from core.application.orchestration.output_contract_receipt import build_output_receipt

            receipt = build_output_receipt(
                captured_contract,
                producer_context,
                identity=message_identity,
                agent_id=manifest.agent_id,
                skill_id=agent_input.skill,
            )
        message = build_agent_message(
            from_agent=manifest.agent_id,
            to_agent="orchestrator",
            **message_identity,
            skill=agent_input.skill or "unknown",
            status=message_status,
            output_contract=receipt,
            correlation_id=message_correlation_id,
            caused_by=caused_by,
            artifacts={**output.artifacts},
            operation_retry=getattr(output, "operation_retry_signal", None),
            summary=output.summary,
            errors=merged_errors,
            started_at=started_at,
            completed_at=completed_at,
            published_at=completed_at,
        )
        publish_agent_message(state, message)

        # Update state with append-only message stream.
        state_updates = {
            **state_updates,
            "agent_messages": [*(state.get("agent_messages") or []), message],
        }

        if message_status == "pass":
            update_runtime_truth(
                state,
                current_agent=manifest.agent_id,
                selected_workflow=state.get("workflow_id", ""),
                last_response_excerpt=output.summary[:1000] if output.summary else "",
                clear_last_error=True,
            )
        else:
            update_runtime_truth(
                state,
                current_agent=manifest.agent_id,
                selected_workflow=state.get("workflow_id", ""),
                last_response_excerpt=output.summary[:1000] if output.summary else "",
                last_error=(("; ".join(merged_errors[:20])) or output.summary or output.status)[
                    :16000
                ],
            )

        return state_updates

    node_fn.__name__ = manifest.agent_id
    return node_fn


def execute_step_node(state: OrchestratorState) -> dict:
    """Execute a pinned step using the graph's agent and clock dependencies."""
    from core.application.orchestration.returned_files_runtime import with_returned_files

    return with_returned_files(
        state,
        _execute_step_node(
            state,
            load_agent_implementation=load_agent_implementation,
            wrap_agent_as_node=wrap_agent_as_node,
            now_iso=lambda: datetime.now(UTC).isoformat(),
            logger=logger,
        ),
    )


def _execute_agent_with_timeout(
    agent: BaseAgent,
    agent_input,
    timeout_seconds: int,
    agent_id: str,
    state: OrchestratorState | None = None,
):
    """Execute agent with hard timeout/cancel enforcement.

    Execution runs in a child process so timeout/cancel can terminate work
    deterministically even if the agent is blocked in Python code.
    """
    job_id = str(state.get("job_id") or "").strip() if isinstance(state, dict) else ""

    # Fast path for local/test runs where we do not need hard control.
    if timeout_seconds <= 0 and not job_id:
        return agent.execute(agent_input)

    start = time.monotonic()
    deadline = start + timeout_seconds if timeout_seconds > 0 else None
    heartbeat_interval = _heartbeat_interval_seconds()
    cancel_poll_interval = _cancel_poll_interval_seconds()
    next_heartbeat = start + heartbeat_interval
    next_cancel_check = start + cancel_poll_interval

    start_method = "fork" if "fork" in mp.get_all_start_methods() else mp.get_start_method()
    ctx = mp.get_context(start_method)
    ctx_runtime = cast(Any, ctx)
    result_queue = ctx_runtime.Queue(maxsize=1)
    proc = cast(
        mp.Process,
        ctx_runtime.Process(
            target=_agent_process_entry,
            args=(agent, agent_input, result_queue),
            daemon=True,
            name=f"agent-runner-{agent_id}",
        ),
    )
    proc.start()

    payload = None
    try:
        while True:
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                _terminate_agent_process(proc, reason=f"timeout ({timeout_seconds}s)")
                raise TimeoutError(f"Agent '{agent_id}' timed out after {timeout_seconds}s")

            if job_id and now >= next_cancel_check:
                if _is_job_cancelled(job_id):
                    _terminate_agent_process(proc, reason="job cancelled")
                    raise AgentHardCancelled(
                        f"Agent '{agent_id}' cancelled for job {job_id}; execution terminated"
                    )
                next_cancel_check = now + cancel_poll_interval

            if job_id and isinstance(state, dict) and now >= next_heartbeat:
                _emit_agent_heartbeat(state, agent_id=agent_id)
                next_heartbeat = now + heartbeat_interval

            try:
                payload = result_queue.get(timeout=0.2)
                break
            except queue.Empty:
                payload = None

            if not proc.is_alive():
                break

        if payload is not None:
            proc.join(timeout=0.2)

        if not isinstance(payload, dict):
            exit_code = proc.exitcode
            if exit_code not in (0, None):
                raise RuntimeError(
                    f"Agent '{agent_id}' process exited unexpectedly with code {exit_code}"
                )
            raise RuntimeError(f"Agent '{agent_id}' exited without returning a result")

        if payload.get("ok"):
            output_raw = payload.get("output")
            output = output_raw if isinstance(output_raw, dict) else {}
            return SimpleNamespace(
                status=normalize_phase_status(output.get("status")),
                artifacts=output.get("artifacts")
                if isinstance(output.get("artifacts"), dict)
                else {},
                summary=str(output.get("summary") or ""),
                confidence=float(output.get("confidence") or 0.0),
                metadata=output.get("metadata") if isinstance(output.get("metadata"), dict) else {},
                operation_retry_signal=(
                    output.get("operation_retry_signal")
                    if isinstance(output.get("operation_retry_signal"), dict)
                    else None
                ),
            )

        error_text = str(payload.get("error") or f"Agent '{agent_id}' failed")
        child_traceback = str(payload.get("traceback") or "")
        stderr_text = str(payload.get("stderr") or "")
        if child_traceback:
            error_text = f"{error_text}\n{child_traceback}"
        raise AgentExecutionError(error_text, stderr=stderr_text)
    finally:
        with contextlib.suppress(Exception):
            if proc.is_alive():
                _terminate_agent_process(proc, reason="cleanup")
            proc.join(timeout=0.2)
        with contextlib.suppress(Exception):
            result_queue.close()


def _sync_contract_helpers() -> None:
    _contracts._get_observability_logger = _get_observability_logger
    _contracts._load_skill_schema = _load_skill_schema
    _contracts._active_agent_step_skill_id = _active_agent_step_skill_id
    _contracts._required_fields_from_schema = _required_fields_from_schema
    _contracts._resolve_required_input = _resolve_required_input
    _contracts._resolve_dotted = _resolve_dotted
    _contracts._has_value = _has_value
    _contracts._set_agent_step_output_state_path = _set_agent_step_output_state_path
    _contracts._synthesized_request_from_materialized_inputs = (
        _synthesized_request_from_materialized_inputs
    )


def _build_message_causal_links(state: OrchestratorState) -> list[dict]:
    _sync_contract_helpers()
    return _contracts._build_message_causal_links(state)


def _materialize_agent_step_input_mapping(state: OrchestratorState) -> None:
    _sync_contract_helpers()
    return _contracts._materialize_agent_step_input_mapping(state)


def _apply_agent_step_output_mapping(
    *,
    step: dict[str, Any],
    artifacts: dict[str, Any],
    state: OrchestratorState,
    state_updates: dict[str, Any],
) -> None:
    _sync_contract_helpers()
    return _contracts._apply_agent_step_output_mapping(
        step=step,
        artifacts=artifacts,
        state=state,
        state_updates=state_updates,
    )


def _validate_skill_input_contract(state: OrchestratorState, skill: str) -> list[str]:
    _sync_contract_helpers()
    return _contracts._validate_skill_input_contract(state, skill)


def _validate_skill_output_contract(skill: str, artifacts: Any) -> list[str]:
    _sync_contract_helpers()
    return _contracts._validate_skill_output_contract(skill, artifacts)


def _sync_agent_runtime_helpers() -> None:
    _agent_runtime._get_observability_logger = _get_observability_logger


def _is_job_cancelled(job_id: str) -> bool:
    _sync_agent_runtime_helpers()
    return _agent_runtime._is_job_cancelled(job_id)


def _emit_agent_heartbeat(state: OrchestratorState, *, agent_id: str) -> None:
    _sync_agent_runtime_helpers()
    return _agent_runtime._emit_agent_heartbeat(state, agent_id=agent_id)
