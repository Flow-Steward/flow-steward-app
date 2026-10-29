"""Dispatch pinned workflow steps and publish their bounded diagnostic results."""

from __future__ import annotations

import copy
import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from core.application.orchestration.graph.nodes_contracts import _agent_primary_skill_id
from core.application.orchestration.retry_policy import normalize_retry_signal
from core.application.orchestration.step_io_preview import step_io_payload, step_io_preview

if TYPE_CHECKING:
    from core.application.orchestration.agents.base import BaseAgent
    from core.application.orchestration.state import OrchestratorState
    from core.application.registry.agent_manifest import AgentManifest


_STEP_DEBUG_VALUE_MAX_BYTES = 2_048


def _bounded_step_debug_value(value: Any) -> dict[str, Any]:
    """Return ``{"value": ...}`` for small values and a size summary otherwise.

    Deterministic step results stay canonical in state; the append-only message
    channel only carries enough to show the step in the debugger, so bulk values
    are summarized instead of being copied into every checkpoint.
    """
    try:
        size = len(json.dumps(value, default=str, separators=(",", ":")))
    except (TypeError, ValueError):
        size = _STEP_DEBUG_VALUE_MAX_BYTES + 1
    if size <= _STEP_DEBUG_VALUE_MAX_BYTES:
        return {"value": copy.deepcopy(value)}
    return {"value_bytes": size, "value_truncated": True}


def _tabular_output_preview(result: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Columns, the first rows and the row count of what a tabular step produced.

    A dataset keeps its schema and first rows from when it was written, so the
    preview never reads the data again; inline rows are cut to the same size.
    """
    from core.application.orchestration.step_io_preview import table_preview

    outputs: dict[str, Any] = {
        key: result[key]
        for key in ("dataset_handle", "artifact_handle", "result_count")
        if result.get(key) not in (None, "")
    }
    handle = str(result.get("dataset_handle") or "").strip()
    plane = state.get("_artifact_plane") or state.get("artifact_plane")
    meta = None
    if handle and plane is not None and callable(getattr(plane, "get_dataset", None)):
        try:
            meta = plane.get_dataset(handle.removeprefix("dataset:"))
        except Exception:
            meta = None
    if isinstance(meta, dict):
        outputs.update(
            table_preview(
                meta.get("preview_rows"),
                total=meta.get("row_count"),
                columns=meta.get("schema"),
            )
        )
    elif isinstance(result.get("rows"), list):
        outputs.update(table_preview(result["rows"]))
    return outputs


def execute_step_node(
    state: OrchestratorState,
    *,
    load_agent_implementation: Callable[[AgentManifest, Any, Any], BaseAgent],
    wrap_agent_as_node: Callable[[BaseAgent, AgentManifest], Callable[[OrchestratorState], dict]],
    now_iso: Callable[[], str],
    logger: logging.Logger,
) -> dict:
    """Generic step executor for direct agent steps.

    Resolves ``step.agent_id`` and executes it through the standard
    wrap_agent_as_node path.
    """
    from core.application.orchestration.active_step import active_step
    from core.application.orchestration.agent_messages import build_agent_message
    from core.application.orchestration.agents.common import publish_agent_message
    from core.application.orchestration.helpers import resolve_step_agent
    from core.application.workflows.workflow_step_kinds import (
        STEP_KIND_AGENT,
        STEP_KIND_CONNECTOR,
        STEP_KIND_DATASET_QUERY,
        STEP_KIND_FILE_FROM_BASE64,
        STEP_KIND_HTTP_REQUEST,
        STEP_KIND_TABULAR_FEED,
        STEP_KIND_TEMPLATE,
        STEP_KIND_TRANSFORM,
        normalize_step_kind,
        runtime_actor_for_step,
    )

    current = active_step(state)
    if current is None:
        logger.error("execute_step_node: the run cursor has no active step")
        return {}

    # A private copy: executors may adjust nested config, the pinned definition never changes.
    step = copy.deepcopy(current.step)
    # Every result names the exact execution and attempt it answers; the hub
    # accepts nothing else as this step's result.
    message_identity = current.message_identity()
    step_kind = normalize_step_kind(step.get("step_kind"), default=STEP_KIND_AGENT)
    actor_id = runtime_actor_for_step(step, default="")
    step_skill = step_kind
    step_id = current.step_id
    step_display_name = str(step.get("display_name") or "").strip()
    step_started_at = now_iso()

    def _to_object(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return {}
            try:
                parsed = json.loads(text)
            except Exception:
                return {}
            return dict(parsed) if isinstance(parsed, dict) else {}
        return {}

    def _set_path(
        update: dict[str, Any], source_state: dict[str, Any], target: str, value: Any
    ) -> None:
        from core.application.orchestration.step_templating import set_state_path

        set_state_path(update, source_state, target, value)

    def _mapping_delta(previous: Any, current: Any) -> dict[str, Any]:
        """Return only mapping values added or changed by this step.

        Connector output mapping keeps a complete ``workflow_data`` mirror in
        the runtime state for selector compatibility. That full mirror belongs
        in the checkpoint, but copying it into every append-only diagnostic
        message makes finite bulk loops grow quadratically.
        """
        if not isinstance(current, dict):
            return {}
        before = previous if isinstance(previous, dict) else {}
        delta: dict[str, Any] = {}
        for key, value in current.items():
            if key not in before:
                delta[key] = copy.deepcopy(value)
                continue
            old_value = before[key]
            if isinstance(old_value, dict) and isinstance(value, dict):
                nested = _mapping_delta(old_value, value)
                if nested:
                    delta[key] = nested
            elif old_value != value:
                delta[key] = copy.deepcopy(value)
        return delta

    def _diagnostic_state_patch(state_updates: dict[str, Any]) -> dict[str, Any]:
        """Keep the canonical patch live while persisting a bounded delta."""
        diagnostic = dict(state_updates)
        workflow_data = diagnostic.get("workflow_data")
        if isinstance(workflow_data, dict):
            delta = _mapping_delta(state.get("workflow_data"), workflow_data)
            if delta:
                diagnostic["workflow_data"] = delta
            else:
                diagnostic.pop("workflow_data", None)
        return diagnostic

    def _append_step_message(
        *,
        status: str,
        summary: str,
        artifacts: dict | None = None,
        operation_retry: dict | None = None,
        errors: Any = None,
        bound_dataset_page_message: bool = False,
    ) -> dict:
        from core.application.orchestration.step_failures import safe_step_error_texts

        timestamp = now_iso()
        safe_artifacts = dict(artifacts or {})
        safe_artifacts.setdefault("step_kind", step_kind)
        if step_id:
            safe_artifacts.setdefault("step_id", step_id)
        if step_display_name:
            safe_artifacts.setdefault("step_display_name", step_display_name)
        if operation_deadline_epoch_seconds > 0:
            safe_artifacts["operation_deadline_epoch_seconds"] = operation_deadline_epoch_seconds
        message = build_agent_message(
            from_agent=actor_id or "execute_step",
            to_agent="orchestrator",
            **message_identity,
            skill=step_skill,
            status=status,
            correlation_id=uuid.uuid4().hex,
            caused_by=[],
            artifacts=safe_artifacts,
            operation_retry=operation_retry,
            summary=summary,
            errors=safe_step_error_texts(errors),
            started_at=step_started_at,
            completed_at=timestamp,
            published_at=timestamp,
        )
        if bound_dataset_page_message:
            from core.application.orchestration.dataset_cursor_runtime import (
                MAX_DATASET_DIAGNOSTIC_BYTES,
            )

            def _message_size(candidate: dict[str, Any]) -> int:
                try:
                    return len(
                        json.dumps(
                            candidate,
                            ensure_ascii=False,
                            sort_keys=True,
                        ).encode("utf-8")
                    )
                except (TypeError, ValueError, OverflowError, RecursionError):
                    return MAX_DATASET_DIAGNOSTIC_BYTES + 1

            if _message_size(message) > MAX_DATASET_DIAGNOSTIC_BYTES:
                dataset_page = safe_artifacts.get("dataset_page")
                dataset_page = dataset_page if isinstance(dataset_page, dict) else {}

                def _bounded_nonnegative_int(value: Any) -> int:
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        return min(value, (1 << 63) - 1)
                    return 0

                bounded_page = {
                    "dataset_handle": str(dataset_page.get("dataset_handle") or "")[:128],
                    "page_ordinal": _bounded_nonnegative_int(dataset_page.get("page_ordinal")),
                    "item_count": _bounded_nonnegative_int(dataset_page.get("item_count")),
                    "digest": str(dataset_page.get("digest") or "")[:64],
                }
                connector_diagnostic = safe_artifacts.get("connector_result")
                connector_diagnostic = (
                    connector_diagnostic if isinstance(connector_diagnostic, dict) else {}
                )
                bounded_artifacts: dict[str, Any] = {
                    "step_kind": STEP_KIND_CONNECTOR,
                    "dataset_page": bounded_page,
                    "connector_result": {
                        "ok": bool(connector_diagnostic.get("ok")),
                        "runtime": str(connector_diagnostic.get("runtime") or "unresolved")[:128],
                        "detail": "dataset_page_diagnostic_truncated",
                    },
                }
                if safe_artifacts.get("error"):
                    bounded_artifacts["error"] = str(safe_artifacts["error"])[:128]
                bounded_retry = None
                if isinstance(operation_retry, dict):
                    bounded_retry = {
                        key: operation_retry[key]
                        for key in (
                            "schema_version",
                            "provenance",
                            "signal",
                            "deadline_epoch_seconds",
                        )
                        if key in operation_retry
                    }
                    bounded_retry["step_id"] = str(operation_retry.get("step_id") or "")[:128]
                message = build_agent_message(
                    from_agent=(actor_id or "execute_step")[:128],
                    to_agent="orchestrator",
                    **message_identity,
                    skill=STEP_KIND_CONNECTOR,
                    status=status,
                    correlation_id=uuid.uuid4().hex,
                    caused_by=[],
                    artifacts=bounded_artifacts,
                    operation_retry=bounded_retry,
                    summary="Dataset page connector diagnostics exceeded the durable-message budget",
                    errors=[],
                    started_at=step_started_at,
                    completed_at=timestamp,
                    published_at=timestamp,
                )
                if _message_size(message) > MAX_DATASET_DIAGNOSTIC_BYTES:
                    message = build_agent_message(
                        from_agent="connector_step",
                        to_agent="orchestrator",
                        **message_identity,
                        skill=STEP_KIND_CONNECTOR,
                        status=status,
                        correlation_id=uuid.uuid4().hex,
                        caused_by=[],
                        artifacts={
                            "step_kind": STEP_KIND_CONNECTOR,
                            "dataset_page": bounded_page,
                            "connector_result": {
                                "ok": bool(connector_diagnostic.get("ok")),
                                "runtime": "unresolved",
                                "detail": "dataset_page_diagnostic_truncated",
                            },
                        },
                        operation_retry=None,
                        summary="Dataset page diagnostics truncated",
                        errors=[],
                        started_at=step_started_at,
                        completed_at=timestamp,
                        published_at=timestamp,
                    )
        publish_agent_message(state, message)
        update = {"agent_messages": [*(state.get("agent_messages") or []), message]}
        if dispatch_matches:
            # The checkpointed dispatch is single-use. Clearing it with the
            # attempt prevents a later visit to the same workflow step from
            # inheriting an expired deadline.
            update["operation_retry_dispatch"] = None
        return update

    def _host_retry_envelope(*, provenance: str, result: dict[str, Any]) -> dict | None:
        signal = normalize_retry_signal(result)
        if not signal:
            return None
        envelope: dict[str, Any] = {
            "schema_version": "operation_retry_signal_v1",
            "provenance": provenance,
            "signal": signal,
            "step_id": step_id,
        }
        if operation_deadline_epoch_seconds > 0:
            envelope["deadline_epoch_seconds"] = operation_deadline_epoch_seconds
        operation_loop = result.get("operation_loop")
        if isinstance(operation_loop, dict):
            envelope["operation_loop"] = dict(operation_loop)
        return envelope

    def _compact_deterministic_result(
        result: dict[str, Any],
        *,
        result_target: str,
        result_key: str,
    ) -> dict[str, Any]:
        """Project a deterministic runtime result into bounded diagnostics.

        The canonical result remains in the node's top-level state update. Keeping
        it in the append-only message channel as well makes each checkpoint copy
        the same bulk value repeatedly.
        """
        compact = {
            key: copy.deepcopy(result.get(key))
            for key in ("ok", "runtime", "detail", "operation", "action_id", "errors")
            if key in result
        }
        compact["result_target"] = result_target
        value = result.get(result_key)
        if isinstance(value, list):
            compact["result_count"] = len(value)
        elif isinstance(value, dict):
            rows = value.get("rows")
            if isinstance(rows, list):
                compact["result_count"] = len(rows)
            for key in ("artifact_handle", "dataset_handle", "row_count"):
                scalar = value.get(key)
                if key in value and (
                    isinstance(scalar, str | int | float | bool) or scalar is None
                ):
                    compact[key] = scalar
        return compact

    retry_dispatch = state.get("operation_retry_dispatch")
    retry_dispatch = retry_dispatch if isinstance(retry_dispatch, dict) else {}
    try:
        retry_deadline = float(retry_dispatch.get("deadline_epoch_seconds") or 0.0)
    except (TypeError, ValueError):
        retry_deadline = 0.0
    try:
        retry_attempt = int(retry_dispatch.get("attempt") or 0)
    except (TypeError, ValueError):
        retry_attempt = 0
    # The hub checkpoints one dispatch per retried attempt: it belongs to this
    # invocation only when it names this execution and the attempt now running.
    dispatch_matches = (
        bool(retry_dispatch)
        and str(retry_dispatch.get("step_execution_id") or "").strip() == current.step_execution_id
        and retry_attempt == current.attempt
    )
    if dispatch_matches:
        operation_deadline_epoch_seconds = retry_deadline
    else:
        from core.application.orchestration.retry_policy import policy_from_state

        operation_deadline_epoch_seconds = (
            time.time() + policy_from_state(state).operation_deadline_seconds
        )
    operation_runtime_state = {
        **dict(state or {}),
        "_operation_deadline_epoch_seconds": operation_deadline_epoch_seconds,
    }
    if dispatch_matches and (retry_deadline <= 0 or time.time() >= retry_deadline):
        return _append_step_message(
            status="fail",
            summary="Operation retry deadline elapsed before invocation",
            artifacts={
                "error": "retry_deadline_exhausted",
                "failure_class": "transient",
                "retryable": False,
                "external_effect_status": "failed",
            },
        )

    if step_kind == STEP_KIND_CONNECTOR:
        connector_id = str(step.get("connector_id") or "").strip()
        if not connector_id:
            logger.error("execute_step_node: connector step missing connector_id")
            return _append_step_message(
                status="fail",
                summary="Connector step is missing connector_id",
                artifacts={"error": "connector_id_missing"},
            )
        from core.application.orchestration.connector_step_runtime import execute_connector_step

        try:
            connector_result = execute_connector_step(
                step=dict(step or {}), state=operation_runtime_state
            )
        except ValueError as exc:
            if (
                state.get("external_effect_resolution_ref") is None
                or str(exc) != "external_effect_resolution_reference_invalid"
            ):
                raise
            connector_result = {
                "ok": False,
                "runtime": "extension_action",
                "detail": str(exc),
            }
        from core.application.orchestration.dataset_cursor_runtime import (
            DatasetCursorError,
            assert_compact_dataset_page_output,
            compact_dataset_connector_result,
            dataset_page_context_from_state,
        )

        dataset_page = dataset_page_context_from_state(state)
        compact_connector_result = (
            compact_dataset_connector_result(connector_result) if dataset_page else None
        )
        ok = bool(connector_result.get("ok"))
        runtime_mode = (
            str((compact_connector_result or {}).get("runtime") or "unresolved").strip()
            if dataset_page
            else str(connector_result.get("runtime") or "unresolved").strip()
        ) or "unresolved"
        detail = (
            str((compact_connector_result or {}).get("detail") or "").strip()
            if dataset_page
            else str(connector_result.get("detail") or "").strip()
        )
        state_updates = (
            dict(connector_result.get("state_patch") or {})
            if isinstance(connector_result.get("state_patch"), dict)
            else {}
        )
        if dataset_page:
            try:
                assert_compact_dataset_page_output(state_updates)
            except DatasetCursorError as exc:
                ok = False
                detail = exc.code
                state_updates = {}
                if compact_connector_result is not None:
                    compact_connector_result["ok"] = False
                    compact_connector_result["detail"] = exc.code
        connector_label = (
            step_display_name or str(connector_result.get("function_name") or "").strip()
        )
        from core.application.extensions.external_effect_contracts import (
            external_effect_guidance_errors,
        )

        guidance_errors = external_effect_guidance_errors(connector_result.get("external_effect"))
        if ok:
            summary = (
                f"Connector step '{connector_label}' executed via {runtime_mode}"
                if connector_label
                else f"Connector step executed via {runtime_mode}"
            )
        elif detail in {
            "extension_disabled",
            "extension_unavailable",
            "external_effect_timeout_unknown",
        }:
            errors = (
                guidance_errors
                if detail == "external_effect_timeout_unknown"
                else connector_result.get("errors")
            )
            safe_message = ""
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                safe_message = str(errors[0].get("message") or "").strip()
            summary = (
                f"Connector step '{connector_label}' failed via {runtime_mode}: "
                f"{safe_message or detail}"
                if connector_label
                else f"Connector step failed via {runtime_mode}: {safe_message or detail}"
            )
        elif detail:
            summary = (
                f"Connector step '{connector_label}' failed via {runtime_mode}: {detail}"
                if connector_label
                else f"Connector step failed via {runtime_mode}: {detail}"
            )
        else:
            summary = (
                f"Connector step '{connector_label}' failed via {runtime_mode}"
                if connector_label
                else f"Connector step failed via {runtime_mode}"
            )
        artifacts: dict[str, Any] = {
            "connector_id": connector_id,
            "_connector_step": {
                "connector_id": connector_id,
                "execution_mode": runtime_mode,
                "detail": detail,
            },
        }
        artifacts.update(normalize_retry_signal(connector_result))
        effect = connector_result.get("external_effect")
        if isinstance(effect, dict) and effect.get("status") == "timeout_unknown":
            artifacts["external_effect_observations"] = [
                {
                    "effect_id": str(effect.get("effect_id") or ""),
                    "claim_version": effect.get("claim_version"),
                    "step_execution_id": current.step_execution_id,
                    "attempt": current.attempt,
                }
            ]

        if dataset_page:
            artifacts["dataset_page"] = dataset_page
            artifacts["connector_result"] = compact_connector_result or {
                "ok": ok,
                "runtime": runtime_mode,
            }
        else:
            if connector_result.get("result") is not None:
                artifacts["connector_result"] = connector_result.get("result")
            if connector_result.get("invocation") is not None:
                artifacts["connector_invocation"] = connector_result.get("invocation")
        if connector_result.get("tool_id") is not None:
            artifacts["tool_id"] = connector_result.get("tool_id")
        if connector_result.get("function_name") is not None:
            artifacts["function_name"] = connector_result.get("function_name")
        if connector_result.get("extension_id") is not None:
            artifacts["extension_id"] = connector_result.get("extension_id")
        if connector_result.get("action_id") is not None:
            artifacts["action_id"] = connector_result.get("action_id")
        if not dataset_page and connector_result.get("input_payload") is not None:
            artifacts["input_payload"] = connector_result.get("input_payload")
        if not dataset_page and connector_result.get("connection_binding") is not None:
            artifacts["connection_binding"] = connector_result.get("connection_binding")
        if not ok:
            artifacts["error"] = (
                detail
                if detail
                in {
                    "dataset_page_output_too_large",
                    "extension_disabled",
                    "extension_unavailable",
                }
                else "connector_execution_failed"
            )
        if state_updates and not dataset_page:
            diagnostic_state_updates = _diagnostic_state_patch(state_updates)
            artifacts["_state_patch"] = diagnostic_state_updates
            for key, value in diagnostic_state_updates.items():
                artifacts.setdefault(key, value)
        connector_errors = None
        if not ok:
            connector_errors = guidance_errors or connector_result.get("errors")
            if dataset_page:
                connector_errors = (compact_connector_result or {}).get("errors")
        message_update = _append_step_message(
            status="pass" if ok else "fail",
            summary=summary,
            artifacts=artifacts,
            bound_dataset_page_message=bool(dataset_page),
            operation_retry=(
                None
                if ok
                else _host_retry_envelope(
                    provenance="deterministic_connector_step",
                    result=connector_result,
                )
            ),
            errors=connector_errors,
        )
        message_update["external_effect_resolution_ref"] = (
            state.get("external_effect_resolution_ref")
            if not ok and not _to_object(effect)
            else None
        )
        return {**state_updates, **message_update} if state_updates else message_update

    if step_kind == STEP_KIND_HTTP_REQUEST:
        from core.application.orchestration.http_request_step_runtime import (
            execute_http_request_step,
        )

        override = _to_object(step.get("override"))
        config = _to_object(override.get("http_request"))
        if not config:
            return _append_step_message(
                status="fail",
                summary="http_request step requires override.http_request configuration",
                artifacts={"error": "http_request_config_missing"},
            )
        try:
            result = execute_http_request_step(
                config=config,
                state=operation_runtime_state,
            )
        except Exception as exc:
            return _append_step_message(
                status="fail",
                summary=f"http_request execution failed: {exc}",
                artifacts={"error": "http_request_execution_error", "detail": str(exc)},
            )

        state_updates: dict[str, Any] = {}
        target = str(config.get("response_target") or "").strip()
        if target and bool(result.get("ok")):
            target_value = (
                result.get("artifact_handle")
                if str(result.get("response_mode") or "").strip().lower() == "artifact"
                else result.get("response_body")
            )
            _set_path(state_updates, dict(state or {}), target, target_value)

        status = "pass" if bool(result.get("ok")) else "fail"
        summary = (
            f"http_request {str(config.get('method') or 'GET').upper()} {config.get('url') or ''!s} -> "
            f"{result.get('status_code')}"
        )
        request_sent = _to_object(result.get("request"))
        response_output: dict[str, Any] = {
            "status_code": result.get("status_code"),
            "content_type": _to_object(result.get("response_headers")).get("content_type"),
        }
        if result.get("artifact_handle"):
            response_output["file"] = result.get("artifact_handle")
        else:
            response_output["body"] = (
                result.get("response_body")
                if result.get("response_body") not in (None, "")
                else result.get("response_excerpt")
            )
        if not bool(result.get("ok")):
            response_output["failure_reason"] = result.get("failure_reason")
        message_update = _append_step_message(
            status=status,
            summary=summary,
            artifacts={
                "input_payload": step_io_payload(request_sent),
                "output_payload": step_io_payload(response_output),
                "operation_id": f"{request_sent.get('method') or ''} {request_sent.get('url') or ''}".strip(),
                "http_request": result,
                "response_target": target,
                "_state_patch": state_updates,
                **normalize_retry_signal(result),
                **state_updates,
            },
            operation_retry=(
                None
                if bool(result.get("ok"))
                else _host_retry_envelope(
                    provenance="deterministic_http_step",
                    result=result,
                )
            ),
            errors=None if bool(result.get("ok")) else result.get("errors"),
        )
        return {**state_updates, **message_update}

    if step_kind == STEP_KIND_FILE_FROM_BASE64:
        from core.application.orchestration.file_from_base64_step_runtime import (
            execute_file_from_base64_step,
        )

        override = _to_object(step.get("override"))
        config = _to_object(override.get(STEP_KIND_FILE_FROM_BASE64))
        if not config:
            return _append_step_message(
                status="fail",
                summary="file_from_base64 step requires override.file_from_base64 configuration",
                artifacts={"error": "file_from_base64_config_missing"},
            )
        result = execute_file_from_base64_step(config=config, state=operation_runtime_state)
        state_updates = {}
        target = str(config.get("result_target") or "").strip()
        if target and bool(result.get("ok")):
            _set_path(state_updates, dict(state or {}), target, result.get("artifact_handle"))
        ok = bool(result.get("ok"))
        summary = (
            f"Saved {result.get('filename')} ({result.get('size_bytes')} bytes)"
            if ok
            else f"file_from_base64 failed: {result.get('failure_reason')}"
        )
        summary_metadata = (
            {
                "summary_key": "react.jobs.step_file_from_base64_saved",
                "summary_params": {
                    "filename": result.get("filename"),
                    "byte_count": result.get("size_bytes"),
                },
            }
            if ok
            else {}
        )
        message_update = _append_step_message(
            status="pass" if ok else "fail",
            summary=summary,
            artifacts={
                "input_payload": step_io_payload(
                    {
                        "source": config.get("source"),
                        "filename": config.get("filename"),
                        "content_type": config.get("content_type"),
                    }
                ),
                "output_payload": step_io_payload(
                    {
                        key: result.get(key)
                        for key in (
                            "artifact_handle",
                            "filename",
                            "content_type",
                            "size_bytes",
                            "failure_reason",
                        )
                        if result.get(key) not in (None, "")
                    }
                ),
                STEP_KIND_FILE_FROM_BASE64: result,
                "result_target": target,
                "_state_patch": state_updates,
                **state_updates,
                **summary_metadata,
            },
            errors=None if ok else result.get("errors"),
        )
        return {**state_updates, **message_update}

    if step_kind == STEP_KIND_TEMPLATE:
        from core.application.orchestration.template_step_runtime import (
            execute_template_step,
            template_step_artifacts,
            template_step_summary,
        )

        config = _to_object(_to_object(step.get("override")).get(STEP_KIND_TEMPLATE))
        result = execute_template_step(config, dict(state or {}))
        state_updates = {}
        target = str(config.get("result_target") or "").strip()
        if target and bool(result.get("ok")):
            _set_path(state_updates, dict(state or {}), target, result.get("text"))
        message_update = _append_step_message(
            status="pass" if result.get("ok") else "fail",
            summary=template_step_summary(result),
            artifacts={**template_step_artifacts(config, result, state_updates)},
            errors=None if result.get("ok") else result.get("errors"),
        )
        return {**state_updates, **message_update}

    if step_kind == STEP_KIND_DATASET_QUERY:
        from core.application.orchestration.dataset_query_step_runtime import (
            execute_dataset_query_step,
        )

        result = execute_dataset_query_step(step=dict(step or {}), state=dict(state or {}))
        ok = bool(result.get("ok"))
        state_updates = (
            dict(result.get("state_patch") or {})
            if isinstance(result.get("state_patch"), dict)
            else {}
        )
        override = _to_object(step.get("override"))
        artifacts: dict[str, Any] = {
            "input_payload": step_io_payload(
                _to_object(override.get("dataset_query") or step.get("dataset_query"))
            ),
            "output_payload": step_io_payload(
                _diagnostic_state_patch(state_updates) if state_updates else result.get("result")
            ),
            "dataset_query": result,
            "_state_patch": state_updates,
        }
        if not ok:
            artifacts["error"] = "dataset_query_failed"
        message_update = _append_step_message(
            status="pass" if ok else "fail",
            summary=result.get("detail") or "dataset_query step executed",
            artifacts=artifacts,
            errors=None if ok else result.get("errors"),
        )
        return {**state_updates, **message_update} if state_updates else message_update

    if step_kind == STEP_KIND_TRANSFORM:
        from core.application.orchestration.transform_step_runtime import execute_transform_step

        override = _to_object(step.get("override"))
        config = _to_object(override.get("transform"))
        if not config:
            return _append_step_message(
                status="fail",
                summary="transform step requires override.transform configuration",
                artifacts={"error": "transform_config_missing"},
            )
        try:
            result = execute_transform_step(config=config, state=dict(state or {}))
        except Exception as exc:
            result = {
                "ok": False,
                "errors": [{"reason": "transform_failed", "detail": str(exc)}],
                "state_patch": {},
                "detail": "runtime_failed",
            }
        ok = bool(result.get("ok"))
        state_updates = (
            dict(result.get("state_patch") or {})
            if isinstance(result.get("state_patch"), dict)
            else {}
        )
        result_target = str(config.get("result_target") or "").strip()
        artifacts: dict[str, Any] = {
            "transform": _compact_deterministic_result(
                result,
                result_target=result_target,
                result_key="output",
            ),
        }
        source_path = str((_to_object(config.get("input")).get("source")) or "").strip()
        rules = {
            key: value for key, value in config.items() if key not in {"input", "result_target"}
        }
        if source_path:
            from core.application.orchestration.step_templating import _resolve_path

            artifacts["input_payload"] = {
                "source": source_path,
                "value": step_io_preview(_resolve_path(dict(state or {}), source_path)),
                "rules": step_io_preview(rules),
            }
        if state_updates:
            diagnostic_patch = _diagnostic_state_patch(state_updates)
            bounded = _bounded_step_debug_value(diagnostic_patch)
            if "value" in bounded:
                artifacts["_state_patch"] = diagnostic_patch
            from core.application.orchestration.step_templating import _resolve_path

            # What the step produced, as written to its target, rather than the
            # whole state patch (which also carries the workflow bookkeeping).
            produced = (
                _resolve_path({**dict(state or {}), **state_updates}, result_target)
                if result_target.startswith("state.")
                else diagnostic_patch
            )
            artifacts["output_payload"] = {
                "result_target": result_target,
                "value": step_io_preview(produced),
            }
        if not ok:
            artifacts["error"] = "transform_failed"
        else:
            artifacts["summary_key"] = "react.jobs.step_transform_finished"
        message_update = _append_step_message(
            status="pass" if ok else "fail",
            summary=str(result.get("detail") or "transform step executed"),
            artifacts=artifacts,
            errors=None if ok else result.get("errors"),
        )
        return {**state_updates, **message_update} if state_updates else message_update

    if step_kind == STEP_KIND_TABULAR_FEED:
        from core.application.orchestration.tabular_feed_step_runtime import (
            execute_tabular_feed_step,
        )

        runtime_state = dict(state or {})
        had_plane = "_artifact_plane" in runtime_state or "artifact_plane" in runtime_state
        result = execute_tabular_feed_step(step=dict(step or {}), state=runtime_state)
        ok = bool(result.get("ok"))
        state_updates = (
            dict(result.get("state_patch") or {})
            if isinstance(result.get("state_patch"), dict)
            else {}
        )
        runtime_state_updates: dict[str, Any] = {}
        if not had_plane and "_artifact_plane" in runtime_state:
            runtime_state_updates["_artifact_plane"] = runtime_state["_artifact_plane"]
        override = _to_object(step.get("override"))
        config = _to_object(override.get("tabular_feed") or step.get("tabular_feed"))
        result_target = str(config.get("result_target") or "").strip()
        artifacts: dict[str, Any] = {
            "input_payload": step_io_payload(
                {key: value for key, value in config.items() if key != "result_target"}
            ),
            "tabular_feed": _compact_deterministic_result(
                result,
                result_target=result_target,
                result_key="result",
            ),
        }
        if ok:
            artifacts["output_payload"] = _tabular_output_preview(
                _to_object(result.get("result")), runtime_state
            )
        if not ok:
            artifacts["error"] = "tabular_feed_failed"
        message_update = _append_step_message(
            status="pass" if ok else "fail",
            summary=str(result.get("detail") or "tabular_feed step executed"),
            artifacts=artifacts,
            errors=None if ok else result.get("errors"),
        )
        merged_updates = {**runtime_state_updates, **state_updates}
        return {**merged_updates, **message_update} if merged_updates else message_update

    if step_kind != STEP_KIND_AGENT:
        logger.error("execute_step_node: unsupported non-agent step_kind '%s'", step_kind)
        return _append_step_message(
            status="fail",
            summary=f"Unsupported execute_step step_kind '{step_kind}'",
            artifacts={"error": "unsupported_step_kind", "step_kind": step_kind},
        )

    # Lazy import to avoid circular dependency at module level
    from core.application.registry.registry_manager import RegistryManager

    registry = RegistryManager.instance()

    resolved_agent_id = resolve_step_agent(step, registry, state=dict(state))
    resolved_skill_id = _agent_primary_skill_id(resolved_agent_id)
    if resolved_skill_id:
        step_skill = resolved_skill_id
    if not resolved_agent_id:
        logger.error("execute_step_node: missing agent_id for agent step")
        return _append_step_message(
            status="fail",
            summary="Agent step is missing agent binding",
            artifacts={"error": "missing_agent_binding"},
        )

    manifest = registry.agents.get(resolved_agent_id)
    if manifest is None:
        logger.error("execute_step_node: agent '%s' not found in registry", resolved_agent_id)
        return _append_step_message(
            status="fail",
            summary=f"Agent '{resolved_agent_id}' not found in registry",
            artifacts={"error": "agent_not_found", "agent_id": resolved_agent_id},
        )

    logger.info("execute_step_node: resolved agent '%s'", resolved_agent_id)

    # Record the resolution in orchestrator_journal
    journal = list(state.get("orchestrator_journal") or [])
    journal.append(
        {
            "event": "step_agent_resolved",
            "resolved_agent": resolved_agent_id,
            "skill": resolved_skill_id,
            "ts": now_iso(),
        }
    )

    impl = load_agent_implementation(manifest, registry.tools, registry.skills)
    node_fn = wrap_agent_as_node(impl, manifest)
    result = node_fn(state)

    if isinstance(result, dict):
        result["orchestrator_journal"] = journal
    return result
