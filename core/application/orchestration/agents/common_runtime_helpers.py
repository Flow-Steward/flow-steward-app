"""Internal runtime/prompt/tool-loop helpers for agents.common."""

import contextlib
import contextvars
import copy
import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from core.application.orchestration.agents.common_runtime_structured_utils import (
    _artifact_value_is_publishable,
    _draft_response_satisfies_phase_result_schema,
    _iter_json_object_candidates,
    _prepare_openai_json_schema,
    _required_artifact_fields,
    _schema_allows_null,
    _serialize_structured_payload,
    _structured_output_kwargs,
)
from core.application.orchestration.agents.common_runtime_tool_classifiers import (
    _MAX_HISTORY_ROUNDS,
    _MAX_TOOL_RESULT_CHARS,
    _RESEARCH_TOOLS,
)
from core.application.orchestration.input_sanitization import (
    resolve_security_scope as _resolve_security_scope,
)
from core.application.orchestration.state import OrchestratorState
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.security.prompt_safety import sanitize_project_config_for_prompt

logger = logging.getLogger(__name__)


def _invoke_with_hard_deadline(llm, messages, invoke_kwargs, timeout_seconds):
    """Run one provider call behind a deadline the provider cannot extend."""
    try:
        seconds = float(timeout_seconds or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds <= 0:
        return llm.invoke(messages, **invoke_kwargs)

    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
    invocation_context = contextvars.copy_context()

    def _invoke() -> None:
        try:
            payload = (True, invocation_context.run(llm.invoke, messages, **invoke_kwargs))
        except Exception as exc:
            payload = (False, exc)
        with contextlib.suppress(queue.Full):
            result_queue.put_nowait(payload)

    invocation_thread = threading.Thread(
        target=_invoke,
        name="llm-provider-invoke",
        daemon=True,
    )
    invocation_thread.start()
    try:
        succeeded, payload = result_queue.get(timeout=seconds)
    except queue.Empty as exc:
        raise TimeoutError(f"LLM request exceeded {seconds:g} seconds") from exc
    if succeeded:
        return payload
    raise payload


__all__ = [
    "_MAX_HISTORY_ROUNDS",
    "_MAX_TOOL_RESULT_CHARS",
    "_RESEARCH_TOOLS",
    "_artifact_value_is_publishable",
    "_draft_response_satisfies_phase_result_schema",
    "_iter_json_object_candidates",
    "_prepare_openai_json_schema",
    "_required_artifact_fields",
    "_schema_allows_null",
    "_serialize_structured_payload",
    "_structured_output_kwargs",
]


def build_prompt_safe_state(state: OrchestratorState | dict) -> dict:
    """Return a prompt-safe state view with minimized project_config payload.

    The runtime state can still contain sensitive fields for tool execution,
    but prompt/template rendering must use a reduced project_config projection.
    """
    safe_state = dict(state or {})
    safe_state["project_config"] = sanitize_project_config_for_prompt(
        safe_state.get("project_config") if isinstance(safe_state, dict) else {}
    )
    return safe_state


def build_project_rules_block(state: OrchestratorState | dict[str, Any]) -> str:
    """Return a markdown Project Rules section from project_config.project_rules.

    Injected into every agent's context so agents are always
    aware of project-specific conventions, constraints, and team standards
    without relying on persona files.
    """
    project_config = state.get("project_config") or {}
    rules = project_config.get("project_rules") or ""
    if not rules.strip():
        return ""
    return f"## Project Rules\n{rules.strip()}"


def build_context_block(
    state: OrchestratorState | dict[str, Any], sections: list[str] | None = None
) -> str:
    """Assemble a markdown context block from selected state fields.

    sections controls which fields to include and in what order.
    Defaults to generic platform context only. Workflow-specific artifact
    sections should be requested explicitly by authored context configuration.

    Each section maps to a state key and a markdown heading.
    Project Rules (from project_config.project_rules) are always prepended first.
    """
    _SECTION_MAP = {
        "kb_context": "## Project context",
    }
    if sections is None:
        sections = [
            "kb_context",
        ]

    parts = []

    # Always include project rules first
    project_rules = build_project_rules_block(state)
    if project_rules:
        parts.append(project_rules)

    for key in sections:
        if key == "attachment_context":
            continue
        value = state.get(key)
        if not value:
            continue
        heading = _SECTION_MAP.get(key, f"## Context: {key}")
        text = str(value) if not isinstance(value, str) else value
        parts.append(f"{heading}\n{text}")

    return "\n\n".join(parts)


class JobCancelledException(Exception):
    """Raised when a durable cancellation stops execution at a graph boundary."""

    error_code = ""


class ParentJobCancelledException(JobCancelledException):
    """An invoked ancestor has durably requested cancellation of this job."""

    def __init__(self) -> None:
        from core.application.jobs.invoke_child_cancellation import (
            CHILD_CANCEL_ERROR_CODE,
            CHILD_CANCEL_MESSAGE,
        )

        super().__init__(CHILD_CANCEL_MESSAGE)
        self.error_code = CHILD_CANCEL_ERROR_CODE


def _assert_job_not_cancelled(bl: Any, job_id: str) -> None:
    from core.application.jobs.invoke_child_link_repository import (
        ancestor_cancellation_requested,
        read_cancellation_job,
    )

    job_row = read_cancellation_job(bl, job_id)
    if not job_row:
        raise JobCancelledException(f"Job {job_id} no longer exists")
    if job_row.get("deleted_at"):
        raise JobCancelledException(f"Job {job_id} was deleted")
    if str(job_row.get("status") or "").strip().lower() == "cancelled":
        raise JobCancelledException(f"Job {job_id} was cancelled by user")
    if ancestor_cancellation_requested(bl, job_id=job_id, job_row=job_row):
        raise ParentJobCancelledException()


def run_graph_with_cancel_check(
    graph,
    input_or_command,
    config,
    bl=None,
    job_id=None,
    cancel_check: Callable[[], bool] | None = None,
    continue_check: Callable[[], None] | None = None,
) -> dict:
    """Run a LangGraph graph with cancellation check between nodes.

    Replaces graph.invoke() in the worker and approve endpoint. Uses
    graph.stream(stream_mode="values") which yields the full state snapshot
    after each node completes — giving us a point to check if the user
    cancelled the job via the dashboard.

    Before stream entry and between advances, observe the job's cancellation
    and valid ancestor cancellation requests. Store failures stop execution as
    infrastructure errors. This does not serialize already-running effects.

    Returns the final state dict (identical to what graph.invoke() returns).
    """
    if isinstance(input_or_command, dict):
        try:
            from core.application.orchestration.state import normalize_orchestrator_state

            input_or_command = normalize_orchestrator_state(input_or_command)
        except Exception:
            # Non-fatal: tolerate partial state payloads during normalization.
            pass

    if cancel_check is not None and cancel_check():
        raise JobCancelledException("Graph execution cancelled before start")
    if continue_check is not None:
        continue_check()
    if bl and job_id:
        _assert_job_not_cancelled(bl, str(job_id))
    result = {}
    for snapshot in graph.stream(input_or_command, config, stream_mode="values"):
        result = snapshot
        if cancel_check is not None and cancel_check():
            raise JobCancelledException("Graph execution cancelled by worker fence")
        if continue_check is not None:
            continue_check()
        if bl and job_id and isinstance(snapshot, dict):
            try:
                current_step_id = snapshot.get("current_step_id")
                current_group_id = snapshot.get("current_group_id")
                workflow_id = snapshot.get("workflow_id")
                current_agent = snapshot.get("current_agent")
                error_text = snapshot.get("error")
                bl.update_job_runtime(
                    job_id=job_id,
                    selected_workflow=workflow_id if isinstance(workflow_id, str) else None,
                    current_step_id=current_step_id if isinstance(current_step_id, str) else None,
                    current_group_id=current_group_id
                    if isinstance(current_group_id, str)
                    else None,
                    current_agent=current_agent if isinstance(current_agent, str) else None,
                    heartbeat_now=True,
                    last_error=error_text[:16000]
                    if isinstance(error_text, str) and error_text
                    else None,
                    clear_last_error=not bool(error_text),
                )
            except Exception as _rt_err:
                logger.debug(
                    "run_graph_with_cancel_check: runtime update failed (non-fatal): %s", _rt_err
                )
        if bl and job_id:
            _assert_job_not_cancelled(bl, str(job_id))
        if cancel_check is not None and cancel_check():
            raise JobCancelledException("Graph execution cancelled by worker fence")
    if bl and job_id:
        # Fully exhausting stream waits for outstanding checkpoint writes.
        # Re-read persisted values rather than trusting the yielded node result.
        try:
            from core.application.jobs.invoke_child_link_repository import (
                settle_checkpointed_children,
            )

            saved = graph.get_state(config)
            values = getattr(saved, "values", None)
            if isinstance(values, dict):
                settle_checkpointed_children(bl, parent_job_id=str(job_id), values=values)
        except Exception as exc:
            # Retain the hold on failure; a later pause/completion can settle it.
            logger.warning(
                "Could not settle checkpointed child results for job %s: %s", job_id, exc
            )
    return result


def _resolve_provider(project_config: dict, agent_name: str = "") -> str:
    """Resolve provider policy from the route selected for model construction."""
    agents_cfg = project_config.get("agents_config") or {}
    from core.application.orchestration.model_provider import (
        ModelUnavailableError,
        _model_provider_account_id,
        _resolve_model_route,
        _resolve_named_provider,
        is_custom_provider_key,
    )

    registry_cfg = None
    try:
        from core.application.orchestration.model_provider import _get_registry_agent_config

        registry_cfg = _get_registry_agent_config(agent_name) if agent_name else {}
        route = _resolve_model_route(
            project_config,
            agent_name or None,
            registry_cfg=registry_cfg,
        )
    except ModelUnavailableError:
        return "unavailable"

    if not is_custom_provider_key(route.provider):
        return route.provider

    try:
        named = _resolve_named_provider(
            route.provider.removeprefix("custom:"),
            account_id=_model_provider_account_id(project_config, agents_cfg),
            require_verified=True,
        )
    except Exception:
        return "unavailable"
    return str(named.get("provider_type") or "unavailable")


def _is_anthropic_provider(project_config: dict, agent_name: str = "") -> bool:
    """Return True if the configured LLM provider for this agent is Anthropic."""
    return _resolve_provider(project_config, agent_name) == "anthropic"


def _apply_prompt_cache(messages: list) -> list:
    """Add Anthropic cache_control to the system message and first HumanMessage.

    The system prompt (persona + context block) and the initial task HumanMessage
    are identical across all ReAct rounds in a single invoke_agent() call.
    Marking them with cache_control=ephemeral tells Anthropic to cache them on
    the first call and serve them from cache on subsequent rounds.

    Pricing impact:
      - Cache write: 125% of normal input token price (one-time per 5-minute window)
      - Cache read:   10% of normal input token price (every subsequent round)
      - Break-even at 2 rounds → profitable for every agent with ≥ 2 LLM calls

    Minimum cacheable block: 1 024 tokens (Sonnet/Opus) or 2 048 (Haiku).
    Anthropic silently ignores cache_control if the block is too short — safe always.

    Only modifies SystemMessage(s) and the first HumanMessage.
    Content already in list form (already processed) is left untouched.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    result: list[Any] = []
    first_human_cached = False
    for msg in messages:
        if isinstance(msg, SystemMessage) and isinstance(msg.content, str):
            result.append(
                SystemMessage(
                    content=[
                        {
                            "type": "text",
                            "text": msg.content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                )
            )
        elif (
            isinstance(msg, HumanMessage)
            and not first_human_cached
            and isinstance(msg.content, str)
        ):
            first_human_cached = True
            result.append(
                HumanMessage(
                    content=[
                        {
                            "type": "text",
                            "text": msg.content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                )
            )
        else:
            result.append(msg)
    return result


def _llm_invoke_with_retry(
    llm,
    messages,
    agent_name: str = "",
    max_retries: int = 3,
    *,
    timeout_seconds: int | float | None = None,
):
    """Invoke the LLM, retrying on recoverable Anthropic errors.

    Two error types are retried:
    - Rate limit (429 / rate_limit_error): token-per-minute window resets every
      60 seconds, so we wait 60 x attempt seconds (60s, 120s, 180s).
    - Overloaded (529 / overloaded_error): temporary server capacity issue,
      usually clears in seconds - we wait 30 x attempt seconds (30s, 60s, 90s).
    """
    invoke_kwargs = (
        {"timeout": float(timeout_seconds)}
        if timeout_seconds is not None and float(timeout_seconds) > 0
        else {}
    )
    for attempt in range(max_retries + 1):
        try:
            return _invoke_with_hard_deadline(llm, messages, invoke_kwargs, timeout_seconds)
        except Exception as e:
            err_str = str(e)
            is_rate_limit = "rate_limit_error" in err_str or (
                "429" in err_str and ("tokens" in err_str.lower() or "rate" in err_str.lower())
            )
            is_overloaded = "overloaded_error" in err_str or "529" in err_str
            if is_rate_limit and attempt < max_retries:
                wait_secs = 60 * (attempt + 1)
                logger.warning(
                    "invoke_agent[%s]: rate limit hit (attempt %d/%d) — sleeping %ds",
                    agent_name,
                    attempt + 1,
                    max_retries,
                    wait_secs,
                )
                time.sleep(wait_secs)
                continue
            if is_overloaded and attempt < max_retries:
                wait_secs = 30 * (attempt + 1)
                logger.warning(
                    "invoke_agent[%s]: Anthropic overloaded (attempt %d/%d) — sleeping %ds",
                    agent_name,
                    attempt + 1,
                    max_retries,
                    wait_secs,
                )
                time.sleep(wait_secs)
                continue
            raise  # non-retryable error or exhausted retries
    return None


def _should_use_native_structured_output(
    state: OrchestratorState | dict[str, Any],
    agent_name: str,
    schema,
    provider_policy: str | None = None,
) -> bool:
    """Require native structured output whenever the caller supplies a schema."""
    del state, agent_name
    if schema is None:
        return False
    provider = str(provider_policy or "").strip().lower()
    if not provider:
        return True
    if provider in {"openai", "openai_compatible", "azure_openai", "anthropic", "google"}:
        return True
    from core.application.settings.model_catalog import is_extension_provider_key

    return is_extension_provider_key(provider)


def _provider_policy_from_model(model: Any) -> str:
    """Read provider behavior from the exact model instance that will be invoked."""
    metadata = getattr(model, "metadata", None)
    if isinstance(metadata, dict):
        provider = str(metadata.get("flow_steward_provider_policy") or "").strip().lower()
        if provider:
            return provider
    provider_key = str(getattr(model, "provider_key", "") or "").strip()
    if provider_key:
        from core.application.settings.model_catalog import is_extension_provider_key

        if is_extension_provider_key(provider_key):
            return provider_key
    return "unavailable"


def _final_structured_output_timeout_seconds(timeout_seconds: int) -> int:
    """Bound optional structured finalization so it can quickly fall back to draft output."""
    raw_cap = str(_runtime_env.get("FS_STRUCTURED_OUTPUT_TIMEOUT_SECONDS", "45") or "").strip()
    try:
        cap = int(float(raw_cap))
    except (TypeError, ValueError):
        cap = 45
    cap = max(1, cap)

    try:
        requested = int(float(timeout_seconds or 0))
    except (TypeError, ValueError):
        requested = 0
    if requested <= 0:
        return cap
    return max(1, min(requested, cap))


def _invoke_final_structured_output(
    *,
    state: OrchestratorState | dict[str, Any],
    agent_name: str,
    timeout_seconds: int,
    messages: list,
    draft_response,
    schema,
    resolved_model,
    expected_provider_policy: str | None = None,
):
    """Run final structured output on the exact model route used by the tool loop."""
    from langchain_core.messages import AIMessage, HumanMessage

    final_timeout_seconds = _final_structured_output_timeout_seconds(timeout_seconds)
    bare_llm = resolved_model
    if bare_llm is None:
        raise RuntimeError("model_unavailable: resolved model snapshot is required")
    provider = _provider_policy_from_model(bare_llm)
    expected_provider = str(expected_provider_policy or "").strip().lower()
    if expected_provider and provider != expected_provider:
        raise RuntimeError(
            "model_unavailable: provider route changed during structured finalization"
        )
    if not hasattr(bare_llm, "with_structured_output"):
        raise RuntimeError(
            f"Configured model for provider '{provider or 'unknown'}' does not support structured output"
        )

    schema_for_provider = (
        _prepare_openai_json_schema(schema)
        if str(provider or "").strip().lower() == "openai"
        else schema
    )
    structured_llm = bare_llm.with_structured_output(
        schema_for_provider,
        include_raw=False,
        **_structured_output_kwargs(provider),
    )
    finalize_msg = HumanMessage(
        content=(
            "Produce the final response using the required structured output schema only. "
            "Use the full conversation, tool results, and your draft response above. "
            "Do not call tools. Do not add markdown fences or free-form text outside the schema."
        )
    )
    safe_draft_response = _strip_tool_call_metadata_from_draft(draft_response)
    structured_result = _llm_invoke_with_retry(
        structured_llm,
        [*messages, safe_draft_response, finalize_msg],
        agent_name=agent_name,
        max_retries=0,
        timeout_seconds=final_timeout_seconds,
    )
    payload = _serialize_structured_payload(structured_result)
    payload_json = json.dumps(payload, ensure_ascii=False)

    with contextlib.suppress(Exception):
        from core.application.orchestration.agents.common import emit_event as _emit_event

        _emit_event(
            state,
            agent_name,
            "llm_response_meta",
            f"Structured final response generated ({len(payload_json)} chars)",
            {
                "structured_output": True,
                "provider": provider,
                "schema": getattr(schema_for_provider, "__name__", str(schema_for_provider)),
                "content_chars": len(payload_json),
            },
        )

    return AIMessage(
        content=payload_json,
        additional_kwargs={"structured_output": payload},
    )


def _strip_tool_call_metadata_from_draft(draft_response):
    """Return a text-only draft message safe to append before a final schema call.

    Some providers can return useful assistant text while leaving raw tool-call
    metadata in ``additional_kwargs`` even when LangChain's parsed
    ``tool_calls`` list is empty. Sending that assistant message back to OpenAI
    without matching ToolMessages makes the next request invalid, so final
    structured-output passes must treat the draft as plain assistant text.
    """
    from langchain_core.messages import AIMessage

    additional_kwargs = getattr(draft_response, "additional_kwargs", None)
    if not isinstance(additional_kwargs, dict):
        additional_kwargs = {}
    parsed_tool_calls = getattr(draft_response, "tool_calls", None)
    has_tool_metadata = bool(parsed_tool_calls) or any(
        key in additional_kwargs for key in ("tool_calls", "function_call")
    )
    if not has_tool_metadata:
        return draft_response

    safe_kwargs = {
        key: value
        for key, value in additional_kwargs.items()
        if key not in {"tool_calls", "function_call"}
    }
    return AIMessage(
        content=getattr(draft_response, "content", "") or "",
        additional_kwargs=safe_kwargs,
    )


def _scan_outbound_response(state: OrchestratorState | dict[str, Any], response):
    """SEC-009: Scan agent output before delivery.

    Blocking decisions and structured-payload integrity violations raise
    ``RuntimeError``. Other scanner/runtime exceptions remain non-fatal so an
    observability outage does not interrupt agent work.
    """
    try:
        content = getattr(response, "content", None)
        if not content or not isinstance(content, str):
            return response
        from core.application.management.scope import scope_from_state
        from core.infrastructure.security.integration import scan_outbound

        project_id, job_id = _resolve_security_scope(state)
        scope = scope_from_state(state)
        account_id = scope.account_id or None
        additional_kwargs = getattr(response, "additional_kwargs", None)
        structured_payload = (
            additional_kwargs.get("structured_output")
            if isinstance(additional_kwargs, dict)
            else None
        )
        if structured_payload is not None:
            try:
                canonical_payload = json.loads(content)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("SEC-009: structured output content is invalid JSON") from exc
            if canonical_payload != structured_payload:
                raise RuntimeError("SEC-009: structured output representations do not match")
        decision = scan_outbound(
            content,
            "outbound",
            project_id=project_id,
            job_id=job_id,
            account_id=account_id,
        )
        if decision.action == "block":
            raise RuntimeError("SEC-009: agent output blocked by security policy")
        if decision.redacted_text is not None and decision.redacted_text != content:
            redacted_text = decision.redacted_text
            if structured_payload is not None:
                try:
                    redacted_payload = json.loads(redacted_text)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "SEC-009: structured output redaction produced invalid JSON"
                    ) from exc
                safe_kwargs = dict(additional_kwargs or {})
                safe_kwargs["structured_output"] = redacted_payload
                model_copy = getattr(response, "model_copy", None)
                if callable(model_copy):
                    return model_copy(
                        update={"content": redacted_text, "additional_kwargs": safe_kwargs}
                    )
                safe_response = copy.copy(response)
                safe_response.content = redacted_text
                safe_response.additional_kwargs = safe_kwargs
                return safe_response
            response.content = redacted_text
    except RuntimeError:
        raise  # re-raise security block — must not be swallowed
    except Exception:
        pass  # non-fatal: scanner/observability failures never interrupt agent work
    return response


def _finalize_response_if_supported(
    *,
    state: OrchestratorState | dict[str, Any],
    agent_name: str,
    timeout_seconds: int,
    messages: list,
    draft_response,
    schema,
    provider_policy: str | None = None,
    resolved_model=None,
):
    """Return a schema-valid final response or fail closed.

    A route selected for native structured output is a structured-required
    route.  A malformed response, missing continuation context, or unavailable
    model must not silently become a free-form draft from another code path.
    """
    if not _should_use_native_structured_output(
        state, agent_name, schema, provider_policy=provider_policy
    ):
        return _scan_outbound_response(state, draft_response)
    if _draft_response_satisfies_phase_result_schema(draft_response, schema):
        with contextlib.suppress(Exception):
            from core.application.orchestration.agents.common import emit_event as _emit_event

            _emit_event(
                state,
                agent_name,
                "llm_response_meta",
                "Structured output draft accepted without rewrite",
                {
                    "structured_output": False,
                    "draft_phase_result_v2": True,
                },
            )
        return _scan_outbound_response(state, draft_response)
    provider = str(provider_policy or "selected route")
    try:
        structured_response = _invoke_final_structured_output(
            state=state,
            agent_name=agent_name,
            timeout_seconds=timeout_seconds,
            messages=messages,
            draft_response=draft_response,
            schema=schema,
            resolved_model=resolved_model,
            expected_provider_policy=provider_policy,
        )
        return _scan_outbound_response(state, structured_response)
    except Exception as exc:
        logger.exception(
            "invoke_agent[%s]: native structured output failed for provider '%s': %s",
            agent_name,
            provider,
            exc,
        )
        with contextlib.suppress(Exception):
            from core.application.orchestration.agents.common import emit_event as _emit_event

            _emit_event(
                state,
                agent_name,
                "llm_response_meta",
                "Structured output failed closed",
                {
                    "structured_output": True,
                    "provider": provider,
                    "failure_reason": str(exc),
                },
            )
        raise RuntimeError(f"structured_output_unavailable: {exc}") from exc


def _prune_message_history(messages: list, max_rounds: int) -> list:
    """Drop the oldest ReAct rounds so the history never exceeds max_rounds.

    Always preserves the initial SystemMessage(s) and the first HumanMessage
    (the task description).  Only the intermediate AI+Tool exchange rounds are
    eligible for removal.
    """
    try:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    except Exception:
        # Test/sandbox fallback when langchain_core is unavailable.
        if not isinstance(messages, list):
            return []
        if max_rounds <= 0:
            return []
        if len(messages) <= max_rounds:
            return list(messages)
        return list(messages[-max_rounds:])

    def _message_kind(msg: Any) -> str | None:
        if isinstance(msg, SystemMessage) or getattr(msg, "type", None) == "system":
            return "system"
        if isinstance(msg, HumanMessage) or getattr(msg, "type", None) == "human":
            return "human"
        if isinstance(msg, AIMessage) or getattr(msg, "type", None) == "ai":
            return "ai"
        return None

    # Split messages into fixed prefix (System + Human) and rolling history.
    # Some tests and older persisted histories use lightweight message objects
    # with LangChain-compatible ``type`` values rather than concrete classes.
    prefix, history = [], []
    in_prefix = True
    for msg in messages:
        if in_prefix and _message_kind(msg) in {"system", "human"}:
            prefix.append(msg)
        else:
            in_prefix = False
            history.append(msg)

    # Group history into rounds — each round starts with an AIMessage
    rounds: list[list] = []
    for msg in history:
        if _message_kind(msg) == "ai":
            rounds.append([msg])
        elif rounds:
            rounds[-1].append(msg)
        else:
            prefix.append(msg)  # stray message before first AI turn → keep in prefix

    # Drop oldest rounds until we're within budget
    if len(rounds) > max_rounds:
        dropped = len(rounds) - max_rounds
        logger.debug(
            "_prune_message_history: dropping %d oldest round(s) to stay within %d-round window",
            dropped,
            max_rounds,
        )
        rounds = rounds[-max_rounds:]

    return prefix + [msg for r in rounds for msg in r]
