"""Extension provider completion operations."""

from __future__ import annotations

from typing import Any

from core.application.extensions.provider_runtime import (
    _as_dict,
    _as_str,
    _default_account_id,
    _extension_row,
    _process_provider_project_state_updates,
    resolve_extension_runtime_config,
)


def _job_row_for_provider_sync(bl, job_id: Any) -> dict:
    if bl is None or not getattr(bl, "conn", None) or not job_id:
        return {}
    try:
        import json as _json

        cursor_kwargs: dict[str, Any] = {}
        try:
            from core.infrastructure.db.psycopg_runtime import extras as _pge

            cursor_kwargs["cursor_factory"] = _pge.RealDictCursor
        except Exception:
            cursor_kwargs = {}

        with bl.conn.cursor(**cursor_kwargs) as cur:
            cur.execute(
                "SELECT id, project_id, task_id, task_source, meta FROM jobs WHERE id = %s",
                (_as_str(job_id),),
            )
            row = cur.fetchone()
        if isinstance(row, dict):
            payload = dict(row)
        elif isinstance(row, (tuple, list)):
            cols = (
                [str(d[0]) for d in (cur.description or [])]
                if getattr(cur, "description", None)
                else []
            )
            payload = (
                {cols[idx]: row[idx] for idx in range(min(len(cols), len(row)))} if cols else {}
            )
        else:
            try:
                payload = dict(row or {})
            except Exception:
                payload = {}
        meta = payload.get("meta") or {}
        if isinstance(meta, str):
            try:
                meta = _json.loads(meta)
            except (TypeError, ValueError):
                meta = {}
        payload["meta"] = _as_dict(meta)
        return payload
    except Exception:
        return {}


def _job_provider_key(row: dict | None) -> str:
    payload = _as_dict(row)
    meta = _as_dict(payload.get("meta"))
    for key in ("external_provider_key", "provider_key"):
        explicit = str(meta.get(key) or "").strip().lower()
        if explicit:
            return explicit

    source_system = str(meta.get("source_system") or "").strip().lower()
    entry_path = str(meta.get("entry_path") or "").strip().lower()
    if source_system in {"manual", "api"} or entry_path in {"manual_ui", "manual_api"}:
        return ""

    task_source = str(payload.get("task_source") or "").strip().lower()
    non_provider_sources = {
        "manual",
        "manual_api",
        "manual_ui",
        "web",
        "ui",
        "chat",
        "api",
        "invoke_workflow",
        "schedule",
        "scheduled",
        "automation",
        "step_replay",
    }
    if not task_source or task_source in non_provider_sources:
        return ""
    if task_source in {"provider", "task_ingress", "webhook"}:
        if source_system and source_system not in {"provider", "task_ingress", "webhook"}:
            return source_system
        return ""
    for separator in (":", "/", "_"):
        if separator in task_source:
            prefix = task_source.split(separator, 1)[0].strip()
            if prefix:
                return prefix
    return task_source


def _job_trace_id(job_id: Any) -> str | None:
    normalized = _as_str(job_id)
    return f"job:{normalized}" if normalized else None


def _extension_action_response(
    extension_key: str,
    *,
    project_id: int | str | None,
    action_id: str,
    input_payload: dict[str, Any],
    request_context: dict[str, Any] | None = None,
    bl=None,
) -> dict | None:
    extension = _extension_row(bl=bl, extension_key=extension_key)
    if not extension:
        raise RuntimeError(f"Extension '{extension_key}' is not available")
    if not bool(extension.get("enabled", False)):
        raise RuntimeError(f"Extension '{extension_key}' is disabled")

    from core.infrastructure.extension_runtime.extension_runtime import (
        normalize_extension_row_for_runtime,
        run_extension_action,
    )

    normalized_extension = normalize_extension_row_for_runtime(extension)
    invocation = run_extension_action(
        normalized_extension,
        action_id=action_id,
        context={"project_id": _as_str(project_id)} if project_id else {},
        input_payload=_as_dict(input_payload),
        runtime_context={"logger": bl} if bl is not None else {},
    )
    context = _as_dict(request_context)
    _process_provider_project_state_updates(
        invocation,
        extension=normalized_extension,
        db=bl,
        account_id=(
            _as_str(context.get("account_id"))
            or _as_str(extension.get("account_id"))
            or _default_account_id()
        ),
        project_id=_as_str(project_id) or _as_str(context.get("project_id")),
        connection_id=_as_str(context.get("connection_id")) or None,
        actor_id=_as_str(context.get("actor_id")) or None,
    )
    if bl is not None and hasattr(bl, "log_extension_invocation"):
        bl.log_extension_invocation(
            extension_key=extension_key,
            invocation_type=f"action:{action_id}",
            status="ok" if invocation.get("status") == "succeeded" else "failed",
            request={"context": {"project_id": project_id}, "input": _as_dict(input_payload)},
            response=invocation,
            error_text=invocation.get("detail"),
            request_context=context,
            account_id=str(context.get("account_id") or "").strip() or None,
            job_id=context.get("job_id"),
            workflow_id=str(context.get("workflow_id") or "").strip() or None,
        )
    if invocation.get("status") != "succeeded":
        raise RuntimeError(
            str(
                invocation.get("detail")
                or f"Extension '{extension_key}' action '{action_id}' failed"
            )
        )
    response = _as_dict(invocation.get("response"))
    if not response:
        raise RuntimeError(
            f"Extension '{extension_key}' action '{action_id}' returned no response payload"
        )
    return response


def sync_provider_completion_status(job_id: Any, final_status: str, bl) -> None:
    from core.infrastructure.runtime.test_mode import is_test_mode_enabled

    if not bl or not job_id:
        return
    if is_test_mode_enabled():
        return

    row = _job_row_for_provider_sync(bl, job_id)
    if not row:
        return

    provider_key = _job_provider_key(row)
    if not provider_key:
        return

    meta = _as_dict(row.get("meta"))
    project_id = row.get("project_id")
    task_id = str(meta.get("external_source_id") or row.get("task_id") or "").strip()
    if not task_id:
        return

    run_cfg = {}
    if project_id and hasattr(bl, "get_project_run_config"):
        try:
            run_cfg = bl.get_project_run_config(_as_str(project_id)) or {}
        except Exception:
            run_cfg = {}

    resolved_config = resolve_extension_runtime_config(
        provider_key,
        run_cfg,
        bl=bl,
        project_id=_as_str(project_id) or None,
    )
    response = _extension_action_response(
        provider_key,
        project_id=_as_str(project_id) or None,
        action_id="sync_completion_status",
        input_payload={
            "task_id": task_id,
            "final_status": str(final_status or "").strip(),
            "provider_key": provider_key,
            "provider_config": _as_dict(resolved_config.get("config")),
        },
        request_context={
            "trace_id": _job_trace_id(job_id),
            "job_id": _as_str(job_id),
            "project_id": _as_str(project_id) or None,
            "correlation_id": f"job:{_as_str(job_id)}:provider.sync_completion_status",
        },
        bl=bl,
    )
    if not isinstance(response, dict) or not bool(response.get("ok")):
        error_text = ""
        if isinstance(response, dict):
            error_text = str(
                response.get("error")
                or response.get("message")
                or _as_dict(response.get("result")).get("error")
                or ""
            ).strip()
        raise RuntimeError(
            error_text
            or f"Extension '{provider_key}' rejected sync_completion_status for job {_as_str(job_id)}"
        )
