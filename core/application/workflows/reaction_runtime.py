"""Reusable helpers for workflow reaction evaluation and execution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.application.orchestration.agents.common import sanitize_external_input

_MESSAGE_CANCELLED_BY_AUTOMATION = "Cancelled by automation"


def _notification_recipients(value: Any) -> list[str]:
    """Email addresses from an automation's recipients: a list, or one comma-separated text."""
    items = value if isinstance(value, list) else str(value or "").split(",")
    return [text for item in items if (text := str(item or "").strip())][:20]


def normalize_reaction_route_keys(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = str(value or "").split(",")
    output: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        key = str(item or "").strip()
        if not key:
            continue
        lowered = key.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        output.append(key)
    return output


def reaction_value_matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        for key, value in expected.items():
            if key not in actual:
                return False
            if not reaction_value_matches(actual.get(key), value):
                return False
        return True
    if isinstance(expected, (list, tuple, set)):
        return actual in expected
    return actual == expected


def lookup_dotted_value(payload: dict[str, Any] | None, dotted_key: str) -> Any:
    if not isinstance(payload, dict):
        return None
    current: Any = payload
    for segment in str(dotted_key or "").split("."):
        if not segment:
            continue
        if not isinstance(current, dict):
            return None
        current = current.get(segment)
    return current


def reaction_conditions_match(
    event: dict[str, Any],
    conditions: dict[str, Any] | None,
    *,
    to_json_obj,
) -> bool:
    if not isinstance(conditions, dict) or not conditions:
        return True
    payload = to_json_obj(event.get("payload")) or {}
    if not isinstance(payload, dict):
        payload = {}
    context = {
        "event_type": str(event.get("event_type") or ""),
        "source": str(event.get("source") or ""),
        "source_id": str(event.get("source_id") or ""),
        "event_key": str(event.get("event_key") or ""),
        "workflow_id": str(event.get("workflow_id") or ""),
        "job_id": event.get("job_id"),
        "payload": payload,
    }
    for key, expected in conditions.items():
        normalized_key = str(key or "").strip()
        if not normalized_key:
            continue
        if normalized_key == "payload":
            actual = payload
        elif normalized_key.startswith("payload."):
            actual = lookup_dotted_value(payload, normalized_key[len("payload.") :])
        else:
            actual = context.get(normalized_key)
        if not reaction_value_matches(actual, expected):
            return False
    return True


def parse_event_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except Exception:
        return None


def reaction_is_debounced(
    bl: Any,
    event: dict[str, Any],
    rule: dict[str, Any],
    *,
    parse_event_datetime_fn=parse_event_datetime,
) -> bool:
    debounce_seconds = max(0, int(rule.get("debounce_seconds") or 0))
    job_id = event.get("job_id")
    if debounce_seconds <= 0 or not job_id or not hasattr(bl, "list_job_reaction_events"):
        return False
    event_type = str(event.get("event_type") or "")
    current_id = event.get("id")
    current_ts = parse_event_datetime_fn(event.get("created_at")) or datetime.now(UTC)
    try:
        history = bl.list_job_reaction_events(str(job_id), limit=100) or []
    except Exception:
        return False
    for row in history:
        if not isinstance(row, dict):
            continue
        if current_id is not None and row.get("id") == current_id:
            continue
        if str(row.get("event_type") or "") != event_type:
            continue
        row_ts = parse_event_datetime_fn(row.get("created_at"))
        if not row_ts:
            continue
        if (current_ts - row_ts).total_seconds() <= debounce_seconds:
            return True
    return False


def resolve_job_workflow_id(job: dict[str, Any] | None, *, to_json_obj) -> str:
    meta = to_json_obj((job or {}).get("meta")) or {}
    if not isinstance(meta, dict):
        return ""
    return str(
        meta.get("selected_workflow_id")
        or meta.get("requested_workflow_id")
        or meta.get("workflow_id")
        or ""
    ).strip()


def reaction_action_result_executed(result: dict[str, Any] | None) -> bool:
    if not isinstance(result, dict):
        return False
    if bool(result.get("executed")):
        return True
    status = str(result.get("status") or "").strip().lower()
    return status in {"executed", "resumed", "completed", "accepted"}


def reaction_action_comment(
    event: dict[str, Any], action_payload: dict[str, Any], *, to_json_obj
) -> str:
    payload = to_json_obj(event.get("payload")) or {}
    if not isinstance(payload, dict):
        payload = {}
    raw_comment = (
        action_payload.get("comment") or payload.get("comment") or payload.get("text") or ""
    )
    return sanitize_external_input(str(raw_comment or "").strip())


def reaction_scope_matches_event(
    rule: dict[str, Any],
    event: dict[str, Any],
    *,
    split_scoped_reaction_key_fn,
    to_json_obj,
) -> bool:
    """Whether an event belongs to the rule's scope.

    A workflow rule sees every event of its workflow; a group rule the events
    of that real group (its own group events, its steps, its waits); a step
    rule the events of that step. A rule of a retired or unknown scope never
    matches.
    """
    key_scope_kind, key_scope_id, _ = split_scoped_reaction_key_fn(rule.get("reaction_key"))
    if key_scope_kind == "workflow":
        return True

    payload = to_json_obj(event.get("payload")) or {}
    if not isinstance(payload, dict):
        payload = {}
    if key_scope_kind == "group":
        candidates = [event.get("group_id"), payload.get("group_id")]
    elif key_scope_kind == "step":
        candidates = [event.get("step_id"), payload.get("step_id")]
    else:
        return False
    target = str(key_scope_id or "").strip()
    return bool(target) and any(str(candidate or "").strip() == target for candidate in candidates)


def cancel_job_from_reaction(
    job_id: str,
    comment: str,
    *,
    get_logger_only,
    err_database_not_configured: str,
    err_job_not_found_lower: str,
    logger,
) -> dict[str, Any]:
    """Cancel a run from a reaction: the same durable cancel as the API.

    The job row is the authority; a pause left in the checkpoint is never
    resumed after it, and child invocations get durable cancel requests.
    """
    bl = get_logger_only()
    if not bl:
        return {
            "status": "ignored",
            "reason": err_database_not_configured,
            "outcome_code": "retryable_error",
            "executed": False,
        }
    job = bl.get_job(str(job_id))
    if not job:
        return {
            "status": "ignored",
            "reason": err_job_not_found_lower,
            "outcome_code": "retryable_error",
            "executed": False,
        }

    current_status = str(job.get("status") or "").strip().lower()
    if current_status in {"completed", "failed", "cancelled", "max_iterations"}:
        return {
            "status": "ignored",
            "reason": f"job is already {current_status}",
            "outcome_code": "already_handled",
            "executed": False,
        }

    try:
        from core.application.jobs.terminal_persistence import TerminalPersistenceService

        TerminalPersistenceService(bl).complete_failure(
            job_id=str(job_id),
            status="cancelled",
            error_message=comment or _MESSAGE_CANCELLED_BY_AUTOMATION,
            error_code="reaction_cancelled",
            category="cancellation",
        )
    except Exception as exc:
        logger.warning("cancel_job_from_reaction failed for job_id=%s: %s", job_id, exc)
        return {"status": "failed", "reason": str(exc), "executed": False}

    return {"status": "cancelled", "job_id": str(job_id), "executed": True}


def retry_interrupted_step_from_reaction(
    job_id: str,
    *,
    requested_by: str,
    get_logger_only,
    err_database_not_configured: str,
    err_job_not_found_lower: str,
    to_json_obj,
    sanitize_retryable_interrupted_step_snapshot,
    retryable_interrupted_step_status: str,
    resolve_job_project_name,
    get_project_run_config,
    build_rerun_workflow_selection,
    load_replay_snapshot,
    enqueue_task_in_queue,
    append_runtime_journal,
) -> dict[str, Any]:
    bl = get_logger_only()
    if not bl:
        return {"status": "ignored", "reason": err_database_not_configured, "executed": False}
    job = bl.get_job(str(job_id))
    if not job:
        return {"status": "ignored", "reason": err_job_not_found_lower, "executed": False}

    meta = to_json_obj(job.get("meta"))
    if not isinstance(meta, dict):
        meta = {}
    snapshot = sanitize_retryable_interrupted_step_snapshot(
        to_json_obj(meta.get("retryable_interrupted_step"))
    )
    if not snapshot:
        return {
            "status": "ignored",
            "reason": "no retryable interrupted-step snapshot is available for this job",
            "executed": False,
        }

    current_status = str(job.get("status") or "").strip().lower()
    if current_status not in {
        retryable_interrupted_step_status,
        "orphaned",
        "stuck",
        "failed",
    }:
        return {
            "status": "ignored",
            "reason": f"job status '{job.get('status')}' is not eligible for interrupted-step retry",
            "executed": False,
        }

    project_id = str(job.get("project_id") or "").strip()
    if not project_id:
        return {"status": "ignored", "reason": "job has no project_id", "executed": False}

    thread_id = str(job.get("thread_id") or "").strip()
    if not thread_id:
        return {
            "status": "ignored",
            "reason": "job has no thread_id checkpoint key",
            "executed": False,
        }

    try:
        loaded_run_cfg = get_project_run_config(project_id)
        run_cfg = loaded_run_cfg if isinstance(loaded_run_cfg, dict) else {}
    except Exception as exc:
        return {
            "status": "failed",
            "reason": f"project run configuration unavailable: {exc}",
            "executed": False,
        }
    project_config = dict(run_cfg) if isinstance(run_cfg, dict) else {}
    project_name = str(
        project_config.get("project_name") or resolve_job_project_name(bl, job) or ""
    ).strip()
    if not project_name:
        project_name = str(job.get("project_name") or f"project-{project_id}").strip()

    from core.application.jobs.job_invocation_artifacts import JobReplaySnapshotError

    try:
        replay_snapshot = load_replay_snapshot(
            pointer=meta.get("replay_snapshot"),
            project_id=project_id,
            job_id=str(job.get("id") or job_id),
        )
        extra_state, _ = build_rerun_workflow_selection(
            job,
            replay_snapshot=replay_snapshot,
        )
    except JobReplaySnapshotError as exc:
        return {"status": "failed", "reason": exc.code, "executed": False}
    if not isinstance(extra_state, dict):
        extra_state = {}
    extra_state["retry_interrupted_step"] = True
    extra_state["retry_interrupted_agent"] = str(snapshot.get("agent_id") or "")
    extra_state["retry_interrupted_skill"] = str(snapshot.get("skill") or "")
    extra_state["retry_interrupted_step_id"] = str(snapshot.get("step_id") or "")
    extra_state["retry_interrupted_step_execution_id"] = str(
        snapshot.get("step_execution_id") or ""
    )

    queue_name = f"platform.project.{project_id}"
    original_meta = dict(meta)
    updated_meta = dict(meta)
    updated_meta.pop("retryable_interrupted_step", None)
    updated_meta["last_retry_step_request"] = {
        "requested_at": datetime.now(UTC).isoformat(),
        "requested_by": requested_by,
        "agent_id": snapshot.get("agent_id"),
        "skill": snapshot.get("skill"),
        "step_id": snapshot.get("step_id"),
        "step_execution_id": snapshot.get("step_execution_id"),
    }

    try:
        bl.update_job(
            str(job_id),
            status="pending",
            queue_name=queue_name,
            queued_at="NOW()",
            worker_id="",
            meta=updated_meta,
        )
        if hasattr(bl, "update_job_runtime"):
            bl.update_job_runtime(
                str(job_id),
                current_step_id=str(snapshot.get("step_id") or ""),
                current_group_id=(
                    str(job.get("current_group_id") or "").strip() or None
                    if str(job.get("current_step_id") or "").strip()
                    == str(snapshot.get("step_id") or "").strip()
                    else None
                ),
                current_agent=str(snapshot.get("agent_id") or ""),
                heartbeat_now=True,
                clear_last_error=True,
                last_error=None,
            )

        enqueue_task_in_queue(
            project_id=project_id,
            job_id=str(job_id),
            task_id=str(job.get("task_id") or f"retry-step-{job_id}"),
            task_summary=str(job.get("task_summary") or "Retry interrupted step"),
            task_source=str(job.get("task_source") or "automation"),
            project_name=project_name,
            project_config=project_config,
            thread_id=thread_id,
            mode=job.get("mode"),
            extra_state=extra_state,
        )
    except Exception as exc:
        try:
            rollback_meta = dict(original_meta)
            rollback_meta["retryable_interrupted_step"] = snapshot
            bl.update_job(
                str(job_id),
                status=retryable_interrupted_step_status,
                worker_id="",
                meta=rollback_meta,
            )
            if hasattr(bl, "update_job_runtime"):
                bl.update_job_runtime(
                    str(job_id),
                    heartbeat_now=True,
                    last_error=f"Retry step enqueue failed: {str(exc)[:3000]}",
                )
        except Exception:
            pass
        return {
            "status": "failed",
            "reason": f"retry step enqueue failed: {exc}",
            "job_id": str(job_id),
            "executed": False,
        }

    append_runtime_journal(
        bl,
        str(job_id),
        event="automation_retry_step",
        reason="Interrupted step re-queued by automation",
        detail={
            "requested_by": requested_by,
            "queue_name": queue_name,
            "thread_id": thread_id,
            "agent_id": snapshot.get("agent_id"),
            "skill": snapshot.get("skill"),
            "step_id": snapshot.get("step_id"),
            "step_execution_id": snapshot.get("step_execution_id"),
        },
    )
    return {
        "status": "pending",
        "enqueued": True,
        "job_id": str(job_id),
        "thread_id": thread_id,
        "queue_name": queue_name,
        "executed": True,
    }


def dispatch_notification_from_reaction(
    event: dict[str, Any],
    action_payload: dict[str, Any],
    *,
    get_logger_only,
    err_database_not_configured: str,
    to_json_obj,
    resolve_job_workflow_id,
    logger,
) -> dict[str, Any]:
    bl = get_logger_only()
    if not bl:
        return {"status": "ignored", "reason": err_database_not_configured, "executed": False}

    job_id_raw = event.get("job_id")
    job_id = str(job_id_raw or "").strip() or None
    job = bl.get_job(job_id) if job_id is not None else None

    project_id_raw = event.get("project_id")
    if project_id_raw in (None, ""):
        project_id_raw = (job or {}).get("project_id") if isinstance(job, dict) else None
    project_id = str(project_id_raw or "").strip()
    if not project_id:
        return {
            "status": "ignored",
            "reason": "project_id is required for notify action",
            "executed": False,
        }

    workflow_id = (
        str(
            event.get("workflow_id")
            or (resolve_job_workflow_id(job) if isinstance(job, dict) else "")
            or ""
        ).strip()
        or None
    )
    payload = to_json_obj(event.get("payload")) or {}
    if not isinstance(payload, dict):
        payload = {}

    if payload.get("propagated") is True:
        from core.application.workflows.approval_waiting_propagation import (
            trusted_forwarded_approval_job,
        )

        approval_job_id = trusted_forwarded_approval_job(bl, event)
        if approval_job_id is None:
            return {
                "status": "ignored",
                "reason": "Forwarded approval is no longer linked and pending",
                "executed": False,
            }
        job_id = approval_job_id

    action_target = str(action_payload.get("target") or "").strip().lower()
    route_keys = normalize_reaction_route_keys(
        action_payload.get("route_keys") or action_payload.get("route_key") or ""
    )
    route_ids: list[str] = []
    direct_routes: list[dict[str, Any]] | None = None
    if route_keys:
        try:
            route_rows = (
                bl.list_notification_routes(project_id=project_id, enabled_only=False) or []
            )
        except Exception as exc:
            return {
                "status": "failed",
                "reason": f"notification route lookup failed: {exc}",
                "executed": False,
            }
        route_index = {
            str((row or {}).get("route_key") or "").strip().lower(): dict(row or {})
            for row in (route_rows or [])
            if str((row or {}).get("route_key") or "").strip()
        }
        missing = [key for key in route_keys if key.lower() not in route_index]
        if missing:
            return {
                "status": "ignored",
                "reason": f"unknown notification route(s): {', '.join(missing)}",
                "executed": False,
            }
        for key in route_keys:
            route_id = str(route_index[key.lower()].get("route_id") or "").strip()
            if route_id:
                route_ids.append(route_id)
        if not route_ids:
            return {
                "status": "ignored",
                "reason": "selected notification route has no persisted route_id",
                "executed": False,
            }
    elif action_target == "extension_channel":
        provider_key = str(action_payload.get("provider_key") or "").strip()
        channel_id = str(action_payload.get("channel_id") or "").strip()
        # An email extension addresses people, not channels: the automation names them.
        recipients = _notification_recipients(action_payload.get("recipients"))
        if not provider_key:
            return {
                "status": "ignored",
                "reason": "provider_key is required for extension_channel target",
                "executed": False,
            }
        if not channel_id and not recipients:
            return {
                "status": "ignored",
                "reason": "channel_id or recipients is required for extension_channel target",
                "executed": False,
            }
        channel_capability = (
            str(action_payload.get("channel_capability") or "notify").strip().lower()
        )
        explicit_event_type = str(action_payload.get("event_type") or "").strip()
        direct_event_type = (
            explicit_event_type
            or ("approval.requested" if channel_capability == "human_decision" else "")
            or str(event.get("event_type") or "").strip()
            or "workflow.automation.notify"
        )
        target_label = channel_id or ",".join(recipients)
        route_config: dict[str, Any] = (
            {"channel_id": channel_id, "required_capability": channel_capability or "notify"}
            if channel_id
            else {"to": recipients}
        )
        direct_routes = [
            {
                "route_key": f"extension_channel:{provider_key}:{target_label}",
                "display_name": f"{provider_key}:{target_label}",
                "provider_key": provider_key,
                "implementation_kind": "extension",
                "enabled": True,
                "event_type": direct_event_type,
                "severity": action_payload.get("severity") or "info",
                "route_config": route_config,
            }
        ]

    notify_payload = dict(payload)
    explicit_message = sanitize_external_input(str(action_payload.get("message") or "").strip())
    if explicit_message:
        notify_payload["message"] = explicit_message
    notify_payload.setdefault("source_event_type", str(event.get("event_type") or "").strip())
    notify_payload.setdefault("reaction_source", "workflow_automation")
    if route_keys:
        notify_payload.setdefault("route_keys", route_keys)

    default_event_type = (
        "approval.requested"
        if action_target == "extension_channel"
        and str(action_payload.get("channel_capability") or "").strip().lower() == "human_decision"
        else ""
    )
    event_type = (
        str(action_payload.get("event_type") or "").strip()
        or default_event_type
        or str(event.get("event_type") or "").strip()
        or "workflow.automation.notify"
    )
    severity = str(action_payload.get("severity") or "info").strip() or "info"

    try:
        from core.application.notifications.dispatch import dispatch_project_notification_event

        deliveries = dispatch_project_notification_event(
            bl,
            project_id=project_id,
            event_type=event_type,
            severity=severity,
            workflow_id=workflow_id,
            job_id=job_id,
            event_id=str(event.get("event_id") or "").strip() or None,
            account_id=str(event.get("account_id") or "").strip() or None,
            correlation_id=str(event.get("correlation_id") or "").strip() or None,
            payload=notify_payload,
            route_ids=route_ids or None,
            direct_routes=direct_routes,
            ignore_filters=bool(route_ids or direct_routes),
        )
    except ValueError as exc:
        return {"status": "failed", "reason": str(exc), "executed": False}
    except Exception as exc:
        logger.warning(
            "dispatch_notification_from_reaction failed for project_id=%s: %s",
            project_id,
            exc,
        )
        return {"status": "failed", "reason": str(exc), "executed": False}

    if not deliveries:
        return {
            "status": "ignored",
            "reason": "no notification deliveries were produced",
            "notification_deliveries": [],
            "executed": False,
        }
    return {
        "status": "completed",
        "notification_deliveries": deliveries,
        "executed": True,
    }


def apply_reaction_action(
    event: dict[str, Any],
    rule: dict[str, Any],
    *,
    to_json_obj,
    reaction_action_comment,
    reaction_action_result_executed,
    resume_job_from_reaction,
    cancel_job_from_reaction,
    retry_interrupted_step_from_reaction,
    dispatch_notification_from_reaction,
    scope=None,
) -> dict[str, Any]:
    action_payload = to_json_obj(rule.get("action")) or {}
    if not isinstance(action_payload, dict):
        action_payload = {}
    action_type = str(action_payload.get("type") or "").strip().lower()
    if action_type in {"", "noop", "none"}:
        return {"status": "skipped", "reason": "no action configured", "executed": False}

    if action_type in {"resume_job", "resume_orchestrator", "approve_job", "reject_job"}:
        job_id = event.get("job_id")
        if job_id is None:
            return {
                "status": "ignored",
                "reason": "job_id required for resume action",
                "executed": False,
            }
        comment = reaction_action_comment(event, action_payload)
        resume_action = str(action_payload.get("resume_action") or "").strip() or None
        approved = bool(action_payload.get("approved", True))
        if action_type == "reject_job":
            approved = False
        elif action_type == "approve_job":
            approved = True
        resume_kwargs = {
            "approved": approved,
            "action": resume_action,
            "expected_approval_id": str(
                (to_json_obj(event.get("payload")) or {}).get("approval_id") or ""
            ).strip(),
        }
        if scope is not None:
            resume_kwargs["scope"] = scope
            resume_kwargs["idempotency_key"] = str(
                event.get("correlation_id")
                or event.get("event_id")
                or event.get("dedupe_key")
                or ""
            ).strip()
        result = resume_job_from_reaction(str(job_id), comment, **resume_kwargs)
        result["executed"] = reaction_action_result_executed(result)
        return result

    if action_type == "cancel_job":
        job_id = event.get("job_id")
        if job_id is None:
            return {
                "status": "ignored",
                "reason": "job_id required for cancel action",
                "executed": False,
            }
        return cancel_job_from_reaction(str(job_id), reaction_action_comment(event, action_payload))

    if action_type == "retry_step":
        job_id = event.get("job_id")
        if job_id is None:
            return {
                "status": "ignored",
                "reason": "job_id required for retry_step action",
                "executed": False,
            }
        requested_by = (
            sanitize_external_input(str(action_payload.get("requested_by") or "automation").strip())
            or "automation"
        )
        return retry_interrupted_step_from_reaction(str(job_id), requested_by=requested_by)

    if action_type == "notify":
        return dispatch_notification_from_reaction(_rule_scoped_event(event, rule), action_payload)

    return {
        "status": "ignored",
        "reason": f"unsupported action type '{action_type}'",
        "executed": False,
    }


def _rule_scoped_event(event: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
    """The event as one rule delivers it: its own notification identity.

    The notification runtime claims each delivery by event id and route, so a
    rule-scoped id lets two rules that share a route both deliver while one
    rule never delivers the same event to the same route twice.
    """
    from core.application.workflows.workflow_reaction_events import rule_scoped_event_id

    event_id = str(event.get("event_id") or "").strip()
    reaction_key = str(rule.get("reaction_key") or "").strip()
    if not event_id or not reaction_key:
        return event
    return {**event, "event_id": rule_scoped_event_id(event_id, reaction_key)}


def evaluate_workflow_reactions(
    bl,
    event: dict[str, Any],
    *,
    to_json_obj,
    reaction_scope_matches_event,
    reaction_conditions_match,
    reaction_is_debounced,
    apply_reaction_action,
    reaction_action_result_executed,
    logger,
    scope=None,
) -> list[dict[str, Any]]:
    workflow_id = str(event.get("workflow_id") or "").strip()
    event_type = str(event.get("event_type") or "").strip()
    results: list[dict[str, Any]] = []
    if not event_type:
        return results

    matching_rules: list[dict[str, Any]] = []
    if workflow_id and hasattr(bl, "list_workflow_reactions"):
        try:
            rules = bl.list_workflow_reactions(workflow_id, enabled_only=True) or []
        except Exception as exc:
            logger.warning(
                "evaluate_workflow_reactions: list failed for workflow=%s: %s",
                workflow_id,
                exc,
            )
            return [{"reaction_key": "", "status": "failed", "reason": str(exc), "executed": False}]
        matching_rules = [
            dict(rule)
            for rule in rules
            if isinstance(rule, dict)
            and str(rule.get("event_type") or "").strip() == event_type
            and reaction_scope_matches_event(rule, event)
        ]

    if (
        not matching_rules
        and event_type in {"external.comment.received", "notification.interaction_received"}
        and event.get("job_id") is not None
    ):
        payload = to_json_obj(event.get("payload")) or {}
        if not isinstance(payload, dict):
            payload = {}
        action_hint = str(payload.get("action") or "").strip().lower()
        approved_hint = payload.get("approved")
        if action_hint in {"abort", "cancel", "cancelled"}:
            default_action_type = "reject_job"
            default_resume_action = "abort"
        elif action_hint in {"approve", "approved"}:
            default_action_type = "approve_job"
            default_resume_action = None
        elif action_hint in {"reject", "rejected"}:
            default_action_type = "reject_job"
            default_resume_action = None
        elif isinstance(approved_hint, bool):
            default_action_type = "approve_job" if approved_hint else "reject_job"
            default_resume_action = None
        else:
            default_action_type = "resume_job"
            default_resume_action = None
        default_action = {"type": default_action_type}
        if default_resume_action:
            default_action["resume_action"] = default_resume_action
        matching_rules = [
            {
                "reaction_key": "__default_external_comment_resume__",
                "event_type": event_type,
                "conditions": {},
                "action": default_action,
                "debounce_seconds": 0,
            }
        ]

    for rule in matching_rules:
        reaction_key = str(rule.get("reaction_key") or "").strip()
        if not reaction_conditions_match(event, to_json_obj(rule.get("conditions"))):
            results.append(
                {
                    "reaction_key": reaction_key,
                    "status": "skipped",
                    "reason": "conditions_not_met",
                    "executed": False,
                }
            )
            continue
        if reaction_is_debounced(bl, event, rule):
            results.append(
                {
                    "reaction_key": reaction_key,
                    "status": "skipped",
                    "reason": "debounced",
                    "executed": False,
                }
            )
            continue
        action_result = (
            apply_reaction_action(event, rule, scope=scope)
            if scope is not None
            else apply_reaction_action(event, rule)
        )
        action_result["reaction_key"] = reaction_key
        if "executed" not in action_result:
            action_result["executed"] = reaction_action_result_executed(action_result)
        results.append(action_result)
    return results
