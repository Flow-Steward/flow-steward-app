"""Provider-neutral readiness validation for saved extension connector bindings."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, NamedTuple

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.connection_viability import project_connection_is_viable
from core.application.extensions.json_schema_subset import validate_json_schema_value
from core.application.extensions.operation_support import (
    extension_operation_contract,
)

_TOKEN_WORKFLOW_EXTENSION_UNAVAILABLE = "workflow.extension_unavailable"
_TOKEN_EXTENSION_PREFIX = "extension."


class NamedStepExtension(NamedTuple):
    """An extension a connector step names by its package id instead of an install."""

    extension_id: str
    operation_id: str
    connector_form: bool


def step_connector_install_id(step: Mapping[str, Any]) -> str:
    override = _mapping(step.get("override"))
    return _text(
        step.get("connector_install_id")
        or override.get("connector_install_id")
        or _mapping(override.get("connector")).get("connector_install_id")
    )


def named_extension_without_install(step: Mapping[str, Any]) -> NamedStepExtension | None:
    """The extension a connector step calls when it carries no connector_install_id.

    The runtime accepts three spellings: ``extension.<extension_id>`` with the
    operation in the connector override, the override's ``extension_id``, and a
    dotted ``<extension_id>.<operation_id>``. Only a canonical package id counts,
    so an install-scoped projected tool (``extension.<install>.<op>``) never does.
    A dotted id may still be a registry tool; callers that can tell check that.
    """
    from core.application.extensions.identity import is_canonical_extension_id

    if _text(step.get("step_kind") or step.get("kind")) not in {"", "connector"}:
        return None
    if step_connector_install_id(step):
        return None
    connector_id = _text(step.get("connector_id"))
    config = _connector_config(step)
    operation_id = _text(
        config.get("operation_id") or config.get("action_id") or config.get("function_name")
    )
    if connector_id.startswith(_TOKEN_EXTENSION_PREFIX):
        extension_id = connector_id.removeprefix(_TOKEN_EXTENSION_PREFIX)
        if is_canonical_extension_id(extension_id) and not _is_install_ref(
            extension_id.split(".", 1)[0]
        ):
            return NamedStepExtension(extension_id, operation_id, connector_form=True)
        return None
    if connector_id.startswith("connector."):
        return None
    extension_id = _text(config.get("extension_id"))
    if extension_id:
        if is_canonical_extension_id(extension_id):
            return NamedStepExtension(extension_id, operation_id, connector_form=False)
        return None
    extension_id, _, dotted_operation = connector_id.rpartition(".")
    if dotted_operation and is_canonical_extension_id(extension_id):
        return NamedStepExtension(
            extension_id, operation_id or dotted_operation, connector_form=False
        )
    return None


def step_extension_id(step: Mapping[str, Any]) -> str:
    """The extension a connector step comes from, to show its logo; '' when unnamed.

    Reads the override's ``extension_id`` first (the builder writes it for every
    extension action, install-pinned or not), then ``extension.<extension_id>``.
    A dotted ``<id>.<operation>`` is not used: it may name a registry tool.
    """
    from core.application.extensions.identity import is_canonical_extension_id

    if _text(step.get("step_kind") or step.get("kind")) not in {"", "connector"}:
        return ""
    extension_id = _text(_connector_config(step).get("extension_id"))
    if extension_id:
        return extension_id if is_canonical_extension_id(extension_id) else ""
    connector_id = _text(step.get("connector_id"))
    if not connector_id.startswith(_TOKEN_EXTENSION_PREFIX):
        return ""
    extension_id = connector_id.removeprefix(_TOKEN_EXTENSION_PREFIX)
    if is_canonical_extension_id(extension_id) and not _is_install_ref(
        extension_id.split(".", 1)[0]
    ):
        return extension_id
    return ""


def _is_install_ref(value: str) -> bool:
    if value.startswith("pi_"):
        return True
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def validate_extension_connector_step(
    db: Any,
    *,
    step: Mapping[str, Any],
    step_path: str,
    account_id: str = "",
) -> list[dict[str, str]]:
    connector_id = _text(step.get("connector_id"))
    if not connector_id.startswith(_TOKEN_EXTENSION_PREFIX):
        return _named_extension_install_issue(
            db, step=step, step_path=step_path, account_id=account_id
        )
    extension_id = connector_id.removeprefix(_TOKEN_EXTENSION_PREFIX)
    if not extension_id:
        return []
    config = _connector_config(step)
    operation_id = _text(
        config.get("operation_id") or config.get("action_id") or config.get("function_name")
    )
    binding = _connection_binding(step, config)
    connection_id = _text(binding.get("connection_id"))
    # Deliberately no early return for a missing binding: whether one is needed
    # is the operation's declaration to make, and returning ready here is what
    # made "leave the connection off" the way to get an invalid step saved.
    connection = _get_connection_metadata(db, connection_id) if connection_id else None
    install_id = _text(step.get("connector_install_id") or config.get("connector_install_id"))
    if not install_id:
        return [
            _issue(
                "workflow.extension_install_missing",
                "Choose the installed extension action again before saving this step.",
                f"{step_path}.connector_install_id",
            )
        ]

    contract, contract_authority = _get_install_contract(
        db,
        install_id=install_id,
        account_id=_text((connection or {}).get("account_id")) or DEFAULT_ACCOUNT_ID,
    )
    if not contract_authority:
        return []
    state_issue = _install_state_issue(
        contract, contract_authority=contract_authority, step_path=step_path
    )
    if state_issue is not None:
        return [state_issue]
    contract_extension_id = (
        _text(contract.get("extension_id")) if isinstance(contract, dict) else ""
    )
    if not isinstance(contract, dict) or not _connector_matches_contract_identity(
        connector_id,
        install_id=install_id,
        contract_extension_id=contract_extension_id,
    ):
        return [
            _issue(
                _TOKEN_WORKFLOW_EXTENSION_UNAVAILABLE,
                "The installed extension contract is unavailable.",
                f"{step_path}.connector_install_id",
            )
        ]
    extension_id = contract_extension_id
    projected_tool, is_projected_tool = _projected_extension_tool(
        db,
        connector_id=connector_id,
        install_id=install_id,
        account_id=_text(contract.get("account_id")) or DEFAULT_ACCOUNT_ID,
    )
    if is_projected_tool:
        if projected_tool is None or not _projected_tool_matches_contract(
            projected_tool,
            install_id=install_id,
            extension_id=extension_id,
        ):
            return [
                _issue(
                    _TOKEN_WORKFLOW_EXTENSION_UNAVAILABLE,
                    "The installed extension tool projection is unavailable.",
                    f"{step_path}.connector_id",
                )
            ]
        implementation = _mapping(projected_tool.get("implementation"))
        operation_id = _text(implementation.get("action"))

    operation = extension_operation_contract(contract, operation_id=operation_id)
    if not operation.declared:
        return [
            _issue(
                "workflow.extension_operation_unavailable",
                "The selected extension operation is not declared by this installed release.",
                f"{step_path}.override.connector.operation_id",
            )
        ]
    if not is_projected_tool and operation.input_mapping_rules:
        input_mapping = _mapping(
            step.get("input_mapping_jsonb")
            or config.get("input_mapping_jsonb")
            or config.get("input_mapping")
        )
        for rule in operation.input_mapping_rules:
            schema = _mapping(rule.get("schema"))
            input_errors: list[str] = []
            validate_json_schema_value(
                schema=schema,
                value=input_mapping,
                path=f"{step_path}.input_mapping_jsonb",
                errors=input_errors,
            )
            if not input_errors:
                continue
            error = _mapping(rule.get("error"))
            field = _text(error.get("field"))
            return [
                _issue(
                    _text(error.get("code")) or "workflow.extension_input_invalid",
                    _text(error.get("message")) or input_errors[0],
                    (
                        f"{step_path}.input_mapping_jsonb.{field}"
                        if field
                        else f"{step_path}.input_mapping_jsonb"
                    ),
                )
            ]
    if not is_projected_tool:
        output_issues = _validate_output_mapping(
            step=step,
            config=config,
            outputs=operation.outputs,
            step_path=step_path,
        )
        if output_issues:
            return output_issues
    connection_path = f"{step_path}.connection_binding_jsonb.connection_id"
    if operation.requires_connection and not connection_id:
        return [
            _issue(
                "workflow.connection_required",
                "Choose a connection for this extension action before running this workflow.",
                connection_path,
            )
        ]
    if not connection_id:
        # Declared, needs no connection, and none was bound. Nothing left to check.
        return []
    if not _connection_matches(
        connection,
        extension_id=extension_id,
        install_id=install_id,
        connection_type_ids=operation.connection_type_ids,
    ):
        return [
            _issue(
                "workflow.connection_unavailable",
                "The selected extension connection is no longer available.",
                connection_path,
            )
        ]
    # _connection_matches establishes this at runtime, but its predicate cannot
    # narrow the optional Mapping for static type checking.
    if connection is None:
        return []
    required_oauth_scopes = operation.required_oauth_scopes
    # The non-secret projection exposes the OAuth block as `oauth_metadata`;
    # richer readers nest it under `connection_config`. Reading both keeps the
    # projection free of the full configuration without losing the one field
    # readiness needs from it.
    oauth_metadata = _mapping(connection.get("oauth_metadata")) or _mapping(
        _mapping(connection.get("connection_config")).get("oauth")
    )
    lifecycle = _text(oauth_metadata.get("lifecycle_status")).lower()
    if lifecycle == "needs_attention":
        return [
            _issue(
                "oauth_needs_attention",
                "The selected OAuth connection needs attention before this workflow can run.",
                connection_path,
            )
        ]
    if lifecycle and lifecycle != "connected":
        return [
            _issue(
                "oauth_authorization_required",
                "Authorize the selected OAuth connection before this workflow can run.",
                connection_path,
            )
        ]
    raw_granted = connection.get("oauth_granted_scopes")
    granted_scopes = (
        {_text(value) for value in raw_granted if isinstance(value, str) and _text(value)}
        if isinstance(raw_granted, list)
        else set()
    )
    if not set(required_oauth_scopes).issubset(granted_scopes):
        return [
            _issue(
                "oauth_scope_required",
                "Authorize the scopes required by this extension operation before running it.",
                connection_path,
            )
        ]
    return []


def _named_extension_install_issue(
    db: Any,
    *,
    step: Mapping[str, Any],
    step_path: str,
    account_id: str,
) -> list[dict[str, str]]:
    """Flag a step that calls an installed extension by package id with no install.

    Such a step skipped this validator entirely. A test run then failed it with
    connection_unavailable and a real run with
    external_effect_install_identity_required. Only an id the account actually
    has installed is flagged, so a registry tool with a dotted id is left alone.
    """
    named = named_extension_without_install(step)
    if named is None:
        return []
    install_ids = extension_install_ids_for_account(
        db,
        extension_id=named.extension_id,
        account_id=_text(account_id) or DEFAULT_ACCOUNT_ID,
    )
    if not install_ids:
        return []
    choices = ", ".join(f"'{install_id}'" for install_id in install_ids)
    return [
        _issue(
            "workflow.extension_install_missing",
            (
                f"This step calls extension '{named.extension_id}' without "
                "connector_install_id, so a run cannot tell which install owns its "
                f"connection and external effects. Set connector_install_id to {choices}."
            ),
            f"{step_path}.connector_install_id",
        )
    ]


def extension_install_ids_for_account(
    db: Any,
    *,
    extension_id: str,
    account_id: str,
) -> list[str]:
    """Active installs of one extension visible to the account (own or global)."""
    getter = getattr(db, "list_extension_install_ids", None)
    if callable(getter):
        return [_text(value) for value in getter(extension_id, account_id=account_id) or []]
    connection = getattr(db, "conn", None)
    if connection is None:
        return []
    # Not extension_catalog_install: it drags the static catalog fetcher into the
    # worker slice. The same active-install filter, read straight from the store.
    from core.application.extensions.install import DBExtensionInstallStore, InstallState

    records = DBExtensionInstallStore(connection).list_for_account(
        account_id, include_global=True, include_inactive_owned=True
    )
    return sorted(
        {
            _text(getattr(record, "install_id", ""))
            for record in records
            if _text(getattr(record, "extension_id", "")) == extension_id
            and _text(getattr(record, "state", "")) == str(InstallState.active)
        }
        - {""}
    )


def _connector_matches_contract_identity(
    connector_id: str,
    *,
    install_id: str,
    contract_extension_id: str,
) -> bool:
    """Accept canonical extension IDs and install-scoped projected tool IDs."""
    normalized_connector_id = _text(connector_id)
    normalized_install_id = _text(install_id)
    normalized_extension_id = _text(contract_extension_id)
    if not normalized_connector_id or not normalized_extension_id:
        return False
    if normalized_connector_id == f"extension.{normalized_extension_id}":
        return True
    projected_prefix = f"extension.{normalized_install_id}."
    return bool(
        normalized_install_id
        and normalized_connector_id.startswith(projected_prefix)
        and _text(normalized_connector_id.removeprefix(projected_prefix))
    )


def _projected_extension_tool(
    db: Any,
    *,
    connector_id: str,
    install_id: str,
    account_id: str,
) -> tuple[dict[str, Any] | None, bool]:
    projected_prefix = f"extension.{_text(install_id)}."
    if not _text(install_id) or not _text(connector_id).startswith(projected_prefix):
        return None, False
    from core.application.registry.workflow_lookup import get_registry_artifact_row

    return (
        get_registry_artifact_row(
            db,
            artifact_kind="tool",
            id_key="tool_id",
            artifact_id=_text(connector_id),
            platform_getter="get_tool",
            account_id=account_id,
        ),
        True,
    )


def _projected_tool_matches_contract(
    tool: Mapping[str, Any],
    *,
    install_id: str,
    extension_id: str,
) -> bool:
    implementation = _mapping(tool.get("implementation"))
    return bool(
        _text(tool.get("source_install_id")) == _text(install_id)
        and _text(implementation.get("extension_key")) == _text(extension_id)
        and _text(implementation.get("action"))
    )


def _get_install_contract(
    db: Any,
    *,
    install_id: str,
    account_id: str,
) -> tuple[dict[str, Any] | None, bool]:
    getter = getattr(db, "get_extension_install_contract", None)
    if callable(getter):
        value = getter(install_id)
        return (dict(value) if isinstance(value, Mapping) else None), True
    connection = getattr(db, "conn", None)
    if connection is None:
        return None, False
    from core.application.extensions.workflow_install_contract import (
        resolve_workflow_extension_install_contract,
    )

    value = resolve_workflow_extension_install_contract(
        connection,
        install_id=install_id,
        account_id=account_id,
        default_account_id=DEFAULT_ACCOUNT_ID,
    )
    return value, True


def _connector_config(step: Mapping[str, Any]) -> dict[str, Any]:
    override = _mapping(step.get("override"))
    return _mapping(override.get("connector"))


def _connection_binding(step: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    return _mapping(
        step.get("connection_binding_jsonb")
        or step.get("connection_binding")
        or config.get("connection_binding_jsonb")
        or config.get("connection_binding")
    )


def _validate_output_mapping(
    *,
    step: Mapping[str, Any],
    config: Mapping[str, Any],
    outputs: tuple[dict[str, Any], ...],
    step_path: str,
) -> list[dict[str, str]]:
    output_mapping = _mapping(
        step.get("output_mapping_jsonb")
        or config.get("output_mapping_jsonb")
        or config.get("output_mapping")
    )
    if not output_mapping:
        return []
    declared = {
        _text(output.get("name")): output for output in outputs if _text(output.get("name"))
    }
    available = ", ".join(sorted(declared)) or "(none)"
    issues: list[dict[str, str]] = []
    for raw_selector in output_mapping:
        selector = _text(raw_selector)
        if _output_selector_is_declared(selector, declared):
            continue
        issues.append(
            _issue(
                "workflow.extension_output_mapping_invalid",
                (
                    f"Output '{selector}' is not declared by the installed extension operation. "
                    f"Choose one of: {available}."
                ),
                f"{step_path}.output_mapping_jsonb.{selector}",
            )
        )
    return issues


def _output_selector_is_declared(
    selector: str,
    declared: Mapping[str, dict[str, Any]],
) -> bool:
    def without_result_prefix(value: str) -> str:
        return value.removeprefix("result.")

    normalized_selector = without_result_prefix(selector)
    if normalized_selector in {without_result_prefix(name) for name in declared}:
        return True

    parts = normalized_selector.split(".")
    if not parts or any(not part for part in parts):
        return False
    first_name = parts[0][:-2] if parts[0].endswith("[]") else parts[0]
    simple_declared = {
        name[:-2] if name.endswith("[]") else name for name in declared if "." not in name
    }
    return first_name in simple_declared


def _get_connection_metadata(db: Any, connection_id: str) -> dict[str, Any] | None:
    """Read the non-secret metadata readiness needs about a connection.

    Readiness runs with `RegistryDBStore`, which implements neither
    `get_project_connection` nor any other reader this used to look for, so the
    answer in production was always `None` — every real connection read as
    unavailable. The narrow store port is tried first, then the same non-secret
    projection through the store's connection, so a store that can answer does.

    Nothing here loads credentials, `secret_refs`, or unrestricted connection
    configuration: readiness needs to know whether a connection is usable, not
    what it is authenticated with.
    """
    port = getattr(db, "get_extension_connection_metadata", None)
    if callable(port):
        row = port(connection_id)
        if isinstance(row, Mapping):
            return dict(row)
        return None
    connection = getattr(db, "conn", None)
    if connection is None:
        return None
    from core.infrastructure.db.project_connection_store import (
        get_project_connection_runtime_metadata,
    )

    row = get_project_connection_runtime_metadata(connection, connection_id)
    return dict(row) if isinstance(row, Mapping) else None


def _install_state_issue(
    contract: Any, *, contract_authority: bool, step_path: str
) -> dict[str, str] | None:
    """The install-state check, read from the contract that readiness already has.

    It used to depend on a `get_extension_install_state` getter that the runtime
    store does not implement, so the check was skipped in production every time.
    The resolved contract carries `install_state` on the real path, which makes
    it the one source that exists where the check has to run.
    """
    if not contract_authority:
        return None
    if not isinstance(contract, Mapping):
        return _issue(
            "workflow.extension_removed",
            "The installed extension is not active. Reinstall or enable it before running this workflow.",
            f"{step_path}.connector_install_id",
        )
    state = _text(contract.get("install_state")).lower() or "active"
    if state == "active":
        return None
    code = (
        "workflow.extension_disabled"
        if state == "disabled"
        else _TOKEN_WORKFLOW_EXTENSION_UNAVAILABLE
    )
    return _issue(
        code,
        "The installed extension is not active. Reinstall or enable it before running this workflow.",
        f"{step_path}.connector_install_id",
    )


def _connection_matches(
    connection: Mapping[str, Any] | None,
    *,
    extension_id: str,
    install_id: str,
    connection_type_ids: frozenset[str],
) -> bool:
    """Whether a bound connection is one this step may actually use.

    `connection_type_ids` is every type the operation declares. An operation can
    declare more than one — a file-transfer operation can work over FTP or
    SFTP — so this is a membership test, not equality against a single name.
    An empty set means the operation declares no connection requirement, and the
    identity and health checks still apply to a binding that is present.
    """
    if connection is None:
        return False
    if connection_type_ids and _text(connection.get("connection_type")) not in connection_type_ids:
        return False
    return (
        project_connection_is_viable(connection)
        and _text(connection.get("extension_id")) == extension_id
        and _text(connection.get("extension_install_id")) == install_id
    )


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _issue(code: str, message: str, path: str) -> dict[str, str]:
    return {"code": code, "message": message, "path": path}


def _text(value: Any) -> str:
    return str(value or "").strip()


__all__ = [
    "NamedStepExtension",
    "extension_install_ids_for_account",
    "named_extension_without_install",
    "step_connector_install_id",
    "step_extension_id",
    "validate_extension_connector_step",
]
