"""Low-level Flow Steward MCP client helpers for orchestration tools."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import threading
import time
from concurrent.futures import Future
from contextlib import suppress
from contextvars import ContextVar
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import httpx2
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-invocation principal context — used by the MCP rate limiter.
# Set this to the current job_id (or another stable principal string) before
# calling any MCP tool.  Falls back to "anonymous" when unset.
# ---------------------------------------------------------------------------
_MCP_PRINCIPAL: ContextVar[str] = ContextVar("_MCP_PRINCIPAL", default="anonymous")


def set_mcp_principal(principal_id: str) -> None:
    """Bind the current principal id to the active execution context.

    Call this once at the start of each agent invocation so that all MCP
    tool calls within that context are attributed to the same principal for
    rate-limiting purposes.
    """
    _MCP_PRINCIPAL.set(str(principal_id or "anonymous").strip() or "anonymous")


def _build_local_url(scheme: str, host: str, port: int, path: str = "") -> str:
    return urlunsplit((scheme, f"{host}:{int(port)}", path, "", ""))


_MCP_URL = _runtime_env.get("MCP_URL", _build_local_url("http", "fs-mcp", 8100, "/mcp"))
_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
_APP_BASE_URL = (
    _runtime_env.get("APP_BASE_URL", "https://localhost:8099").strip() or "https://localhost:8099"
)
_VERIFY_SSL = str(_runtime_env.get("E2E_VERIFY_SSL", "0") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_LOCAL_HTTP_HOSTS = {"localhost", "127.0.0.1", "::1", "fs-app"}
_MCP_BEARER_TOKEN_CACHE: str | None = None
_FS_AUTH_LOGGER = None
_FS_AUTH_LOGGER_DSN = ""
_MCP_OBSERVABILITY_SECRET_RE = re.compile(
    r"(Authorization\s*[:=]\s*Bearer\s+|Bearer\s+)[^\s\"'}]+",
    re.IGNORECASE,
)


def _safe_json_len(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return len(str(value or ""))


def _redact_mcp_observability_text(value: str) -> str:
    return _MCP_OBSERVABILITY_SECRET_RE.sub(r"\1[REDACTED]", str(value or ""))


def _normalize_mcp_principal_job_id(value: str) -> str:
    text = str(value or "").strip()
    if text.lower().startswith("job:"):
        return text.split(":", 1)[1].strip()
    return text


def _resolve_backend_admin_creds():
    try:
        from core.application.tooling.testing.runtime_env import resolve_test_env

        return resolve_test_env(_runtime_env, lane="backend", role="admin", include_dotenv=True)
    except Exception as e:
        logger.debug("MCP auth bootstrap: could not resolve backend admin creds: %s", e)
        return None


def _normalize_runtime_base_url(raw_url: str) -> str:
    value = str(raw_url or "").strip()
    if not value:
        return value
    try:
        parsed = urlsplit(value)
    except Exception:
        return value

    host = str(parsed.hostname or "").strip().lower()
    if (
        parsed.scheme == "https"
        and host in _LOCAL_HTTP_HOSTS
        and not _VERIFY_SSL
        and str(_runtime_env.get("FS_SERVE_SSL", "0") or "").strip().lower()
        not in {"1", "true", "yes", "on"}
    ):
        return urlunsplit(("http", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    return value


def _candidate_platform_base_urls() -> list[str]:
    urls: list[str] = []
    for raw_candidate in (
        _APP_BASE_URL,
        _runtime_env.get("FS_INTERNAL_BASE_URL", "").strip(),
        _build_local_url("https", "localhost", 8099),
        _build_local_url("http", "localhost", 8099),
        _build_local_url("http", "fs-app", 8099),
    ):
        for candidate in (
            str(raw_candidate or "").strip(),
            _normalize_runtime_base_url(str(raw_candidate or "").strip()),
        ):
            normalized = str(candidate or "").strip().rstrip("/")
            if normalized and normalized not in urls:
                urls.append(normalized)
    return urls


def _candidate_platform_mcp_urls() -> list[str]:
    urls: list[str] = []
    explicit_url = str(_runtime_env.get("MCP_URL", "") or "").strip()
    for candidate in (
        _MCP_URL,
        explicit_url,
        _build_local_url("http", "fs-mcp", 8100, "/mcp"),
        _build_local_url("http", "localhost", 8100, "/mcp"),
        _build_local_url("http", "127.0.0.1", 8100, "/mcp"),
        _build_local_url("http", "host.docker.internal", 8100, "/mcp"),
    ):
        normalized = str(candidate or "").strip().rstrip("/")
        if normalized and normalized not in urls:
            urls.append(normalized)
    return urls


def _clear_fs_mcp_bearer_token_cache() -> None:
    global _MCP_BEARER_TOKEN_CACHE
    _MCP_BEARER_TOKEN_CACHE = None


def _get_platform_auth_logger():
    global _FS_AUTH_LOGGER, _FS_AUTH_LOGGER_DSN
    try:
        from core.infrastructure.config.runtime_config import CONFIG
        from core.infrastructure.db.platform_stores import PlatformJobStore
    except Exception as e:
        logger.debug("MCP auth bootstrap: could not import Flow Steward logger stack: %s", e)
        return None

    dsn = str(
        CONFIG.get("fs_observability_dsn")
        or CONFIG.get("fs_db_dsn")
        or _runtime_env.get("FS_DB_DSN", "")
    ).strip()
    if not dsn:
        return None

    try:
        if _FS_AUTH_LOGGER is not None and dsn == _FS_AUTH_LOGGER_DSN:
            with _FS_AUTH_LOGGER.conn.cursor() as cur:
                cur.execute("SELECT 1")
            return _FS_AUTH_LOGGER
    except Exception as e:
        logger.debug(
            "MCP auth bootstrap: cached Flow Steward logger is not usable, reconnecting: %s", e
        )
        _FS_AUTH_LOGGER = None

    try:
        _FS_AUTH_LOGGER = PlatformJobStore(dsn)
        _FS_AUTH_LOGGER_DSN = dsn
        return _FS_AUTH_LOGGER
    except Exception as e:
        logger.debug("MCP auth bootstrap: could not connect Flow Steward logger: %s", e)
        _FS_AUTH_LOGGER = None
        return None


def _bootstrap_fs_session_token(username: str, password: str) -> str:
    normalized_username = str(username or "").strip()
    normalized_password = str(password or "")
    if not normalized_username or not normalized_password:
        return ""

    bl = _get_platform_auth_logger()
    if not bl:
        return ""

    try:
        from core.application.auth.config import AUTH_SESSION_TTL_HOURS, DEFAULT_ACCOUNT_ID
        from core.application.auth.password import session_token_hash, verify_password
        from core.application.auth.resolve import auth_tables_available, get_user_by_username
    except Exception as e:
        logger.debug("MCP auth bootstrap: could not import auth helpers: %s", e)
        return ""

    if not auth_tables_available(bl):
        return ""

    try:
        user = get_user_by_username(bl, normalized_username)
        if not user or not bool(user.get("is_active", True)):
            return ""
        if bool(user.get("must_change_password", False)):
            return ""
        if not verify_password(normalized_password, str(user.get("password_hash") or "")):
            return ""

        session_token = secrets.token_urlsafe(48)
        account_id = str(user.get("account_id") or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID
        with bl.conn.cursor() as cur:
            session_has_account_id = False
            with suppress(Exception):
                session_has_account_id = bool(bl._column_exists("auth_sessions", "account_id"))
            if session_has_account_id:
                cur.execute(
                    """
                    INSERT INTO auth_sessions (
                        user_id,
                        account_id,
                        session_token_hash,
                        expires_at,
                        revoked
                    )
                    VALUES (%s, %s, %s, now() + make_interval(hours => %s), false)
                    """,
                    (
                        str(user.get("id") or ""),
                        account_id,
                        session_token_hash(session_token),
                        int(AUTH_SESSION_TTL_HOURS),
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO auth_sessions (user_id, session_token_hash, expires_at, revoked)
                    VALUES (%s, %s, now() + make_interval(hours => %s), false)
                    """,
                    (
                        str(user.get("id") or ""),
                        session_token_hash(session_token),
                        int(AUTH_SESSION_TTL_HOURS),
                    ),
                )
        bl.conn.commit()
        return session_token
    except Exception as e:
        with suppress(Exception):
            bl.conn.rollback()
        logger.debug("MCP auth bootstrap via DB session failed for %s: %s", normalized_username, e)
        return ""


def _seed_local_auth_bootstrap_user(
    username: str,
    password: str,
    *,
    display_name: str = "",
) -> bool:
    normalized_username = str(username or "").strip()
    normalized_password = str(password or "")
    normalized_display_name = str(display_name or "").strip() or normalized_username
    if not normalized_username or not normalized_password:
        return False

    bl = _get_platform_auth_logger()
    if not bl:
        return False

    try:
        from core.application.auth.first_admin import (
            FirstAdminAlreadyExists,
            FirstAdminService,
        )
        from core.application.auth.first_admin_repository import PostgresFirstAdminRepository
        from core.application.auth.password import password_hash
        from core.application.auth.resolve import auth_tables_available, seed_default_roles
        from core.infrastructure.runtime.entrypoint_db_adapters import database_transaction
    except Exception as e:
        logger.debug("MCP auth bootstrap: could not import local auth seed helpers: %s", e)
        return False

    if not auth_tables_available(bl):
        return False

    try:
        service = FirstAdminService(
            repository=PostgresFirstAdminRepository(
                logger=bl,
                seed_default_roles=seed_default_roles,
                transaction_connection_factory=database_transaction,
            ),
            password_hash=password_hash,
        )
        result = service.create(
            email=normalized_username,
            password=normalized_password,
            display_name=normalized_display_name,
        )
        return bool(str(result.get("id") or "").strip())
    except FirstAdminAlreadyExists:
        return False
    except Exception as e:
        logger.debug(
            "MCP auth bootstrap: could not seed local auth user for %s: %s", normalized_username, e
        )
        return False


def _bootstrap_fs_session_token_via_http(username: str, password: str) -> str:
    normalized_username = str(username or "").strip()
    normalized_password = str(password or "")
    if not normalized_username or not normalized_password:
        return ""

    for base_url in _candidate_platform_base_urls():
        login_url = f"{base_url.rstrip('/')}/api/auth/login"
        try:
            with httpx.Client(timeout=10.0, verify=_VERIFY_SSL, follow_redirects=True) as client:
                resp = client.post(
                    login_url,
                    json={"username": normalized_username, "password": normalized_password},
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                token = str(
                    client.cookies.get("fs_session")
                    or getattr(resp, "cookies", {}).get("fs_session")
                    or ""
                ).strip()
                if token:
                    return token
        except Exception as e:
            logger.debug(
                "MCP auth bootstrap via HTTP failed for %s at %s: %s",
                normalized_username,
                base_url,
                e,
            )
    return ""


def _fs_mcp_bearer_token(*, force_refresh: bool = False) -> str:
    global _MCP_BEARER_TOKEN_CACHE
    explicit = str(
        _runtime_env.get("MCP_BEARER_TOKEN") or _runtime_env.get("FS_SESSION_TOKEN") or ""
    ).strip()
    if explicit:
        _MCP_BEARER_TOKEN_CACHE = explicit
        return explicit
    if not force_refresh and _MCP_BEARER_TOKEN_CACHE:
        return _MCP_BEARER_TOKEN_CACHE

    creds = _resolve_backend_admin_creds()
    username = str(getattr(creds, "username", "") or "").strip()
    password = str(getattr(creds, "password", "") or "").strip()
    display_name = str(getattr(creds, "display_name", "") or username).strip()
    if not username or not password:
        return explicit

    token = _bootstrap_fs_session_token(username, password)
    if token:
        _MCP_BEARER_TOKEN_CACHE = token
        return token

    if _seed_local_auth_bootstrap_user(username, password, display_name=display_name):
        token = _bootstrap_fs_session_token(username, password)
        if token:
            _MCP_BEARER_TOKEN_CACHE = token
            return token

    token = _bootstrap_fs_session_token_via_http(username, password)
    if token:
        _MCP_BEARER_TOKEN_CACHE = token
        return token

    logger.warning(
        "MCP auth bootstrap failed for %s: neither in-process nor HTTP session bootstrap succeeded. "
        "Set MCP_BEARER_TOKEN or FS_SESSION_TOKEN to provide an explicit token.",
        username,
    )
    return ""


def _reset_fs_mcp_bearer_token_cache() -> None:
    _clear_fs_mcp_bearer_token_cache()


def _headers_for_url(
    url: str,
    *,
    force_refresh: bool = False,
    include_platform_auth: bool | None = None,
) -> dict[str, str]:
    headers = dict(_HEADERS)
    should_include_auth = (
        (url == _MCP_URL) if include_platform_auth is None else bool(include_platform_auth)
    )
    if should_include_auth:
        bearer = _fs_mcp_bearer_token(force_refresh=force_refresh)
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
    return headers


async def _raise_mcp_auth_status(response: httpx2.Response) -> None:
    """Keep HTTP auth failures visible before the SDK maps them to MCP errors."""
    if response.status_code == 401:
        await response.aread()
        response.raise_for_status()


async def _call_modern_mcp_endpoint(
    url: str,
    tool_name: str,
    arguments: dict,
    *,
    headers: dict[str, str],
) -> str:
    """Call one tool through the SDK v2 stateless 2026-07-28 client path."""
    async with httpx2.AsyncClient(
        headers=headers,
        timeout=60.0,
        verify=_VERIFY_SSL,
        follow_redirects=False,
        event_hooks={"response": [_raise_mcp_auth_status]},
    ) as http_client:
        transport = streamable_http_client(
            url,
            http_client=http_client,
            terminate_on_close=False,
        )
        try:
            async with Client(transport, mode="2026-07-28", cache=None) as client:
                result = await client.call_tool(tool_name, arguments)
        except MCPError as exc:
            raise RuntimeError(
                f"MCP error from {tool_name}: code={exc.error.code} message={exc.error.message}"
            ) from exc

    text_blocks = [
        str(text) for block in result.content if (text := getattr(block, "text", None)) is not None
    ]
    result_text = "\n".join(text_blocks)
    if result.is_error:
        raise RuntimeError(f"MCP error from {tool_name}: {result_text or 'tool call failed'}")
    if not text_blocks:
        raise RuntimeError(f"MCP error from {tool_name}: response contained no text result")
    return result_text


class _McpResultHandoff:
    """Transfer thread failures, including cancellation, to their original caller."""

    def __init__(self, result: Future[Any]) -> None:
        self._result = result

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> bool:
        if error is None:
            return False
        self._result.set_exception(error)
        return True


def _run_mcp_coro_sync(coro: Any) -> str:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return str(asyncio.run(coro))

    result: Future[Any] = Future()

    def worker() -> None:
        with _McpResultHandoff(result):
            result.set_result(asyncio.run(coro))

    # A caller interrupt must not register an executor atexit join for a blocked
    # network operation. The daemon's outcome belongs to the waiting caller.
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join()
    return str(result.result() or "")


def _find_nested_exception(
    exc: BaseException,
    exception_type: type[BaseException],
) -> BaseException | None:
    if isinstance(exc, exception_type):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for nested in exc.exceptions:
            if found := _find_nested_exception(nested, exception_type):
                return found
    return None


def _call_mcp_endpoint(
    url: str,
    tool_name: str,
    arguments: dict,
    *,
    include_platform_auth: bool | None = None,
) -> str:
    """Send a tools/call JSON-RPC request to an MCP endpoint and return the text result."""
    started_at = time.monotonic()
    normalized_url = str(url or "").strip().rstrip("/")
    should_include_auth = (
        normalized_url == _MCP_URL.rstrip("/")
        if include_platform_auth is None
        else bool(include_platform_auth)
    )
    attempts = 2 if should_include_auth else 1
    try:
        for attempt in range(attempts):
            headers = _headers_for_url(
                normalized_url,
                force_refresh=attempt > 0,
                include_platform_auth=should_include_auth,
            )
            if should_include_auth and not str(headers.get("Authorization") or "").strip():
                raise RuntimeError(
                    "Flow Steward MCP auth token unavailable for authenticated MCP request"
                )
            try:
                result_text = _run_mcp_coro_sync(
                    _call_modern_mcp_endpoint(
                        normalized_url,
                        tool_name,
                        arguments,
                        headers=headers,
                    )
                )
                break
            except Exception as exc:
                status_error = _find_nested_exception(exc, httpx2.HTTPStatusError)
                status_code = getattr(getattr(status_error, "response", None), "status_code", None)
                if should_include_auth and status_code == 401 and attempt < attempts - 1:
                    _reset_fs_mcp_bearer_token_cache()
                    continue
                if status_error is not None:
                    request = httpx.Request("POST", normalized_url)
                    response = httpx.Response(int(status_code or 500), request=request)
                    raise httpx.HTTPStatusError(
                        str(status_error), request=request, response=response
                    ) from exc
                if timeout_error := _find_nested_exception(exc, httpx2.TimeoutException):
                    raise httpx.ReadTimeout(
                        str(timeout_error), request=httpx.Request("POST", normalized_url)
                    ) from exc
                if transport_error := _find_nested_exception(exc, httpx2.TransportError):
                    raise httpx.ConnectError(
                        str(transport_error), request=httpx.Request("POST", normalized_url)
                    ) from exc
                raise
        else:  # pragma: no cover - loop always breaks or raises
            raise RuntimeError(f"MCP error from {tool_name}: request did not complete")
        unwrapped = _unwrap_platform_mcp_result(result_text)
        _log_mcp_tool_observability(
            tool_name=tool_name,
            arguments=arguments,
            url=normalized_url,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            success=True,
            response_chars=len(str(unwrapped or "")),
        )
        return unwrapped
    except Exception as exc:
        _log_mcp_tool_observability(
            tool_name=tool_name,
            arguments=arguments,
            url=normalized_url,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            success=False,
            response_chars=0,
            error=str(exc),
        )
        raise


def _log_mcp_tool_observability(
    *,
    tool_name: str,
    arguments: dict,
    url: str,
    duration_ms: int,
    success: bool,
    response_chars: int,
    error: str = "",
) -> None:
    bl = _get_platform_auth_logger()
    if not bl:
        return
    principal = _normalize_mcp_principal_job_id(str(_MCP_PRINCIPAL.get() or "").strip())
    job_row: dict[str, Any] | None = None
    if principal and principal != "anonymous" and hasattr(bl, "get_job"):
        with suppress(Exception):
            candidate = bl.get_job(principal)
            if isinstance(candidate, dict):
                job_row = candidate
    if not isinstance(job_row, dict):
        return
    job_id = str(job_row.get("id") or principal or "").strip()
    project_id = job_row.get("project_id")
    if not job_id or not project_id:
        return
    parsed = urlsplit(str(url or ""))
    correlation_id = (
        str(arguments.get("correlation_id") or arguments.get("correlation") or "").strip()
        if isinstance(arguments, dict)
        else ""
    )
    agent = (
        str(arguments.get("agent") or arguments.get("agent_id") or "mcp_tool").strip()
        if isinstance(arguments, dict)
        else "mcp_tool"
    )
    with suppress(Exception):
        bl.log_action(
            project_id=project_id,
            task_id=job_row.get("task_id"),
            task_summary=job_row.get("task_summary"),
            job_id=job_id,
            agent=agent or "mcp_tool",
            action_type="mcp_tool_call",
            status="done" if success else "error",
            payload={
                "provider": "mcp",
                "tool_name": str(tool_name or "")[:160],
                "server": parsed.netloc[:160],
                "scheme": parsed.scheme,
                "args_chars": _safe_json_len(arguments),
                "response_chars": int(response_chars or 0),
                "duration_ms": int(duration_ms or 0),
                "success": bool(success),
                "correlation_id": correlation_id[:160] if correlation_id else "",
            },
            result={
                "summary": (
                    f"MCP tool {tool_name} {'succeeded' if success else 'failed'} "
                    f"in {int(duration_ms or 0)}ms"
                )
            },
            error=_redact_mcp_observability_text(str(error or ""))[:4000] if error else None,
        )


def _json_compact(value) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _unwrap_platform_mcp_result(result_text: str) -> str:
    text = str(result_text or "")
    stripped = text.strip()
    if not stripped:
        return text
    try:
        payload = json.loads(stripped)
    except Exception:
        return text
    if not isinstance(payload, dict) or not {"ok", "data", "error", "meta"} <= set(payload):
        return text
    if not bool(payload.get("ok")):
        error = payload.get("error") or {}
        if isinstance(error, dict):
            message = str(error.get("message") or "Flow Steward MCP tool failed").strip()
            code = str(error.get("code") or "tool_execution_failed").strip()
            raise RuntimeError(f"{code}: {message}")
        raise RuntimeError(str(error or "Flow Steward MCP tool failed"))

    data = payload.get("data")
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return _json_compact(data)
