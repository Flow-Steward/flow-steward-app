"""Auth runtime helpers extracted from serve.py."""

from __future__ import annotations

import hmac
import ipaddress
import re
import socket
from collections.abc import Collection
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import psycopg

from core.application.auth.account_memberships import enrich_user_payload_with_account_memberships
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

_AUTH_SESSION_ISSUE_GUARDS: list[Any] = []


def register_auth_session_issue_guard(callback: Any) -> None:
    if callable(callback) and callback not in _AUTH_SESSION_ISSUE_GUARDS:
        _AUTH_SESSION_ISSUE_GUARDS.append(callback)


def auth_session_issue_guard_response(
    *,
    bl: Any,
    user: dict[str, Any],
    request: Any,
    selected_account_id: str = "",
    auth_source: str = "local",
) -> Any | None:
    for callback in _AUTH_SESSION_ISSUE_GUARDS.copy():
        response = callback(
            bl=bl,
            user=user,
            request=request,
            selected_account_id=selected_account_id,
            auth_source=auth_source,
        )
        if response is not None:
            return response
    return None


def tables_available(bl: Any, required_tables: Collection[str]) -> bool:
    if not bl:
        return False
    try:
        return all(bool(bl._table_exists(str(table_name))) for table_name in required_tables)
    except Exception:
        return False


def build_tables_available_checker(required_tables: Collection[str]):
    normalized = tuple(
        table_name
        for table_name in (str(item or "").strip() for item in required_tables)
        if table_name
    )

    def _checker(bl: Any) -> bool:
        return tables_available(bl, normalized)

    return _checker


def auth_user_column_exists(bl: Any, column_name: str) -> bool:
    if not bl:
        return False
    try:
        return bool(bl._column_exists("users", str(column_name or "").strip()))
    except Exception:
        return False


def auth_user_must_change_password_select(
    bl: Any,
    alias: str = "u",
    *,
    auth_user_column_exists_fn=auth_user_column_exists,
) -> str:
    if auth_user_column_exists_fn(bl, "must_change_password"):
        return f"{alias}.must_change_password AS must_change_password"
    return "false AS must_change_password"


def auth_user_account_id_select(
    bl: Any,
    alias: str = "u",
    *,
    auth_user_column_exists_fn=auth_user_column_exists,
) -> str:
    """SELECT fragment for account_id — NULL when column absent (fail-closed)."""
    if auth_user_column_exists_fn(bl, "account_id"):
        return f"{alias}.account_id AS account_id"
    return "NULL AS account_id"


def auth_user_locale_select(
    bl: Any,
    alias: str = "u",
    *,
    auth_user_column_exists_fn=auth_user_column_exists,
) -> str:
    if auth_user_column_exists_fn(bl, "locale"):
        return f"{alias}.locale AS locale"
    return "NULL AS locale"


def auth_session_column_exists(bl: Any, column_name: str) -> bool:
    if not bl:
        return False
    try:
        return bool(bl._column_exists("auth_sessions", str(column_name or "").strip()))
    except Exception:
        return False


def _execute_with_prepare_retry(cur: Any, query: Any, params: tuple[Any, ...]) -> None:
    """Retry once without server-side prepare when psycopg statement names collide."""
    try:
        cur.execute(query, params)
    except psycopg.errors.DuplicatePreparedStatement:
        cur.execute(query, params, prepare=False)


def auth_session_account_id_select(
    bl: Any,
    *,
    session_alias: str = "s",
    user_alias: str = "u",
    auth_user_column_exists_fn=auth_user_column_exists,
) -> str:
    if auth_session_column_exists(bl, "account_id"):
        if auth_user_column_exists_fn(bl, "account_id"):
            return (
                f"COALESCE(NULLIF({session_alias}.account_id::text, ''), "
                f"{user_alias}.account_id::text) AS account_id"
            )
        return f"{session_alias}.account_id AS account_id"
    return auth_user_account_id_select(
        bl,
        alias=user_alias,
        auth_user_column_exists_fn=auth_user_column_exists_fn,
    )


def auth_user_deleted_condition(
    bl: Any,
    alias: str = "u",
    *,
    auth_user_column_exists_fn=auth_user_column_exists,
) -> str:
    if auth_user_column_exists_fn(bl, "deleted_at"):
        return f" AND {alias}.deleted_at IS NULL"
    return ""


def auth_identity_tables_available(bl: Any) -> bool:
    required = {"users", "roles", "user_roles"}
    if not bl:
        return False
    try:
        return all(bool(bl._table_exists(table_name)) for table_name in required)
    except Exception:
        return False


def seed_default_roles(bl: Any) -> None:
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO roles (key, label)
            VALUES
                ('admin', 'Admin'),
                ('operator', 'Operator'),
                ('viewer', 'Viewer')
            ON CONFLICT (key) DO UPDATE
            SET label = EXCLUDED.label
            """
        )


def get_user_roles(bl: Any, user_id: Any) -> list[str]:
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        return []
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.key
            FROM user_roles ur
            JOIN roles r ON r.id = ur.role_id
            WHERE ur.user_id = %s
            ORDER BY r.key
            """,
            (normalized_user_id,),
        )
        rows = cur.fetchall()
    return [str(row[0]) for row in rows if row and row[0]]


def real_dict_cursor_kwargs() -> dict[str, Any]:
    try:
        from core.infrastructure.db.psycopg_runtime import extras as _pge2

        return {"cursor_factory": _pge2.RealDictCursor}
    except Exception:
        return {}


def _load_user_payload(
    bl: Any,
    query: str,
    params: tuple[Any, ...],
    *,
    real_dict_cursor_kwargs_fn=real_dict_cursor_kwargs,
    get_user_roles_fn=get_user_roles,
) -> dict[str, Any] | None:
    with bl.conn.cursor(**real_dict_cursor_kwargs_fn()) as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    if not row:
        return None
    payload = dict(row)
    payload["roles"] = get_user_roles_fn(bl, str(payload["id"]))
    return enrich_user_payload_with_account_memberships(bl, payload)


def get_user_by_username(
    bl: Any,
    username: str,
    *,
    auth_user_must_change_password_select_fn=auth_user_must_change_password_select,
    auth_user_account_id_select_fn=auth_user_account_id_select,
    auth_user_locale_select_fn=auth_user_locale_select,
    auth_user_deleted_condition_fn=auth_user_deleted_condition,
    real_dict_cursor_kwargs_fn=real_dict_cursor_kwargs,
    get_user_roles_fn=get_user_roles,
    sql_from_users: str,
    sql_limit_1: str,
) -> dict[str, Any] | None:
    query = "\n".join(
        [
            "SELECT u.id, u.username, u.display_name, u.password_hash, u.is_active, u.created_at, u.updated_at",
            ", ",
            auth_user_must_change_password_select_fn(bl, alias="u"),
            ", ",
            auth_user_locale_select_fn(bl, alias="u"),
            ", ",
            auth_user_account_id_select_fn(bl, alias="u"),
            sql_from_users,
            "WHERE lower(u.username) = lower(%s)",
            auth_user_deleted_condition_fn(bl, alias="u"),
            sql_limit_1,
        ]
    )
    return _load_user_payload(
        bl,
        query,
        (str(username or "").strip(),),
        real_dict_cursor_kwargs_fn=real_dict_cursor_kwargs_fn,
        get_user_roles_fn=get_user_roles_fn,
    )


def get_user_by_id(
    bl: Any,
    user_id: Any,
    *,
    auth_user_must_change_password_select_fn=auth_user_must_change_password_select,
    auth_user_account_id_select_fn=auth_user_account_id_select,
    auth_user_locale_select_fn=auth_user_locale_select,
    auth_user_deleted_condition_fn=auth_user_deleted_condition,
    real_dict_cursor_kwargs_fn=real_dict_cursor_kwargs,
    get_user_roles_fn=get_user_roles,
    sql_from_users: str,
    sql_limit_1: str,
) -> dict[str, Any] | None:
    query = "\n".join(
        [
            "SELECT u.id, u.username, u.display_name, u.password_hash, u.is_active, u.created_at, u.updated_at",
            ", ",
            auth_user_must_change_password_select_fn(bl, alias="u"),
            ", ",
            auth_user_locale_select_fn(bl, alias="u"),
            ", ",
            auth_user_account_id_select_fn(bl, alias="u"),
            sql_from_users,
            "WHERE u.id = %s",
            auth_user_deleted_condition_fn(bl, alias="u"),
            sql_limit_1,
        ]
    )
    return _load_user_payload(
        bl,
        query,
        (str(user_id or "").strip(),),
        real_dict_cursor_kwargs_fn=real_dict_cursor_kwargs_fn,
        get_user_roles_fn=get_user_roles_fn,
    )


def get_user_by_session_token(
    bl: Any,
    token: str,
    *,
    session_token_hash_fn,
    auth_user_must_change_password_select_fn=auth_user_must_change_password_select,
    auth_user_locale_select_fn=auth_user_locale_select,
    _auth_user_account_id_select_fn=auth_user_account_id_select,
    auth_user_deleted_condition_fn=auth_user_deleted_condition,
    real_dict_cursor_kwargs_fn=real_dict_cursor_kwargs,
    get_user_roles_fn=get_user_roles,
    sql_user_username_column: str,
    sql_limit_1: str,
) -> dict[str, Any] | None:
    token_hash = session_token_hash_fn(token)
    account_select = auth_session_account_id_select(
        bl,
        session_alias="s",
        user_alias="u",
    )
    query = "\n".join(
        [
            "SELECT",
            "    u.id,",
            sql_user_username_column,
            "    u.display_name,",
            "    u.is_active,",
            "    ",
            auth_user_must_change_password_select_fn(bl, alias="u"),
            ",",
            "    ",
            auth_user_locale_select_fn(bl, alias="u"),
            ",",
            "    ",
            account_select,
            ",",
            "    s.id AS session_id,",
            "    s.expires_at",
            "FROM auth_sessions s",
            "JOIN users u ON u.id = s.user_id",
            "WHERE s.session_token_hash = %s",
            "  AND s.revoked = false",
            "  AND s.expires_at > now()",
            auth_user_deleted_condition_fn(bl, alias="u"),
            sql_limit_1,
        ]
    )
    with bl.conn.cursor(**real_dict_cursor_kwargs_fn()) as cur:
        _execute_with_prepare_retry(cur, query, (token_hash,))
        row = cur.fetchone()
        if not row:
            return None
        _execute_with_prepare_retry(
            cur,
            "UPDATE auth_sessions SET last_seen_at = now() WHERE session_token_hash = %s",
            (token_hash,),
        )
    payload = dict(row)
    if not str(payload.get("account_id") or "").strip():
        return None
    payload["roles"] = get_user_roles_fn(bl, str(payload["id"]))
    return enrich_user_payload_with_account_memberships(bl, payload)


def build_password_reset_url(request, reset_token: str, *, public_base_url_fn) -> str:
    _ = request
    base_url = str(public_base_url_fn() or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("FS_PUBLIC_BASE_URL or FS_AUTH_TRUSTED_ORIGINS is required")
    return f"{base_url}/reset-password?token={quote(str(reset_token or ''))}"


def dispatch_password_reset_email(
    request,
    user: dict[str, Any] | None,
    reset_token: str,
    *,
    send_password_reset_email_fn,
    build_password_reset_url_fn,
    auth_password_reset_ttl_minutes: int,
    logger,
) -> dict[str, Any]:
    from core.application.settings.ui_i18n import resolve_ui_locale

    username = str((user or {}).get("username") or "").strip()
    locale = resolve_ui_locale(request, auth_user=user or {})
    try:
        reset_url = build_password_reset_url_fn(request, reset_token)
        result = send_password_reset_email_fn(
            recipient=username,
            reset_url=reset_url,
            expires_in_minutes=int(auth_password_reset_ttl_minutes),
            username=username,
            locale=locale,
        )
    except Exception as exc:
        logger.warning("Reset email dispatch failed: %s", exc)
        return {"ok": False, "reason": "dispatch_exception"}
    if not bool(result.get("ok")):
        reason = str(result.get("reason") or "").strip().lower() or "send_failed"
        if reason not in {"disabled_or_unconfigured", "invalid_recipient"}:
            logger.warning("Reset email send did not succeed: %s", reason)
    return result


def verify_password(password: str, encoded_hash: str) -> bool:
    from core.application.auth.password import verify_password as _verify_password_impl

    return _verify_password_impl(password, encoded_hash)


def log_auth_audit_event(
    bl,
    *,
    action: str,
    username: str = "",
    actor: str = "",
    status: str = "ok",
    detail: dict | None = None,
    to_json_obj,
) -> None:
    from core.application.auth.audit import log_auth_audit_event as _log_auth_audit_event_impl

    return _log_auth_audit_event_impl(
        bl,
        action=action,
        username=username,
        actor=actor,
        status=status,
        detail=to_json_obj(detail) or {},
    )


def build_auth_audit_logger(*, to_json_obj):
    def _log(
        bl,
        *,
        action: str,
        username: str = "",
        actor: str = "",
        status: str = "ok",
        detail: dict | None = None,
    ) -> None:
        return log_auth_audit_event(
            bl,
            action=action,
            username=username,
            actor=actor,
            status=status,
            detail=detail,
            to_json_obj=to_json_obj,
        )

    return _log


def public_base_url() -> str:
    """Return the canonical public base URL for external callback construction."""
    url = str(_runtime_env.get("FS_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if url:
        return url
    configured = str(_runtime_env.get("FS_AUTH_TRUSTED_ORIGINS", "") or "").strip()
    for item in configured.split(","):
        origin = item.strip().rstrip("/")
        if origin:
            return origin
    return ""


def trusted_auth_origins(*, public_base_url_fn=public_base_url) -> set[str]:
    configured = str(_runtime_env.get("FS_AUTH_TRUSTED_ORIGINS", "") or "").strip()
    origins: set[str] = set()
    if configured:
        origins = {
            item.strip().rstrip("/").lower() for item in configured.split(",") if item.strip()
        }
    public_url = public_base_url_fn()
    if public_url:
        origins.add(public_url.lower())
    return origins


def origin_host(origin: str) -> str:
    text = str(origin or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except Exception:
        return ""
    scheme = str(parsed.scheme or "").strip().lower()
    netloc = str(parsed.netloc or "").strip().lower()
    if not scheme or not netloc:
        return ""
    return f"{scheme}://{netloc}"


def expected_request_origin(request, *, auth_cookie_secure_fn) -> str:
    host = str(request.headers.get("host") or "").strip().lower()
    if not host:
        return ""
    origin_hint = str(request.headers.get("origin") or "").strip().lower()
    try:
        parsed_origin = urlparse(origin_hint)
    except Exception:
        parsed_origin = None
    origin_scheme = str(getattr(parsed_origin, "scheme", "") or "").strip().lower()
    origin_netloc = str(getattr(parsed_origin, "netloc", "") or "").strip().lower()
    if origin_netloc and origin_scheme in {"https", "http"}:
        scheme = origin_scheme
    else:
        scheme = "https" if auth_cookie_secure_fn() else "http"
    return "://".join((scheme, host))


def same_origin_request(
    request,
    *,
    trusted_auth_origins_fn,
    expected_request_origin_fn,
    origin_host_fn=origin_host,
) -> bool:
    trusted = trusted_auth_origins_fn()
    expected = expected_request_origin_fn(request)
    if expected:
        trusted.add(expected)
    origin = origin_host_fn(request.headers.get("origin") or "")
    if origin:
        return origin in trusted
    referer = str(request.headers.get("referer") or "").strip()
    referer_origin = origin_host_fn(referer)
    if referer_origin:
        return referer_origin in trusted
    return False


def local_auth_bootstrap_required(
    bl=None,
    *,
    local_auth_enabled_fn,
    env_flag,
    get_logger_only_fn,
    auth_tables_available_fn,
) -> bool:
    """Return True when local-auth is enabled and no users exist yet."""
    if not local_auth_enabled_fn():
        return False
    if not env_flag("FS_AUTH_BOOTSTRAP_ENABLED", True):
        return False
    logger_ref = bl if bl is not None else get_logger_only_fn()
    if not logger_ref or not auth_tables_available_fn(logger_ref):
        return False
    try:
        with logger_ref.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            row = cur.fetchone()
            return int((row[0] if row else 0) or 0) == 0
    except Exception:
        return False


def csrf_request_valid(
    request,
    *,
    csrf_header_name: str,
    csrf_cookie_name: str,
    same_origin_request_fn,
) -> bool:
    header_value = str(request.headers.get(csrf_header_name) or "").strip()
    cookie_value = str(request.cookies.get(csrf_cookie_name) or "").strip()
    if header_value and cookie_value and hmac.compare_digest(header_value, cookie_value):
        return True
    return same_origin_request_fn(request)


def _normalized_hostname(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"//{text}")
    return str(parsed.hostname or "").strip().lower()


def _is_loopback_hostname(value: str) -> bool:
    hostname = _normalized_hostname(value)
    if not hostname:
        return False
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _default_gateway_host() -> str:
    try:
        with open("/proc/net/route", encoding="utf-8") as route_file:
            next(route_file, None)
            for raw_line in route_file:
                fields = raw_line.strip().split()
                if len(fields) < 3 or fields[1] != "00000000":
                    continue
                gateway_hex = fields[2]
                gateway_bytes = bytes.fromhex(gateway_hex)
                return str(ipaddress.ip_address(int.from_bytes(gateway_bytes, "little")))
    except (OSError, ValueError):
        return ""
    return ""


@lru_cache(maxsize=1)
def trusted_local_proxy_hosts() -> set[str]:
    hosts: set[str] = set()
    for host_name in ("host.docker.internal", "gateway.docker.internal", "traefik"):
        try:
            resolved = socket.gethostbyname(host_name)
        except OSError:
            continue
        normalized = _normalized_hostname(resolved)
        if normalized:
            hosts.add(normalized)
    default_gateway = _normalized_hostname(_default_gateway_host())
    if default_gateway:
        hosts.add(default_gateway)
    return hosts


@lru_cache(maxsize=1)
def trusted_bootstrap_proxy_hosts() -> set[str]:
    try:
        resolved = socket.gethostbyname("traefik")
    except OSError:
        return set()
    normalized = _normalized_hostname(resolved)
    return {normalized} if normalized else set()


@lru_cache(maxsize=1)
def trusted_local_proxy_networks() -> tuple[ipaddress._BaseNetwork, ...]:
    """Return the trusted local proxy network set.

    Bootstrap trust stays bound to exact local proxy hosts, not adjacent subnet
    peers, so this intentionally returns an empty tuple.
    """
    return ()


def _client_host_in_trusted_local_proxy_scope(client_host: str) -> bool:
    normalized_client = _normalized_hostname(client_host)
    if not normalized_client:
        return False
    return normalized_client in trusted_local_proxy_hosts()


def _request_trusts_forwarded_headers(request: Any) -> bool:
    client = getattr(request, "client", None)
    client_host = _normalized_hostname(str(getattr(client, "host", "") or ""))
    if not client_host:
        return False
    if client_host in {"127.0.0.1", "::1", "localhost"}:
        return True
    return client_host in trusted_local_proxy_hosts()


def _valid_ip_literal(value: str) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return ""


def _is_trusted_forwarded_proxy_hop(value: str) -> bool:
    normalized = _valid_ip_literal(value)
    if not normalized:
        return False
    if normalized in {"127.0.0.1", "::1"}:
        return True
    if normalized in trusted_local_proxy_hosts():
        return True
    return _client_host_in_trusted_local_proxy_scope(normalized)


def _client_ip_from_forwarded_for(header_value: str) -> str:
    hops = [
        normalized
        for item in str(header_value or "").split(",")
        if (normalized := _valid_ip_literal(item))
    ]
    if not hops:
        return ""
    for hop in reversed(hops):
        if not _is_trusted_forwarded_proxy_hop(hop):
            return hop
    return hops[-1]


def _localhost_proxy_bootstrap_allowed(request, client_host: str) -> bool:
    forwarded_values = str(request.headers.get("x-forwarded-for") or "").split(",")
    if len(forwarded_values) != 1:
        return False
    forwarded_client = _valid_ip_literal(forwarded_values[0])
    if not forwarded_client or not ipaddress.ip_address(forwarded_client).is_loopback:
        return False
    if _is_loopback_hostname(client_host):
        return True
    return _normalized_hostname(client_host) in trusted_bootstrap_proxy_hosts()


def bootstrap_request_allowed(
    request,
    *,
    env_flag,
    bootstrap_token_header: str,
) -> tuple[bool, str]:
    if not env_flag("FS_AUTH_BOOTSTRAP_ENABLED", True):
        return False, "Bootstrap endpoint is disabled by configuration"

    state_file = str(_runtime_env.get("FS_AUTH_BOOTSTRAP_STATE_FILE", "") or "").strip()
    if state_file:
        from core.application.auth.bootstrap_tokens import bootstrap_token_state_request_valid

        provided = str(request.headers.get(bootstrap_token_header) or "").strip()
        return bootstrap_token_state_request_valid(provided, state_path=Path(state_file))

    required_token = str(_runtime_env.get("FS_AUTH_BOOTSTRAP_TOKEN", "") or "").strip()
    if required_token:
        from core.application.auth.bootstrap_tokens import bootstrap_token_request_valid

        provided = str(request.headers.get(bootstrap_token_header) or "").strip()
        return bootstrap_token_request_valid(
            provided,
            required=required_token,
            expires_at=str(_runtime_env.get("FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT", "") or ""),
        )

    if env_flag("FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY", True):
        client = getattr(request, "client", None)
        client_host = str((client.host if client else "") or "").strip().lower()
        allow_trusted_proxy = env_flag("FS_AUTH_BOOTSTRAP_ALLOW_TRUSTED_PROXY", False)
        forwarded_for = str(request.headers.get("x-forwarded-for") or "").strip()
        direct_loopback = _is_loopback_hostname(client_host) and not forwarded_for
        if not direct_loopback and not (
            allow_trusted_proxy
            and _localhost_proxy_bootstrap_allowed(
                request,
                client_host,
            )
        ):
            return False, "Bootstrap is restricted to localhost requests"
    return True, ""


def is_public_auth_path(
    path: str,
    *,
    public_health_paths: set[str],
    auth_public_paths: set[str],
    public_webhook_path_prefixes: tuple[str, ...],
    public_frontend_path_prefixes: tuple[str, ...],
    path_login: str,
) -> bool:
    _ = path_login
    path_text = str(path or "").strip()
    if path_text in public_health_paths or path_text in auth_public_paths:
        return True
    if re.fullmatch(r"/api/mcp/oauth/client-metadata/[^/]+\.json", path_text):
        return True
    if any(path_text.startswith(prefix) for prefix in public_webhook_path_prefixes):
        return True
    if any(
        path_text == prefix or path_text.startswith(f"{prefix}/")
        for prefix in public_frontend_path_prefixes
    ):
        return True
    return path_text.startswith(("/static/", "/static-v/"))


def is_self_service_auth_write_route(path: str) -> bool:
    path_text = str(path or "").strip()
    if not path_text:
        return False
    if path_text in {
        "/api/auth/change-password",
        "/api/auth/logout",
        "/api/auth/tokens",
        "/api/users/me/email-change/request",
    }:
        return True
    return bool(re.fullmatch(r"/api/auth/tokens/[^/]+/revoke", path_text))


def optional_route_path_allowed(
    path: str,
    *,
    paths: Any = None,
    predicates: Any = None,
) -> bool:
    path_text = str(path or "").strip()
    if not path_text:
        return False

    if isinstance(paths, (set, list, tuple)) and path_text in {str(item) for item in paths}:
        return True

    if isinstance(predicates, (list, tuple)):
        for predicate in predicates:
            if not callable(predicate):
                continue
            try:
                if bool(predicate(path_text)):
                    return True
            except Exception:
                continue
    return False


def request_client_ip(request) -> str:
    if _request_trusts_forwarded_headers(request):
        forwarded = str(request.headers.get("x-forwarded-for") or "").strip()
        if forwarded:
            client_ip = _client_ip_from_forwarded_for(forwarded)
            if client_ip:
                return client_ip
        real_ip = str(request.headers.get("x-real-ip") or "").strip()
        if validated_real_ip := _valid_ip_literal(real_ip):
            return validated_real_ip
    client = getattr(request, "client", None)
    host = str(getattr(client, "host", "") or "").strip()
    return host or "unknown"


__all__ = [
    "auth_identity_tables_available",
    "auth_user_account_id_select",
    "auth_user_column_exists",
    "auth_user_deleted_condition",
    "auth_user_locale_select",
    "auth_user_must_change_password_select",
    "get_user_by_id",
    "get_user_by_session_token",
    "get_user_by_username",
    "get_user_roles",
    "is_public_auth_path",
    "is_self_service_auth_write_route",
    "optional_route_path_allowed",
    "real_dict_cursor_kwargs",
    "request_client_ip",
    "seed_default_roles",
]
