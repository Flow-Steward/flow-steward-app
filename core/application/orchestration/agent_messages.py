"""Helpers for AgentMessageV1 state access and strict selector resolution."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.orchestration.models import AgentMessageV1, validate_agent_message_payload


class MessageSelectorError(ValueError):
    """Raised when a strict message selector cannot be resolved."""


def get_state_messages(state: Mapping[str, Any] | None) -> list[dict]:
    """Return validated AgentMessageV1 payloads from state.

    Fallback behavior:
    - If job_id exists, merge validated state messages with persisted MessageBus DB rows.
      This keeps in-memory-yet-unpersisted envelopes while also healing incomplete
      checkpoint state after retries/restarts.
    """

    if not isinstance(state, Mapping):
        return []
    raw = state.get("agent_messages")
    out: list[dict] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            model, errors = validate_agent_message_payload(item)
            if model and not errors:
                out.append(model.model_dump())
    job_id = state.get("job_id")
    jid = str(job_id or "").strip()
    if not jid:
        return _sort_messages_by_recency(out)

    try:
        from core.application.orchestration.message_bus import MessageBus

        bus = MessageBus.for_state(state)
        persisted = bus.list_messages(jid, limit=2000)
    except Exception:
        return out

    if not persisted:
        return _sort_messages_by_recency(out)

    merged: list[dict] = []
    seen: set[tuple[Any, ...]] = set()
    # Runtime/checkpoint messages are authoritative for an active execution.
    # The MessageBus copy is intentionally redacted for display persistence;
    # preferring it for the same correlation_id can corrupt actionable review
    # feedback (for example, a concrete bind address becomes a PII token).
    for item in out:
        if not isinstance(item, dict):
            continue
        identity = _message_identity(item)
        if identity in seen:
            continue
        merged.append(item)
        seen.add(identity)
    for item in persisted:
        identity = _message_identity(item)
        if identity in seen:
            continue
        merged.append(item)
        seen.add(identity)
    return _sort_messages_by_recency(merged)


def _message_identity(message: Mapping[str, Any]) -> tuple[Any, ...]:
    correlation_id = str(message.get("correlation_id") or "").strip()
    if correlation_id:
        return ("correlation_id", correlation_id, "", "", "", "", 0)
    published_at = _message_timestamp(message)
    return (
        "fallback",
        str(message.get("from_agent") or "").strip(),
        str(message.get("step_execution_id") or message.get("step_id") or "").strip(),
        str(message.get("skill") or "").strip(),
        str(message.get("status") or "").strip(),
        str(message.get("summary") or "").strip(),
        published_at,
    )


def _message_id(message: Mapping[str, Any]) -> int:
    for key in ("id", "message_id"):
        raw_value = message.get(key)
        if raw_value in (None, ""):
            continue
        try:
            parsed = int(str(raw_value))
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return -1


def _message_timestamp(message: Mapping[str, Any]) -> str:
    timestamps = message.get("timestamps")
    timestamps = timestamps if isinstance(timestamps, Mapping) else {}
    return str(
        message.get("published_at")
        or message.get("created_at")
        or message.get("timestamp")
        or timestamps.get("published_at")
        or timestamps.get("completed_at")
        or timestamps.get("started_at")
        or ""
    )


def _message_recency_key(message: Mapping[str, Any], index: int) -> tuple[int, str, int]:
    return (_message_id(message), _message_timestamp(message), index)


def _sort_messages_by_recency(messages: list[dict]) -> list[dict]:
    """Return messages in oldest-to-newest order, independent of merge order."""
    return [
        message
        for index, message in sorted(
            enumerate(messages or []),
            key=lambda item: _message_recency_key(item[1], item[0]),
        )
    ]


def latest_message(
    messages: list[dict],
    *,
    from_agent: str | None = None,
    step_id: str | None = None,
    group_id: str | None = None,
    step_execution_id: str | None = None,
) -> dict | None:
    """Return the most recent message matching optional filters."""
    matches: list[tuple[tuple[int, str, int], dict]] = []
    for index, msg in enumerate(messages or []):
        if not isinstance(msg, dict):
            continue
        if from_agent and str(msg.get("from_agent") or "") != from_agent:
            continue
        if step_id and str(msg.get("step_id") or "") != step_id:
            continue
        if group_id and str(msg.get("group_id") or "") != group_id:
            continue
        if step_execution_id and str(msg.get("step_execution_id") or "") != step_execution_id:
            continue
        matches.append((_message_recency_key(msg, index), msg))
    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def latest_message_with_artifact(
    messages: list[dict],
    *,
    step_id: str | None = None,
    from_agent: str | None = None,
    artifact_key: str,
) -> dict | None:
    """Return latest message that includes a given artifact key."""
    matches: list[tuple[tuple[int, str, int], dict]] = []
    for index, msg in enumerate(messages or []):
        if not isinstance(msg, dict):
            continue
        if step_id and str(msg.get("step_id") or "") != step_id:
            continue
        if from_agent and str(msg.get("from_agent") or "") != from_agent:
            continue
        artifacts = msg.get("artifacts")
        if isinstance(artifacts, dict) and artifact_key in artifacts:
            matches.append((_message_recency_key(msg, index), msg))
    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def message_artifacts(message: Mapping[str, Any] | None) -> dict:
    """Return artifacts dict from message payload."""
    if not isinstance(message, Mapping):
        return {}
    artifacts = message.get("artifacts")
    return artifacts if isinstance(artifacts, dict) else {}


def _resolve_nested(data: Any, parts: list[str]) -> Any:
    current = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


def resolve_message_selector(path: str, state: Mapping[str, Any]) -> Any:
    """Resolve strict selectors from message stream.

    Supported formats:
    - message.agent.<agent_id>.artifacts.<key>[.<nested>...]
    - message.agent.<agent_id>.<field>
    - message.step.<step_id>.artifacts.<key>[.<nested>...]
    - message.step.<step_id>.<field>
    - message.group.<group_id>.artifacts.<key>[.<nested>...] (latest step of the group)
    - message.group.<group_id>.<field>
    """
    messages = get_state_messages(state)
    parts = str(path or "").split(".")
    if len(parts) < 2:
        raise MessageSelectorError(f"Invalid selector '{path}'")

    if parts[0] != "message" or len(parts) < 4:
        raise MessageSelectorError(f"Unsupported selector '{path}'")

    scope = parts[1]
    ident = parts[2]
    tail = parts[3:]

    if scope == "agent":
        msg = latest_message(messages, from_agent=ident)
        if msg is None:
            raise MessageSelectorError(f"No message found for agent '{ident}'")
    elif scope == "step":
        msg = latest_message(messages, step_id=ident)
        if msg is None:
            raise MessageSelectorError(f"No message found for step '{ident}'")
    elif scope == "group":
        msg = latest_message(messages, group_id=ident)
        if msg is None:
            raise MessageSelectorError(f"No message found for group '{ident}'")
    else:
        raise MessageSelectorError(f"Unsupported selector scope '{scope}'")

    if not tail:
        return msg
    value = _resolve_nested(msg, tail)
    if value is None:
        raise MessageSelectorError(
            f"Missing required selector path '{'.'.join(tail)}' in {scope} '{ident}'"
        )
    return value


def build_agent_message(
    *,
    from_agent: str,
    to_agent: str,
    skill: str,
    status: str,
    correlation_id: str,
    caused_by: list[dict] | None = None,
    artifacts: dict | None = None,
    operation_retry: dict | None = None,
    output_contract: dict | None = None,
    summary: str = "",
    errors: list[str] | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    published_at: str,
    step_id: str | None = None,
    step_execution_id: str | None = None,
    attempt: int | None = None,
    group_id: str | None = None,
    for_each_id: str | None = None,
    item_index: int | None = None,
) -> dict:
    """Build and validate AgentMessageV1 payload."""
    payload = {
        "schema_version": "agent_message_v1",
        "from_agent": from_agent,
        "to_agent": to_agent,
        "step_id": step_id,
        "step_execution_id": step_execution_id,
        "attempt": attempt,
        "group_id": group_id,
        "for_each_id": for_each_id,
        "item_index": item_index,
        "skill": skill,
        "status": status,
        "correlation_id": correlation_id,
        "caused_by": [dict(x) for x in (caused_by or []) if isinstance(x, dict)],
        "artifacts": artifacts or {},
        "operation_retry": operation_retry,
        "output_contract": output_contract,
        "summary": summary or "",
        "errors": [str(e) for e in (errors or []) if str(e).strip()],
        "timestamps": {
            "started_at": started_at,
            "completed_at": completed_at,
            "published_at": published_at,
        },
    }
    model = AgentMessageV1.model_validate(payload)
    return model.model_dump()
