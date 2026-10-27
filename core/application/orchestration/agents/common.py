"""
Shared utilities for agent nodes.
Eliminates boilerplate repeated across configured workflow agents.
"""

import contextlib
import logging
import math
import time
from typing import Any

from core.application.orchestration.input_sanitization import (
    resolve_security_scope,
    sanitize_external_input,
)
from core.application.orchestration.model_provider import _normalize_timeout_seconds, get_chat_model
from core.application.orchestration.model_response_text import model_response_text
from core.application.orchestration.prompt_templates import build_langchain_prompt
from core.application.orchestration.state import OrchestratorState
from core.infrastructure.security.prompt_safety import assert_prompt_payload_safe

from . import common_runtime_helpers as _runtime_helpers
from .common_runtime_helpers import (
    _MAX_HISTORY_ROUNDS,
    _MAX_TOOL_RESULT_CHARS,
    _RESEARCH_TOOLS,
    JobCancelledException,
    _apply_prompt_cache,
    _finalize_response_if_supported,
    _llm_invoke_with_retry,
    _provider_policy_from_model,
    _prune_message_history,
    _resolve_provider,
    _scan_outbound_response,
    build_context_block,
    build_project_rules_block,
    build_prompt_safe_state,
    run_graph_with_cancel_check,
)
from .common_tool_runtime import (
    MCP_CANONICAL_TOOL_ID_METADATA_KEY,
    MCP_PROJECTED_TOOL_METADATA_KEY,
    _agent_tool_call_timeout_seconds,
    _invoke_tool_with_timeout,
    _resolve_tool_call_id,
    _stringify_tool_result,
)

logger = logging.getLogger(__name__)
_DEFAULT_TOOL_CALL_TIMEOUT_SECONDS = 120


def _is_projected_mcp_tool(tool_fn: Any) -> bool:
    metadata = getattr(tool_fn, "metadata", None)
    return isinstance(metadata, dict) and metadata.get(MCP_PROJECTED_TOOL_METADATA_KEY) is True


def _canonical_projected_tool_id(tool_fn: Any) -> str:
    metadata = getattr(tool_fn, "metadata", None)
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get(MCP_CANONICAL_TOOL_ID_METADATA_KEY) or "").strip()


__all__ = [
    "JobCancelledException",
    "_resolve_provider",
    "_scan_outbound_response",
    "build_project_rules_block",
    "emit_event",
    "invoke_agent",
    "publish_agent_message",
    "resolve_security_scope",
    "run_graph_with_cancel_check",
    "sanitize_external_input",
    "update_runtime_truth",
]


def _finalize_response_with_current_runtime_helpers(**kwargs):
    """Run structured finalization with any local test-time provider patches scoped."""
    original_resolve_provider = _runtime_helpers._resolve_provider
    _runtime_helpers._resolve_provider = _resolve_provider
    try:
        return _finalize_response_if_supported(**kwargs)
    finally:
        _runtime_helpers._resolve_provider = original_resolve_provider


# ---------------------------------------------------------------------------
# Observability: emit_event() — thin wrapper for semantic agent events
# ---------------------------------------------------------------------------

# Module-level cache: DSN -> platform event logger instance (one per worker process)
_event_loggers: dict = {}
_event_replay_next_attempt_by_logger: dict[int, float] = {}
_EVENT_REPLAY_COOLDOWN_SECONDS = 60.0
_EVENT_REPLAY_MAX_BATCH = 20


def _resolve_observability_dsn() -> str:
    """Return DSN for platform observability/event logging."""
    from core.infrastructure.config.runtime_config import CONFIG
    from core.infrastructure.db.platform_logger import resolve_observability_dsn

    return resolve_observability_dsn(
        str(CONFIG.get("fs_observability_dsn") or CONFIG.get("fs_db_dsn") or "")
    )


def _get_event_logger(dsn: str):
    """Return (or create) a cached platform event logger for the given DSN.

    Reconnects if the connection has been lost.
    """
    from core.infrastructure.db.platform_stores import PlatformJobStore

    cached = _event_loggers.get(dsn)
    if cached:
        try:
            # Quick liveness check
            with cached.conn.cursor() as _cur:
                _cur.execute("SELECT 1")
            return cached
        except Exception:
            pass  # stale connection — recreate below
    bl = PlatformJobStore(dsn)
    _event_loggers[dsn] = bl
    return bl


def _maybe_replay_queued_events(bl) -> None:
    """Replay queued events on a cooldown so observability recovery stays off hot paths."""
    logger_key = id(bl)
    now = time.monotonic()
    next_attempt = _event_replay_next_attempt_by_logger.get(logger_key, 0.0)
    if now < next_attempt:
        return
    _event_replay_next_attempt_by_logger[logger_key] = now + _EVENT_REPLAY_COOLDOWN_SECONDS
    try:
        bl.replay_queued_events(max_events=_EVENT_REPLAY_MAX_BATCH)
    except Exception as replay_err:
        logger.debug("emit_event replay_queued_events failed (non-fatal): %s", replay_err)


def _queue_event_for_retry(bl, payload: dict, reason: str = "") -> None:
    """Best-effort enqueue of failed event write for DB-backed replay."""
    try:
        queued = bl.queue_event_for_retry(
            payload=payload,
            failure_reason=reason[:1000] if reason else "",
        )
        if not queued:
            logger.debug("emit_event queue_event_for_retry returned false")
    except Exception as queue_err:
        logger.debug("emit_event queue_event_for_retry failed (non-fatal): %s", queue_err)


def emit_event(
    state: OrchestratorState | dict[str, Any],
    agent: str,
    event_type: str,
    title: str,
    detail: dict | None = None,
    severity: str = "info",
) -> None:
    """Emit a semantic event from an agent node into platform_events.

    This is the one-liner every node uses to report what it's doing.
    Completely non-fatal — observability failures never interrupt agent work.

    Args:
        state:      Current OrchestratorState (used to extract job_id and phase).
        agent:      Agent name, e.g. "orchestrator", "writer", "reviewer".
        event_type: Category: "decision", "tool_call", "quality_gate",
                    "tool_result", "llm_response_meta", "routing_decision",
                    "validation_fail", "phase_complete", "error",
                    "iteration_start", "commit".
        title:      Human-readable one-liner shown in the timeline UI.
        detail:     Optional dict with structured payload (shown on expand).
        severity:   "info" | "warn" | "error"
    """
    project_id, job_id = resolve_security_scope(state)
    if not job_id:
        return

    # Redact secrets/PII from detail payload before persistence
    if detail is not None:
        try:
            import json as _json

            from core.infrastructure.security.integration import scan_outbound

            # Scanned as written; see MessageBus.publish.
            _detail_str = _json.dumps(detail, default=str, ensure_ascii=False)
            _sec = scan_outbound(
                _detail_str,
                project_id=project_id,
                job_id=job_id,
                context_type="display",
            )
            if _sec.redacted_text is not None and _sec.redacted_text != _detail_str:
                try:
                    redacted_detail = _json.loads(_sec.redacted_text)
                    from core.infrastructure.security.prompt_safety import (
                        restore_canonical_structured_registry_identifiers,
                    )

                    detail = restore_canonical_structured_registry_identifiers(
                        detail,
                        redacted_detail,
                    )
                except Exception:
                    detail = {"_redacted": True}
        except Exception:
            pass

    phase = agent
    payload = {
        "job_id": job_id,
        "phase": phase,
        "agent": agent,
        "event_type": event_type,
        "title": title,
        "detail": detail,
        "severity": severity,
    }

    bl = None
    try:
        dsn = _resolve_observability_dsn()
        if not dsn:
            return

        bl = _get_event_logger(dsn)
        _maybe_replay_queued_events(bl)

        wrote = bl.log_event(**payload)
        if not wrote:
            _queue_event_for_retry(bl, payload, reason="log_event returned false")
    except Exception as e:
        if bl is not None:
            _queue_event_for_retry(bl, payload, reason=str(e))
        logger.debug("emit_event failed; payload queued in db when possible (non-fatal): %s", e)


def publish_agent_message(state: OrchestratorState | dict[str, Any], envelope: dict) -> bool:
    """Persist AgentMessageV1 envelope to observability DB (best-effort)."""
    project_id, job_id = resolve_security_scope(state)
    if not job_id or not isinstance(envelope, dict):
        return False
    child_label = str(state.get("invoke_child_label") or "").strip()
    if child_label and str(state.get("invoke_parent_job_id") or "") == str(job_id):
        # A child run inside the parent's job (a test run): mark its steps so
        # the run view does not mix them up with the parent's own.
        artifacts = envelope.get("artifacts")
        if isinstance(artifacts, dict):
            artifacts.setdefault("child_workflow", child_label)
    try:
        from core.application.orchestration.message_bus import MessageBus

        bus = MessageBus.for_state(state)
        wrote = bus.publish(job_id, envelope, project_id=project_id)
        if wrote:
            return True

        logger.warning(
            "publish_agent_message: MessageBus.publish returned false for job_id=%s; "
            "retrying with a fresh bus",
            job_id,
        )
        MessageBus._instances.clear()
        wrote = MessageBus.for_state(state).publish(job_id, envelope, project_id=project_id)
        if not wrote:
            logger.warning(
                "publish_agent_message: retry also failed for job_id=%s from_agent=%s skill=%s",
                job_id,
                envelope.get("from_agent"),
                envelope.get("skill"),
            )
        return bool(wrote)
    except Exception as e:
        logger.warning("publish_agent_message failed (non-fatal): %s", e)
        return False


def update_runtime_truth(
    state: OrchestratorState | dict[str, Any],
    *,
    current_agent: str | None = None,
    current_step_id: str | None = None,
    current_group_id: str | None = None,
    selected_workflow: str | None = None,
    last_error: str | None = None,
    last_response_excerpt: str | None = None,
    clear_last_error: bool = False,
) -> None:
    """Best-effort update of jobs runtime observability fields."""
    try:
        job_id = state.get("job_id")
        if not job_id:
            return
        dsn = _resolve_observability_dsn()
        if not dsn:
            return
        bl = _get_event_logger(dsn)
        bl.update_job_runtime(
            job_id=job_id,
            current_agent=current_agent,
            current_step_id=current_step_id,
            current_group_id=current_group_id,
            selected_workflow=selected_workflow,
            heartbeat_now=True,
            last_error=last_error,
            last_response_excerpt=last_response_excerpt,
            clear_last_error=clear_last_error,
        )
    except Exception as e:
        logger.debug("update_runtime_truth failed (non-fatal): %s", e)


def _compute_round_limits(
    agent_name: str,
    max_tool_rounds: int,
    registry_config: dict | None,
) -> tuple[int, int]:
    """Compute bounded round limits from configured multipliers."""
    _ = agent_name  # Kept for stable call sites; behavior is config-driven.
    config = registry_config or {}
    hard_cap = math.ceil(max_tool_rounds * config.get("hard_cap_multiplier", 3))
    max_consecutive_research = int(config.get("max_consecutive_research", 20))
    hard_cap = max(hard_cap, max_consecutive_research)
    return hard_cap, max_consecutive_research


def _tool_loop_budget_logs_as_info(registry_config: dict | None) -> bool:
    """Return whether configured loop budget exhaustion should be non-warning."""
    config = registry_config or {}
    raw = str(config.get("tool_loop_budget_log_level") or "").strip().lower()
    return raw in {"info", "debug"}


def build_tool_map(agent_name: str, tools: list[Any]) -> dict[str, Any]:
    """Index tools by the name the provider will call them by.

    Two tools answering to one provider name is not a preference to settle
    last-wins: the agent would ask for one and the platform would run the other,
    under the wrong authorization and the wrong audit trail. Fail closed instead.
    """
    tool_map: dict[str, Any] = {}
    for tool in tools or []:
        tool_name = str(getattr(tool, "name", "") or "").strip()
        if not tool_name:
            continue
        if tool_name in tool_map:
            raise RuntimeError(
                f"Agent '{agent_name}' has two tools named '{tool_name}'. "
                "Provider tool names must be unique; refusing to guess which one "
                "a call means."
            )
        tool_map[tool_name] = tool
    return tool_map


def invoke_agent(
    state: OrchestratorState | dict[str, Any],
    persona: dict,
    platform_config: dict,
    task_input: str,
    agent_name: str,
    tools: list | None = None,
    context_sections: list[str] | None = None,
    max_tool_rounds: int = 8,
    timeout_seconds: int = 0,
    final_response_schema=None,
):
    """Build prompt, bind tools, invoke LLM with ReAct tool-call loop, return final response.

    Tools supplied by enabled project ToolProvider bindings are merged
    with the agent's ordinary tools.

    Runs a ReAct loop: if the LLM returns tool calls, executes each tool,
    appends the results as ToolMessages, and re-invokes the LLM until the
    model returns a content-bearing response (no further tool calls).

    Token controls applied every round:
    - Tool results truncated to _MAX_TOOL_RESULT_CHARS chars before appending.
    - Message history pruned to _MAX_HISTORY_ROUNDS rounds after each round.
    - Model request timeout forwarded to the provider-specific chat client.

    Returns the final LangChain AIMessage response object.
    """
    from langchain_core.messages import ToolMessage

    from core.application.orchestration.retry_policy import OperationRetrySignalError
    from core.application.orchestration.tool_registry import build_project_provider_tools

    # Bind the current job_id as the MCP rate-limit principal so all
    # tool calls within this invocation are attributed to the same principal.
    _job_id = str(state.get("job_id") or "").strip()
    if _job_id:
        try:
            from core.application.orchestration.tools.mcp_tools import set_mcp_principal

            set_mcp_principal(f"job:{_job_id}")
        except Exception:
            pass  # non-fatal — rate limiting degrades to anonymous bucket

    prompt_state = build_prompt_safe_state(state)
    context_block = build_context_block(prompt_state, sections=context_sections)
    assert_prompt_payload_safe(
        str(persona.get("system_prompt") or ""),
        f"{context_block}\n\n{task_input or ''}",
        project_config=state.get("project_config", {}),
    )
    prompt = build_langchain_prompt(persona, platform_config, state.get("project_name"))
    llm = get_chat_model(
        state.get("project_config", {}),
        agent_name=agent_name,
        timeout_seconds=timeout_seconds,
    )
    resolved_llm = llm
    provider_key = _provider_policy_from_model(resolved_llm)
    request_timeout_seconds = _normalize_timeout_seconds(timeout_seconds)

    all_tools = list(tools or [])
    provider_tools = build_project_provider_tools(state.get("project_config", {}))
    if provider_tools:
        all_tools = all_tools + provider_tools
        logger.debug(
            "invoke_agent[%s]: merged %d provider tool(s)", agent_name, len(provider_tools)
        )

    # Build name→callable lookup for tool execution.
    tool_map = build_tool_map(agent_name, all_tools)

    if all_tools:
        llm = llm.bind_tools(all_tools)

    messages = prompt.format_messages(context_block=context_block, task_input=task_input)
    logger.debug("invoke_agent[%s]: starting with %d context chars", agent_name, len(context_block))

    # ── Anthropic prompt caching ──────────────────────────────────────────────
    # The system message (persona + context) and the first HumanMessage (task)
    # are identical on every ReAct round.  Caching them costs 125 % on write
    # (round 1) but only 10 % on every subsequent cache-read round — so we
    # break even at round 2 and save ~90 % of those tokens on rounds 3+.
    if provider_key == "anthropic" or bool(getattr(llm, "supports_prompt_caching", False)):
        messages = _apply_prompt_cache(messages)
        logger.debug(
            "invoke_agent[%s]: Anthropic prompt caching applied to system + task prefix", agent_name
        )

    # Research-only rounds (all tools in _RESEARCH_TOOLS) don't count toward
    # max_tool_rounds — only rounds with at least one action tool do.
    # Hard cap: multiplier x max_tool_rounds total rounds prevents infinite research loops.
    # Per-agent multiplier from registry config (default 3).
    registry_config = {}
    try:
        from core.application.registry.registry_manager import RegistryManager

        reg = RegistryManager.instance()
        agent_manifest = reg.agents.get_agent(agent_name)
        if agent_manifest:
            registry_config = agent_manifest.config or {}
    except Exception:
        pass
    _hard_cap, _max_consecutive_research = _compute_round_limits(
        agent_name,
        max_tool_rounds,
        registry_config,
    )
    _budget_logs_as_info = _tool_loop_budget_logs_as_info(registry_config)
    # Per-agent action_tools override: tools in this set count as ACTION even
    # if they appear in _RESEARCH_TOOLS. This forces an agent to be economical
    # with reads instead of burning unlimited research rounds.
    _agent_action_tools = frozenset(registry_config.get("action_tools", []))
    action_rounds = 0
    total_rounds = 0
    consecutive_research = 0
    tool_call_timeout_seconds = _agent_tool_call_timeout_seconds(timeout_seconds)
    from core.infrastructure.runtime.test_mode import (
        is_test_mode_state,
        should_block_tool_call,
    )

    while action_rounds < max_tool_rounds and total_rounds < _hard_cap:
        response = _llm_invoke_with_retry(
            llm,
            messages,
            agent_name=agent_name,
            timeout_seconds=request_timeout_seconds,
        )
        try:
            response_content = getattr(response, "content", "") or ""
            emit_event(
                state,
                agent_name,
                "llm_response_meta",
                f"LLM response received ({len(response_content)} chars, {len(getattr(response, 'tool_calls', []) or [])} tool calls)",
                {
                    "round_total": total_rounds + 1,
                    "round_action": action_rounds,
                    "content_chars": len(response_content),
                    "tool_calls": len(getattr(response, "tool_calls", []) or []),
                },
            )
        except Exception:
            pass

        # No tool calls → final content-bearing response, return immediately
        if not getattr(response, "tool_calls", None):
            return _finalize_response_with_current_runtime_helpers(
                state=state,
                agent_name=agent_name,
                timeout_seconds=timeout_seconds,
                messages=messages,
                draft_response=response,
                schema=final_response_schema,
                provider_policy=provider_key,
                resolved_model=resolved_llm,
            )

        total_rounds += 1

        # Classify this round: research-only or action.
        # A round is "research" if every tool call is in _RESEARCH_TOOLS
        # (and NOT in the per-agent action_tools override).
        is_research_only = all(
            tc.get("name", "") in _RESEARCH_TOOLS and tc.get("name", "") not in _agent_action_tools
            for tc in response.tool_calls
        )
        if not is_research_only:
            action_rounds += 1
            consecutive_research = 0
        else:
            consecutive_research += 1
            if consecutive_research >= _max_consecutive_research:
                if _budget_logs_as_info:
                    logger.info(
                        "invoke_agent[%s]: finalizing after configured research budget "
                        "(consecutive=%d, action %d/%d, total %d/%d)",
                        agent_name,
                        consecutive_research,
                        action_rounds,
                        max_tool_rounds,
                        total_rounds,
                        _hard_cap,
                    )
                else:
                    logger.warning(
                        "invoke_agent[%s]: %d consecutive research rounds without action — breaking out "
                        "(action %d/%d, total %d/%d)",
                        agent_name,
                        consecutive_research,
                        action_rounds,
                        max_tool_rounds,
                        total_rounds,
                        _hard_cap,
                    )
                break

        # ── Execute tool calls ────────────────────────────────────────────────
        tool_names = [tc.get("name", "?") for tc in response.tool_calls]
        logger.info(
            "invoke_agent[%s]: round %d (action %d/%d, %s) — %s",
            agent_name,
            total_rounds,
            action_rounds,
            max_tool_rounds,
            "research" if is_research_only else "ACTION",
            ", ".join(tool_names),
        )
        messages.append(response)

        for index, tc in enumerate(response.tool_calls):
            tool_name = tc.get("name", "")
            tool_args = dict(tc.get("args") or {})
            call_id = _resolve_tool_call_id(response, tc, index)
            tool_fn = tool_map.get(tool_name)
            is_projected_mcp = _is_projected_mcp_tool(tool_fn)
            canonical_tool_id = _canonical_projected_tool_id(tool_fn)
            call_detail = {
                "tool_name": tool_name,
                "tool_call_id": call_id,
                "round_total": total_rounds,
                "round_action": action_rounds,
            }
            if is_projected_mcp and canonical_tool_id:
                call_detail["canonical_tool_id"] = canonical_tool_id
            if not is_projected_mcp:
                call_detail["args_excerpt"] = str(tool_args)[:1000]
            with contextlib.suppress(Exception):
                emit_event(
                    state,
                    agent_name,
                    "tool_call",
                    f"Tool call: {tool_name}",
                    call_detail,
                )

            if tool_fn is None:
                tool_result = f"[Error: tool '{tool_name}' not found]"
                logger.warning("invoke_agent[%s]: unknown tool '%s'", agent_name, tool_name)
            elif is_test_mode_state(state) and should_block_tool_call(tool_name, tool_args):
                tool_result = f"[Tool blocked in test_mode: {tool_name}]"
                logger.info(
                    "invoke_agent[%s]: blocked tool '%s' in test_mode",
                    agent_name,
                    tool_name,
                )
            else:
                try:
                    # SEC-010: scan tool input before execution
                    try:
                        from core.infrastructure.security.integration import (
                            scan_tool_input,
                            scan_tool_output,
                        )

                        _sec_project_id = state.get("project_config", {}).get("project_id")
                        _sec_job_id = state.get("job_id")
                        _ti_decision = scan_tool_input(
                            str(tool_args),
                            tool_name=tool_name,
                            project_id=_sec_project_id,
                            job_id=_sec_job_id,
                        )
                        if _ti_decision.action == "block":
                            tool_result = (
                                f"[Security: tool input blocked by policy for {tool_name}]"
                            )
                            logger.warning(
                                "invoke_agent[%s]: tool '%s' input blocked by security policy",
                                agent_name,
                                tool_name,
                            )
                            raw = None
                        else:
                            raw = _invoke_tool_with_timeout(
                                tool_fn,
                                tool_args,
                                tool_name=tool_name,
                                timeout_seconds=tool_call_timeout_seconds,
                            )
                    except ImportError:
                        raw = _invoke_tool_with_timeout(
                            tool_fn,
                            tool_args,
                            tool_name=tool_name,
                            timeout_seconds=tool_call_timeout_seconds,
                        )

                    if raw is not None:
                        tool_result = _stringify_tool_result(raw)
                        # SEC-010: scan tool output
                        try:
                            _to_decision = scan_tool_output(
                                tool_result,
                                tool_name=tool_name,
                                project_id=_sec_project_id,
                                job_id=_sec_job_id,
                            )
                            if _to_decision.redacted_text is not None:
                                tool_result = _to_decision.redacted_text
                        except Exception:
                            pass  # non-fatal

                    logger.debug(
                        "invoke_agent[%s]: tool '%s' returned %d chars",
                        agent_name,
                        tool_name,
                        len(tool_result),
                    )
                except OperationRetrySignalError:
                    raise
                except Exception as e:
                    tool_result = f"[Tool error: {e}]"
                    logger.warning(
                        "invoke_agent[%s]: tool '%s' raised: %s", agent_name, tool_name, e
                    )
            result_detail = {
                "tool_name": tool_name,
                "tool_call_id": call_id,
                "is_error": str(tool_result).startswith("[Tool error:")
                or str(tool_result).startswith("[Error:"),
                "round_total": total_rounds,
                "round_action": action_rounds,
            }
            if is_projected_mcp and canonical_tool_id:
                result_detail["canonical_tool_id"] = canonical_tool_id
            if not is_projected_mcp:
                result_detail["result_excerpt"] = str(tool_result)[:1200]
            with contextlib.suppress(Exception):
                emit_event(
                    state,
                    agent_name,
                    "tool_result",
                    f"Tool result: {tool_name}",
                    result_detail,
                )

            # ── Truncate large tool results before adding to history ──────────
            if len(tool_result) > _MAX_TOOL_RESULT_CHARS:
                logger.debug(
                    "invoke_agent[%s]: truncating tool '%s' result %d→%d chars",
                    agent_name,
                    tool_name,
                    len(tool_result),
                    _MAX_TOOL_RESULT_CHARS,
                )
                tool_result = (
                    tool_result[:_MAX_TOOL_RESULT_CHARS]
                    + f"\n\n[...truncated — {len(tool_result) - _MAX_TOOL_RESULT_CHARS} chars omitted]"
                )

            messages.append(
                ToolMessage(
                    content=tool_result,
                    tool_call_id=call_id,
                    name=tool_name,
                )
            )

        # ── Prune oldest rounds to keep history bounded ───────────────────────
        messages = _prune_message_history(messages, _MAX_HISTORY_ROUNDS)

    # Safety: exhausted tool rounds — do one final call without tools to force a text response
    if _budget_logs_as_info:
        logger.info(
            "invoke_agent[%s]: finalizing after configured tool loop budget "
            "(action_rounds=%d/%d, total_rounds=%d/%d)",
            agent_name,
            action_rounds,
            max_tool_rounds,
            total_rounds,
            _hard_cap,
        )
    else:
        logger.warning(
            "invoke_agent[%s]: hit limit (action_rounds=%d/%d, total_rounds=%d/%d), forcing final response without tools",
            agent_name,
            action_rounds,
            max_tool_rounds,
            total_rounds,
            _hard_cap,
        )
    from langchain_core.messages import HumanMessage as _HumanMessage

    bare_llm = get_chat_model(
        state.get("project_config", {}),
        agent_name=agent_name,
        timeout_seconds=timeout_seconds,
    )

    # Inject an explicit stop-tools instruction so the model doesn't try to call
    # tools in its final response (which would produce empty content).
    stop_msg = _HumanMessage(
        content=(
            "IMPORTANT: You have exhausted your tool call budget. No more tools are available. "
            "Write your complete final deliverable as plain text RIGHT NOW. "
            "Follow the original instructions and required output format exactly, including every required section and artifact. "
            "Tool-budget exhaustion is not itself a blocker. If you already observed acceptance evidence, "
            "such as successful tool results or check output you directly observed, "
            "base your final verdict on that evidence instead of reporting blocked solely because no more tools are available. "
            "Only report blocked when required evidence is genuinely unavailable or contradictory. "
            "Do not attempt any tool calls or function calls."
        )
    )
    final_response = _llm_invoke_with_retry(
        bare_llm,
        [*messages, stop_msg],
        agent_name=agent_name,
        timeout_seconds=request_timeout_seconds,
    )
    try:
        final_content = model_response_text(final_response)
        emit_event(
            state,
            agent_name,
            "llm_response_meta",
            f"Final non-tool LLM response ({len(final_content)} chars)",
            {
                "final_fallback": True,
                "content_chars": len(final_content),
            },
        )
    except Exception:
        pass

    # If content is still empty (model still tried to call a tool), retry once more
    # with an even more direct prompt that includes the task reminder.
    if not model_response_text(final_response).strip():
        logger.warning(
            "invoke_agent[%s]: bare_llm returned empty content after stop-tools prompt, retrying",
            agent_name,
        )
        retry_msg = _HumanMessage(
            content=(
                "Your response was empty. Based on all the information you gathered above, "
                "write your complete final response as a single plain-text document now. "
                "Follow the original required output format exactly, including every required section and artifact. "
                "This is your last chance — no tool calls, just text."
            )
        )
        final_response = _llm_invoke_with_retry(
            bare_llm,
            [*messages, stop_msg, retry_msg],
            agent_name=agent_name,
            timeout_seconds=request_timeout_seconds,
        )
        try:
            final_content = model_response_text(final_response)
            emit_event(
                state,
                agent_name,
                "llm_response_meta",
                f"Final retry LLM response ({len(final_content)} chars)",
                {
                    "final_fallback": True,
                    "retry_after_empty": True,
                    "content_chars": len(final_content),
                },
            )
        except Exception:
            pass

    return _finalize_response_with_current_runtime_helpers(
        state=state,
        agent_name=agent_name,
        timeout_seconds=timeout_seconds,
        messages=[*messages, stop_msg],
        draft_response=final_response,
        schema=final_response_schema,
        provider_policy=provider_key,
        resolved_model=resolved_llm,
    )
