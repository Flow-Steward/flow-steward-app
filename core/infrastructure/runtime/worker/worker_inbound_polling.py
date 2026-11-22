# mypy: ignore-errors
"""Extension inbound polling loop for worker processes."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

from .worker_common import _to_json_obj, logger

_INBOUND_POLL_SCAN_INTERVAL_SECONDS = 15
#: Pending provider-event dispatch records are swept this often, and at start-up.
_PROVIDER_EVENT_SWEEP_INTERVAL_SECONDS = 60


def _parse_iso_datetime(value: object | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        normalized = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _binding_poll_due(
    connection: dict[str, object],
    declaration: dict[str, object],
    *,
    mode_default: str = "auto",
) -> bool:
    """Whether the connection's poll is switched on and its interval has passed.

    A missing mode takes ``mode_default``: the connection type's declared
    ``ingress_schema`` default, ``auto`` only when the type declares none.
    """
    policy = dict(declaration)
    ingress = _to_json_obj(connection.get("ingress_config"))
    mode_field = str(policy["mode_field"]).strip()
    interval_field = str(policy["interval_field"]).strip()
    default = str(mode_default or "auto").strip().lower() or "auto"
    inbound_mode = str(ingress.get(mode_field) or default).strip().lower() or default
    if inbound_mode not in {"auto", "polling"}:
        return False
    active_transport = str(ingress.get("active_transport") or "").strip().lower()
    webhook_status = _to_json_obj(ingress.get("webhook_status"))
    webhook_healthy = bool(webhook_status.get("url_configured")) and str(
        webhook_status.get("status") or ""
    ).strip().lower() in {"healthy", "configured"}
    if (
        str(policy["transport_policy"]) == "webhook_preferred"
        and inbound_mode == "auto"
        and (active_transport == "webhook" or webhook_healthy)
    ):
        return False
    try:
        interval_seconds = int(ingress.get(interval_field) or 60)
    except Exception:
        interval_seconds = 60
    interval_seconds = max(15, interval_seconds)
    last_sync = _parse_iso_datetime(ingress.get("last_inbound_sync_at"))
    if last_sync is None:
        return True
    return (datetime.now(UTC) - last_sync).total_seconds() >= interval_seconds


def _background_poll_actions(extension: dict[str, object] | None) -> list[dict[str, object]]:
    payload = dict(extension or {})
    compiled = _to_json_obj(payload.get("compiled_manifest"))
    action_rows = payload.get("action_definitions") or compiled.get("action_definitions") or []
    if not isinstance(action_rows, list):
        return []
    declarations: list[dict[str, object]] = []
    for row in action_rows:
        action = _to_json_obj(row)
        action_id = str(action.get("action_id") or action.get("id") or "").strip()
        background = _to_json_obj(_to_json_obj(action.get("contract")).get("background"))
        if action_id and str(background.get("kind") or "").strip() == "connection_poll":
            runtime_resources = _to_json_obj(
                _to_json_obj(_to_json_obj(action.get("handler")).get("config")).get(
                    "runtime_resources"
                )
            )
            required_secrets = runtime_resources.get("provider_secrets")
            type_ids = action.get("connection_type_ids")
            declarations.append(
                {
                    "action_id": action_id,
                    **background,
                    "required_provider_secrets": (
                        required_secrets if isinstance(required_secrets, list) else []
                    ),
                    "connection_type_ids": [
                        str(item).strip()
                        for item in (type_ids if isinstance(type_ids, list) else [])
                        if str(item or "").strip()
                    ],
                }
            )
    return declarations


def _required_provider_secrets_available(
    bl: object,
    *,
    extension_id: str,
    project_id: str,
    secret_keys: list[str],
) -> bool:
    get_provider_secret = getattr(bl, "get_provider_secret", None)
    if not callable(get_provider_secret):
        return False
    for secret_key in secret_keys:
        try:
            secret_value = str(
                get_provider_secret(
                    provider_key=extension_id,
                    secret_key=secret_key,
                    project_id=project_id,
                    include_global=False,
                )
                or ""
            ).strip()
        except Exception:
            secret_value = ""
        if not secret_value:
            return False
    return True


#: Skip reason of a temporary credential failure: retried at the next interval.
SKIP_REFRESH_UNAVAILABLE = "oauth_refresh_unavailable"


@dataclass(frozen=True)
class BackgroundPollPorts:
    """Application use cases the poll loop needs, injected by the worker entrypoint.

    - ``ingress_defaults(extension, connection_type) -> dict``: declared
      ``ingress_schema`` defaults (a poll mode of ``off``);
    - ``resolve_connection(bl, connection_row=..., extension=..., extension_id=...,
      extension_install_id=..., account_id=..., project_id=...)``: an object with
      ``ok``, ``skip_reason`` and ``connection`` (the resolved ``target.connection``);
    - ``record_skip(bl, connection_id=..., action_id=..., reason=..., at=...)`` and
      ``clear_skip(bl, connection_id=...)``: the visible skip status.
    """

    ingress_defaults: Callable[[dict[str, Any], str], dict[str, Any]]
    resolve_connection: Callable[..., Any]
    record_skip: Callable[..., None]
    clear_skip: Callable[..., None]


def _connection_mode_default(
    ports: BackgroundPollPorts | None,
    extension: dict[str, object],
    connection_type: str,
    mode_field: str,
) -> str:
    if ports is None:
        return "auto"
    value = ports.ingress_defaults(extension, connection_type).get(mode_field)
    return str(value or "auto").strip().lower() or "auto"


def _poll_interval_seconds(connection: dict[str, object], declaration: dict[str, object]) -> int:
    ingress = _to_json_obj(connection.get("ingress_config"))
    try:
        interval = int(ingress.get(str(declaration.get("interval_field") or "")) or 60)
    except Exception:
        interval = 60
    return max(15, interval)


def _poll_connection_once(
    bl: object,
    *,
    worker_id: str,
    extension: dict[str, object],
    extension_id: str,
    install_id: str,
    account_id: str,
    project_id: str,
    connection: dict[str, object],
    background_action: dict[str, object],
    run_extension_action: Callable[..., dict],
    process_extension_response_envelope: Callable[..., object],
    skip_until: dict[tuple[str, str], float],
    ports: BackgroundPollPorts | None = None,
) -> None:
    """One background poll of one connection, filtered and resolved before the call."""
    import time

    action_id = str(background_action["action_id"])
    connection_id = str(connection.get("connection_id") or connection.get("id") or "").strip()
    connection_type = str(connection.get("connection_type") or "").strip()
    type_ids = list(background_action.get("connection_type_ids") or [])
    if type_ids and connection_type not in type_ids:
        # Declared for other connection types: never called for this one.
        return
    mode_field = str(background_action.get("mode_field") or "").strip()
    mode_default = _connection_mode_default(ports, extension, connection_type, mode_field)
    if not _binding_poll_due(connection, background_action, mode_default=mode_default):
        return
    skip_key = (connection_id, action_id)
    if skip_until.get(skip_key, 0.0) > time.monotonic():
        return
    poll_state = connection
    if type_ids:
        if ports is None:
            # Connection types need per-connection resolution; without it, no call.
            return
        resolution = ports.resolve_connection(
            bl,
            connection_row=connection,
            extension=extension,
            extension_id=extension_id,
            extension_install_id=install_id,
            account_id=account_id,
            project_id=project_id,
        )
        if not resolution.ok:
            if resolution.skip_reason == SKIP_REFRESH_UNAVAILABLE:
                # Temporary: keep the status, retry at the next interval.
                skip_until[skip_key] = time.monotonic() + _poll_interval_seconds(
                    connection, background_action
                )
            else:
                ports.record_skip(
                    bl,
                    connection_id=connection_id,
                    action_id=action_id,
                    reason=resolution.skip_reason,
                    at=datetime.now(UTC).isoformat(),
                )
            logger.info(
                "[%s] extension inbound poll skipped for %s/%s/%s: %s",
                worker_id,
                project_id,
                extension_id,
                connection_id,
                resolution.skip_reason,
            )
            return
        skip_until.pop(skip_key, None)
        if "background_poll_status" in _to_json_obj(connection.get("ingress_config")):
            ports.clear_skip(bl, connection_id=connection_id)
        poll_state = resolution.connection
        target = {"connection_id": connection_id, "connection": resolution.connection}
    else:
        required_secret_keys = [
            str(item or "").strip()
            for item in background_action.get("required_provider_secrets", [])
            if str(item or "").strip()
        ]
        if required_secret_keys and not (
            _required_provider_secrets_available(
                bl,
                extension_id=extension_id,
                project_id=project_id,
                secret_keys=required_secret_keys,
            )
        ):
            return
        target = {"connection_id": connection_id}
    poll_context = {
        "connection_id": connection_id,
        "state_version": int(poll_state.get("state_version") or 0),
        "config_generation": int(poll_state.get("config_generation") or 1),
        "mode_field": mode_field,
        "mode_default": mode_default,
    }
    result = run_extension_action(
        extension,
        action_id=action_id,
        context={"project_id": project_id},
        target=target,
        runtime_context={
            "logger": bl,
            "account_id": account_id,
            "project_id": project_id,
            "connection_id": connection_id,
            "extension_install_id": install_id,
        },
        timeout_seconds=30,
    )
    if result.get("status") == "succeeded":
        process_extension_response_envelope(
            result,
            extension,
            extension_id,
            scope=None,
            audit={
                "account_id": account_id,
                "project_id": project_id,
                "extension_install_id": install_id,
                "connection_id": connection_id,
            },
            bl=bl,
            get_logger=lambda bl=bl: bl,
            poll_context=poll_context,
        )
    response = _to_json_obj(result.get("response"))
    summary = _to_json_obj(response.get("result"))
    try:
        processed = int(summary.get("processed_updates") or 0)
    except Exception:
        processed = 0
    if result.get("status") != "succeeded":
        logger.warning(
            "[%s] extension inbound poll failed for %s/%s: %s",
            worker_id,
            project_id,
            extension_id,
            result.get("detail") or response.get("error") or "unknown error",
        )
    elif processed > 0:
        logger.info(
            "[%s] extension inbound poll processed %d update(s) for %s/%s",
            worker_id,
            processed,
            project_id,
            extension_id,
        )
    if result.get("detail") == "operation_serialization_unavailable":
        logger.warning(
            "[%s] extension inbound serialization unavailable for %s/%s",
            worker_id,
            project_id,
            extension_id,
        )


def _start_extension_inbound_poll_loop(
    *,
    worker_id: str,
    project_id: str | None,
    process_extension_response_envelope: Callable[..., object] | None = None,
    poll_ports: BackgroundPollPorts | None = None,
) -> tuple[threading.Event, threading.Thread] | None:
    from core.infrastructure.config.runtime_config import CONFIG
    from core.infrastructure.db.platform_stores import (
        PlatformInboundPollingStore,
        resolve_observability_dsn,
    )
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action
    from core.infrastructure.extension_runtime.extension_runtime_helpers import (
        resolve_extension_runtime_record_by_install_id,
    )

    enabled = str(_runtime_env.get("FS_EXTENSION_INBOUND_POLL_ENABLED", "1")).strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return None
    if process_extension_response_envelope is None:
        logger.info(
            "[%s] Extension inbound poll loop skipped: response handler unavailable", worker_id
        )
        return None
    dsn = resolve_observability_dsn(
        str(CONFIG.get("fs_observability_dsn") or CONFIG.get("fs_db_dsn") or "")
    )
    if not dsn:
        logger.info(
            "[%s] Extension inbound poll loop skipped: no observability DSN configured", worker_id
        )
        return None

    stop_event = threading.Event()
    #: (connection_id, action_id) -> monotonic time before which a temporarily
    #: failed poll (OAuth refresh unavailable) is not retried.
    skip_until: dict[tuple[str, str], float] = {}

    def _loop() -> None:
        while not stop_event.wait(_INBOUND_POLL_SCAN_INTERVAL_SECONDS):
            try:
                with PlatformInboundPollingStore.dedicated(dsn) as bl:
                    # Polling needs both project/connection inventory and extension
                    # runtime lookup. PlatformWorkerStore intentionally does not own
                    # the extension catalog; use the dedicated integration surface.
                    project_ids = (
                        [str(project_id)]
                        if project_id
                        else [
                            str((row or {}).get("id") or "").strip()
                            for row in (bl.get_projects() or [])
                            if str((row or {}).get("id") or "").strip()
                        ]
                    )
                    for scanned_project_id in project_ids:
                        try:
                            connections = (
                                bl.list_project_connections(scanned_project_id, enabled_only=True)
                                or []
                            )
                        except Exception as exc:
                            logger.debug(
                                "[%s] extension inbound project scan failed for %s: %s",
                                worker_id,
                                scanned_project_id,
                                exc,
                            )
                            continue
                        for connection in connections:
                            payload = dict(connection or {})
                            if (
                                str(payload.get("connection_kind") or "").strip().lower()
                                != "extension"
                            ):
                                continue
                            extension_id = str(payload.get("extension_id") or "").strip()
                            binding_project_id = str(payload.get("project_id") or "").strip()
                            account_id = str(payload.get("account_id") or "").strip()
                            install_id = str(payload.get("extension_install_id") or "").strip()
                            if not all((extension_id, binding_project_id, account_id, install_id)):
                                continue
                            try:
                                extension, _ = resolve_extension_runtime_record_by_install_id(
                                    install_id,
                                    get_logger_only=lambda bl=bl: bl,
                                    account_id=account_id,
                                )
                                if not extension:
                                    continue
                                resolved_extension_id = str(
                                    extension.get("extension_key")
                                    or extension.get("extension_id")
                                    or extension.get("id")
                                    or ""
                                ).strip()
                                resolved_install_id = str(
                                    extension.get("extension_install_id")
                                    or extension.get("install_id")
                                    or ""
                                ).strip()
                                if (
                                    resolved_extension_id != extension_id
                                    or resolved_install_id != install_id
                                ):
                                    raise ValueError("resolved extension identity mismatch")
                                for background_action in _background_poll_actions(extension):
                                    _poll_connection_once(
                                        bl,
                                        worker_id=worker_id,
                                        extension=extension,
                                        extension_id=extension_id,
                                        install_id=install_id,
                                        account_id=account_id,
                                        project_id=binding_project_id,
                                        connection=payload,
                                        background_action=background_action,
                                        run_extension_action=run_extension_action,
                                        process_extension_response_envelope=(
                                            process_extension_response_envelope
                                        ),
                                        skip_until=skip_until,
                                        ports=poll_ports,
                                    )
                            except Exception as exc:
                                # One connection/install must not starve other bindings.
                                logger.warning(
                                    "[%s] extension inbound poll crashed for %s/%s/%s: %s",
                                    worker_id,
                                    binding_project_id,
                                    extension_id,
                                    install_id,
                                    exc,
                                )
                                continue
            except Exception as exc:
                logger.debug("[%s] extension inbound poll scan failed: %s", worker_id, exc)

    thread = threading.Thread(
        target=_loop,
        daemon=True,
        name=f"extension-inbound-poll-{project_id or 'all'}",
    )
    thread.start()
    logger.info(
        "[%s] Started extension inbound poll loop (project_scope=%s)",
        worker_id,
        project_id or "all",
    )
    return stop_event, thread


def _start_provider_event_dispatch_sweep_loop(
    *,
    worker_id: str,
    sweep: Callable[[], object] | None,
    interval_seconds: float = _PROVIDER_EVENT_SWEEP_INTERVAL_SECONDS,
) -> tuple[threading.Event, threading.Thread] | None:
    """Run ``sweep`` after start-up and then every ``interval_seconds``.

    The sweep starts the workflows of provider-event dispatch records that no
    attempt finished: a crash between the poll and the job, a worker restart,
    or a poll that never comes again.
    """
    if sweep is None:
        return None
    stop_event = threading.Event()

    def _loop() -> None:
        while True:
            try:
                sweep()
            except Exception as exc:
                logger.warning("[%s] provider event dispatch sweep failed: %s", worker_id, exc)
            if stop_event.wait(interval_seconds):
                return

    thread = threading.Thread(target=_loop, daemon=True, name="provider-event-dispatch-sweep")
    thread.start()
    logger.info("[%s] Started provider event dispatch sweep", worker_id)
    return stop_event, thread
