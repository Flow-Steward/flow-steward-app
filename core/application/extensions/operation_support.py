"""Typed support functions shared by extension operation orchestration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.command_models import (
    CommandDispatchInput,
    ProviderEventIngestInput,
)
from core.application.management.scope import ScopeContext, normalize_scope

logger = logging.getLogger(__name__)


class ExtensionEventCommandService(Protocol):
    def ingest_provider_events(
        self,
        inp: ProviderEventIngestInput,
        *,
        scope: ScopeContext | None = None,
    ) -> Any: ...


class ExtensionEventCommandServiceFactory(Protocol):
    def __call__(self) -> ExtensionEventCommandService | None: ...


class ExtensionCommandDispatchService(Protocol):
    def dispatch(
        self,
        inp: CommandDispatchInput,
        *,
        scope: ScopeContext | None = None,
    ) -> Any: ...


class ExtensionCommandDispatchServiceFactory(Protocol):
    def __call__(
        self,
        *,
        get_logger: Callable[[], Any],
    ) -> ExtensionCommandDispatchService: ...


def extension_declared_scopes(extension: Any) -> set[str]:
    ext = extension if isinstance(extension, dict) else {}
    manifest = ext.get("compiled_manifest")
    manifest = manifest if isinstance(manifest, dict) else {}
    scopes: set[str] = set()
    for source in (ext.get("required_scopes"), manifest.get("required_scopes")):
        if isinstance(source, (list, tuple, set)):
            scopes.update(str(item).strip() for item in source if str(item or "").strip())
    return scopes


def extension_operation_declared_scopes(
    extension: Any,
    *,
    operation_type: str,
    operation_id: str,
) -> set[str]:
    op_id = str(operation_id or "").strip()
    op_type = str(operation_type or "").strip().lower()
    if not op_id or op_type not in {"query", "action"}:
        return set()
    ext = extension if isinstance(extension, dict) else {}
    manifest = ext.get("compiled_manifest")
    manifest = manifest if isinstance(manifest, dict) else {}
    key = "query_definitions" if op_type == "query" else "action_definitions"
    id_field = "query_id" if op_type == "query" else "action_id"
    rows: list[dict[str, Any]] = []
    for source in (ext.get(key), manifest.get(key)):
        if isinstance(source, (list, tuple)):
            rows.extend(row for row in source if isinstance(row, dict))
    scopes: set[str] = set()
    for row in rows:
        row_id = str(row.get(id_field) or row.get("id") or "").strip()
        if row_id != op_id:
            continue
        source = row.get("required_scopes")
        if isinstance(source, (list, tuple, set)):
            scopes.update(str(item).strip() for item in source if str(item or "").strip())
    return scopes


@dataclass(frozen=True)
class ExtensionOperationContract:
    """What one installed release says about a single operation.

    `declared` and `connection_type_ids` are deliberately separate. The previous
    projection returned an empty connection type for an operation that does not
    exist *and* for one that needs no connection, so readiness could not tell
    those apart: a declared action was rejected as unavailable whenever its
    valid connection was bound, and accepted when the connection was left off.
    """

    declared: bool
    operation_kind: str = ""
    connection_type_ids: frozenset[str] = frozenset()
    required_oauth_scopes: frozenset[str] = frozenset()
    input_mapping_rules: tuple[dict[str, Any], ...] = ()
    outputs: tuple[dict[str, Any], ...] = ()

    @property
    def requires_connection(self) -> bool:
        return bool(self.connection_type_ids)

    def accepts_connection_type(self, connection_type: str) -> bool:
        """Whether a bound connection's type is one this operation declares."""
        return str(connection_type or "").strip() in self.connection_type_ids


def _declared_connection_type_ids(operation: dict[str, Any]) -> frozenset[str]:
    """Every connection type this operation accepts."""
    plural = operation.get("connection_type_ids")
    values = plural if isinstance(plural, (list, tuple, set, frozenset)) else ()
    return frozenset(
        str(value).strip() for value in values if isinstance(value, str) and str(value).strip()
    )


def extension_operation_contract(
    extension: Any,
    *,
    operation_id: str,
) -> ExtensionOperationContract:
    """Project one operation's declaration from an immutable installed release."""
    normalized_operation_id = str(operation_id or "").strip()
    if not normalized_operation_id:
        return ExtensionOperationContract(declared=False)
    ext = extension if isinstance(extension, dict) else {}
    compiled = ext.get("compiled_manifest")
    compiled = compiled if isinstance(compiled, dict) else {}
    rows: list[dict[str, Any]] = []
    for source in (ext.get("operation_manifest"), compiled.get("operation_manifest")):
        if isinstance(source, (list, tuple)):
            rows.extend(row for row in source if isinstance(row, dict))
    matches = [
        row for row in rows if str(row.get("operation_id") or "").strip() == normalized_operation_id
    ]
    if len(matches) != 1:
        # Zero means undeclared. More than one means the release contradicts
        # itself about the same operation, which is not a declaration either.
        return ExtensionOperationContract(declared=False)
    operation = matches[0]
    raw_scopes = operation.get("required_oauth_scopes")
    scopes = (
        frozenset(
            str(value).strip()
            for value in raw_scopes
            if isinstance(value, str) and str(value).strip()
        )
        if isinstance(raw_scopes, (list, tuple))
        else frozenset()
    )
    return ExtensionOperationContract(
        declared=True,
        operation_kind=str(operation.get("operation_kind") or operation.get("kind") or "action")
        .strip()
        .lower(),
        connection_type_ids=_declared_connection_type_ids(operation),
        required_oauth_scopes=scopes,
        input_mapping_rules=tuple(
            dict(rule)
            for rule in operation.get("input_mapping_rules", [])
            if isinstance(rule, dict)
        ),
        outputs=tuple(
            dict(output)
            for output in operation.get("outputs", [])
            if isinstance(output, dict) and str(output.get("name") or "").strip()
        ),
    )


def connection_type_satisfies_operation(
    extension: Any,
    *,
    operation_id: str,
    connection_type: str,
) -> bool:
    """Whether a bound connection's type is one this operation declares.

    Membership, not equality against a single name: an operation can declare
    several — a file-transfer operation can work over FTP or SFTP — and
    reducing the set to one is what rejected a valid SFTP binding at runtime
    while the identical FTP one ran. An operation that declares no policy
    constrains nothing.
    """
    contract = extension_operation_contract(extension, operation_id=operation_id)
    if not contract.requires_connection:
        return True
    return contract.accepts_connection_type(connection_type)


def _route_event_through_command_service(
    *,
    event_command_service_factory: ExtensionEventCommandServiceFactory,
    extension_id: str,
    event: dict[str, Any],
    scope: ScopeContext | None,
    runtime_context: dict[str, Any] | None,
) -> bool:
    event_payload = dict(event or {})
    project_id = str(
        (runtime_context or {}).get("project_id") or getattr(scope, "project_id", None) or ""
    ).strip()
    if not project_id:
        return False
    event_payload["project_id"] = project_id
    service = event_command_service_factory()
    if service is None:
        return False
    event_scope = normalize_scope(
        scope,
        project_id=project_id,
        workflow_id=str(event_payload.get("workflow_id") or "").strip() or None,
    )
    service.ingest_provider_events(
        ProviderEventIngestInput(
            provider_key=str(extension_id or "").strip().lower(),
            project_id=project_id,
            result={},
            events=[event_payload],
        ),
        scope=event_scope,
    )
    return True


def process_extension_response_envelope(
    invocation: dict[str, Any],
    extension: dict[str, Any],
    extension_id: str,
    *,
    scope: ScopeContext | None = None,
    audit: dict[str, Any] | None = None,
    bl: Any = None,
    get_logger: Callable[[], Any] | None = None,
    event_command_service_factory: ExtensionEventCommandServiceFactory | None = None,
    command_dispatch_service_factory: ExtensionCommandDispatchServiceFactory | None = None,
) -> None:
    raw_response = invocation.get("response")
    if not isinstance(raw_response, dict) or not raw_response:
        return
    from core.infrastructure.db.extension_response import (
        filter_valid_commands,
        filter_valid_events,
        normalize_extension_response,
    )

    envelope = normalize_extension_response(raw_response)
    invocation["_envelope"] = envelope.model_dump(mode="json")
    from .response_envelope import process_project_state_updates

    audit = dict(audit or {})
    process_project_state_updates(
        invocation,
        extension=extension,
        extension_id=extension_id,
        db=bl,
        account_id=str(
            audit.get("account_id") or getattr(scope, "account_id", None) or DEFAULT_ACCOUNT_ID
        ).strip()
        or DEFAULT_ACCOUNT_ID,
        project_id=str(audit.get("project_id") or getattr(scope, "project_id", None) or "").strip(),
        actor_id=str(audit.get("actor_id") or getattr(scope, "actor_id", None) or "").strip()
        or None,
        connection_id=str(audit.get("connection_id") or "").strip() or None,
    )
    if invocation.get("status") == "failed":
        return
    compiled = extension.get("compiled_manifest") or {}
    supported_events = list(
        compiled.get("supported_events") or extension.get("supported_events") or []
    )
    if envelope.commands:
        from core.infrastructure.extension_runtime.extension_runtime import command_scope

        declared_scopes = extension_declared_scopes(extension)
        for command in filter_valid_commands(list(envelope.commands)):
            command_type = str(command.get("command_type") or "").strip()
            needed_scope = command_scope(command_type)
            if needed_scope and needed_scope not in declared_scopes:
                logger.warning(
                    "Extension %s attempted command '%s' without declared scope '%s' — skipped",
                    extension_id,
                    command_type,
                    needed_scope,
                )
                continue
            try:
                if command_dispatch_service_factory is None:
                    continue
                command_dispatch_service_factory(get_logger=get_logger or (lambda: bl)).dispatch(
                    CommandDispatchInput(
                        command_type=command_type,
                        target=dict(command.get("target") or {}),
                        payload=dict(command.get("payload") or {}),
                        source=f"extension:{extension_id}",
                        correlation_id=str(command.get("correlation_id") or "").strip(),
                    ),
                    scope=scope,
                )
            except Exception as exc:
                logger.warning(
                    "Extension command dispatch failed: ext=%s cmd_type=%s error=%s",
                    extension_id,
                    command.get("command_type"),
                    exc,
                )
    if envelope.events:
        from core.infrastructure.extension_runtime import extension_runtime

        runtime_context = {**audit}
        if bl is not None:
            runtime_context["logger"] = bl
        for event in filter_valid_events(
            list(envelope.events), declared_event_types=supported_events
        ):
            try:
                if (
                    event_command_service_factory is not None
                    and _route_event_through_command_service(
                        event_command_service_factory=event_command_service_factory,
                        extension_id=extension_id,
                        event=event,
                        scope=scope,
                        runtime_context=runtime_context,
                    )
                ):
                    continue
                emit_extension_event = vars(extension_runtime).get("emit_extension_event")
                if not callable(emit_extension_event):
                    continue
                emit_extension_event(
                    extension,
                    event_type=str(event.get("event_type") or "").strip(),
                    correlation_id=str(event.get("correlation_id") or "").strip(),
                    scope=scope,
                    project_id=str(audit.get("project_id") or "").strip() or None,
                    summary=str(event.get("summary") or "").strip(),
                    severity=str(event.get("severity") or "info").strip(),
                    payload=dict(event.get("payload") or {}),
                    runtime_context=runtime_context,
                )
            except Exception as exc:
                logger.warning(
                    "Extension event emission failed: ext=%s evt_type=%s error=%s",
                    extension_id,
                    event.get("event_type"),
                    exc,
                )


__all__ = [
    "ExtensionCommandDispatchService",
    "ExtensionCommandDispatchServiceFactory",
    "ExtensionEventCommandService",
    "ExtensionEventCommandServiceFactory",
    "extension_declared_scopes",
    "extension_operation_declared_scopes",
    "process_extension_response_envelope",
]
