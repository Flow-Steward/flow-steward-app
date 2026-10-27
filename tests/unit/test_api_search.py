from __future__ import annotations

import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

if "psycopg_runtime" not in sys.modules:
    psycopg_runtime_mod = types.ModuleType("psycopg_runtime")
    extras_mod = types.ModuleType("psycopg_runtime.extras")
    extras_mod.RealDictCursor = object
    psycopg_runtime_mod.extras = extras_mod
    sys.modules["psycopg_runtime"] = psycopg_runtime_mod
    sys.modules["psycopg_runtime.extras"] = extras_mod

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.management.errors import ServiceValidationError
from core.application.management.scope import ScopeContext
from core.application.search.global_search_categories import (
    global_search_category_groups,
    global_search_category_options,
)
from core.entrypoints.api.search import create_api_search_router


class _FakeSearchService:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def global_search(self, query, *, scope, filters=None, limit=20):
        if not str(query or "").strip():
            raise ServiceValidationError("Search query is required")
        self.calls.append(
            {
                "query": query,
                "scope": scope,
                "filters": filters,
                "limit": limit,
            }
        )
        return {
            "query": query,
            "scope": {"account_id": "acc_default"},
            "groups": [],
            "related_context": [],
        }


def _service_error_response(exc):
    return JSONResponse(
        getattr(exc, "payload", {"error": str(exc)}),
        status_code=int(getattr(exc, "status_code", 500) or 500),
    )


def _api_error_response(exc):
    return JSONResponse({"error": str(exc)}, status_code=500)


@pytest.mark.parametrize(
    ("role", "included", "excluded"),
    [
        ("viewer", "jobs", "workflows"),
        (None, "jobs", "workflows"),
        ("  ", "jobs", "workflows"),
        ("operator", "workflows", "users"),
        ("admin", "users", "kb"),
    ],
)
def test_api_search_config_publishes_only_role_supported_categories(role, included, excluded):
    app = FastAPI()
    app.include_router(
        create_api_search_router(
            search_service=_FakeSearchService,
            request_scope=lambda _request, **_kwargs: ScopeContext(
                account_id="acc_default", principal_type=role
            ),
            service_error_response=_service_error_response,
            api_error_response=_api_error_response,
        )
    )

    response = TestClient(app).get("/api/search/config")

    assert response.status_code == 200
    payload = response.json()
    categories = {entry["value"] for entry in payload["categories"]}
    assert payload["default_category"] == "all"
    assert {"all", included} <= categories
    assert excluded not in categories
    assert set(payload["group_labels"]) == categories - {"all"}
    assert payload["filters"] == {}


def test_api_search_config_preserves_scope_denial():
    from core.application.management.errors import ServiceForbiddenError

    def denied_scope(_request, **_kwargs):
        raise ServiceForbiddenError("Not authorized")

    app = FastAPI()
    app.include_router(
        create_api_search_router(
            search_service=_FakeSearchService,
            request_scope=denied_scope,
            service_error_response=_service_error_response,
            api_error_response=_api_error_response,
        )
    )

    assert TestClient(app).get("/api/search/config").status_code == 403


def test_api_search_route_maps_category_and_scope() -> None:
    service = _FakeSearchService()
    captured_scope_kwargs: list[dict[str, Any]] = []

    def _request_scope(_request, **kwargs):
        captured_scope_kwargs.append(kwargs)
        return ScopeContext(account_id="acc_default")

    app = FastAPI()
    app.include_router(
        create_api_search_router(
            search_service=lambda: service,
            request_scope=_request_scope,
            service_error_response=_service_error_response,
            api_error_response=_api_error_response,
        )
    )
    client = TestClient(app)

    response = client.get(
        "/api/search?query=timeout&category=jobs&project_id=7&workspace_id=ws-1&limit=15"
    )
    assert response.status_code == 200
    assert service.calls
    call = service.calls[0]
    assert call["query"] == "timeout"
    assert call["limit"] == 15
    assert call["filters"]["result_groups"] == list(global_search_category_groups("jobs")[1])
    assert call["filters"]["workspace_id"] == "ws-1"
    assert captured_scope_kwargs[0]["project_id"] == "7"
    assert captured_scope_kwargs[0]["workspace_id"] == "ws-1"
    assert response.json()["category"] == "jobs"


def test_api_search_route_returns_controlled_timeout_problem(monkeypatch) -> None:
    class _SlowSearchService:
        def global_search(self, query, *, scope, filters=None, limit=20):
            _ = (query, scope, filters, limit)
            time.sleep(0.05)
            return {"groups": []}

    monkeypatch.setattr(
        "core.entrypoints.api.search.SEARCH_REQUEST_TIMEOUT_SECONDS",
        0.001,
        raising=False,
    )
    app = FastAPI()
    app.include_router(
        create_api_search_router(
            search_service=_SlowSearchService,
            request_scope=lambda _request, **_kwargs: ScopeContext(account_id="acc_default"),
            service_error_response=_service_error_response,
            api_error_response=_api_error_response,
        )
    )

    response = TestClient(app).get("/api/search?query=nightfire&category=all")

    assert response.status_code == 503
    assert response.json() == {
        "error": "Search request timed out",
        "code": "search_timeout",
    }


def test_api_search_route_forwards_cursor_for_category_views() -> None:
    service = _FakeSearchService()

    app = FastAPI()
    app.include_router(
        create_api_search_router(
            search_service=lambda: service,
            request_scope=lambda _request, **_kwargs: ScopeContext(account_id="acc_default"),
            service_error_response=_service_error_response,
            api_error_response=_api_error_response,
        )
    )
    client = TestClient(app)

    response = client.get("/api/search?query=timeout&category=jobs&cursor=opaque-token")
    assert response.status_code == 200
    assert service.calls[0]["filters"]["cursor"] == "opaque-token"


def test_api_search_route_uses_all_category_by_default() -> None:
    service = _FakeSearchService()

    app = FastAPI()
    app.include_router(
        create_api_search_router(
            search_service=lambda: service,
            request_scope=lambda _request, **_kwargs: ScopeContext(account_id="acc_default"),
            service_error_response=_service_error_response,
            api_error_response=_api_error_response,
        )
    )
    client = TestClient(app)

    response = client.get("/api/search?query=global")
    assert response.status_code == 200
    assert service.calls[0]["filters"]["result_groups"] == list(
        global_search_category_groups("all")[1]
    )
    assert service.calls[0]["filters"]["group_item_limit"] == 5
    assert "cursor" not in service.calls[0]["filters"]
    assert response.json()["category"] == "all"


def test_api_search_route_rejects_unknown_category() -> None:
    service = _FakeSearchService()

    app = FastAPI()
    app.include_router(
        create_api_search_router(
            search_service=lambda: service,
            request_scope=lambda _request, **_kwargs: ScopeContext(account_id="acc_default"),
            service_error_response=_service_error_response,
            api_error_response=_api_error_response,
        )
    )
    client = TestClient(app)

    response = client.get("/api/search?query=oops&category=invalid")
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "Unsupported search category"
    assert service.calls == []


def test_api_search_route_maps_workflow_category() -> None:
    service = _FakeSearchService()

    app = FastAPI()
    app.include_router(
        create_api_search_router(
            search_service=lambda: service,
            request_scope=lambda _request, **_kwargs: ScopeContext(account_id="acc_default"),
            service_error_response=_service_error_response,
            api_error_response=_api_error_response,
        )
    )
    client = TestClient(app)

    response = client.get("/api/search?query=berlin&category=workflows")
    assert response.status_code == 200
    assert service.calls[0]["filters"]["result_groups"] == list(
        global_search_category_groups("workflows")[1]
    )
    assert response.json()["category"] == "workflows"


def test_global_search_categories_cover_every_user_visible_catalog_entity() -> None:
    options = {item["value"]: item["label"] for item in global_search_category_options()}

    assert options == {
        "all": "Everything",
        "jobs": "Jobs / workflow runs",
        "events": "Events",
        "runtime_messages": "Runtime messages",
        "workflows": "Workflows",
        "agents": "Agents",
        "tools": "Tools",
        "skills": "Skills",
        "mcp_connectors": "MCP connectors",
        "projects": "Projects",
        "extensions": "Extensions",
        "guardrails": "Guardrails",
        "data_contracts": "Data contracts",
        "fields": "Fields",
        "published_blocks": "Published blocks",
        "datasets": "Datasets",
        "saved_queries": "Saved queries",
        "data_connections": "Data connections",
        "model_providers": "Model providers",
        "model_routes": "Model routes",
        "users": "Users",
        "workspaces": "Workspaces",
    }
    assert global_search_category_groups("agents") == ("agents", ("agents",))
    assert global_search_category_groups("data_connections") == (
        "data_connections",
        ("data_connections",),
    )
