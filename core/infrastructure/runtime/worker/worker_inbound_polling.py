# mypy: ignore-errors
"""Extension inbound polling loop for worker processes."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

from .worker_common import _to_json_obj, logger

_INBOUND_POLL_SCAN_INTERVAL_SECONDS = 15


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


def _binding_poll_due(connection: dict[str, object], declaration: dict[str, object]) -> bool:
    policy = dict(declaration)
    ingress = _to_json_obj(connection.get("ingress_config"))
    mode_field = str(policy["mode_field"]).strip()
    interval_field = str(policy["interval_field"]).strip()
    inbound_mode = str(ingress.get(mode_field) or "auto").strip().lower() or "auto"
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
            declarations.append(
                {
                    "action_id": action_id,
                    **background,
                    "required_provider_secrets": (
                        required_secrets if isinstance(required_secrets, list) else []
                    ),
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


def _start_extension_inbound_poll_loop(
    *,
    worker_id: str,
    project_id: str | None,
    process_extension_response_envelope: Callable[..., object] | None = None,
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
                                    if not _binding_poll_due(payload, background_action):
                                        continue
                                    required_secret_keys = [
                                        str(item or "").strip()
                                        for item in background_action.get(
                                            "required_provider_secrets", []
                                        )
                                        if str(item or "").strip()
                                    ]
                                    if required_secret_keys and not (
                                        _required_provider_secrets_available(
                                            bl,
                                            extension_id=extension_id,
                                            project_id=binding_project_id,
                                            secret_keys=required_secret_keys,
                                        )
                                    ):
                                        continue
                                    action_id = str(background_action["action_id"])
                                    connection_id = str(
                                        payload.get("connection_id") or payload.get("id") or ""
                                    ).strip()
                                    result = run_extension_action(
                                        extension,
                                        action_id=action_id,
                                        context={"project_id": binding_project_id},
                                        target={"connection_id": connection_id},
                                        runtime_context={
                                            "logger": bl,
                                            "account_id": account_id,
                                            "project_id": binding_project_id,
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
                                                "project_id": binding_project_id,
                                                "extension_install_id": install_id,
                                            },
                                            bl=bl,
                                            get_logger=lambda bl=bl: bl,
                                        )
                                    response = _to_json_obj(result.get("response"))
                                    summary = _to_json_obj(response.get("result"))
                                    processed = int(summary.get("processed_updates") or 0)
                                    if result.get("status") != "succeeded":
                                        logger.warning(
                                            "[%s] extension inbound poll failed for %s/%s: %s",
                                            worker_id,
                                            binding_project_id,
                                            extension_id,
                                            result.get("detail")
                                            or response.get("error")
                                            or "unknown error",
                                        )
                                    elif processed > 0:
                                        logger.info(
                                            "[%s] extension inbound poll processed %d update(s) for %s/%s",
                                            worker_id,
                                            processed,
                                            binding_project_id,
                                            extension_id,
                                        )
                                    if (
                                        result.get("detail")
                                        == "operation_serialization_unavailable"
                                    ):
                                        logger.warning(
                                            "[%s] extension inbound serialization unavailable for %s/%s",
                                            worker_id,
                                            binding_project_id,
                                            extension_id,
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
