from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.application.management.errors import ServiceForbiddenError, ServiceValidationError
from core.application.management.mcp_connector_common import (
    _project_lineage_for_scope,
    _sanitize_connector_row_for_view,
)
from core.application.management.mcp_connector_services import (
    McpConnectorCommandService,
    McpConnectorQueryService,
    McpConnectorStore,
)
from core.application.management.scope import ScopeContext
from core.infrastructure.db import mcp_connector_oauth_store


def _scope(*, project_id: str = "project-a", tenant_id: str = "tenant-a") -> ScopeContext:
    return ScopeContext(
        account_id="account-a",
        tenant_id=tenant_id or None,
        project_id=project_id or None,
        allowed_project_ids=frozenset({"project-a"}),
        actor_id="operator-a",
        principal_type="operator",
    )


def test_connector_lineage_comes_from_authenticated_scope() -> None:
    assert _project_lineage_for_scope(_scope()) == ("account-a", "project-a", "tenant-a")


@pytest.mark.parametrize("scope", [_scope(project_id=""), _scope(tenant_id="")])
def test_connector_lineage_rejects_missing_project_or_tenant(scope: ScopeContext) -> None:
    with pytest.raises(ServiceValidationError, match="project scope is required"):
        _project_lineage_for_scope(scope)


def test_connector_lineage_rejects_guessed_cross_project_id() -> None:
    _raises_input_43_1 = _scope(project_id="project-b")
    with pytest.raises(ServiceForbiddenError, match="Project access denied"):
        _project_lineage_for_scope(_raises_input_43_1)


def test_connector_view_returns_presence_without_secret_refs() -> None:
    view = _sanitize_connector_row_for_view(
        {
            "connector_id": "mcp_alpha",
            "account_id": "account-a",
            "project_id": "project-a",
            "tenant_id": "tenant-a",
            "oauth_secret_refs_jsonb": {
                "refs": {
                    "_oauth_client_id": "mcp_connector:mcp_alpha:_oauth_client_id",
                    "_oauth_client_secret": "mcp_connector:mcp_alpha:_oauth_client_secret",
                }
            },
        }
    )

    assert view["project_id"] == "project-a"
    assert "tenant_id" not in view
    assert "account_id" not in view
    assert view["has_client_id"] is True
    assert view["has_client_secret"] is True
    assert "oauth_secret_refs_jsonb" not in view


class _Cursor:
    def __init__(self) -> None:
        self.query = ""
        self.params: tuple[object, ...] = ()
        self.description: list[object] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...] | list[object]) -> None:
        self.query = query.as_string() if hasattr(query, "as_string") else query
        self.params = tuple(params)

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _Logger:
    def __init__(self) -> None:
        self.cur = _Cursor()

    def cursor(self) -> _Cursor:
        return self.cur


@pytest.mark.parametrize("operation", ["get", "list", "update", "delete"])
def test_connector_store_sql_uses_exact_project_tenant_scope(operation: str) -> None:
    bl = _Logger()
    store = McpConnectorStore()
    kwargs = {
        "account_id": "account-a",
        "project_id": "project-a",
        "tenant_id": "tenant-a",
    }

    if operation == "get":
        store.get_connector_row(bl, "mcp-alpha", **kwargs)
    elif operation == "list":
        store.list_connector_rows(bl, **kwargs)
    elif operation == "update":
        store.update_connector_row(bl, "mcp-alpha", {"status": "active"}, **kwargs)
    else:
        store.soft_delete_connector_row(bl, "mcp-alpha", **kwargs)

    normalized_query = " ".join(bl.cur.query.split())
    assert "account_id = %s" in normalized_query
    assert "project_id = %s" in normalized_query
    assert "tenant_id = %s" in normalized_query
    assert "account-a" in bl.cur.params
    assert "project-a" in bl.cur.params
    assert "tenant-a" in bl.cur.params


def test_query_service_passes_authenticated_project_lineage_to_store() -> None:
    class _Store:
        def __init__(self) -> None:
            self.scope: tuple[str, str, str] | None = None

        def list_connector_rows(
            self,
            _bl: object,
            *,
            account_id: str,
            project_id: str,
            tenant_id: str,
            include_trashed: bool = False,
        ) -> list[dict[str, object]]:
            del include_trashed
            self.scope = (account_id, project_id, tenant_id)
            return []

    store = _Store()
    service = McpConnectorQueryService(get_logger=lambda: object(), store=store)  # type: ignore[arg-type]

    assert service.list_connectors(scope=_scope()).total == 0
    assert store.scope == ("account-a", "project-a", "tenant-a")


def test_oauth_state_persists_full_project_and_revision_binding() -> None:
    bl = _Logger()

    mcp_connector_oauth_store.insert_oauth_state(
        bl,
        state_hash="state-hash",
        connector_id="mcp-alpha",
        account_id="account-a",
        project_id="project-a",
        tenant_id="tenant-a",
        oauth_credential_revision=7,
        code_verifier_encrypted="encrypted-verifier",
        redirect_uri="https://app.example.test/oauth/callback",
        return_path="/settings/connectors/mcp-alpha",
        auth_payload={},
        expires_at=1000.0,
    )

    normalized_query = " ".join(bl.cur.query.split())
    assert "project_id" in normalized_query
    assert "tenant_id" in normalized_query
    assert "oauth_credential_revision" in normalized_query
    assert "project-a" in bl.cur.params
    assert "tenant-a" in bl.cur.params
    assert 7 in bl.cur.params


def test_projected_tool_manifest_carries_exact_owner_lineage() -> None:
    service = McpConnectorCommandService(get_logger=lambda: object())

    manifest = service._build_projected_tool_manifest(
        {
            "connector_id": "mcp-alpha",
            "account_id": "account-a",
            "project_id": "project-a",
            "tenant_id": "tenant-a",
            "display_name": "Example",
        },
        {
            "remote_tool_name": "items.list",
            "projected_tool_id": "example.items.list",
            "input_schema": {},
            "output_schema": {},
        },
        enabled=True,
    )

    assert manifest["account_id"] == "account-a"
    assert manifest["project_id"] == "project-a"
    assert manifest["scope_kind"] == "project"
    assert manifest["implementation"]["owner_project_id"] == "project-a"
    assert manifest["implementation"]["owner_tenant_id"] == "tenant-a"
    assert manifest["config"]["owner_project_id"] == "project-a"
    assert manifest["config"]["owner_tenant_id"] == "tenant-a"


def test_runtime_connector_lookup_rejects_cross_project_before_database_access(monkeypatch) -> None:
    from core.application.orchestration.tools import tool_resolver

    manifest = SimpleNamespace(
        project_id="project-a",
        implementation=SimpleNamespace(
            owner_account_id="account-a",
            owner_project_id="project-a",
            owner_tenant_id="tenant-a",
        ),
        config={},
    )
    calls: list[bool] = []
    monkeypatch.setattr(
        McpConnectorStore,
        "get_connector_row",
        lambda *_args, **_kwargs: calls.append(True),
    )

    _raises_input_230_1 = {
        "account_id": "account-a",
        "project_id": "project-b",
        "tenant_id": "tenant-a",
        "logger": _Logger(),
    }
    with pytest.raises(RuntimeError, match="project mismatch"):
        tool_resolver._load_mcp_connector_row(
            "mcp-alpha", manifest=manifest, runtime_context=_raises_input_230_1
        )

    assert calls == []
