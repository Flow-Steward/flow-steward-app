"""MCP connector service layer for registry-backed MCP catalogs."""

from __future__ import annotations

import logging
import urllib.error
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

from core.application.management.mcp_connector_client import streamable_http_request_with_latency
from core.application.management.mcp_connector_common import (
    DATABASE_NOT_CONFIGURED_MESSAGE,
    _extract_tools_page,
    _normalize_auth_mode,
    _normalize_discovered_tools,
    _normalize_namespace_prefix,
    _normalize_string_list,
    _project_lineage_for_scope,
    _projected_tools_for_connector,
    _remap_allowlisted_tools_for_namespace,
    _require_connector_mutation_access,
    _sanitize_connector_row_for_view,
    _service_scope,
    _utc_now_iso,
)
from core.application.management.mcp_connector_header_secrets import (
    HEADER_SECRET_REFS_FIELD,
    connector_header_names,
    resolve_connector_headers,
    save_connector_header_secrets,
)
from core.application.management.mcp_connector_http import _connector_rpc_url
from core.application.management.mcp_connector_observability import (
    log_mcp_pagination_completed,
    mcp_observability_context,
)
from core.application.management.mcp_connector_protocol import (
    MCP_DISCOVERY_PAGE_LIMIT,
    _normalize_transport,
)
from core.application.management.scope import ScopeContext
from core.infrastructure.db.db_pool import (
    transaction_connection,
)

from .command_models import McpConnectorCreateInput, McpConnectorUpdateInput
from .errors import (
    ServiceConflictError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from .mcp_connector_deletion import McpConnectorDeletionMixin
from .mcp_connector_oauth_contract import (
    _annotate_oauth_scope_availability,
    _oauth_metadata_from_config,
    _sync_configuration_fingerprint,
    _validate_oauth_headers,
)
from .mcp_connector_projection import McpConnectorProjectionMixin
from .mcp_connector_queries import McpConnectorQueryService as McpConnectorQueryService
from .mcp_connector_store import McpConnectorStore as McpConnectorStore
from .query_models import McpConnectorHealthView, McpConnectorView

logger = logging.getLogger(__name__)

_MCP_HEALTH_FAILURE_MESSAGE = "MCP connector health check failed; verify the endpoint and retry"
_MCP_SYNC_FAILURE_MESSAGE = "MCP connector sync failed; verify the endpoint and retry"


# Vendor parameters whose names contain a credential marker but carry no
# secret: Dropbox's token_access_type=offline asks for a refresh token.


def _json_rpc_post_with_latency(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> tuple[dict[str, Any], int]:
    return streamable_http_request_with_latency(
        url=url,
        headers=headers,
        payload=payload,
        timeout_seconds=timeout_seconds,
    )


def _json_rpc_post(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    payload, _latency_ms = _json_rpc_post_with_latency(
        url=url,
        headers=headers,
        payload=payload,
        timeout_seconds=timeout_seconds,
    )
    return payload


class McpConnectorCommandService(McpConnectorDeletionMixin, McpConnectorProjectionMixin):
    """Command-side MCP connector operations."""

    def __init__(self, *, get_logger: Callable[[], Any], store: McpConnectorStore | None = None):
        self._get_logger = get_logger
        self._store = store or McpConnectorStore()

    def create_connector(
        self,
        inp: McpConnectorCreateInput,
        *,
        scope: ScopeContext | None = None,
    ) -> McpConnectorView:
        inp = McpConnectorCreateInput.model_validate(inp)
        scope = _service_scope(scope)
        _require_connector_mutation_access(scope)
        account_id, project_id, tenant_id = _project_lineage_for_scope(scope)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)

        if not inp.base_url:
            raise ServiceValidationError("base_url is required")
        if not inp.display_name:
            raise ServiceValidationError("display_name is required")

        connector_id = f"mcp_{uuid.uuid4().hex[:16]}"
        now_iso = _utc_now_iso()

        row = {
            "connector_id": connector_id,
            "account_id": account_id,
            "project_id": project_id,
            "tenant_id": tenant_id,
            "display_name": inp.display_name,
            "description": inp.description,
            "transport": _normalize_transport(inp.transport),
            "base_url": inp.base_url,
            "auth_mode": _normalize_auth_mode(inp.auth_mode),
            "headers": inp.headers,
            "allowlisted_tools": _normalize_string_list(inp.allowlisted_tools),
            "namespace_prefix": _normalize_namespace_prefix(inp.namespace_prefix),
            "status": "unchecked",
            "auth_status": (
                "auth_required"
                if _normalize_auth_mode(inp.auth_mode) == "oauth2"
                else "not_configured"
            ),
            "oauth_metadata_jsonb": _oauth_metadata_from_config(inp.oauth_config),
            "oauth_secret_refs_jsonb": {},
            "oauth_credential_revision": 1,
            "oauth_connected_identity": "",
            "oauth_last_error": "",
            "discovered_tools_jsonb": [],
            "last_synced_at": None,
            "last_sync_status": "never",
            "last_sync_error": "",
            "enabled": bool(inp.enabled),
            "catalog_item_id": inp.catalog_item_id,
            "catalog_remote_id": inp.catalog_remote_id,
            "catalog_item_revision": inp.catalog_item_revision,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        _validate_oauth_headers(str(row["auth_mode"]), row["headers"])
        _connector_rpc_url(row)

        row.pop("headers")
        try:
            with transaction_connection(bl) as persistence:
                row[HEADER_SECRET_REFS_FIELD] = save_connector_header_secrets(
                    persistence,
                    connector_id=connector_id,
                    account_id=account_id,
                    project_id=project_id,
                    tenant_id=tenant_id,
                    headers=inp.headers,
                )
                self._insert_connector(persistence, row)
        except Exception as exc:
            logger.exception("Failed to persist MCP connector %s", connector_id)
            raise ServiceUnavailableError("Failed to persist MCP connector") from exc
        return McpConnectorView(**_sanitize_connector_row_for_view(row))

    def update_connector(
        self,
        connector_id: str,
        inp: McpConnectorUpdateInput,
        *,
        scope: ScopeContext | None = None,
    ) -> McpConnectorView:
        inp = McpConnectorUpdateInput.model_validate(inp)
        scope = _service_scope(scope)
        _require_connector_mutation_access(scope)
        account_id, project_id, tenant_id = _project_lineage_for_scope(scope)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        with transaction_connection(bl) as persistence:
            if callable(getattr(persistence, "cursor", None)):
                with persistence.cursor() as cur:
                    cur.execute(
                        """SELECT 1 FROM mcp_connectors
                            WHERE connector_id = %s AND account_id = %s
                              AND project_id = %s AND tenant_id = %s
                              AND deleted_at IS NULL FOR UPDATE""",
                        (connector_id, account_id, project_id, tenant_id),
                    )
                    if not cur.fetchone():
                        raise ServiceNotFoundError(f"MCP connector '{connector_id}' not found")
            return self._update_connector_serialized(
                connector_id,
                inp,
                scope=scope,
                persistence=persistence,
            )

    def _update_connector_serialized(
        self,
        connector_id: str,
        inp: McpConnectorUpdateInput,
        *,
        scope: ScopeContext,
        persistence: Any,
    ) -> McpConnectorView:
        inp = McpConnectorUpdateInput.model_validate(inp)
        scope = _service_scope(scope)
        _require_connector_mutation_access(scope)
        account_id, project_id, tenant_id = _project_lineage_for_scope(scope)
        bl = persistence

        existing = self._get_connector_row(
            bl,
            connector_id,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )
        if not existing:
            raise ServiceNotFoundError(f"MCP connector '{connector_id}' not found")

        current_discovered_tools = _normalize_discovered_tools(
            existing,
            existing.get("discovered_tools_jsonb"),
        )
        requested_allowlist = (
            _normalize_string_list(inp.allowlisted_tools)
            if inp.allowlisted_tools is not None
            else None
        )
        requested_namespace_prefix = (
            _normalize_namespace_prefix(inp.namespace_prefix)
            if inp.namespace_prefix is not None
            else None
        )

        updates: dict[str, Any] = {}
        headers_mode = str(getattr(inp, "headers_mode", None) or "").strip().lower()
        if headers_mode == "keep" and inp.headers is not None:
            raise ServiceValidationError(
                "headers must be omitted when headers_mode is keep",
                payload={
                    "error": "invalid_headers_mode_payload",
                    "field": "headers",
                    "message": "headers must be omitted when headers_mode is keep",
                },
            )
        if headers_mode == "clear":
            if inp.headers not in (None, {}):
                raise ServiceValidationError(
                    "headers must be omitted when headers_mode is clear",
                    payload={
                        "error": "invalid_headers_mode_payload",
                        "field": "headers",
                        "message": "headers must be omitted when headers_mode is clear",
                    },
                )
            updates["headers"] = {}
        elif headers_mode == "replace":
            if not inp.headers:
                raise ServiceValidationError(
                    "headers are required when headers_mode is replace; use clear to remove them",
                    payload={
                        "error": "invalid_headers_mode_payload",
                        "field": "headers",
                        "message": (
                            "headers are required when headers_mode is replace; "
                            "use clear to remove them"
                        ),
                    },
                )
            updates["headers"] = dict(inp.headers)
        for field in (
            "display_name",
            "description",
            "transport",
            "base_url",
            "auth_mode",
            "oauth_config",
            "enabled",
        ):
            value = getattr(inp, field, None)
            if value is None:
                continue
            if field == "auth_mode":
                value = _normalize_auth_mode(str(value))
            if field == "transport":
                value = _normalize_transport(value)
            if field == "oauth_config":
                updates["oauth_metadata_jsonb"] = {
                    **(
                        existing.get("oauth_metadata_jsonb")
                        if isinstance(existing.get("oauth_metadata_jsonb"), dict)
                        else {}
                    ),
                    **_oauth_metadata_from_config(value),
                }
                continue
            updates[field] = value
        if not headers_mode and inp.headers is not None:
            updates["headers"] = dict(inp.headers)

        existing_auth_mode_for_transition = _normalize_auth_mode(
            str(existing.get("auth_mode") or "none")
        )
        requested_auth_mode_for_transition = _normalize_auth_mode(
            str(updates.get("auth_mode") or existing_auth_mode_for_transition)
        )
        if (
            existing_auth_mode_for_transition in {"bearer", "api_key"}
            and requested_auth_mode_for_transition in {"none", "oauth2"}
            and existing_auth_mode_for_transition != requested_auth_mode_for_transition
            and "headers" not in updates
        ):
            raise ServiceValidationError(
                "Switching away from credential headers requires headers_mode clear or replace"
            )

        auth_mode_for_headers = str(updates.get("auth_mode") or existing.get("auth_mode") or "")
        headers_for_validation = updates.get("headers", connector_header_names(existing))
        _validate_oauth_headers(auth_mode_for_headers, headers_for_validation)

        if requested_namespace_prefix is not None:
            updates["namespace_prefix"] = requested_namespace_prefix
            current_discovered_tools = _normalize_discovered_tools(
                {
                    **existing,
                    "namespace_prefix": requested_namespace_prefix,
                },
                current_discovered_tools,
            )
            updates["discovered_tools_jsonb"] = current_discovered_tools
            if requested_allowlist is None:
                requested_allowlist = _remap_allowlisted_tools_for_namespace(
                    existing,
                    namespace_prefix=requested_namespace_prefix,
                    discovered_tools=current_discovered_tools,
                )

        if requested_allowlist is not None:
            if requested_allowlist and not current_discovered_tools:
                raise ServiceValidationError(
                    "allowlisted_tools can only be saved after a successful MCP sync"
                )
            discovered_by_projected_id = {
                str(tool.get("projected_tool_id") or "").strip(): tool
                for tool in current_discovered_tools
                if str(tool.get("projected_tool_id") or "").strip()
            }
            missing_tool_ids = [
                tool_id
                for tool_id in requested_allowlist
                if tool_id not in discovered_by_projected_id
            ]
            if missing_tool_ids:
                raise ServiceValidationError(
                    "allowlisted_tools must reference discovered MCP tools: "
                    + ", ".join(sorted(missing_tool_ids))
                )
            current_allowlist = set(_normalize_string_list(existing.get("allowlisted_tools")))
            live_projected_ids = {
                str(tool.get("tool_id") or "").strip()
                for tool in _projected_tools_for_connector(connector_id)
                if str(tool.get("tool_id") or "").strip()
            }
            removed_tool_ids = sorted(
                (current_allowlist - set(requested_allowlist)).intersection(live_projected_ids)
            )
            if removed_tool_ids:
                raise ServiceConflictError(
                    "Published MCP tools must be moved to Trash through their confirmed delete flow",
                    payload={
                        "error": "mcp_projected_tool_delete_plan_required",
                        "tool_ids": removed_tool_ids,
                        "recovery_guidance": "Delete each projected tool from Tools after reviewing its workflow impact.",
                    },
                )
            self._reject_trashed_projected_tool_publication(
                connector_id=connector_id,
                account_id=account_id,
                tool_ids=sorted(set(requested_allowlist) - current_allowlist),
            )
            updates["allowlisted_tools"] = requested_allowlist

        if "headers" in updates:
            try:
                unchanged_headers = updates["headers"] == resolve_connector_headers(bl, existing)
            except ServiceUnavailableError:
                unchanged_headers = False
            if unchanged_headers:
                del updates["headers"]

        updated = {**existing, **updates}
        if any(key in updates for key in {"transport", "base_url", "auth_mode", "headers"}):
            _connector_rpc_url(updated)

        existing_auth_mode = _normalize_auth_mode(str(existing.get("auth_mode") or "none"))
        updated_auth_mode = _normalize_auth_mode(str(updated.get("auth_mode") or "none"))
        binding_fields_changed = "headers" in updates or any(
            key in updates and existing.get(key) != updated.get(key)
            for key in ("transport", "base_url", "auth_mode")
        )
        oauth_configuration_changed = False
        if "oauth_metadata_jsonb" in updates:
            previous_oauth = existing.get("oauth_metadata_jsonb")
            previous_oauth = previous_oauth if isinstance(previous_oauth, dict) else {}
            current_oauth = updates["oauth_metadata_jsonb"]
            oauth_configuration_changed = any(
                previous_oauth.get(field) != current_oauth.get(field)
                for field in (
                    "requested_scopes",
                    "authorization_parameters",
                    "refresh_token_required",
                )
            )
        has_oauth_state = bool(
            existing.get("oauth_secret_refs_jsonb")
            or existing.get("oauth_metadata_jsonb")
            or existing.get("oauth_connected_identity")
        )
        oauth_reset_required = (binding_fields_changed or oauth_configuration_changed) and (
            existing_auth_mode == "oauth2" or updated_auth_mode == "oauth2" or has_oauth_state
        )
        persistence_context = nullcontext(bl)
        with persistence_context as persistence:
            if oauth_reset_required:
                from core.application.auth.mcp_connector_oauth import (
                    mcp_connector_oauth_reset_updates,
                    prepare_mcp_connector_oauth_reset,
                )

                existing = prepare_mcp_connector_oauth_reset(
                    persistence,
                    connector_row=existing,
                )
                reset_updates = mcp_connector_oauth_reset_updates(
                    persistence,
                    connector_row=existing,
                    auth_status=(
                        "auth_required" if updated_auth_mode == "oauth2" else "not_configured"
                    ),
                    message=(
                        "MCP OAuth reconnect required after connector transport, endpoint, auth mode, or headers change"
                        if updated_auth_mode == "oauth2"
                        else ""
                    ),
                    preserve_client_credentials=(
                        oauth_configuration_changed and not binding_fields_changed
                    ),
                )
                if oauth_configuration_changed and not binding_fields_changed:
                    requested_oauth_configuration = {
                        key: updates.get("oauth_metadata_jsonb", {})[key]
                        for key in (
                            "requested_scopes",
                            "authorization_parameters",
                            "refresh_token_required",
                        )
                        if key in updates.get("oauth_metadata_jsonb", {})
                    }
                    reset_updates["oauth_metadata_jsonb"] = {
                        **(
                            reset_updates.get("oauth_metadata_jsonb")
                            if isinstance(reset_updates.get("oauth_metadata_jsonb"), dict)
                            else {}
                        ),
                        **requested_oauth_configuration,
                    }
                updates.update(reset_updates)
                if updated_auth_mode == "oauth2":
                    updates["status"] = "degraded"
                updated = {**existing, **updates}

            if "headers" in updates:
                updates[HEADER_SECRET_REFS_FIELD] = save_connector_header_secrets(
                    persistence,
                    connector_id=connector_id,
                    account_id=account_id,
                    project_id=project_id,
                    tenant_id=tenant_id,
                    headers=updates.pop("headers"),
                )
                updated.pop("headers", None)
                updated[HEADER_SECRET_REFS_FIELD] = updates[HEADER_SECRET_REFS_FIELD]

            if any(key in updates for key in {"allowlisted_tools", "namespace_prefix", "enabled"}):
                snapshot = self._snapshot_projected_registry_tools(existing, updated)
                shares_connector_transaction = False
                try:
                    with self._projected_registry_transaction(
                        persistence
                    ) as shares_connector_transaction:
                        self._sync_projected_registry_tools(updated)
                    if updates:
                        self._update_connector_row(
                            persistence,
                            connector_id,
                            updates,
                            account_id=account_id,
                            project_id=project_id,
                            tenant_id=tenant_id,
                        )
                except Exception:
                    if not shares_connector_transaction:
                        self._restore_projected_registry_tools(snapshot)
                    raise
                self._publish_projected_registry_after_commit(
                    persistence,
                    shares_connector_transaction=shares_connector_transaction,
                )
            elif updates:
                self._update_connector_row(
                    persistence,
                    connector_id,
                    updates,
                    account_id=account_id,
                    project_id=project_id,
                    tenant_id=tenant_id,
                )

            updated = (
                self._get_connector_row(
                    persistence,
                    connector_id,
                    account_id=account_id,
                    project_id=project_id,
                    tenant_id=tenant_id,
                )
                or updated
            )

        return McpConnectorView(**_sanitize_connector_row_for_view(updated))

    def check_health(
        self,
        connector_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> McpConnectorHealthView:
        scope = _service_scope(scope)
        _require_connector_mutation_access(scope)
        account_id, project_id, tenant_id = _project_lineage_for_scope(scope)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)

        existing = self._get_connector_row(
            bl,
            connector_id,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )
        if not existing:
            raise ServiceNotFoundError(f"MCP connector '{connector_id}' not found")

        status = "offline"
        latency_ms: int | None = None
        error_text: str | None = None

        try:
            request_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            }
            with mcp_observability_context(connector_id=connector_id, job_id=""):
                if str(existing.get("auth_mode") or "").strip().lower() == "oauth2":
                    from core.application.auth.mcp_connector_oauth import (
                        mcp_connector_rpc_post_with_latency,
                    )

                    _payload, latency_ms = mcp_connector_rpc_post_with_latency(
                        bl,
                        connector_row=existing,
                        payload=request_payload,
                        timeout_seconds=10.0,
                    )
                else:
                    _payload, latency_ms = _json_rpc_post_with_latency(
                        url=_connector_rpc_url(existing),
                        headers=resolve_connector_headers(bl, existing),
                        payload=request_payload,
                        timeout_seconds=10.0,
                    )
            status = "active"
        except ServiceUnavailableError as exc:
            cause = exc.__cause__
            if isinstance(cause, urllib.error.HTTPError):
                status = "degraded"
                error_text = f"HTTP {cause.code}"
            else:
                status = "offline"
                error_text = _MCP_HEALTH_FAILURE_MESSAGE
            logger.exception("MCP connector health check failed for %s", connector_id)
        except ServiceValidationError:
            status = "degraded"
            error_text = _MCP_HEALTH_FAILURE_MESSAGE
            logger.exception("MCP connector health validation failed for %s", connector_id)
        except Exception as exc:
            status = "degraded" if exc.__class__.__name__ == "OAuthConnectionError" else "offline"
            error_text = _MCP_HEALTH_FAILURE_MESSAGE
            logger.exception("MCP connector health check failed for %s", connector_id)

        self._update_connector_row(
            bl,
            connector_id,
            {"status": status},
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )
        latest = (
            self._get_connector_row(
                bl,
                connector_id,
                account_id=account_id,
                project_id=project_id,
                tenant_id=tenant_id,
            )
            or existing
        )

        return McpConnectorHealthView(
            connector_id=connector_id,
            status=status,
            auth_status=str(latest.get("auth_status") or "not_configured"),
            latency_ms=latency_ms,
            error=error_text,
            checked_at=_utc_now_iso(),
        )

    def sync_connector(
        self,
        connector_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> McpConnectorView:
        scope = _service_scope(scope)
        _require_connector_mutation_access(scope)
        account_id, project_id, tenant_id = _project_lineage_for_scope(scope)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)

        existing = self._get_connector_row(
            bl,
            connector_id,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )
        if not existing:
            raise ServiceNotFoundError(f"MCP connector '{connector_id}' not found")
        sync_configuration_fingerprint = _sync_configuration_fingerprint(existing)

        sync_timestamp = _utc_now_iso()
        try:
            from core.application.auth.mcp_connector_oauth import mcp_connector_rpc_context

            aggregated_tools: list[dict[str, Any]] = []
            seen_tool_names: set[str] = set()
            seen_cursors: set[str] = set()
            next_cursor: str | None = None
            page_count = 0
            with (
                mcp_observability_context(
                    connector_id=connector_id,
                    job_id="",
                ) as observability,
                mcp_connector_rpc_context(
                    bl,
                    connector_row=existing,
                    timeout_seconds=30.0,
                ) as rpc_post,
            ):
                while True:
                    if page_count >= MCP_DISCOVERY_PAGE_LIMIT:
                        raise ServiceValidationError(
                            f"MCP tools/list exceeded {MCP_DISCOVERY_PAGE_LIMIT} pages"
                        )
                    request_params = {} if page_count == 0 else {"cursor": next_cursor}
                    payload, _latency_ms = rpc_post(
                        {
                            "jsonrpc": "2.0",
                            "id": page_count + 1,
                            "method": "tools/list",
                            "params": request_params,
                        }
                    )
                    page_tools, next_cursor = _extract_tools_page(payload)
                    page_count += 1
                    for tool in page_tools:
                        remote_tool_name = str(tool.get("name") or "")
                        if remote_tool_name in seen_tool_names:
                            continue
                        seen_tool_names.add(remote_tool_name)
                        aggregated_tools.append(tool)
                    if next_cursor is None:
                        break
                    if next_cursor in seen_cursors:
                        raise ServiceValidationError("MCP tools/list returned a repeated cursor")
                    seen_cursors.add(next_cursor)
                log_mcp_pagination_completed(
                    observability,
                    page_count=page_count,
                )

            discovered_tools = _annotate_oauth_scope_availability(
                existing,
                _normalize_discovered_tools(existing, aggregated_tools),
            )
            discovered_ids = {
                str(tool.get("projected_tool_id") or "").strip()
                for tool in discovered_tools
                if str(tool.get("projected_tool_id") or "").strip()
                and bool(tool.get("can_publish", True))
            }
            reconciled_allowlist = [
                tool_id
                for tool_id in _normalize_string_list(existing.get("allowlisted_tools"))
                if tool_id in discovered_ids
            ]
            trashed_ids = self._trashed_projected_tool_ids(
                connector_id=connector_id,
                account_id=account_id,
                tool_ids=reconciled_allowlist,
            )
            reconciled_allowlist = [
                tool_id for tool_id in reconciled_allowlist if tool_id not in trashed_ids
            ]
            updates = {
                "discovered_tools_jsonb": discovered_tools,
                "allowlisted_tools": reconciled_allowlist,
                "last_synced_at": sync_timestamp,
                "last_sync_status": "success",
                "last_sync_error": (
                    "projected_tool_in_trash: " + ", ".join(sorted(trashed_ids))
                    if trashed_ids
                    else ""
                ),
            }
            updated_row = {**existing, **updates}
            projected_tools = _projected_tools_for_connector(connector_id)
            needs_projection_reconcile = bool(
                projected_tools
                or _normalize_string_list(existing.get("allowlisted_tools"))
                or reconciled_allowlist
            )
            with transaction_connection(bl) as transaction_conn:
                with transaction_conn.cursor() as cur:
                    cur.execute(
                        """SELECT 1 FROM mcp_connectors
                            WHERE connector_id = %s AND account_id = %s
                              AND project_id = %s AND tenant_id = %s
                              AND deleted_at IS NULL FOR UPDATE""",
                        (connector_id, account_id, project_id, tenant_id),
                    )
                    if not cur.fetchone():
                        raise ServiceConflictError(
                            "MCP connector changed while sync was in progress"
                        )
                locked_row = self._get_connector_row(
                    transaction_conn,
                    connector_id,
                    account_id=account_id,
                    project_id=project_id,
                    tenant_id=tenant_id,
                )
                locked_fingerprint = _sync_configuration_fingerprint(locked_row or {})
                if locked_fingerprint != sync_configuration_fingerprint:
                    raise ServiceConflictError(
                        "MCP connector changed while sync was in progress; run Sync again"
                    )
                snapshot = self._snapshot_projected_registry_tools(existing, updated_row)
                shares_connector_transaction = False
                try:
                    if needs_projection_reconcile:
                        with self._projected_registry_transaction(
                            transaction_conn
                        ) as shares_connector_transaction:
                            self._sync_projected_registry_tools(updated_row)
                    self._update_connector_row(
                        transaction_conn,
                        connector_id,
                        updates,
                        account_id=account_id,
                        project_id=project_id,
                        tenant_id=tenant_id,
                    )
                except Exception:
                    if not shares_connector_transaction:
                        self._restore_projected_registry_tools(snapshot)
                    raise
                if needs_projection_reconcile:
                    self._publish_projected_registry_after_commit(
                        transaction_conn,
                        shares_connector_transaction=shares_connector_transaction,
                    )
        except ServiceConflictError:
            raise
        except ServiceValidationError as exc:
            logger.exception("MCP connector sync validation failed for %s", connector_id)
            self._update_connector_row(
                bl,
                connector_id,
                {
                    "last_synced_at": sync_timestamp,
                    "last_sync_status": "failed",
                    "last_sync_error": _MCP_SYNC_FAILURE_MESSAGE,
                },
                account_id=account_id,
                project_id=project_id,
                tenant_id=tenant_id,
            )
            raise ServiceValidationError(_MCP_SYNC_FAILURE_MESSAGE) from exc
        except Exception as exc:
            logger.exception("MCP connector sync failed for %s", connector_id)
            self._update_connector_row(
                bl,
                connector_id,
                {
                    "last_synced_at": sync_timestamp,
                    "last_sync_status": "failed",
                    "last_sync_error": _MCP_SYNC_FAILURE_MESSAGE,
                },
                account_id=account_id,
                project_id=project_id,
                tenant_id=tenant_id,
            )
            raise ServiceUnavailableError(_MCP_SYNC_FAILURE_MESSAGE) from exc

        updated = (
            self._get_connector_row(
                bl,
                connector_id,
                account_id=account_id,
                project_id=project_id,
                tenant_id=tenant_id,
            )
            or existing
        )
        return McpConnectorView(**_sanitize_connector_row_for_view(updated))

    # --- DB helpers ---

    def _insert_connector(self, bl: Any, row: dict[str, Any]) -> None:
        self._store.insert_connector(bl, row)

    def _get_connector_row(
        self,
        bl: Any,
        connector_id: str,
        *,
        account_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any] | None:
        return self._store.get_connector_row(
            bl,
            connector_id,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )

    def _list_connector_rows(
        self,
        bl: Any,
        account_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._store.list_connector_rows(
            bl,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )

    def _update_connector_row(
        self,
        bl: Any,
        connector_id: str,
        updates: dict[str, Any],
        *,
        account_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        self._store.update_connector_row(
            bl,
            connector_id,
            updates,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )

    def _soft_delete_connector_row(
        self,
        bl: Any,
        connector_id: str,
        *,
        account_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
        deleted_by: str = "system",
        child_tool_ids: list[str] | None = None,
        oauth_metadata: dict[str, Any] | None = None,
    ) -> None:
        self._store.soft_delete_connector_row(
            bl,
            connector_id,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
            deleted_by=deleted_by,
            child_tool_ids=child_tool_ids,
            oauth_metadata=oauth_metadata,
        )

    @staticmethod
    def _row_to_dict(row: tuple, description: Any) -> dict[str, Any]:
        return McpConnectorStore.row_to_dict(row, description)
