"""Notification dispatch using installed provider extensions."""

from __future__ import annotations

import logging
from typing import Any

from core.application.notifications.contracts import (
    _as_dict,
    _as_str,
    _event_type_matches,
    _is_test_mode_dispatch,
    _normalize_severity,
    _resolve_authoritative_account_id,
    _severity_allows,
    _trace_id_for_job,
    _uuid_text_or_none,
)
from core.application.notifications.delivery import (
    _deliver_via_provider_runtime,
    _dispatch_status_from_delivery,
)
from core.application.orchestration.models import build_event_envelope_v1
from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID

logger = logging.getLogger(__name__)


def dispatch_project_notification_event(
    bl,
    *,
    project_id: Any,
    event_type: str,
    severity: str = "info",
    workflow_id: str | None = None,
    job_id: Any | None = None,
    payload: dict | None = None,
    event_id: str | None = None,
    account_id: str | None = None,
    correlation_id: str | None = None,
    route_ids: list[str] | None = None,
    direct_routes: list[dict[str, Any]] | None = None,
    ignore_filters: bool = False,
) -> list[dict]:
    """
    Deliver one normalized event through DB-configured notification routes.

    Returns persisted delivery rows when available; otherwise best-effort in-memory rows.
    """
    if bl is None or not hasattr(bl, "list_notification_routes"):
        return []
    normalized_event_type = _as_str(event_type)
    if not normalized_event_type:
        raise ValueError("event_type is required")
    normalized_severity = _normalize_severity(severity)
    normalized_workflow_id = _as_str(workflow_id) or None
    normalized_payload = _as_dict(payload)
    normalized_project_id = _as_str(project_id)
    normalized_job_id = _as_str(job_id) or None
    normalized_account_id = _resolve_authoritative_account_id(bl, normalized_project_id, account_id)
    if not _as_str(event_id) and not _as_str(correlation_id) and normalized_job_id is None:
        raise ValueError(
            "event_id or correlation_id is required for notifications without a durable job_id"
        )
    normalized_correlation_id = (
        _as_str(correlation_id)
        or _as_str(event_id)
        or (
            f"job:{normalized_job_id}:{normalized_event_type}"
            if normalized_job_id is not None
            else f"project:{normalized_project_id}:{normalized_event_type}"
        )
    )
    event = build_event_envelope_v1(
        event_type=normalized_event_type,
        account_id=normalized_account_id,
        correlation_id=normalized_correlation_id,
        severity=normalized_severity,
        summary=_as_str(
            normalized_payload.get("summary")
            or normalized_payload.get("message")
            or normalized_event_type
        ),
        payload=normalized_payload,
        project_id=normalized_project_id,
        workflow_id=normalized_workflow_id,
        job_id=normalized_job_id,
        source="notification_runtime",
        aggregate_type="project",
        aggregate_id=normalized_project_id,
        event_id=_as_str(event_id) or normalized_correlation_id,
        metadata={
            "trace_id": _trace_id_for_job(job_id),
        },
    )

    test_mode_dispatch = _is_test_mode_dispatch(bl, event)
    if test_mode_dispatch:
        logger.info(
            "dispatch_project_notification_event: suppressing provider delivery in test_mode project_id=%s job_id=%s event_type=%s",
            project_id,
            event.get("job_id"),
            normalized_event_type,
        )

    routes: list[dict[str, Any]] = []
    if direct_routes:
        routes = [dict(row) for row in direct_routes if isinstance(row, dict)]
    else:
        try:
            enabled_only = not bool(route_ids)
            routes = [
                dict(row)
                for row in (
                    bl.list_notification_routes(
                        project_id=normalized_project_id, enabled_only=enabled_only
                    )
                    or []
                )
                if isinstance(row, dict)
            ]
        except Exception as exc:
            logger.warning("dispatch_project_notification_event: route lookup failed: %s", exc)
            return []

    deliveries: list[dict] = []
    route_id_filter = {str(item).strip() for item in (route_ids or []) if str(item).strip()}
    normalized_event_id = _as_str(event.get("event_id")) or None
    can_track_dispatch = bool(normalized_event_id and hasattr(bl, "record_event_dispatch"))
    if can_track_dispatch and hasattr(bl, "record_domain_event"):
        try:
            bl.record_domain_event(
                event_id=str(normalized_event_id),
                event_type=normalized_event_type,
                account_id=_as_str(event.get("account_id")) or DEFAULT_ACCOUNT_ID,
                severity=normalized_severity,
                summary=_as_str(event.get("summary")) or normalized_event_type,
                payload=_as_dict(event.get("payload")),
                actor=_as_dict(event.get("actor")),
                urls=_as_dict(event.get("urls")),
                envelope=event,
                occurred_at=_as_str(event.get("occurred_at")) or None,
                project_id=normalized_project_id,
                workflow_id=normalized_workflow_id,
                job_id=normalized_job_id,
                group_id=None,
                step_id=None,
                source=_as_str(event.get("source")) or "notification_runtime",
                aggregate_type=_as_str(event.get("aggregate_type")) or "project",
                aggregate_id=_as_str(event.get("aggregate_id")) or normalized_project_id,
                correlation_id=_as_str(event.get("correlation_id")) or None,
                status="dispatching",
            )
        except Exception as exc:
            logger.warning(
                "dispatch_project_notification_event: domain event ledger write failed: %s", exc
            )
            can_track_dispatch = False

    for route in routes:
        if not isinstance(route, dict):
            continue
        route_id = str(route.get("route_id") or "").strip()
        if route_id_filter and route_id not in route_id_filter:
            continue
        route_key = _as_str(route.get("route_key")) or (
            f"route:{route_id}" if route_id else "route:unknown"
        )
        dispatch: dict[str, Any] | None = None

        def _record_skip(
            reason_code: str,
            reason_detail: str,
            *,
            _route_key: str = route_key,
            _route: dict[str, Any] = route,
            _route_id: str = route_id,
        ) -> None:
            if not can_track_dispatch:
                return
            try:
                bl.record_event_dispatch(
                    event_id=str(normalized_event_id),
                    target_type="notification_route",
                    target_id=_route_key,
                    target_label=_as_str(_route.get("display_name")) or _route_key,
                    account_id=_as_str(event.get("account_id")) or DEFAULT_ACCOUNT_ID,
                    project_id=normalized_project_id,
                    workflow_id=normalized_workflow_id,
                    job_id=normalized_job_id,
                    status="skipped",
                    reason_code=reason_code,
                    reason_detail=reason_detail,
                    metadata={
                        "route_id": _route_id or None,
                        "provider_key": _as_str(_route.get("provider_key")) or None,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "dispatch_project_notification_event: skip dispatch tracking failed: %s", exc
                )

        route_workflow_id = _as_str(route.get("workflow_id"))
        route_account_id = _as_str(route.get("account_id"))
        if route_account_id and route_account_id != normalized_account_id:
            _record_skip(
                "account_scope_mismatch",
                f"Route account '{route_account_id}' does not match event account '{normalized_account_id}'",
            )
            continue
        if (
            not ignore_filters
            and route_workflow_id
            and route_workflow_id != (normalized_workflow_id or "")
        ):
            _record_skip(
                "workflow_not_allowed",
                f"Route workflow '{route_workflow_id}' does not match event workflow '{normalized_workflow_id or ''}'",
            )
            continue
        if not ignore_filters and not _event_type_matches(
            _as_str(route.get("event_type")), normalized_event_type
        ):
            _record_skip(
                "event_type_not_subscribed",
                f"Route event_type '{_as_str(route.get('event_type'))}' does not match '{normalized_event_type}'",
            )
            continue
        if not ignore_filters and not _severity_allows(
            _as_str(route.get("severity")), normalized_severity
        ):
            _record_skip(
                "severity_filter_mismatch",
                f"Route severity '{_as_str(route.get('severity'))}' does not allow '{normalized_severity}'",
            )
            continue
        if can_track_dispatch:
            try:
                dispatch = (
                    bl.record_event_dispatch(
                        event_id=str(normalized_event_id),
                        target_type="notification_route",
                        target_id=route_key,
                        target_label=_as_str(route.get("display_name")) or route_key,
                        account_id=_as_str(event.get("account_id")) or DEFAULT_ACCOUNT_ID,
                        project_id=normalized_project_id,
                        workflow_id=normalized_workflow_id,
                        job_id=normalized_job_id,
                        status="matched",
                        metadata={
                            "route_id": route_id or None,
                            "provider_key": _as_str(route.get("provider_key")) or None,
                        },
                    )
                    or {}
                )
            except Exception as exc:
                logger.warning(
                    "dispatch_project_notification_event: dispatch tracking failed: %s", exc
                )
                dispatch = None

        provider_key = _as_str(route.get("provider_key"))
        implementation_kind = _as_str(route.get("implementation_kind")).lower() or "builtin"
        status = "failed"
        error_text = ""
        provider_message_id = ""
        response_payload: dict[str, Any] = {}
        external_effect_id: str | None = None

        try:
            result = _deliver_via_provider_runtime(
                bl=bl,
                provider=route,
                event=event,
                route=route,
                project_id=normalized_project_id,
                implementation_kind=implementation_kind,
                test_mode=test_mode_dispatch,
            )
            status = _as_str(result.get("status")).lower() or "failed"
            error_text = _as_str(result.get("error_text"))
            provider_message_id = _as_str(result.get("provider_message_id"))
            response_payload = _as_dict(result.get("response"))
            external_effect_id = _as_str(result.get("external_effect_id")) or None
        except Exception as exc:
            status = "failed"
            error_text = str(exc)
            response_payload = {}
            external_effect_id = None

        saved = None
        persisted_route_id = _uuid_text_or_none(route.get("route_id"))
        if hasattr(bl, "log_notification_delivery"):
            try:
                saved = bl.log_notification_delivery(
                    route_id=persisted_route_id,
                    project_id=normalized_project_id,
                    workflow_id=normalized_workflow_id,
                    job_id=normalized_job_id,
                    provider_key=provider_key or None,
                    event_type=normalized_event_type,
                    severity=normalized_severity,
                    status=status
                    if status in {"pending", "delivered", "failed", "skipped"}
                    else "failed",
                    request={
                        "event": event,
                        "route": {
                            "route_key": _as_str(route.get("route_key")),
                            "event_type": _as_str(route.get("event_type")),
                            "severity": _as_str(route.get("severity")),
                            "provider_key": provider_key,
                            "route_config": _as_dict(route.get("route_config")),
                        },
                    },
                    response=response_payload,
                    provider_message_id=provider_message_id or None,
                    error_text=error_text or None,
                    external_effect_id=external_effect_id,
                )
            except Exception as exc:
                logger.warning(
                    "dispatch_project_notification_event: delivery logging failed: %s", exc
                )

        if isinstance(saved, dict):
            delivery_row = dict(saved)
        else:
            delivery_row = {
                "route_id": persisted_route_id,
                "project_id": normalized_project_id,
                "workflow_id": normalized_workflow_id,
                "job_id": normalized_job_id,
                "provider_key": provider_key,
                "event_type": normalized_event_type,
                "severity": normalized_severity,
                "status": status,
                "provider_message_id": provider_message_id or None,
                "error_text": error_text or None,
                "external_effect_id": external_effect_id,
                "request": {
                    "event": event,
                    "route": _as_dict(route),
                },
                "response": response_payload,
            }

        if isinstance(dispatch, dict):
            dispatch_id = _as_str(dispatch.get("dispatch_id")) or None
            if dispatch_id:
                dispatch_status, reason_code = _dispatch_status_from_delivery(status, error_text)
                if hasattr(bl, "record_event_delivery_attempt"):
                    attempt_kwargs: dict[str, Any] = {}
                    if reason_code:
                        attempt_kwargs["dispatch_reason_code"] = reason_code
                        attempt_kwargs["dispatch_reason_detail"] = _as_str(error_text) or None
                    if dispatch_status == "succeeded":
                        attempt_status = "succeeded"
                    elif dispatch_status == "skipped":
                        attempt_status = "skipped"
                    else:
                        attempt_status = "failed"
                    try:
                        bl.record_event_delivery_attempt(
                            event_id=str(normalized_event_id),
                            dispatch_id=dispatch_id,
                            status=attempt_status,
                            handler="notification_runtime",
                            request=delivery_row.get("request")
                            if isinstance(delivery_row, dict)
                            else {},
                            response=_as_dict(response_payload),
                            error_text=_as_str(error_text) or None,
                            external_effect_id=external_effect_id,
                            **attempt_kwargs,
                        )
                    except Exception as exc:
                        logger.warning(
                            "dispatch_project_notification_event: attempt tracking failed: %s", exc
                        )
                elif hasattr(bl, "update_event_dispatch_status"):
                    update_kwargs: dict[str, Any] = {}
                    if reason_code:
                        update_kwargs["reason_code"] = reason_code
                        update_kwargs["reason_detail"] = _as_str(error_text) or None
                    try:
                        bl.update_event_dispatch_status(
                            dispatch_id, status=dispatch_status, **update_kwargs
                        )
                    except Exception as exc:
                        logger.warning(
                            "dispatch_project_notification_event: dispatch status update failed: %s",
                            exc,
                        )
                delivery_row["dispatch_id"] = dispatch_id

        deliveries.append(delivery_row)
    return deliveries
