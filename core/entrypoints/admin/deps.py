"""Shared dependencies for authenticated HTTP route modules."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import Request

CORE_ENTRYPOINTS_ADMIN_DEPS_INIT_NOT_CALLED_VALUE = "core.entrypoints.admin.deps.init() not called"


logger = logging.getLogger(__name__)

# ── Wired at init-time by the HTTP app bootstrap ───────────────────────

_get_logger_only_fn: Callable | None = None
_get_platform_logger_fn: Callable | None = None
_enqueue_task_fn: Callable | None = None
_get_ready_queue_depth_fn: Callable | None = None
_get_queue_depth_fn: Callable | None = None
_get_checkpointer_fn: Callable | None = None
_get_thread_state_snapshot_fn: Callable | None = None
_resolve_artifact_fn: Callable | None = None
_artifact_plane: Any = None
_compute_readiness_fn: Callable | None = None
_watchdog_scan_fn: Callable | None = None
_auth_csrf_cookie_name: str = "fs_csrf"
_env_flag_fn: Callable | None = None
_api_error_response_fn: Callable | None = None
_to_json_obj_fn: Callable | None = None
_local_auth_enabled_fn: Callable | None = None
_external_auth_enabled_fn: Callable | None = None
_require_authenticated_user_fn: Callable | None = None
_optional_actor_username_fn: Callable | None = None
_resolve_actor_username_fn: Callable | None = None
_request_auth_token_fn: Callable | None = None
_request_client_ip_fn: Callable | None = None
_redirect_fn: Callable | None = None
_flash_fn: Callable | None = None


def init(
    *,
    get_logger_only: Callable,
    get_platform_logger: Callable,
    enqueue_task: Callable,
    get_ready_queue_depth: Callable,
    get_queue_depth: Callable,
    get_checkpointer: Callable,
    get_thread_state_snapshot: Callable | None = None,
    compute_readiness: Callable,
    watchdog_scan: Callable,
    auth_csrf_cookie_name: str,
    env_flag: Callable,
    api_error_response: Callable,
    to_json_obj: Callable,
    local_auth_enabled: Callable,
    external_auth_enabled: Callable,
    require_authenticated_user: Callable,
    optional_actor_username: Callable,
    resolve_actor_username: Callable,
    request_auth_token: Callable,
    request_client_ip: Callable,
    redirect: Callable,
    flash: Callable,
    resolve_artifact: Callable | None = None,
    artifact_plane: Any = None,
) -> None:
    """Wire up shared dependencies once during HTTP app bootstrap."""
    global _get_logger_only_fn, _get_platform_logger_fn
    global _enqueue_task_fn, _get_ready_queue_depth_fn, _get_queue_depth_fn
    global \
        _get_checkpointer_fn, \
        _get_thread_state_snapshot_fn, \
        _compute_readiness_fn, \
        _watchdog_scan_fn
    global _auth_csrf_cookie_name, _env_flag_fn, _api_error_response_fn
    global _to_json_obj_fn, _local_auth_enabled_fn, _external_auth_enabled_fn
    global _require_authenticated_user_fn, _optional_actor_username_fn
    global _resolve_actor_username_fn, _request_auth_token_fn, _request_client_ip_fn
    global _redirect_fn, _flash_fn
    global _resolve_artifact_fn, _artifact_plane

    _get_logger_only_fn = get_logger_only
    _get_platform_logger_fn = get_platform_logger
    _enqueue_task_fn = enqueue_task
    _get_ready_queue_depth_fn = get_ready_queue_depth
    _get_queue_depth_fn = get_queue_depth
    _get_checkpointer_fn = get_checkpointer
    _get_thread_state_snapshot_fn = get_thread_state_snapshot
    _compute_readiness_fn = compute_readiness
    _watchdog_scan_fn = watchdog_scan
    _auth_csrf_cookie_name = auth_csrf_cookie_name
    _env_flag_fn = env_flag
    _api_error_response_fn = api_error_response
    _to_json_obj_fn = to_json_obj
    _local_auth_enabled_fn = local_auth_enabled
    _external_auth_enabled_fn = external_auth_enabled
    _require_authenticated_user_fn = require_authenticated_user
    _optional_actor_username_fn = optional_actor_username
    _resolve_actor_username_fn = resolve_actor_username
    _request_auth_token_fn = request_auth_token
    _request_client_ip_fn = request_client_ip
    _redirect_fn = redirect
    _flash_fn = flash
    _resolve_artifact_fn = resolve_artifact
    _artifact_plane = artifact_plane


# ── Public accessor API (used by route modules) ────────────────────────


def get_logger_only():
    """Return the Flow Steward logger singleton or None."""
    assert _get_logger_only_fn is not None, CORE_ENTRYPOINTS_ADMIN_DEPS_INIT_NOT_CALLED_VALUE
    return _get_logger_only_fn()


def get_platform_logger(project_name: str = "default"):
    """Return (Flow Steward logger, project_id) or (None, None)."""
    assert _get_platform_logger_fn is not None, CORE_ENTRYPOINTS_ADMIN_DEPS_INIT_NOT_CALLED_VALUE
    return _get_platform_logger_fn(project_name)


def enqueue_task(**kwargs) -> int:
    assert _enqueue_task_fn is not None, CORE_ENTRYPOINTS_ADMIN_DEPS_INIT_NOT_CALLED_VALUE
    return _enqueue_task_fn(**kwargs)


def get_ready_queue_depth() -> int:
    assert _get_ready_queue_depth_fn is not None, CORE_ENTRYPOINTS_ADMIN_DEPS_INIT_NOT_CALLED_VALUE
    return _get_ready_queue_depth_fn()


def get_queue_depth(project_id: str) -> int:
    assert _get_queue_depth_fn is not None, CORE_ENTRYPOINTS_ADMIN_DEPS_INIT_NOT_CALLED_VALUE
    return _get_queue_depth_fn(project_id)


def get_checkpointer():
    assert _get_checkpointer_fn is not None, CORE_ENTRYPOINTS_ADMIN_DEPS_INIT_NOT_CALLED_VALUE
    return _get_checkpointer_fn()


def get_thread_state_snapshot(thread_id: str):
    """Return a read-only LangGraph checkpoint snapshot when wired by the app."""
    if _get_thread_state_snapshot_fn is None:
        return None
    return _get_thread_state_snapshot_fn(str(thread_id or ""))


def resolve_artifact(artifact_id: str) -> dict[str, Any] | None:
    if _resolve_artifact_fn is None:
        return None
    row = _resolve_artifact_fn(str(artifact_id or "").strip())
    return dict(row) if isinstance(row, dict) else None


def get_artifact_plane() -> Any:
    """The shared artifact storage, or ``None`` when the app did not wire it."""
    return _artifact_plane


def compute_readiness(bl) -> dict:
    assert _compute_readiness_fn is not None, CORE_ENTRYPOINTS_ADMIN_DEPS_INIT_NOT_CALLED_VALUE
    return _compute_readiness_fn(bl)


def watchdog_scan(bl, *, apply: bool = False) -> dict:
    assert _watchdog_scan_fn is not None, CORE_ENTRYPOINTS_ADMIN_DEPS_INIT_NOT_CALLED_VALUE
    return _watchdog_scan_fn(bl, apply=apply)


def env_flag(name: str, default: bool) -> bool:
    assert _env_flag_fn is not None, CORE_ENTRYPOINTS_ADMIN_DEPS_INIT_NOT_CALLED_VALUE
    return _env_flag_fn(name, default)


def api_error_response(
    exc: Exception, *, status_code: int = 500, fallback_message: str = "Internal server error"
):
    assert _api_error_response_fn is not None, CORE_ENTRYPOINTS_ADMIN_DEPS_INIT_NOT_CALLED_VALUE
    return _api_error_response_fn(exc, status_code=status_code, fallback_message=fallback_message)


def to_json_obj(value: Any):
    assert _to_json_obj_fn is not None, CORE_ENTRYPOINTS_ADMIN_DEPS_INIT_NOT_CALLED_VALUE
    return _to_json_obj_fn(value)


def local_auth_enabled() -> bool:
    assert _local_auth_enabled_fn is not None, CORE_ENTRYPOINTS_ADMIN_DEPS_INIT_NOT_CALLED_VALUE
    return _local_auth_enabled_fn()


def external_auth_enabled() -> bool:
    return bool(_external_auth_enabled_fn and _external_auth_enabled_fn())


def require_authenticated_user(request: Request, *, required_roles: set[str] | None = None) -> dict:
    assert _require_authenticated_user_fn is not None, (
        CORE_ENTRYPOINTS_ADMIN_DEPS_INIT_NOT_CALLED_VALUE
    )
    return _require_authenticated_user_fn(request, required_roles=required_roles)


def optional_actor_username(request: Request, default: str = "operator") -> str:
    assert _optional_actor_username_fn is not None, (
        CORE_ENTRYPOINTS_ADMIN_DEPS_INIT_NOT_CALLED_VALUE
    )
    return _optional_actor_username_fn(request, default)


def resolve_actor_username(
    request: Request, *, default: str = "operator", preferred_username: str = ""
) -> str:
    assert _resolve_actor_username_fn is not None, CORE_ENTRYPOINTS_ADMIN_DEPS_INIT_NOT_CALLED_VALUE
    return _resolve_actor_username_fn(
        request, default=default, preferred_username=preferred_username
    )


def request_auth_token(request: Request) -> str:
    assert _request_auth_token_fn is not None, CORE_ENTRYPOINTS_ADMIN_DEPS_INIT_NOT_CALLED_VALUE
    return _request_auth_token_fn(request)


def request_client_ip(request: Request) -> str:
    assert _request_client_ip_fn is not None, CORE_ENTRYPOINTS_ADMIN_DEPS_INIT_NOT_CALLED_VALUE
    return _request_client_ip_fn(request)


def redirect(url: str, flash: str | None = None, flash_type: str = "info"):
    assert _redirect_fn is not None, CORE_ENTRYPOINTS_ADMIN_DEPS_INIT_NOT_CALLED_VALUE
    return _redirect_fn(url, flash, flash_type)


def flash(request: Request, message: str, flash_type: str = "info"):
    assert _flash_fn is not None, CORE_ENTRYPOINTS_ADMIN_DEPS_INIT_NOT_CALLED_VALUE
    return _flash_fn(request, message, flash_type)
