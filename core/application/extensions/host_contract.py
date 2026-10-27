"""The host <-> extension runtime contract: its token and its execution scope.

``extension_host_v2`` is the one final contract of the first release. Every
envelope carries the four execution-scope keys, the handshake confirms them
exactly and a runtime refuses an envelope that does not. There is no second
parser, negotiation of alternative shapes or fallback (ADR 0038).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

# The host <-> extension runtime wire contract. A release descriptor declares it
# in ``runtime.contract_version``, the handshake negotiates it and every
# invocation/event envelope carries it. This module's file name is the Extension
# Platform v2 protocol, not this token's number; the descriptor schema, workflow
# bundle version and SDK package version are separate contracts. There is no
# fallback to an older token: a runtime that does not speak exactly this one is
# incompatible.
HOST_CONTRACT_VERSION = "extension_host_v2"
# Where a step sits in a run: the business id of its immediate Group or its
# immediate For each (at most one of them; both null for a root step), the step
# id and its logical execution. A Group- or For each-level event names only its
# container; a workflow-level scope has all four null. Every envelope carries
# all four keys, and the runtime confirms exactly this list in the handshake.
# There is no phase in this contract.
EXECUTION_SCOPE_FIELDS: tuple[str, ...] = (
    "group_id",
    "for_each_id",
    "step_id",
    "step_execution_id",
)
OWNER_SCOPE_FIELDS: tuple[str, ...] = ("group_id", "for_each_id")


def _optional_scope_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


class ExecutionScopeError(ValueError):
    """A scope that is not the final four-field execution scope."""


def execution_scope_error(scope: Mapping[str, Any] | None, *, complete: bool = True) -> str:
    """Why *scope* is not a final execution scope, or ``""``.

    With ``complete`` every one of the four keys must be present (an envelope
    as the runtime receives it). A step scope always names both owner keys: a
    missing owner is an error, never a hint that the step runs at the root.
    """
    source = scope if isinstance(scope, Mapping) else {}
    step_scoped = bool(
        _optional_scope_text(source.get("step_id"))
        or _optional_scope_text(source.get("step_execution_id"))
    )
    required = EXECUTION_SCOPE_FIELDS if complete or step_scoped else ()
    missing = [field for field in required if field not in source]
    if missing:
        return f"Execution scope misses '{', '.join(missing)}'"
    if all(_optional_scope_text(source.get(field)) for field in OWNER_SCOPE_FIELDS):
        return "Execution scope names both a Group and a For each as the immediate owner"
    if step_scoped and not (
        _optional_scope_text(source.get("step_id"))
        and _optional_scope_text(source.get("step_execution_id"))
    ):
        return "Execution scope names a step without its logical execution, or the reverse"
    return ""


def normalize_execution_scope(values: Mapping[str, Any] | None) -> dict[str, str | None]:
    """The four execution-scope fields, each a string or null, in contract order.

    ``step_execution_id`` is the run cursor's logical execution (a UUID the hub
    mints; it is put in canonical form here, never re-minted). ``group_id`` and
    ``for_each_id`` are the business ids of the immediate container as the
    run's definition names it, never a database UUID. A step scope must name
    both owner keys (null included); a missing or conflicting owner raises
    :class:`ExecutionScopeError` before any extension effect.
    """
    source = values if isinstance(values, Mapping) else {}
    problem = execution_scope_error(source, complete=False)
    if problem:
        raise ExecutionScopeError(problem)
    scope = {field: _optional_scope_text(source.get(field)) for field in EXECUTION_SCOPE_FIELDS}
    execution_id = scope["step_execution_id"]
    if execution_id is not None:
        try:
            scope["step_execution_id"] = str(uuid.UUID(execution_id))
        except ValueError:
            scope["step_execution_id"] = execution_id
    return scope


def canonical_envelope_scope(scope: Mapping[str, Any] | None) -> dict[str, Any]:
    """An envelope scope in the host contract: execution fields always present.

    A workflow, job, account and project scope passes through unchanged. The
    removed phase scope is never sent, not even as an empty key.
    """
    payload = {
        str(key): value
        for key, value in dict(scope or {}).items()
        if str(key) not in {"phase_id", *EXECUTION_SCOPE_FIELDS}
    }
    payload.update(normalize_execution_scope(scope))
    return payload


def build_handshake_request(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The handshake body: the host names the one contract it speaks."""
    request = dict(payload or {})
    request["contract_version"] = HOST_CONTRACT_VERSION
    request["execution_scope_fields"] = list(EXECUTION_SCOPE_FIELDS)
    return request


def handshake_contract_error(response: Mapping[str, Any] | None) -> str:
    """Why a successful handshake response did not confirm the host contract.

    The runtime confirms with ``result.contract_version`` equal to
    :data:`HOST_CONTRACT_VERSION` and ``result.execution_scope_fields`` equal to
    :data:`EXECUTION_SCOPE_FIELDS`, in order. This confirms the one final
    contract; it is not a negotiation. A missing or different value is an
    incompatible runtime; a health check never stands in for this confirmation.
    """
    result = (response or {}).get("result")
    confirmed = (
        str(result.get("contract_version") or "").strip() if isinstance(result, Mapping) else ""
    )
    if not confirmed:
        return f"Remote runtime did not confirm contract '{HOST_CONTRACT_VERSION}'"
    if confirmed != HOST_CONTRACT_VERSION:
        # The runtime's own text is never echoed into install state.
        return f"Remote runtime confirmed another contract; this host requires '{HOST_CONTRACT_VERSION}'"
    fields = result.get("execution_scope_fields") if isinstance(result, Mapping) else None
    if not isinstance(fields, list) or fields != list(EXECUTION_SCOPE_FIELDS):
        return (
            "Remote runtime did not confirm execution_scope_fields "
            f"{list(EXECUTION_SCOPE_FIELDS)} of contract '{HOST_CONTRACT_VERSION}'"
        )
    return ""


def envelope_contract_error(envelope: Mapping[str, Any] | None) -> str:
    """Runtime-side check: refuse an envelope of a foreign contract before executing."""
    declared = str((envelope or {}).get("contract_version") or "").strip()
    if declared == HOST_CONTRACT_VERSION:
        scope = (envelope or {}).get("scope")
        return execution_scope_error(scope if isinstance(scope, Mapping) else {})
    if not declared:
        return f"Envelope does not declare contract '{HOST_CONTRACT_VERSION}'"
    return f"Envelope contract '{declared}' is not '{HOST_CONTRACT_VERSION}'"


__all__ = [
    "EXECUTION_SCOPE_FIELDS",
    "HOST_CONTRACT_VERSION",
    "OWNER_SCOPE_FIELDS",
    "ExecutionScopeError",
    "build_handshake_request",
    "canonical_envelope_scope",
    "envelope_contract_error",
    "execution_scope_error",
    "handshake_contract_error",
    "normalize_execution_scope",
]
