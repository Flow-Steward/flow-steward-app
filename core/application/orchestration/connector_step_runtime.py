# mypy: ignore-errors
"""Connector execution entry and authorized secret-reference dispatch boundary."""

from __future__ import annotations

from typing import Any

from core.application.extensions.secret_reflection import collect_secret_values
from core.application.orchestration.connector_events import (
    _runtime_ids_from_state,
    _shared_job_store,
    _shared_logger,
)
from core.application.orchestration.connector_extension_runtime import _dispatch_extension_runtime
from core.application.orchestration.connector_mapping import (
    _apply_output_mapping,
    _dependency_suppressed_result,
    _project_retry_signal,
    _render_input_payload,
    _suppressed_input_dependencies,
    _to_object,
)
from core.application.orchestration.connector_tool_runtime import _dispatch_tool_runtime
from core.application.orchestration.state_paths import StatePathError
from core.application.orchestration.step_activity import step_activity_heartbeat

_MESSAGE_UNNAMED_STEP = "<unnamed step>"


def _read_secret_ciphertext(account_id: str, ref_key: str) -> str | None:
    """Read one encrypted value from the account-scoped secret store."""
    from core.infrastructure.db.model_provider_store import get_secret_ref_ciphertext

    store = _shared_logger()
    if store is None:
        return None
    return get_secret_ref_ciphertext(store.conn, account_id=account_id, ref_key=ref_key)


def _decrypt_secret_value(ciphertext: str) -> str:
    from core.infrastructure.security.secret_codec import decrypt_secret

    return decrypt_secret(ciphertext)


def _presented_secret_ref_keys(value: Any) -> set[str]:
    from core.infrastructure.security.secret_reference import as_secret_reference

    reference = as_secret_reference(value)
    if reference is not None:
        return {reference.ref_key} if reference.ref_key else set()
    if isinstance(value, dict):
        return set().union(*(_presented_secret_ref_keys(item) for item in value.values()))
    if isinstance(value, (list, tuple)):
        return set().union(*(_presented_secret_ref_keys(item) for item in value))
    return set()


def _authorized_secret_ref_keys(
    runtime_state: dict[str, Any], input_payload: Any = None
) -> frozenset[str]:
    """Authorize presented refs against the immutable launch snapshot."""
    from core.application.registry.workflow_secret_defaults import secret_ref_fingerprint

    snapshot = {
        str(item or "").strip()
        for item in (runtime_state.get("_workflow_secret_ref_fingerprints") or [])
        if str(item or "").strip()
    }
    return frozenset(
        ref
        for ref in _presented_secret_ref_keys(input_payload)
        if secret_ref_fingerprint(ref) in snapshot
    )


def _payload_for_dispatch(
    input_payload: dict[str, Any], runtime_state: dict[str, Any]
) -> tuple[Any, frozenset[str], bool]:
    """Exchange secret references for values, for the call only.

    This is the authorized boundary: everything upstream of it carries
    references, and the payload reported back into logs, job output and Test Run
    keeps them. Resolving in place would put the credential into the very field
    that exists to say what was sent.
    """
    from core.infrastructure.security.secret_reference import resolve_secret_references_in

    runtime_context = dict(runtime_state.get("runtime_context") or {})
    resolved_payload = resolve_secret_references_in(
        input_payload,
        account_id=str(runtime_context.get("account_id") or "").strip(),
        authorized_ref_keys=_authorized_secret_ref_keys(runtime_state, input_payload),
        read_ciphertext=_read_secret_ciphertext,
        decrypt=_decrypt_secret_value,
        authorized_inline_ref_fingerprints=frozenset(
            str(item or "").strip()
            for item in (runtime_state.get("_workflow_secret_ref_fingerprints") or [])
            if str(item or "").strip()
        ),
    )
    protected_values, budget_exceeded = _resolved_secret_values(input_payload, resolved_payload)
    return resolved_payload, protected_values, budget_exceeded


def _resolved_secret_values(presented: Any, resolved: Any) -> tuple[frozenset[str], bool]:
    """Collect only values produced by resolving presented secret references."""
    from core.infrastructure.security.secret_reference import as_secret_reference

    values: list[Any] = []
    pending: list[tuple[Any, Any]] = [(presented, resolved)]
    while pending:
        presented_node, resolved_node = pending.pop()
        if as_secret_reference(presented_node) is not None:
            values.append(resolved_node)
            continue
        if isinstance(presented_node, dict) and isinstance(resolved_node, dict):
            pending.extend(
                (nested, resolved_node.get(key))
                for key, nested in presented_node.items()
                if key in resolved_node
            )
        elif isinstance(presented_node, (list, tuple)) and isinstance(resolved_node, (list, tuple)):
            pending.extend(zip(presented_node, resolved_node, strict=False))
    return collect_secret_values(values)


def execute_connector_step(
    *,
    step: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute connector step through tool or extension runtimes."""
    step_payload = dict(step or {})
    runtime_state = dict(state or {})
    connector_id = str(step_payload.get("connector_id") or "").strip()
    if not connector_id:
        return {
            "ok": False,
            "runtime": "unresolved",
            "detail": "connector_id_missing",
        }

    override = _to_object(step_payload.get("override"))
    connector_override = _to_object(override.get("connector"))
    raw_input_mapping = (
        _to_object(step_payload.get("input_mapping_jsonb"))
        or _to_object(connector_override.get("input_mapping_jsonb"))
        or _to_object(connector_override.get("input_mapping"))
    )
    raw_connection_binding = (
        _to_object(step_payload.get("connection_binding_jsonb"))
        or _to_object(connector_override.get("connection_binding_jsonb"))
        or _to_object(connector_override.get("connection_binding"))
    )
    try:
        input_payload, unresolved_inputs = _render_input_payload(raw_input_mapping, runtime_state)
    except StatePathError as exc:
        # A selector that is not valid syntax is an authoring mistake, and it
        # used to leave the step as an unhandled exception — the operator saw a
        # crash rather than the name of the selector they mistyped.
        step_id = str(step_payload.get("step_id") or "").strip() or _MESSAGE_UNNAMED_STEP
        return {
            "ok": False,
            "runtime": "unresolved",
            "connector_id": connector_id,
            "error": "invalid_input_selector",
            "detail": f"Step '{step_id}' has an input selector that is not valid: {exc}",
        }
    function_name = str(connector_override.get("function_name") or "").strip()
    operation_id = str(connector_override.get("operation_id") or "").strip()
    extension_id = str(connector_override.get("extension_id") or "").strip()
    connector_install_id = str(step_payload.get("connector_install_id") or "").strip()
    action_id = str(connector_override.get("action_id") or "").strip()

    test_mode = _runtime_ids_from_state(runtime_state).get("test_mode") is True
    suppressed_dependencies = (
        _suppressed_input_dependencies(unresolved_inputs, runtime_state) if test_mode else {}
    )
    if suppressed_dependencies:
        dependency_suppressed = _dependency_suppressed_result(
            connector_id=connector_id,
            input_payload=input_payload,
            unresolved_inputs=unresolved_inputs,
            dependencies=suppressed_dependencies,
            connection_binding=raw_connection_binding,
        )
        _apply_output_mapping(
            step_payload=step_payload,
            dispatch_result=dependency_suppressed,
            state=runtime_state,
        )
        return _project_retry_signal(dependency_suppressed)

    suppressed_dispatch = None
    if test_mode:
        suppressed_dispatch = _dispatch_tool_runtime(
            connector_id=connector_id,
            connector_install_id=connector_install_id,
            function_name=function_name,
            operation_id=operation_id,
            input_payload=input_payload,
            event_input_payload=input_payload,
            protected_input_secrets=frozenset(),
            protected_input_secret_budget_exceeded=False,
            connection_binding=raw_connection_binding,
            state=runtime_state,
            suppression_probe_only=True,
        )
        if not (
            suppressed_dispatch
            and (suppressed_dispatch.get("handled") or suppressed_dispatch.get("manifest_matched"))
        ):
            suppressed_dispatch = _dispatch_extension_runtime(
                connector_id=connector_id,
                extension_id=extension_id,
                connector_install_id=connector_install_id,
                action_id=action_id,
                operation_id=operation_id,
                input_payload=input_payload,
                event_input_payload=input_payload,
                protected_input_secrets=frozenset(),
                protected_input_secret_budget_exceeded=False,
                connection_binding=raw_connection_binding,
                state=runtime_state,
                suppression_probe_only=True,
            )
    if suppressed_dispatch and suppressed_dispatch.get("handled"):
        suppressed_dispatch["connector_id"] = connector_id
        suppressed_dispatch["input_payload"] = input_payload
        if unresolved_inputs:
            # A preceding suppressed mutation intentionally has no provider
            # result to map into state. Once the runtime has proved this
            # operation is also suppressed, its unresolved dependent values
            # are diagnostic context rather than a reason to fail the run.
            suppressed_dispatch["input_mapping_unresolved"] = dict(unresolved_inputs)
        suppressed_dispatch["connection_binding"] = raw_connection_binding
        _apply_output_mapping(
            step_payload=step_payload,
            dispatch_result=suppressed_dispatch,
            state=runtime_state,
        )
        return _project_retry_signal(suppressed_dispatch)

    if unresolved_inputs:
        # Fail closed, before any real dispatch. Omitting the field and calling
        # anyway lets a connector act on data the workflow never had. Test mode
        # is the sole exception above, and only after the operation contract has
        # positively identified this exact operation as suppressed.
        step_id = str(step_payload.get("step_id") or "").strip() or _MESSAGE_UNNAMED_STEP
        named = ", ".join(
            f"{field} from {selector}" for field, selector in sorted(unresolved_inputs.items())
        )
        return {
            "ok": False,
            "runtime": "unresolved",
            "connector_id": connector_id,
            "error": "unresolved_required_input",
            "detail": (
                f"Step '{step_id}' could not resolve {named}. The connector was not called."
            ),
            "input_mapping_unresolved": dict(unresolved_inputs),
            "input_payload": input_payload,
        }

    from core.infrastructure.security.secret_reference import (
        SecretReferenceUnauthorized,
        SecretReferenceUnresolved,
    )

    try:
        (
            dispatch_payload,
            protected_input_secrets,
            protected_input_secret_budget_exceeded,
        ) = _payload_for_dispatch(input_payload, runtime_state)
    except (SecretReferenceUnauthorized, SecretReferenceUnresolved) as exc:
        # A configured secret with nothing behind it. Calling anyway would send a
        # credential that is not one, and the failure would come back as
        # somebody else's authentication error.
        step_id = str(step_payload.get("step_id") or "").strip() or _MESSAGE_UNNAMED_STEP
        return {
            "ok": False,
            "runtime": "unresolved",
            "connector_id": connector_id,
            "error": "unresolved_secret_input",
            "detail": f"Step '{step_id}' could not resolve a secret input. {exc}",
            "input_payload": input_payload,
        }
    activity_ids = _runtime_ids_from_state(runtime_state)
    activity_job_id = activity_ids.get("job_id")
    step_label = (
        str(activity_ids.get("step_id") or step_payload.get("step_id") or "").strip()
        or connector_id
    )
    with step_activity_heartbeat(_shared_job_store(), job_id=activity_job_id, label=step_label):
        tool_dispatch = _dispatch_tool_runtime(
            connector_id=connector_id,
            connector_install_id=connector_install_id,
            function_name=function_name,
            operation_id=operation_id,
            input_payload=dispatch_payload,
            event_input_payload=input_payload,
            protected_input_secrets=protected_input_secrets,
            protected_input_secret_budget_exceeded=protected_input_secret_budget_exceeded,
            connection_binding=raw_connection_binding,
            state=runtime_state,
        )
    if tool_dispatch and tool_dispatch.get("handled"):
        tool_dispatch["connector_id"] = connector_id
        tool_dispatch["input_payload"] = input_payload
        if unresolved_inputs:
            tool_dispatch["input_mapping_unresolved"] = dict(unresolved_inputs)
        tool_dispatch["connection_binding"] = raw_connection_binding
        _apply_output_mapping(
            step_payload=step_payload,
            dispatch_result=tool_dispatch,
            state=runtime_state,
        )
        return _project_retry_signal(tool_dispatch)

    with step_activity_heartbeat(_shared_job_store(), job_id=activity_job_id, label=step_label):
        extension_dispatch = _dispatch_extension_runtime(
            connector_id=connector_id,
            extension_id=extension_id,
            connector_install_id=connector_install_id,
            action_id=action_id,
            operation_id=operation_id,
            input_payload=dispatch_payload,
            event_input_payload=input_payload,
            protected_input_secrets=protected_input_secrets,
            protected_input_secret_budget_exceeded=protected_input_secret_budget_exceeded,
            connection_binding=raw_connection_binding,
            state=runtime_state,
        )
    if extension_dispatch and extension_dispatch.get("handled"):
        extension_dispatch["connector_id"] = connector_id
        extension_dispatch["input_payload"] = input_payload
        if unresolved_inputs:
            extension_dispatch["input_mapping_unresolved"] = dict(unresolved_inputs)
        extension_dispatch["connection_binding"] = raw_connection_binding
        _apply_output_mapping(
            step_payload=step_payload,
            dispatch_result=extension_dispatch,
            state=runtime_state,
        )
        return _project_retry_signal(extension_dispatch)

    return {
        "ok": False,
        "runtime": "unresolved",
        "connector_id": connector_id,
        "input_payload": input_payload,
        "input_mapping_unresolved": dict(unresolved_inputs),
        "connection_binding": raw_connection_binding,
        "detail": "No connector runtime backend matched this connector_id",
    }
