# ruff: noqa: F405, RUF100
# mypy: ignore-errors

from core.application.auth import request_principal_runtime as _request_principal_runtime
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

from .http_app_lifecycle import *  # noqa: F403,F401  # NOSONAR


def _auth_logger_only():
    from .http_app_runtime_helpers import _get_identity_store

    return _get_identity_store()


def _get_checkpointer():
    return _state_reader_runtime.get_checkpointer(config=CONFIG, logger=logger)


def _reset_state_reader_graph() -> None:
    return _state_reader_runtime.reset_cached_graph(
        state_lock=_state_reader_graph_lock,
        set_cached_graph=_set_state_reader_graph_cache,
    )


def _get_state_reader_graph_cache() -> tuple[Any, str]:
    return _state_reader_graph, _state_reader_graph_dsn


def _set_state_reader_graph_cache(graph: Any, dsn: str) -> None:
    global _state_reader_graph, _state_reader_graph_dsn
    _state_reader_graph = graph
    _state_reader_graph_dsn = dsn


def _get_state_reader_graph(*, force_refresh: bool = False):
    return _state_reader_runtime.get_state_reader_graph(
        config=CONFIG,
        force_refresh=force_refresh,
        state_lock=_state_reader_graph_lock,
        get_cached_graph=_get_state_reader_graph_cache,
        set_cached_graph=_set_state_reader_graph_cache,
        get_checkpointer=_get_checkpointer,
        build_graph=build_registry_graph,
    )


def _get_thread_state_snapshot(thread_id: str):
    return _state_reader_runtime.get_thread_state_snapshot(
        thread_id,
        get_state_reader_graph=_get_state_reader_graph,
        logger=logger,
    )


def _prewarm_state_reader_graph() -> None:
    return _state_reader_runtime.prewarm_state_reader_graph(
        config=CONFIG,
        get_state_reader_graph=_get_state_reader_graph,
        logger=logger,
    )


def _to_json_obj(value):
    return _serve_request_runtime.to_json_obj(value)


def _local_auth_enabled() -> bool:
    return _env_flag("FS_LOCAL_AUTH_ENABLED", True)


def _password_reset_enabled() -> bool:
    return _env_flag("FS_AUTH_PASSWORD_RESET_ENABLED", True)


def _password_reset_debug_reveal_token() -> bool:
    if not _env_flag("FS_AUTH_PASSWORD_RESET_DEBUG_REVEAL_TOKEN", False):
        return False
    environment_markers = (
        _runtime_env.get("FS_STACK_ENVIRONMENT"),
        _runtime_env.get("ENVIRONMENT"),
        _runtime_env.get("APP_ENV"),
    )
    normalized_values = {
        str(value or "").strip().lower().replace("-", "_")
        for value in environment_markers
        if str(value or "").strip()
    }
    return not normalized_values.intersection({"production", "prod"})


def _build_password_reset_url(request: Request, reset_token: str) -> str:
    return _serve_auth_runtime.build_password_reset_url(
        request,
        reset_token,
        public_base_url_fn=_public_base_url,
    )


def _dispatch_password_reset_email(request, user, reset_token):
    return _serve_auth_runtime.dispatch_password_reset_email(
        request,
        user,
        reset_token,
        send_password_reset_email_fn=send_password_reset_email,
        build_password_reset_url_fn=lambda current_request, current_token: (
            _build_password_reset_url(
                current_request,
                current_token,
            )
        ),
        auth_password_reset_ttl_minutes=_AUTH_PASSWORD_RESET_TTL_MINUTES,
        logger=logger,
    )


_password_hash = _password_hash_impl


def _verify_password(password: str, encoded_hash: str) -> bool:
    return _serve_auth_runtime.verify_password(password, encoded_hash)


_session_token_hash = _session_token_hash_impl


_auth_tables_available = _serve_auth_runtime.build_tables_available_checker(
    {"users", "roles", "user_roles", "auth_sessions"}
)


def _local_auth_bootstrap_required(bl=None, *, get_logger_only_fn) -> bool:
    return _serve_auth_runtime.local_auth_bootstrap_required(
        bl,
        local_auth_enabled_fn=_local_auth_enabled,
        env_flag=_env_flag,
        get_logger_only_fn=get_logger_only_fn,
        auth_tables_available_fn=_auth_tables_available,
    )


_auth_password_reset_tables_available = _serve_auth_runtime.build_tables_available_checker(
    {"users", "auth_password_resets"}
)
_auth_pat_tables_available = _serve_auth_runtime.build_tables_available_checker(
    {"users", "personal_access_tokens"}
)
_auth_account_tables_available = _serve_auth_runtime.build_tables_available_checker({"accounts"})
_auth_service_account_tables_available = _serve_auth_runtime.build_tables_available_checker(
    {"service_accounts"}
)
_auth_machine_token_tables_available = _serve_auth_runtime.build_tables_available_checker(
    {"service_accounts", "machine_tokens"}
)


_auth_user_column_exists = _serve_auth_runtime.auth_user_column_exists


def _auth_user_must_change_password_select(bl, alias="u"):
    return _serve_auth_runtime.auth_user_must_change_password_select(
        bl,
        alias=alias,
        auth_user_column_exists_fn=lambda current_bl, column_name: _auth_user_column_exists(
            current_bl,
            column_name,
        ),
    )


def _auth_user_account_id_select(bl, alias="u"):
    return _serve_auth_runtime.auth_user_account_id_select(
        bl,
        alias=alias,
        auth_user_column_exists_fn=lambda current_bl, column_name: _auth_user_column_exists(
            current_bl,
            column_name,
        ),
    )


def _auth_user_deleted_condition(bl, alias="u"):
    return _serve_auth_runtime.auth_user_deleted_condition(
        bl,
        alias=alias,
        auth_user_column_exists_fn=lambda current_bl, column_name: _auth_user_column_exists(
            current_bl,
            column_name,
        ),
    )


_auth_identity_tables_available = _serve_auth_runtime.auth_identity_tables_available


_seed_default_roles = _serve_auth_runtime.seed_default_roles


_get_user_roles = _serve_auth_runtime.get_user_roles


_real_dict_cursor_kwargs = _serve_auth_runtime.real_dict_cursor_kwargs


_get_user_by_username = lambda bl, username: _serve_auth_runtime.get_user_by_username(  # noqa: E731
    bl,
    username,
    auth_user_must_change_password_select_fn=lambda current_bl, alias="u": (
        _auth_user_must_change_password_select(
            current_bl,
            alias=alias,
        )
    ),
    auth_user_account_id_select_fn=lambda current_bl, alias="u": _auth_user_account_id_select(
        current_bl,
        alias=alias,
    ),
    auth_user_deleted_condition_fn=lambda current_bl, alias="u": _auth_user_deleted_condition(
        current_bl,
        alias=alias,
    ),
    real_dict_cursor_kwargs_fn=lambda: _real_dict_cursor_kwargs(),
    get_user_roles_fn=lambda current_bl, user_id: _get_user_roles(current_bl, user_id),
    sql_from_users=SQL_FROM_USERS,
    sql_limit_1=SQL_LIMIT_1,
)


_get_user_by_id = lambda bl, user_id: _serve_auth_runtime.get_user_by_id(  # noqa: E731
    bl,
    user_id,
    auth_user_must_change_password_select_fn=lambda current_bl, alias="u": (
        _auth_user_must_change_password_select(
            current_bl,
            alias=alias,
        )
    ),
    auth_user_account_id_select_fn=lambda current_bl, alias="u": _auth_user_account_id_select(
        current_bl,
        alias=alias,
    ),
    auth_user_deleted_condition_fn=lambda current_bl, alias="u": _auth_user_deleted_condition(
        current_bl,
        alias=alias,
    ),
    real_dict_cursor_kwargs_fn=lambda: _real_dict_cursor_kwargs(),
    get_user_roles_fn=lambda current_bl, current_user_id: _get_user_roles(
        current_bl,
        current_user_id,
    ),
    sql_from_users=SQL_FROM_USERS,
    sql_limit_1=SQL_LIMIT_1,
)


_get_user_by_session_token = lambda bl, token: _serve_auth_runtime.get_user_by_session_token(  # noqa: E731
    bl,
    token,
    session_token_hash_fn=lambda current_token: _session_token_hash(current_token),
    auth_user_must_change_password_select_fn=lambda current_bl, alias="u": (
        _auth_user_must_change_password_select(
            current_bl,
            alias=alias,
        )
    ),
    auth_user_deleted_condition_fn=lambda current_bl, alias="u": _auth_user_deleted_condition(
        current_bl,
        alias=alias,
    ),
    real_dict_cursor_kwargs_fn=lambda: _real_dict_cursor_kwargs(),
    get_user_roles_fn=lambda current_bl, user_id: _get_user_roles(current_bl, user_id),
    sql_user_username_column=SQL_USER_USERNAME_COLUMN,
    sql_limit_1=SQL_LIMIT_1,
)


def _roles_intersect(actual: list[str], required: set[str]) -> bool:
    return _request_principal_runtime.roles_intersect(actual, required)


_is_admin_only_route = _serve_request_runtime.build_path_pattern_matcher(
    patterns=_AUTH_ADMIN_ONLY_PATTERNS,
)


_is_registry_read_route = _serve_request_runtime.build_path_pattern_matcher(
    patterns=_AUTH_REGISTRY_READ_PATTERNS,
)


def _normalize_live_principal(raw_user, default_auth_source):
    return _request_principal_runtime.normalize_live_principal(
        raw_user,
        default_auth_source=default_auth_source,
        normalize_principal_fn=normalize_principal,
        resolved_principal_type=ResolvedPrincipal,
    )


_request_auth_user_payload = lambda request: _request_principal_runtime.request_auth_user_payload(  # noqa: E731
    request,
    resolved_principal_type=ResolvedPrincipal,
)


_request_principal = lambda request: _request_principal_runtime.request_principal(  # noqa: E731
    request,
    normalize_principal_fn=normalize_principal,
    resolved_principal_type=ResolvedPrincipal,
)


def _require_authenticated_user(request, required_roles=None):
    return _request_principal_runtime.require_authenticated_user(
        request,
        required_roles=required_roles,
        request_auth_user_payload_fn=lambda current_request: _request_auth_user_payload(
            current_request
        ),
        roles_intersect_fn=_roles_intersect,
        err_authentication_required=ERR_AUTHENTICATION_REQUIRED,
    )


def _optional_actor_username(request, default="operator"):
    return _request_principal_runtime.optional_actor_username(
        request,
        default=default,
        request_auth_user_payload_fn=lambda current_request: _request_auth_user_payload(
            current_request
        ),
    )


def _resolve_actor_username(request, explicit_actor, default="operator"):
    return _request_principal_runtime.resolve_actor_username(
        request,
        explicit_actor,
        default=default,
        request_auth_user_payload_fn=lambda current_request: _request_auth_user_payload(
            current_request
        ),
        optional_actor_username_fn=lambda current_request, default="operator": (
            _optional_actor_username(
                current_request,
                default=default,
            )
        ),
    )


_request_scope = _request_principal_runtime.build_request_scope_resolver(
    request_auth_user_payload_fn=lambda request: _request_auth_user_payload(request),
    optional_actor_username_fn=lambda request, default="operator": _optional_actor_username(
        request,
        default=default,
    ),
    scope_from_principal_fn=lambda principal, **kwargs: scope_from_principal(principal, **kwargs),
)


_request_auth_token = lambda request: _request_principal_runtime.request_auth_token(  # noqa: E731
    request,
    auth_cookie_name=_AUTH_COOKIE_NAME,
)


_request_client_ip = _serve_auth_runtime.request_client_ip


def _auth_login_lock_state(username, client_ip):
    return _auth_login_lock_state_impl(username, client_ip, bl=_auth_logger_only())


def _auth_login_record_failure(username, client_ip):
    return _auth_login_record_failure_impl(username, client_ip, bl=_auth_logger_only())


def _auth_login_clear_failures(username, client_ip):
    return _auth_login_clear_failures_impl(username, client_ip, bl=_auth_logger_only())


def _auth_login_clear_user_failures(username, *, bl=None):
    logger = bl if bl is not None else _auth_logger_only()
    return _auth_login_clear_user_failures_impl(username, bl=logger)


_password_reset_throttle_key = _password_reset_throttle_key_impl


def _password_reset_throttle_check(client_ip, action="request", subject=""):
    return _password_reset_throttle_check_impl(
        client_ip,
        action=action,
        subject=subject,
        attempts_store=_password_reset_attempts,
        attempts_lock=_password_reset_lock,
        rate_window_seconds=_PASSWORD_RESET_RATE_WINDOW_SECONDS,
        max_attempts=_PASSWORD_RESET_MAX_ATTEMPTS,
        lockout_seconds=_PASSWORD_RESET_LOCKOUT_SECONDS,
    )


def _password_reset_throttle_record(client_ip, action="request", subject=""):
    return _password_reset_throttle_record_impl(
        client_ip,
        action=action,
        subject=subject,
        attempts_store=_password_reset_attempts,
        attempts_lock=_password_reset_lock,
        rate_window_seconds=_PASSWORD_RESET_RATE_WINDOW_SECONDS,
        max_attempts=_PASSWORD_RESET_MAX_ATTEMPTS,
        lockout_seconds=_PASSWORD_RESET_LOCKOUT_SECONDS,
    )


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
