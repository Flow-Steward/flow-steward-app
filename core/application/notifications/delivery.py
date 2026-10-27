"""Notification delivery using installed provider extensions."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from core.application.extensions.notification_install_binding import select_notification_install
from core.application.notifications.contracts import (
    _as_dict,
    _as_str,
    _normalize_notification_delivery_invocation,
    _notification_delivery_fields,
    _trace_id_for_job,
)
from core.application.orchestration.models import build_event_envelope_v1
from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID
from core.infrastructure.extension_runtime.extension_runtime import run_extension_invocation

logger = logging.getLogger(__name__)


def _notification_extension_key(provider: dict) -> str:
    provider_config = _as_dict(provider.get("provider_config")) or _as_dict(provider.get("config"))
    extension_key = _as_str(
        provider_config.get("extension_key")
        or provider.get("integration_key")
        or provider.get("provider_key")
    )
    if not extension_key:
        raise ValueError("Notification provider is missing extension_key")
    return extension_key


def _resolve_notification_extension(
    bl,
    provider: dict,
    *,
    extension_install_id: str = "",
    account_id: str = "",
) -> dict:
    from core.infrastructure.extension_runtime.extension_runtime_helpers import (
        resolve_extension_runtime_record_by_install_id,
    )

    extension_key = _notification_extension_key(provider)
    normalized_install_id = _as_str(extension_install_id)
    normalized_account_id = _as_str(account_id) or DEFAULT_ACCOUNT_ID
    selected_release_id = ""
    if not normalized_install_id:
        selected = select_notification_install(
            getattr(bl, "conn", None), extension_id=extension_key, account_id=normalized_account_id
        )
        normalized_install_id = _as_str(selected.install_id)
        selected_release_id = _as_str(selected.release_id)
    extension, _ = resolve_extension_runtime_record_by_install_id(
        normalized_install_id,
        get_logger_only=lambda: bl,
        account_id=normalized_account_id,
    )
    if not extension:
        raise ValueError(f"Extension '{extension_key}' not found")
    if not bool(extension.get("enabled")):
        raise ValueError(f"Extension '{extension_key}' is disabled")
    if _as_str(extension.get("kind")) != "notification_provider":
        raise ValueError(
            f"Extension '{extension_key}' kind '{extension.get('kind')}' does not match notification_provider"
        )
    if normalized_install_id:
        resolved_install_id = _as_str(
            extension.get("extension_install_id") or extension.get("install_id")
        )
        if resolved_install_id != normalized_install_id:
            raise ValueError("Notification extension install identity mismatch")
        if (
            _as_str(extension.get("extension_key") or extension.get("extension_id"))
            != extension_key
        ):
            raise ValueError("Notification extension identity mismatch")
    if selected_release_id and _as_str(extension.get("release_id")) != selected_release_id:
        raise ValueError("Notification extension release identity mismatch")
    return extension


def _notification_connection_id(resolved_runtime: dict, route_config: dict) -> str:
    connection_row = _as_dict(resolved_runtime.get("project_connection"))
    return (
        _as_str(route_config.get("connection_id"))
        or _as_str(connection_row.get("connection_id"))
        or _as_str(resolved_runtime.get("connection_id"))
    )


def _load_notification_connection_row(
    bl,
    *,
    connection_id: str,
    account_id: str,
    project_id: str,
    extension_id: str,
    extension_install_id: str,
) -> dict[str, Any]:
    normalized_connection_id = _as_str(connection_id)
    if not normalized_connection_id or bl is None:
        return {}
    row = None
    getter = getattr(bl, "get_project_connection", None)
    if callable(getter):
        try:
            row = getter(
                project_id=project_id or None,
                connection_id=normalized_connection_id,
                extension_install_id=extension_install_id or None,
                enabled_only=True,
            )
        except Exception:
            row = None
    if row is None and hasattr(bl, "cursor"):
        try:
            from core.infrastructure.db import project_connection_store

            row = project_connection_store.get_project_connection_row(bl, normalized_connection_id)
        except Exception:
            row = None
    connection = _as_dict(row)
    if not connection:
        return {}
    if _as_str(connection.get("connection_id")) != normalized_connection_id:
        return {}
    if project_id and _as_str(connection.get("project_id")) != project_id:
        return {}
    if account_id and _as_str(connection.get("account_id")) != account_id:
        return {}
    row_extension_id = _as_str(connection.get("extension_id")).lower()
    if extension_id and row_extension_id != extension_id.lower():
        return {}
    if (
        extension_install_id
        and _as_str(connection.get("extension_install_id")) != extension_install_id
    ):
        return {}
    if connection.get("enabled") is False:
        return {}
    return connection


def _notification_connection_target(
    bl,
    *,
    connection_id: str,
    account_id: str,
    project_id: str,
    extension_id: str,
    extension_install_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not _as_str(extension_install_id):
        return {}, {}
    connection = _load_notification_connection_row(
        bl,
        connection_id=connection_id,
        account_id=account_id,
        project_id=project_id,
        extension_id=extension_id,
        extension_install_id=extension_install_id,
    )
    if not connection:
        return {}, {}

    secrets: dict[str, str] = {}
    secret_refs = _as_dict(connection.get("secret_refs"))
    configuration_refs = _as_dict(secret_refs.get("refs"))
    if configuration_refs and hasattr(bl, "cursor"):
        try:
            from core.application.management.project_connection_secrets import (
                resolve_connection_secrets,
            )

            secrets = resolve_connection_secrets(
                bl,
                account_id=_as_str(connection.get("account_id")),
                secret_refs={"refs": configuration_refs},
            )
        except Exception as exc:
            raise RuntimeError(
                f"connection_secret_resolution_failed:{connection_id}:{exc}"
            ) from exc

    provider_secret_refs = _as_dict(secret_refs.get("provider_secrets"))
    provider_key = _as_str(provider_secret_refs.get("provider_key"))
    provider_secret_keys = _as_dict(provider_secret_refs.get("keys"))
    if provider_secret_keys:
        get_provider_secret = getattr(bl, "get_provider_secret", None)
        if not provider_key or not callable(get_provider_secret):
            raise RuntimeError(f"provider_secret_resolution_unavailable:{connection_id}")
        for input_name, raw_secret_key in provider_secret_keys.items():
            secret_name = _as_str(input_name)
            secret_key = _as_str(raw_secret_key)
            if not secret_name or not secret_key:
                continue
            secret_value = _as_str(
                get_provider_secret(
                    provider_key=provider_key,
                    secret_key=secret_key,
                    project_id=_as_str(connection.get("project_id")),
                    include_global=False,
                )
            )
            if not secret_value:
                raise RuntimeError(
                    f"provider_secret_resolution_failed:{connection_id}:{secret_key}"
                )
            secrets[secret_name] = secret_value

    runtime_payload = {
        "connection_id": _as_str(connection.get("connection_id")),
        "account_id": _as_str(connection.get("account_id")),
        "project_id": _as_str(connection.get("project_id")),
        "display_name": _as_str(connection.get("display_name")),
        "connection_type": _as_str(connection.get("connection_type")),
        "extension_install_id": _as_str(connection.get("extension_install_id")),
        "environment": _as_str(connection.get("environment")),
        "credential_revision": int(connection.get("credential_revision") or 1),
        "config": _as_dict(connection.get("connection_config")),
        "secret_refs": secret_refs,
        "secrets": secrets,
        "connection_routes": list(connection.get("connection_routes") or []),
        "ingress_config": _as_dict(connection.get("ingress_config")),
        "workflow_mappings": _as_dict(connection.get("workflow_mappings")),
    }
    logged_payload = {key: value for key, value in runtime_payload.items() if key != "secrets"}
    logged_payload["secrets"] = dict.fromkeys(secrets, "<redacted>")
    return runtime_payload, logged_payload


def _notification_external_effect_source_ref(event: dict, route_payload: dict) -> dict[str, Any]:
    return {
        "event_id": _as_str(event.get("event_id")) or None,
        "correlation_id": _as_str(event.get("correlation_id")) or None,
        "workflow_id": _as_str(event.get("workflow_id")) or None,
        "job_id": event.get("job_id"),
        "route_id": _as_str(route_payload.get("route_id")) or None,
        "route_key": _as_str(route_payload.get("route_key")) or None,
    }


def _claim_notification_external_effect(
    *,
    bl,
    extension: dict,
    extension_id: str,
    extension_install_id: str,
    connection_id: str,
    account_id: str,
    project_id: str,
    event: dict,
    route_payload: dict,
    provider_payload: dict,
    action_payload: dict | None = None,
    test_mode: bool = False,
) -> dict[str, Any]:
    from core.application.extensions import external_effects

    fingerprint_event = dict(event)
    # Delivery retries rebuild transport envelopes; wall-clock construction time is
    # not part of the durable notification intent.
    fingerprint_event.pop("occurred_at", None)
    request_payload = {
        "action": "deliver_notification",
        "action_payload": _as_dict(action_payload),
        "event": fingerprint_event,
        "route": route_payload,
        "provider": provider_payload,
    }
    source_ref = _notification_external_effect_source_ref(event, route_payload)
    intent_key = external_effects.external_effect_identity_hmac(
        purpose="notification-intent",
        value={
            "account_id": account_id,
            "project_id": project_id,
            "extension_id": extension_id,
            "extension_install_id": extension_install_id,
            "connection_id": connection_id,
            "operation_id": "deliver_notification",
            "event_id": source_ref.get("event_id"),
            "route_id": source_ref.get("route_id"),
            "route_key": source_ref.get("route_key"),
            "test_mode": bool(test_mode),
        },
    )
    return external_effects.claim_external_effect_for_action(
        bl,
        extension=extension,
        account_id=account_id,
        project_id=project_id or None,
        extension_id=extension_id,
        extension_install_id=extension_install_id or None,
        connection_id=connection_id or None,
        operation_id="deliver_notification",
        source_type="notification_event",
        source_ref=source_ref,
        request_payload=request_payload,
        idempotency_key_override=intent_key,
        test_mode=test_mode,
    )


def _with_standard_email(extension: dict, event: dict, route_config: dict) -> dict:
    """Give a workflow event sent by email the platform's standard subject and text.

    Only for an extension whose deliveries address a recipient (email); a chat
    extension formats its own messages. A subject set on the route is kept. The
    platform's own messages (``system.*``) already carry their text.
    """
    from core.application.auth.serve_auth_runtime import public_base_url
    from core.application.extensions.event_email import render_event_email
    from core.application.extensions.external_effects import notification_target_kind

    if _as_str(event.get("event_type")).startswith("system."):
        return event
    if notification_target_kind(extension) != "recipient":
        return event
    subject, body = render_event_email(event, base_url=public_base_url())
    route_config.setdefault("subject", subject)
    if not _as_str(route_config.get("subject")):
        route_config["subject"] = subject
    payload = {**_as_dict(event.get("payload")), "message": body}
    return {**event, "payload": payload}


def _deliver_via_extension(
    *,
    bl,
    provider: dict,
    event: dict,
    route: dict,
    project_id: Any | None = None,
    test_mode: bool = False,
) -> dict:
    from core.application.extensions.provider_runtime import resolve_extension_runtime_config

    extension_id = _notification_extension_key(provider)
    route_config = _as_dict(route.get("route_config"))
    route_payload = {
        "route_id": _as_str(route.get("route_id")) or None,
        "route_key": _as_str(route.get("route_key")) or None,
        "provider_key": _as_str(route.get("provider_key")) or None,
        "event_type": _as_str(route.get("event_type")) or None,
        "severity": _as_str(route.get("severity")) or None,
        "display_name": _as_str(route.get("display_name")) or None,
        "workflow_id": _as_str(route.get("workflow_id")) or None,
        "enabled": bool(route.get("enabled", True)),
        "route_config": route_config,
    }
    project_run_config: dict[str, Any] = {}
    normalized_project_id = _as_str(project_id)
    if normalized_project_id and hasattr(bl, "get_project_run_config"):
        try:
            loaded = bl.get_project_run_config(normalized_project_id) or {}
            if isinstance(loaded, dict):
                project_run_config = loaded
                if project_run_config.get("project_id") is not None:
                    project_run_config = {
                        **project_run_config,
                        "project_id": _as_str(project_run_config.get("project_id")),
                    }
        except Exception:
            project_run_config = {}
    extension_install_id = ""
    account_id = _as_str(event.get("account_id")) or DEFAULT_ACCOUNT_ID
    resolved_runtime: dict[str, Any] = {}
    route_connection_id = _as_str(route_config.get("connection_id"))
    if route_connection_id:
        connection_id = route_connection_id
    else:
        resolved_runtime = resolve_extension_runtime_config(
            extension_id,
            project_run_config,
            bl=bl,
            project_id=normalized_project_id or None,
        )
        connection_id = _notification_connection_id(resolved_runtime, route_config)
    runtime_connection: dict[str, Any] = {}
    logged_connection: dict[str, Any] = {}
    if connection_id:
        scoped_connection = _load_notification_connection_row(
            bl,
            connection_id=connection_id,
            account_id=_as_str(event.get("account_id")) or DEFAULT_ACCOUNT_ID,
            project_id=normalized_project_id,
            extension_id=extension_id,
            extension_install_id="",
        )
        extension_install_id = _as_str(scoped_connection.get("extension_install_id"))
        if not extension_install_id:
            return {
                "status": "failed",
                "error_text": f"connection_install_scope_missing:{connection_id}",
                "provider_message_id": "",
                "external_effect_id": None,
                "response": {},
            }
        try:
            extension = _resolve_notification_extension(
                bl,
                provider,
                extension_install_id=extension_install_id,
                account_id=_as_str(event.get("account_id")) or DEFAULT_ACCOUNT_ID,
            )
        except Exception as exc:
            return {
                "status": "failed",
                "error_text": _as_str(exc) or "extension_install_resolution_failed",
                "provider_message_id": "",
                "external_effect_id": None,
                "response": {},
            }
        try:
            runtime_connection, logged_connection = _notification_connection_target(
                bl,
                connection_id=connection_id,
                account_id=_as_str(event.get("account_id")) or DEFAULT_ACCOUNT_ID,
                project_id=normalized_project_id,
                extension_id=extension_id,
                extension_install_id=extension_install_id,
            )
        except Exception as exc:
            return {
                "status": "failed",
                "error_text": _as_str(exc) or "connection_resolution_failed",
                "provider_message_id": "",
                "external_effect_id": None,
                "response": {},
            }
        if not runtime_connection:
            return {
                "status": "failed",
                "error_text": f"connection_resolution_failed:{connection_id}",
                "provider_message_id": "",
                "external_effect_id": None,
                "response": {},
            }
    else:
        extension = _resolve_notification_extension(bl, provider, account_id=account_id)
        extension_install_id = _as_str(
            extension.get("extension_install_id") or extension.get("install_id")
        )
    # Fills route_config (shared with route_payload) before the claim fingerprints it.
    event = _with_standard_email(extension, event, route_config)
    target_payload = {"connection": runtime_connection} if runtime_connection else {}
    logged_target_payload = {"connection": logged_connection} if logged_connection else {}
    action_payload = {
        "action_id": "deliver_notification",
        "target": target_payload,
    }
    logged_action_payload = {
        "action_id": "deliver_notification",
        "target": logged_target_payload,
    }
    provider_payload = {
        "provider_key": _as_str(provider.get("provider_key")),
        "integration_key": _as_str(provider.get("integration_key")),
        "project_id": normalized_project_id or None,
        "provider_config": _as_dict(provider.get("provider_config"))
        or _as_dict(provider.get("config"))
        or ({} if runtime_connection else _as_dict(resolved_runtime.get("config"))),
        "secret_refs": _as_dict(resolved_runtime.get("secret_refs")),
        "connection_routes": list(resolved_runtime.get("connection_routes") or []),
        "ingress_config": _as_dict(resolved_runtime.get("ingress_config")),
        "workflow_mappings": _as_dict(resolved_runtime.get("workflow_mappings")),
        "project_config": project_run_config,
    }
    from core.application.extensions.delivery_coordinator import (
        coordinate_extension_delivery,
        provider_payload_for_effect_claim,
    )

    claim_provider_payload = provider_payload_for_effect_claim(
        extension,
        "deliver_notification",
        provider_payload,
    )
    effect_claim = _claim_notification_external_effect(
        bl=bl,
        extension=extension,
        extension_id=extension_id,
        extension_install_id=extension_install_id,
        connection_id=connection_id,
        account_id=_as_str(event.get("account_id")) or DEFAULT_ACCOUNT_ID,
        project_id=normalized_project_id,
        event=event,
        route_payload=route_payload,
        provider_payload=claim_provider_payload,
        action_payload=logged_action_payload,
        test_mode=test_mode,
    )
    external_effect = _as_dict(effect_claim.get("effect"))
    external_effect_id = _as_str(external_effect.get("effect_id"))
    external_effect_claim_token = _as_str(external_effect.get("claim_token"))
    if effect_claim.get("declared") and not effect_claim.get("should_spawn"):
        from core.application.extensions.external_effect_replay import (
            hydrate_external_effect_claim,
        )

        effect_claim = hydrate_external_effect_claim(bl, effect_claim)
        if effect_claim.get("error"):
            return {
                "status": "failed",
                "error_text": _as_str(effect_claim.get("error")),
                "provider_message_id": "",
                "external_effect_id": external_effect_id or None,
                "response": {"external_effect": external_effect},
            }
        effect_status = _as_str(external_effect.get("status")).lower()
        replay_invocation = _as_dict(effect_claim.get("replay_invocation"))
        replay_response, delivered, _replay_error, replay_message_id = (
            _notification_delivery_fields(replay_invocation)
        )
        replay_message_id = replay_message_id or _as_str(external_effect.get("provider_message_id"))
        replay_status = "failed"
        if effect_status == "succeeded" and delivered:
            replay_status = "delivered"
        elif effect_status == "suppressed":
            replay_status = "skipped"
        error_text = effect_status
        if replay_status == "delivered" or effect_status == "suppressed":
            error_text = ""
        elif effect_status == "succeeded":
            error_text = _replay_error or "notification_not_delivered"
        return {
            "status": replay_status,
            "error_text": error_text,
            "provider_message_id": replay_message_id,
            "external_effect_id": external_effect_id or None,
            "response": {**replay_response, "external_effect": external_effect},
        }
    if test_mode:
        return {
            "status": "skipped",
            "error_text": "test_mode_suppressed",
            "provider_message_id": "",
            "external_effect_id": external_effect_id or None,
            "response": {"external_effect": external_effect} if external_effect else {},
        }

    def invoke_delivery(request_payload: dict[str, Any]) -> dict[str, Any]:
        invocation = run_extension_invocation(
            extension,
            request_payload=request_payload,
            timeout_seconds=int(route_config.get("timeout_seconds") or 30),
            runtime_context={
                "logger": bl,
                "external_effect_id": external_effect_id or None,
                "account_id": account_id,
                "extension_install_id": extension_install_id,
            },
        )
        invocation["operation_type"] = "action"
        invocation["operation_id"] = "deliver_notification"
        return invocation

    def persist_delivery_updates(invocation: dict[str, Any]) -> None:
        from core.application.extensions.response_envelope import process_project_state_updates

        process_project_state_updates(
            invocation,
            extension=extension,
            extension_id=extension_id,
            db=bl,
            account_id=_as_str(event.get("account_id")) or DEFAULT_ACCOUNT_ID,
            project_id=normalized_project_id,
            connection_id=connection_id or None,
            extension_install_id=extension_install_id or None,
            actor_id=None,
        )

    delivery_started = False

    def mark_delivery_running(effect_id: str, claim_token: str) -> dict[str, Any] | None:
        from core.application.extensions.external_effects import mark_external_effect_running

        nonlocal delivery_started
        running_effect = mark_external_effect_running(
            bl,
            effect_id=effect_id,
            claim_token=claim_token,
        )
        delivery_started = bool(running_effect)
        return running_effect

    def report_delivery_persistence_error(phase: str, exc: Exception) -> None:
        logger.warning("failed to persist %s delivery host mutations: %s", phase, exc)

    invocation = coordinate_extension_delivery(
        extension=extension,
        operation_id="deliver_notification",
        runtime_connection=runtime_connection,
        action_payload=action_payload,
        provider_payload=provider_payload,
        event=event,
        route_payload=route_payload,
        external_effect_id=external_effect_id,
        external_effect_claim_token=external_effect_claim_token,
        invoke=invoke_delivery,
        persist=persist_delivery_updates,
        mark_running=mark_delivery_running,
        report_persistence_error=report_delivery_persistence_error,
    )
    invocation = _normalize_notification_delivery_invocation(invocation)
    if external_effect_id:
        try:
            from core.application.extensions.external_effects import (
                apply_external_effect_confirmation,
                confirm_external_effect_from_invocation,
                public_confirmed_external_effect,
            )
            from core.application.extensions.secret_reflection import collect_secret_values

            protected_values, protected_budget_exceeded = collect_secret_values(
                _as_dict(runtime_connection.get("secrets"))
            )
            if protected_budget_exceeded:
                raise RuntimeError("external_effect_replay_secret_budget_exceeded")

            confirmed_effect = confirm_external_effect_from_invocation(
                bl,
                effect_id=external_effect_id,
                invocation=invocation,
                claim_token=external_effect_claim_token,
                account_id=account_id,
                extension=extension,
                operation_id="deliver_notification",
                protected_values=protected_values,
                expected_current_status=(
                    "claimed"
                    if not delivery_started
                    and bool(
                        _as_dict(invocation.get("response")).get("definitely_no_external_effect")
                    )
                    else "running"
                ),
                expected_project_id=normalized_project_id or None,
                expected_connection_id=connection_id or None,
            )
            if confirmed_effect:
                invocation = apply_external_effect_confirmation(invocation, confirmed_effect)
                external_effect = public_confirmed_external_effect(confirmed_effect)
        except Exception as exc:
            logger.warning("failed to confirm notification external effect: %s", exc)
            from core.application.extensions.external_effects import (
                external_effect_confirmation_failure_invocation,
            )

            invocation = external_effect_confirmation_failure_invocation(exc)
    if hasattr(bl, "log_extension_invocation"):
        raw_job_id = event.get("job_id")
        job_id_value = _as_str(raw_job_id) or None
        bl.log_extension_invocation(
            extension_key=_as_str(extension.get("extension_key")),
            invocation_type="notification_provider_deliver",
            status="ok" if invocation.get("status") == "succeeded" else "failed",
            request={"event": event, "route": route_payload},
            response=invocation,
            error_text=_as_str(invocation.get("detail")),
            request_context={
                "trace_id": _trace_id_for_job(event.get("job_id")),
                "event_id": _as_str(event.get("event_id")) or None,
                "correlation_id": _as_str(event.get("correlation_id")) or None,
                "account_id": _as_str(event.get("account_id")) or None,
                "project_id": normalized_project_id or None,
                "workflow_id": _as_str(event.get("workflow_id")) or None,
                "job_id": event.get("job_id"),
                "parent_span_id": (
                    f"event:{_as_str(event.get('event_id'))}"
                    if _as_str(event.get("event_id"))
                    else None
                ),
            },
            account_id=_as_str(event.get("account_id")) or None,
            job_id=job_id_value,
            workflow_id=_as_str(event.get("workflow_id")) or None,
            external_effect_id=external_effect_id or None,
        )
    if invocation.get("status") != "succeeded":
        return {
            "status": "failed",
            "error_text": _as_str(invocation.get("detail")) or "extension invocation failed",
            "provider_message_id": "",
            "external_effect_id": external_effect_id or None,
            "response": {**_as_dict(invocation), "external_effect": external_effect},
        }
    response, delivered, error, provider_message_id = _notification_delivery_fields(invocation)
    return {
        "status": "delivered" if delivered else "failed",
        "error_text": error,
        "provider_message_id": provider_message_id,
        "external_effect_id": external_effect_id or None,
        "response": {**response, "external_effect": external_effect}
        if external_effect
        else response,
    }


def _deliver_via_provider_runtime(
    *,
    bl,
    provider: dict,
    event: dict,
    route: dict,
    project_id: Any,
    implementation_kind: str,
    test_mode: bool = False,
) -> dict:
    normalized_kind = _as_str(implementation_kind).lower() or "builtin"
    try_extension = normalized_kind == "extension"
    if not try_extension:
        try:
            # Legacy catalog lookup classifies only. Delivery always binds an
            # installed release, and its failures must not become builtin fallback.
            extension_key = _notification_extension_key(provider)
            catalog_row = _as_dict(bl.get_extension(extension_key))
            try_extension = _as_str(catalog_row.get("kind")) == "notification_provider"
        except Exception:
            try_extension = False

    if try_extension:
        return _deliver_via_extension(
            bl=bl,
            provider=provider,
            event=event,
            route=route,
            project_id=project_id,
            test_mode=test_mode,
        )

    if test_mode:
        return {
            "status": "skipped",
            "error_text": "test_mode_suppressed",
            "provider_message_id": "",
            "response": {},
        }

    provider_key = (
        _as_str(provider.get("provider_key")) or _as_str(route.get("provider_key")) or "unknown"
    )
    return {
        "status": "failed",
        "error_text": f"unsupported core notification provider '{provider_key}'; use an enabled provider extension",
        "provider_message_id": "",
        "response": {},
    }


def _dispatch_status_from_delivery(status: str, error_text: str) -> tuple[str, str | None]:
    normalized_status = _as_str(status).lower()
    normalized_error = _as_str(error_text).lower()
    if normalized_status == "delivered":
        return "succeeded", None
    if normalized_status == "skipped":
        if "disabled" in normalized_error:
            return "skipped", "extension_disabled"
        return "skipped", "condition_not_met"
    if "timeout" in normalized_error:
        return "failed", "delivery_timeout"
    return "failed", "handler_exception"


def deliver_direct_notification(
    bl,
    *,
    connection: dict,
    extension_key: str,
    event_type: str,
    recipients: list[str],
    subject: str,
    message: str,
) -> dict:
    """Send one message through a notification extension's connection, outside any route.

    For messages the platform itself sends, such as a password reset: there is no
    project route, so the connection is named explicitly and the recipient travels
    in the route config, as it does for a configured email route. Delivery,
    secrets and the external-effect claim are the same as for any notification.
    """
    project_id = _as_str(connection.get("project_id"))
    account_id = _as_str(connection.get("account_id")) or DEFAULT_ACCOUNT_ID
    event_id = f"direct:{uuid.uuid4().hex}"
    event = build_event_envelope_v1(
        event_type=_as_str(event_type) or "system.notification",
        account_id=account_id,
        correlation_id=event_id,
        severity="info",
        summary=_as_str(subject),
        payload={"message": _as_str(message), "summary": _as_str(subject)},
        project_id=project_id or None,
        source="notification_runtime",
        aggregate_type="project",
        aggregate_id=project_id or None,
        event_id=event_id,
    )
    route = {
        "route_id": None,
        "route_key": f"direct:{_as_str(event_type)}",
        "provider_key": _as_str(extension_key),
        "event_type": _as_str(event_type),
        "severity": "info",
        "enabled": True,
        "route_config": {
            "connection_id": _as_str(connection.get("connection_id")),
            "to": [_as_str(item) for item in recipients if _as_str(item)],
            "subject": _as_str(subject),
        },
    }
    provider = {
        "provider_key": _as_str(extension_key),
        "integration_key": _as_str(extension_key),
        "provider_config": {"extension_key": _as_str(extension_key)},
    }
    return _deliver_via_extension(
        bl=bl,
        provider=provider,
        event=event,
        route=route,
        project_id=project_id or None,
    )
