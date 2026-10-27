"""Authenticated MCP RPC sessions with scoped credential and retry ownership."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from core.application.auth.mcp_connector_oauth_policy import (
    _as_string_list,
    _as_text,
    _bearer_challenge,
    _http_status_from_error,
    _now,
    _oauth_metadata,
    _require_current_oauth_revision,
    _www_auth_from_error,
)
from core.application.auth.mcp_connector_oauth_provider import (
    discover_mcp_oauth,
)
from core.application.auth.mcp_connector_oauth_repository import (
    _locked_connector_row,
    _update_connector_oauth,
)
from core.application.auth.mcp_connector_oauth_tokens import (
    ensure_valid_mcp_connector_access_token,
)
from core.application.management.errors import (
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.mcp_connector_client import (
    StreamableHttpMcpClient,
    _json_rpc_post_with_latency,
    get_execution_mcp_client,
)
from core.application.management.mcp_connector_common import (
    _connector_headers,
)
from core.application.management.mcp_connector_header_secrets import (
    connector_has_headers,
    resolve_connector_headers,
)
from core.application.management.mcp_connector_http import (
    _connector_rpc_url,
)
from core.application.management.mcp_connector_protocol import McpExecutionScopeInactiveError
from core.application.management.mcp_connector_store import McpConnectorStore
from core.infrastructure.db.db_pool import (
    transaction_connection,
)


def _probe_mcp_oauth_www_authenticate(
    connector_row: dict[str, Any],
    *,
    timeout_seconds: float = 10.0,
) -> str:
    headers = {
        key: value
        for key, value in _connector_headers(connector_row).items()
        if key.lower() not in {"authorization", "x-api-key", "api-key"}
    }
    try:
        _json_rpc_post_with_latency(
            url=_connector_rpc_url(connector_row),
            headers=headers,
            payload={
                "jsonrpc": "2.0",
                "id": "oauth-discovery",
                "method": "tools/list",
                "params": {},
            },
            timeout_seconds=max(1.0, float(timeout_seconds)),
        )
    except ServiceUnavailableError as exc:
        if _http_status_from_error(exc) == 401:
            return _www_auth_from_error(exc)
    except Exception:
        return ""
    return ""


def _headers_for_connector(
    conn: Any,
    *,
    connector_row: dict[str, Any],
    force_refresh: bool = False,
) -> dict[str, str]:
    headers = resolve_connector_headers(conn, connector_row)
    if _as_text(connector_row.get("auth_mode")).lower() != "oauth2":
        return headers
    token = ensure_valid_mcp_connector_access_token(
        conn,
        connector_row=connector_row,
        force_refresh=force_refresh,
    )
    return {**headers, "Authorization": f"Bearer {token}"}


class _ConnectorHeadersProvider:
    """Resolve current connector credentials for one retained protocol client."""

    def __init__(self, conn: Any, connector_row: dict[str, Any]) -> None:
        self._conn = conn
        self._connector_row = connector_row
        self._binding = self._binding_for(connector_row)
        self._force_refresh = False

    @staticmethod
    def _binding_for(connector_row: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
        return (
            _as_text(connector_row.get("connector_id")),
            _as_text(connector_row.get("account_id")),
            _as_text(connector_row.get("project_id")),
            _as_text(connector_row.get("tenant_id")),
            _connector_rpc_url(connector_row),
            _as_text(connector_row.get("auth_mode")).lower(),
        )

    def assert_compatible(self, connector_row: dict[str, Any]) -> None:
        if self._binding_for(connector_row) != self._binding:
            raise ServiceValidationError(
                "MCP retained client binding no longer matches the connector endpoint or auth mode"
            )

    @property
    def connector_row(self) -> dict[str, Any]:
        return self._connector_row

    def update(self, connector_row: dict[str, Any], *, force_refresh: bool = False) -> None:
        self.assert_compatible(connector_row)
        self._connector_row = connector_row
        self._force_refresh = self._force_refresh or force_refresh

    def _reload_connector_row(self) -> dict[str, Any]:
        connector_row = self._connector_row
        connector_id, account_id, project_id, tenant_id, _endpoint, _auth_mode = self._binding
        if not connector_id or not account_id:
            return connector_row
        if _auth_mode == "none" and not connector_has_headers(connector_row):
            return connector_row
        latest = McpConnectorStore().get_connector_row(
            self._conn,
            connector_id,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )
        if not latest:
            raise ServiceValidationError(
                f"MCP connector '{connector_id}' no longer exists or is unavailable"
            )
        self.assert_compatible(latest)
        self._connector_row = latest
        return latest

    def __call__(self) -> dict[str, str]:
        # Reload before every POST/DELETE header resolution. OAuth secret refs are
        # intentionally indirect, so a retained client must validate the current
        # endpoint binding before it can resolve credentials through those refs.
        connector_row = self._reload_connector_row()
        if _as_text(connector_row.get("auth_mode")).lower() != "oauth2":
            return resolve_connector_headers(self._conn, connector_row)
        force_refresh = self._force_refresh
        self._force_refresh = False
        return _headers_for_connector(
            self._conn,
            connector_row=connector_row,
            force_refresh=force_refresh,
        )


@contextlib.contextmanager
def mcp_connector_rpc_context(
    conn: Any,
    *,
    connector_row: dict[str, Any],
    timeout_seconds: float,
    runtime_context: Mapping[str, Any] | None = None,
) -> Iterator[Callable[..., tuple[dict[str, Any], int]]]:
    """Yield one authenticated RPC caller backed by one selected protocol client."""

    is_oauth = _as_text(connector_row.get("auth_mode")).lower() == "oauth2"
    connector_id = _as_text(connector_row.get("connector_id"))
    connector_account_id = _as_text(connector_row.get("account_id"))
    connector_project_id = _as_text(connector_row.get("project_id"))
    connector_tenant_id = _as_text(connector_row.get("tenant_id"))
    context = runtime_context if isinstance(runtime_context, Mapping) else {}
    runtime_account_id = _as_text(context.get("account_id"))
    runtime_project_id = _as_text(context.get("project_id"))
    runtime_tenant_id = _as_text(context.get("tenant_id"))
    runtime_job_id = _as_text(context.get("job_id"))
    if runtime_account_id and runtime_account_id != connector_account_id:
        raise ServiceValidationError(
            "MCP runtime account identity does not match the connector owner"
        )
    if runtime_project_id and runtime_project_id != connector_project_id:
        raise ServiceValidationError(
            "MCP runtime project identity does not match the connector owner"
        )
    if runtime_tenant_id and runtime_tenant_id != connector_tenant_id:
        raise ServiceValidationError(
            "MCP runtime tenant identity does not match the connector owner"
        )

    def _client_factory() -> StreamableHttpMcpClient:
        headers_provider = _ConnectorHeadersProvider(conn, connector_row)
        created = StreamableHttpMcpClient(
            url=_connector_rpc_url(connector_row),
            headers_provider=headers_provider,
            timeout_seconds=timeout_seconds,
        )
        created._fs_connector_headers_provider = headers_provider  # type: ignore[attr-defined]
        return created

    close_client = False
    if runtime_account_id and runtime_job_id:
        try:
            client = get_execution_mcp_client(
                account_id=runtime_account_id,
                job_id=runtime_job_id,
                connector_id=connector_id,
                client_factory=_client_factory,
            )
        except McpExecutionScopeInactiveError:
            client = _client_factory()
            close_client = True
    else:
        client = _client_factory()
        close_client = True

    retained_provider = getattr(client, "_fs_connector_headers_provider", None)
    if isinstance(retained_provider, _ConnectorHeadersProvider):
        retained_provider.assert_compatible(connector_row)

    def _rpc_post(
        payload: dict[str, Any],
        *,
        generated_transport_headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], int]:
        _connector_rpc_url(
            connector_row,
            generated_transport_headers=generated_transport_headers,
        )
        params = payload.get("params")
        method = _as_text(payload.get("method"))
        raw_name = (params or {}).get("name") if isinstance(params, dict) else ""
        name = raw_name if isinstance(raw_name, str) else ""

        def _client_request() -> tuple[dict[str, Any], int]:
            if generated_transport_headers:
                return client.request(
                    method,
                    params,
                    name=name,
                    generated_transport_headers=generated_transport_headers,
                )
            return client.request(method, params, name=name)

        try:
            return _client_request()
        except ServiceUnavailableError as exc:
            status = _http_status_from_error(exc)
            if not is_oauth:
                raise
            if status == 401:
                discover_mcp_oauth(
                    conn,
                    connector_row=connector_row,
                    www_authenticate=_www_auth_from_error(exc),
                )
                latest = (
                    McpConnectorStore().get_connector_row(
                        conn,
                        connector_id,
                        account_id=connector_account_id,
                        project_id=connector_project_id,
                        tenant_id=connector_tenant_id,
                    )
                    or connector_row
                )
                if _as_text(latest.get("account_id")) != connector_account_id:
                    raise ServiceValidationError(
                        "MCP refreshed connector owner does not match the request account"
                    ) from exc
                if (
                    _as_text(latest.get("project_id")) != connector_project_id
                    or _as_text(latest.get("tenant_id")) != connector_tenant_id
                ):
                    raise ServiceValidationError(
                        "MCP refreshed connector lineage does not match the request"
                    ) from exc
                retained_provider = getattr(
                    client,
                    "_fs_connector_headers_provider",
                    None,
                )
                if isinstance(retained_provider, _ConnectorHeadersProvider):
                    retained_provider.assert_compatible(latest)
                    retained_provider.update(latest, force_refresh=True)
                else:
                    _headers_for_connector(
                        conn,
                        connector_row=latest,
                        force_refresh=True,
                    )
                return _client_request()
            if status == 403:
                challenge = _bearer_challenge(_www_auth_from_error(exc))
                if _as_text(challenge.get("error")) == "insufficient_scope":
                    with transaction_connection(conn) as transaction_conn:
                        locked_row = _locked_connector_row(transaction_conn, connector_row)
                        _require_current_oauth_revision(connector_row, locked_row)
                        metadata = _oauth_metadata(locked_row)
                        metadata["last_scope_challenge"] = {
                            "scope": _as_string_list(
                                _as_text(challenge.get("scope")).replace(",", " ").split()
                            ),
                            "resource_metadata": _as_text(challenge.get("resource_metadata")),
                            "at": _now(),
                        }
                        _update_connector_oauth(
                            transaction_conn,
                            connector_id,
                            account_id=connector_account_id,
                            project_id=connector_project_id,
                            tenant_id=connector_tenant_id,
                            updates={
                                "oauth_metadata_jsonb": metadata,
                                "auth_status": "auth_required",
                                "status": "degraded",
                                "oauth_last_error": "insufficient_scope",
                            },
                        )
                    raise ServiceUnavailableError(
                        "MCP connector requires reconnect with additional OAuth scopes"
                    ) from exc
            raise

    try:
        yield _rpc_post
    finally:
        if close_client:
            client.close()


def mcp_connector_rpc_post_with_latency(
    conn: Any,
    *,
    connector_row: dict[str, Any],
    payload: dict[str, Any],
    timeout_seconds: float,
    runtime_context: Mapping[str, Any] | None = None,
    generated_transport_headers: dict[str, str] | None = None,
) -> tuple[dict[str, Any], int]:
    with mcp_connector_rpc_context(
        conn,
        connector_row=connector_row,
        timeout_seconds=timeout_seconds,
        runtime_context=runtime_context,
    ) as rpc_post:
        if generated_transport_headers:
            return rpc_post(
                payload,
                generated_transport_headers=generated_transport_headers,
            )
        return rpc_post(payload)


def mcp_connector_rpc_post(
    conn: Any,
    *,
    connector_row: dict[str, Any],
    payload: dict[str, Any],
    timeout_seconds: float,
    runtime_context: Mapping[str, Any] | None = None,
    generated_transport_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    payload, _latency_ms = mcp_connector_rpc_post_with_latency(
        conn,
        connector_row=connector_row,
        payload=payload,
        timeout_seconds=timeout_seconds,
        runtime_context=runtime_context,
        generated_transport_headers=generated_transport_headers,
    )
    return payload
