"""Guards for workflow state paths that must not be user-writable."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.orchestration.state_paths import StatePathError, parse_state_path

STATE_PREFIX = "state."

RESERVED_STATE_ROOTS: frozenset[str] = frozenset(
    {
        "_artifact_plane",
        "_test_mode_suppressed_outputs",
        "_trusted_runtime_context",
        "_workflow_secret_ref_fingerprints",
        "trusted_runtime_context",
        "external_effect_resolution_ref",
        "account_id",
        "actor_id",
        "artifact_plane",
        # Returned files (state.artifacts.<key>) are captured from the step
        # result ticked "Return from workflow"; no author step writes them.
        "artifacts",
        # The graph's conversation channels. state.messages goes through the
        # LangGraph chat reducer, so a step that writes plain records there (an
        # IMAP search did) fails the run on its first non-empty value.
        "agent_messages",
        "messages",
        # Hook outputs (state.hooks.<hook_id>) are published by the Invoke
        # runtime only; no author step writes them.
        "hooks",
        "job_id",
        "project_config",
        "project_id",
        "run_id",
        "step_test_run_context",
        "step_test_run_terminal",
        "tenant_id",
        "test_mode",
        "user_id",
        "workflow_id",
        "workflow_run_trace",
        "workflow_test_run_kept_outputs",
        "workflow_container_progress",
        "workspace_id",
    }
)


def canonical_state_write_parts(target: Any) -> tuple[str, ...] | None:
    """Return canonical mapping-key parts, or ``None`` for an unsafe malformed target."""
    normalized = str(target or "").strip()
    if not normalized:
        return ()
    try:
        tokens = parse_state_path(normalized)
    except StatePathError:
        return None
    if any(token.kind != "key" for token in tokens):
        return None
    parts = tuple(str(token.value) for token in tokens)
    return parts if all(parts) else None


def state_path_root(target: Any) -> str:
    parts = canonical_state_write_parts(target)
    return parts[0].strip() if parts else ""


def is_reserved_state_target(target: Any) -> bool:
    parts = canonical_state_write_parts(target)
    if parts is None:
        return True
    return bool(parts and parts[0].strip() in RESERVED_STATE_ROOTS)


def strip_reserved_runtime_state(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Remove caller-controlled runtime scope fields from an external state patch."""
    if not isinstance(payload, Mapping):
        return {}
    return {
        str(key): value
        for key, value in payload.items()
        if str(key).strip() not in RESERVED_STATE_ROOTS
    }


__all__ = [
    "RESERVED_STATE_ROOTS",
    "canonical_state_write_parts",
    "is_reserved_state_target",
    "state_path_root",
    "strip_reserved_runtime_state",
]
