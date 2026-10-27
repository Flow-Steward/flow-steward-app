"""Query-side MCP connector operations."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from core.application.auth.principal import principal_type_has_minimum_role
from core.application.management.mcp_connector_common import (
    DATABASE_NOT_CONFIGURED_MESSAGE,
    _project_lineage_for_scope,
    _sanitize_connector_row_for_view,
    _service_scope,
)
from core.application.management.scope import ScopeContext

from .errors import (
    ServiceNotFoundError,
    ServiceUnavailableError,
)
from .mcp_connector_store import McpConnectorStore
from .query_models import McpConnectorListView, McpConnectorView


class McpConnectorQueryService:
    """Query-side MCP connector operations."""

    def __init__(self, *, get_logger: Callable[[], Any], store: McpConnectorStore | None = None):
        self._get_logger = get_logger
        self._store = store or McpConnectorStore()

    def list_connectors(
        self,
        *,
        scope: ScopeContext | None = None,
    ) -> McpConnectorListView:
        scope = _service_scope(scope)
        account_id, project_id, tenant_id = _project_lineage_for_scope(scope)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)

        rows = self._store.list_connector_rows(
            bl,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )
        include_deleted_actor = principal_type_has_minimum_role(scope.principal_type, "admin")
        connectors = [
            McpConnectorView(
                **_sanitize_connector_row_for_view(
                    row,
                    include_deleted_actor=include_deleted_actor,
                )
            )
            for row in rows
        ]
        return McpConnectorListView(connectors=connectors, total=len(connectors))

    def list_connectors_in_projects(
        self,
        *,
        scopes: Sequence[ScopeContext],
    ) -> McpConnectorListView:
        """The connectors of several projects, each read with its own scope."""
        connectors: list[McpConnectorView] = []
        for project_scope in scopes:
            connectors.extend(self.list_connectors(scope=project_scope).connectors)
        return McpConnectorListView(connectors=connectors, total=len(connectors))

    def get_connector(
        self,
        connector_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> McpConnectorView:
        scope = _service_scope(scope)
        account_id, project_id, tenant_id = _project_lineage_for_scope(scope)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)

        row = self._store.get_connector_row(
            bl,
            connector_id,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )
        if not row:
            raise ServiceNotFoundError(f"MCP connector '{connector_id}' not found")
        return McpConnectorView(
            **_sanitize_connector_row_for_view(
                row,
                include_deleted_actor=principal_type_has_minimum_role(
                    scope.principal_type,
                    "admin",
                ),
            )
        )
