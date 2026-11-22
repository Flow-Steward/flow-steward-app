"""Extension response envelope processing: commands[], events[] and connection-poll results."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.scope import ScopeContext, normalize_scope

from .command_models import ProviderEventIngestInput

logger = logging.getLogger(__name__)


def _extension_declared_scopes(extension: Any) -> set[str]:
    """Declared (operator-approved) scope envelope for an extension.

    Self-contained extraction (extension.required_scopes + compiled_manifest
    .required_scopes) that avoids the wildcard-import helpers used by
    ``extension_runtime_entrypoints.extension_required_scopes``.
    """
    ext = extension if isinstance(extension, dict) else {}
    manifest = ext.get("compiled_manifest")
    manifest = manifest if isinstance(manifest, dict) else {}
    scopes: set[str] = set()
    for source in (ext.get("required_scopes"), manifest.get("required_scopes")):
        if isinstance(source, (list, tuple, set)):
            scopes.update(str(item).strip() for item in source if str(item or "").strip())
    return scopes


def _process_extension_response_envelope(
    invocation: dict[str, Any],
    extension: dict[str, Any],
    extension_id: str,
    *,
    scope: ScopeContext | None = None,
    audit: dict[str, Any] | None = None,
    bl: Any = None,
    get_logger: Callable[[], Any] | None = None,
    event_command_service_factory: Callable[[], Any] | None = None,
    poll_context: dict[str, Any] | None = None,
) -> None:
    """Process commands[] and events[] from a normalized extension response.

    Called after a successful extension invocation. Normalizes the raw subprocess
    response into v1.1 canonical envelope, dispatches valid commands, and emits
    valid events through the domain event ledger.

    ``poll_context`` marks the result of a ``connection_poll`` background action:
    the connection and the ``state_version``/``config_generation`` it was polled
    at. Its state and events are then committed in one transaction under the
    connection row lock (see ``connection_poll_commit``).

    Mutates *invocation* by adding ``_envelope`` key with the normalized shape.
    """
    raw_response = invocation.get("response")
    if not isinstance(raw_response, dict) or not raw_response:
        return

    from core.infrastructure.db.extension_response import normalize_extension_response

    env = normalize_extension_response(raw_response)

    # Attach normalized envelope to invocation for callers
    invocation["_envelope"] = env.model_dump(mode="json")
    audit = dict(audit or {})

    from core.application.extensions.connection_poll_commit import ConnectionPollContext

    context = ConnectionPollContext.from_mapping(poll_context) if poll_context else None
    if context is not None and getattr(bl, "conn", None) is not None:
        if _commit_connection_poll_result(
            invocation,
            env,
            extension,
            extension_id,
            context=context,
            scope=scope,
            audit={**audit, "connection_id": context.connection_id},
            bl=bl,
            event_command_service_factory=event_command_service_factory,
        ):
            _dispatch_envelope_commands(
                env,
                extension,
                extension_id,
                scope=scope,
                bl=bl,
                get_logger=get_logger,
                event_command_service_factory=event_command_service_factory,
            )
        return

    from core.application.extensions.response_envelope import process_project_state_updates

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
    _dispatch_envelope_commands(
        env,
        extension,
        extension_id,
        scope=scope,
        bl=bl,
        get_logger=get_logger,
        event_command_service_factory=event_command_service_factory,
    )
    _emit_envelope_events(
        env,
        extension,
        extension_id,
        scope=scope,
        audit=audit,
        bl=bl,
        event_command_service_factory=event_command_service_factory,
    )


class _PollStateRejected(Exception):
    """The poll's state write failed; its savepoint rolls back, events stay."""


def _commit_connection_poll_result(
    invocation: dict[str, Any],
    env: Any,
    extension: dict[str, Any],
    extension_id: str,
    *,
    context: Any,
    scope: ScopeContext | None,
    audit: dict[str, Any],
    bl: Any,
    event_command_service_factory: Callable[[], Any] | None,
) -> bool:
    """One transaction: lock the connection, judge, then write events and cursor.

    Returns False when the result belonged to an old configuration and was
    dropped completely.
    """
    from core.application.extensions.connection_poll_commit import (
        advance_state_version,
        lock_and_judge,
    )
    from core.application.extensions.response_envelope import process_project_state_updates
    from core.application.workflows.provider_event_delivery import fault_point
    from core.infrastructure.db.db_pool import transaction_connection

    account_id = (
        str(
            audit.get("account_id") or getattr(scope, "account_id", None) or DEFAULT_ACCOUNT_ID
        ).strip()
        or DEFAULT_ACCOUNT_ID
    )
    project_id = str(audit.get("project_id") or getattr(scope, "project_id", None) or "").strip()
    with transaction_connection(bl):
        with bl.conn.cursor() as cur:
            verdict = lock_and_judge(cur, context)
        fault_point("poll_after_lock")
        outcome: dict[str, Any] = {
            "verdict": verdict.verdict,
            "reason": verdict.reason,
            "state_saved": False,
        }
        invocation["connection_poll"] = outcome
        if not verdict.keeps_events:
            logger.info(
                "Connection poll result of %s/%s dropped: %s",
                extension_id,
                context.connection_id,
                verdict.reason,
            )
            return False
        if verdict.keeps_state:
            try:
                with transaction_connection(bl):
                    process_project_state_updates(
                        invocation,
                        extension=extension,
                        extension_id=extension_id,
                        db=bl,
                        account_id=account_id,
                        project_id=project_id,
                        actor_id=str(audit.get("actor_id") or "").strip() or None,
                        connection_id=context.connection_id,
                        extension_install_id=str(audit.get("extension_install_id") or "").strip()
                        or None,
                        poll_context=context,
                    )
                    if invocation.get("status") == "failed":
                        raise _PollStateRejected()
                    with bl.conn.cursor() as cur:
                        advance_state_version(cur, context.connection_id)
                outcome["state_saved"] = True
            except _PollStateRejected:
                logger.warning(
                    "Connection poll state of %s/%s not saved: %s",
                    extension_id,
                    context.connection_id,
                    invocation.get("detail"),
                )
        else:
            logger.info(
                "Connection poll cursor of %s/%s dropped: %s",
                extension_id,
                context.connection_id,
                verdict.reason,
            )
        _emit_envelope_events(
            env,
            extension,
            extension_id,
            scope=scope,
            audit=audit,
            bl=bl,
            event_command_service_factory=event_command_service_factory,
            connection_poll={
                "connection_id": context.connection_id,
                "config_generation": context.config_generation,
            },
        )
        fault_point("poll_before_commit")
    return True


def _dispatch_envelope_commands(
    env: Any,
    extension: dict[str, Any],
    extension_id: str,
    *,
    scope: ScopeContext | None,
    bl: Any,
    get_logger: Callable[[], Any] | None,
    event_command_service_factory: Callable[[], Any] | None,
) -> None:
    if not env.commands:
        return
    from core.infrastructure.db.extension_response import filter_valid_commands
    from core.infrastructure.extension_runtime.extension_runtime import command_scope

    # An extension may only dispatch host commands whose command-scope it
    # declared in required_scopes (what the operator approved at install).
    declared_scopes = _extension_declared_scopes(extension)
    valid_commands = filter_valid_commands(list(env.commands))
    for cmd in valid_commands:
        command_type = str(cmd.get("command_type") or "").strip()
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
            from .command_models import CommandDispatchInput
            from .command_services_dispatch import CommandDispatchService

            cmd_svc = CommandDispatchService(
                get_logger=get_logger or (lambda: bl),
                event_command_service_factory=event_command_service_factory,
            )
            cmd_svc.dispatch(
                CommandDispatchInput(
                    command_type=str(cmd.get("command_type") or "").strip(),
                    target=dict(cmd.get("target") or {}),
                    payload=dict(cmd.get("payload") or {}),
                    source=f"extension:{extension_id}",
                    correlation_id=str(cmd.get("correlation_id") or "").strip(),
                ),
                scope=scope,
            )
        except Exception as exc:
            logger.warning(
                "Extension command dispatch failed: ext=%s cmd_type=%s error=%s",
                extension_id,
                cmd.get("command_type"),
                exc,
            )


def _emit_envelope_events(
    env: Any,
    extension: dict[str, Any],
    extension_id: str,
    *,
    scope: ScopeContext | None,
    audit: dict[str, Any],
    bl: Any,
    event_command_service_factory: Callable[[], Any] | None,
    connection_poll: dict[str, Any] | None = None,
) -> None:
    if not env.events:
        return
    from core.infrastructure.db.extension_response import filter_valid_events
    from core.infrastructure.extension_runtime.extension_runtime import emit_extension_event

    compiled = extension.get("compiled_manifest") or {}
    supported_events = list(
        compiled.get("supported_events") or extension.get("supported_events") or []
    )
    valid_events = filter_valid_events(list(env.events), declared_event_types=supported_events)
    runtime_ctx = {**audit}
    if bl is not None:
        runtime_ctx["logger"] = bl
    for evt in valid_events:
        try:
            if connection_poll is not None:
                from core.infrastructure.db.db_pool import transaction_connection

                # One savepoint per event: a bad event must not abort the
                # poll transaction that holds the others and the cursor.
                with transaction_connection(bl):
                    routed = _route_extension_event_through_event_command_service(
                        extension_id=extension_id,
                        event=evt,
                        scope=scope,
                        runtime_context=runtime_ctx,
                        event_command_service_factory=event_command_service_factory,
                        connection_poll=connection_poll,
                    )
            else:
                routed = _route_extension_event_through_event_command_service(
                    extension_id=extension_id,
                    event=evt,
                    scope=scope,
                    runtime_context=runtime_ctx,
                    event_command_service_factory=event_command_service_factory,
                )
            if routed:
                continue
            emit_extension_event(
                extension,
                event_type=str(evt.get("event_type") or "").strip(),
                correlation_id=str(evt.get("correlation_id") or "").strip(),
                scope=scope,
                project_id=str(audit.get("project_id") or "").strip() or None,
                summary=str(evt.get("summary") or "").strip(),
                severity=str(evt.get("severity") or "info").strip(),
                payload=dict(evt.get("payload") or {}),
                runtime_context=runtime_ctx,
            )
        except Exception as exc:
            logger.warning(
                "Extension event emission failed: ext=%s evt_type=%s error=%s",
                extension_id,
                evt.get("event_type"),
                exc,
            )


def _route_extension_event_through_event_command_service(
    *,
    extension_id: str,
    event: dict[str, Any],
    scope: ScopeContext | None,
    runtime_context: dict[str, Any] | None = None,
    event_command_service_factory: Callable[[], Any] | None = None,
    connection_poll: dict[str, Any] | None = None,
) -> bool:
    event_payload = dict(event or {})
    project_id = str(
        (runtime_context or {}).get("project_id") or getattr(scope, "project_id", None) or ""
    ).strip()
    if not project_id:
        return False
    event_payload["project_id"] = project_id

    if event_command_service_factory is None:
        return False

    event_command_service = event_command_service_factory()
    if event_command_service is None or not hasattr(
        event_command_service, "ingest_provider_events"
    ):
        return False

    event_scope = normalize_scope(
        scope,
        project_id=project_id,
        workflow_id=str(event_payload.get("workflow_id") or "").strip() or None,
    )
    event_command_service.ingest_provider_events(
        ProviderEventIngestInput(
            provider_key=str(extension_id or "").strip().lower(),
            project_id=project_id,
            result={},
            events=[event_payload],
            connection_poll=connection_poll,
        ),
        scope=event_scope,
    )
    return True
