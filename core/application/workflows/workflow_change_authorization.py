"""Per-operation authorization of a workflow change set.

The resulting definition is judged as a whole by ``apply_workflow_changes``;
what each operation may *name* is decided here, before that call, for the
builder, the API and MCP alike. A new step must be of a kind the platform
runs, and every agent, child workflow, connection and extension install an
operation references must be one the workflow's account may use. A
reference that does not exist and one owned by another account are refused
with the same issue, so the answer never tells whether a foreign id exists.

Only what an operation carries is checked: a config change that leaves an
agent alone does not re-judge it, so a step broken from outside can still be
repaired.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from core.application.workflows.workflow_definition_service import (
    WorkflowDefinitionChangeError,
)
from core.application.workflows.workflow_step_kinds import SUPPORTED_STEP_KINDS

# Operations whose payload is a step, and the key that holds it.
STEP_PAYLOAD_KEYS = {
    "create_step": "step",
    "insert_step_between": "step",
    "change_step_kind": "step",
    "update_step_config": "patch",
}
# Operations that name a step kind, which must be one the platform runs.
_NEW_STEP_OPERATIONS = frozenset({"create_step", "insert_step_between", "change_step_kind"})

REFERENCE_UNAVAILABLE = "workflow.change.reference_unavailable"
STEP_KIND_UNSUPPORTED = "workflow.change.step_kind_unsupported"


@dataclass(frozen=True)
class ChangeReferenceAccess:
    """Whether the workflow's account may use a referenced resource."""

    agent: Callable[[str], bool]
    workflow: Callable[[str], bool]
    connection: Callable[[str], bool]
    install: Callable[[str], bool]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def step_references(step: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """(kind, id, path suffix) of every resource a step payload names."""
    override = _mapping(step.get("override"))
    connector = _mapping(override.get("connector"))
    invoke = _mapping(override.get("invoke_workflow"))
    binding = _mapping(
        step.get("connection_binding_jsonb")
        or step.get("connection_binding")
        or connector.get("connection_binding_jsonb")
        or connector.get("connection_binding")
    )
    candidates = [
        ("agent", step.get("agent_id"), "agent_id"),
        ("workflow", invoke.get("workflow_id"), "override.invoke_workflow.workflow_id"),
        ("install", step.get("connector_install_id"), "connector_install_id"),
        (
            "install",
            connector.get("connector_install_id"),
            "override.connector.connector_install_id",
        ),
        ("connection", binding.get("connection_id"), "connection_binding.connection_id"),
    ]
    return [(kind, _text(value), path) for kind, value, path in candidates if _text(value)]


def authorize_change_operations(operations: Sequence[Any], access: ChangeReferenceAccess) -> None:
    """Refuse the whole set when one operation names what it may not use."""
    issues: list[dict[str, str]] = []
    for index, operation in enumerate(operations):
        if not isinstance(operation, Mapping):
            continue  # the change service refuses the shape itself
        op = _text(operation.get("op"))
        payload_key = STEP_PAYLOAD_KEYS.get(op)
        payload = operation.get(payload_key) if payload_key else None
        if not isinstance(payload, Mapping):
            continue
        path = f"operations[{index}].{payload_key}"
        step_kind = _text(payload.get("step_kind")).lower()
        if op in _NEW_STEP_OPERATIONS and step_kind and step_kind not in SUPPORTED_STEP_KINDS:
            issues.append(
                {
                    "code": STEP_KIND_UNSUPPORTED,
                    "message": f"Step kind '{step_kind}' is not supported",
                    "path": f"{path}.step_kind",
                }
            )
        for kind, reference_id, suffix in step_references(payload):
            if getattr(access, kind)(reference_id):
                continue
            issues.append(
                {
                    "code": REFERENCE_UNAVAILABLE,
                    "message": f"The referenced {kind} '{reference_id}' is not available",
                    "path": f"{path}.{suffix}",
                }
            )
    if issues:
        raise WorkflowDefinitionChangeError(
            "The change set references what this workflow may not use; nothing was saved",
            issues,
        )


def authorize_definition_references(
    steps: Sequence[Any],
    access: ChangeReferenceAccess,
    *,
    current_steps: Sequence[Any] = (),
) -> None:
    """Refuse a whole definition — a restored revision — that names what it may not use.

    A history entry is not a grant: every step kind and resource it brings back
    is judged now, for this request, as Apply judges the steps an operation
    carries. What the current definition already names is left alone, as Apply
    leaves a step it does not touch.
    """
    current = {
        (kind, reference_id)
        for step in current_steps
        if isinstance(step, Mapping)
        for kind, reference_id, _suffix in step_references(step)
    }
    issues: list[dict[str, str]] = []
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            continue
        path = f"steps.{_text(step.get('step_id')) or f'[{index}]'}"
        step_kind = _text(step.get("step_kind")).lower()
        if step_kind and step_kind not in SUPPORTED_STEP_KINDS:
            issues.append(
                {
                    "code": STEP_KIND_UNSUPPORTED,
                    "message": f"Step kind '{step_kind}' is not supported",
                    "path": f"{path}.step_kind",
                }
            )
        for kind, reference_id, suffix in step_references(step):
            if (kind, reference_id) in current or getattr(access, kind)(reference_id):
                continue
            issues.append(
                {
                    "code": REFERENCE_UNAVAILABLE,
                    "message": f"The referenced {kind} '{reference_id}' is not available",
                    "path": f"{path}.{suffix}",
                }
            )
    if issues:
        raise WorkflowDefinitionChangeError(
            "The revision references what this workflow may not use; nothing was saved",
            issues,
        )


def _account_matches(row: Mapping[str, Any], account_id: str) -> bool:
    row_account = _text(row.get("account_id"))
    return not row_account or row_account == account_id


def store_reference_access(
    db: Any,
    *,
    account_id: str,
    workflow_visible: Callable[[Mapping[str, Any]], bool],
    project_visible: Callable[[str], bool],
) -> ChangeReferenceAccess:
    """Reference checks against the registry store, in the workflow's account.

    *workflow_visible* is the caller's request-scope check of a workflow row
    (granted workflows and projects), applied to a child workflow on top of
    the account match; *project_visible* is its check of the project a
    connection belongs to.
    """
    from core.application.registry.step_skill_resolution import catalog_agent_row
    from core.application.registry.workflow_lookup import get_workflow_row
    from core.application.workflows.extension_connection_validation import (
        _get_connection_metadata,
        _get_install_contract,
    )

    def agent(agent_id: str) -> bool:
        row = catalog_agent_row(db, agent_id, account_id=account_id)
        if not isinstance(row, Mapping):
            return False
        return _text(row.get("scope_kind")) == "system" or _account_matches(row, account_id)

    def workflow(workflow_id: str) -> bool:
        row = get_workflow_row(db, workflow_id, account_id=account_id)
        return (
            isinstance(row, Mapping) and _account_matches(row, account_id) and workflow_visible(row)
        )

    def connection(connection_id: str) -> bool:
        row = _get_connection_metadata(db, connection_id)
        return (
            isinstance(row, Mapping)
            and _text(row.get("account_id")) == account_id
            and project_visible(_text(row.get("project_id")))
        )

    def install(install_id: str) -> bool:
        contract, authority = _get_install_contract(
            db, install_id=install_id, account_id=account_id
        )
        if not authority:
            return True  # a store that cannot answer leaves it to readiness
        return isinstance(contract, Mapping) and _account_matches(contract, account_id)

    return ChangeReferenceAccess(
        agent=agent, workflow=workflow, connection=connection, install=install
    )


__all__ = [
    "REFERENCE_UNAVAILABLE",
    "STEP_KIND_UNSUPPORTED",
    "STEP_PAYLOAD_KEYS",
    "ChangeReferenceAccess",
    "authorize_change_operations",
    "authorize_definition_references",
    "step_references",
    "store_reference_access",
]
