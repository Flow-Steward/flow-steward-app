from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

try:
    from core.infrastructure.db import psycopg_runtime as psycopg_runtime
except ImportError:
    _mock_psycopg_runtime = MagicMock()
    _mock_psycopg_runtime.extras.RealDictCursor = "RealDictCursor"
    sys.modules.setdefault("psycopg_runtime", _mock_psycopg_runtime)
    sys.modules.setdefault("psycopg_runtime.extras", _mock_psycopg_runtime.extras)

from core.application.management.query_services import ToolQueryService
from core.application.registry.db_store import RegistryDBStore

_OPTIONAL_TEST_DEPS = {
    "fastapi",
    "starlette",
    "pydantic",
}


def _skip_only_missing_optional_dep(exc: Exception, *, context: str) -> None:
    if isinstance(exc, ModuleNotFoundError) and getattr(exc, "name", "") in _OPTIONAL_TEST_DEPS:
        pytest.skip(f"{context}: missing optional dependency '{exc.name}'")
    raise exc


class _SearchDBSpy:
    _TABLES_WITH_DELETED_AT: ClassVar[set[str]] = {
        "registry_agents",
        "registry_tools",
        "registry_skills",
        "registry_workflows",
    }

    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []
        self.executed_sql: list[str] = []
        self.executed_params: list[tuple] = []
        self._db = RegistryDBStore("postgres://unused")
        self._db._column_exists = lambda table, column: (
            (column == "deleted_at" and table in self._TABLES_WITH_DELETED_AT)
            or (column == "account_id" and table == "registry_skills")
        )

        @contextmanager
        def _cursor():
            outer = self

            class _Cursor:
                def execute(self, sql, params=None):
                    outer.executed_sql.append(str(sql))
                    outer.executed_params.append(tuple(params or ()))

                def fetchall(self):
                    return []

            yield _Cursor()

        self._db._cursor = _cursor

    def search_entities(self, query: str, entity_type: str | None = None):
        self.calls.append((query, entity_type))
        return self._db.search_entities(query, entity_type=entity_type)

    def list_skills(
        self,
        enabled_only: bool = False,
        *,
        account_id: str | None = None,
    ):
        return self._db.list_skills(enabled_only=enabled_only, account_id=account_id)

    def list_agents(self, enabled_only: bool = False):
        return self._db.list_agents(enabled_only=enabled_only)

    def list_tools(self, enabled_only: bool = False):
        return self._db.list_tools(enabled_only=enabled_only)

    def list_workflows(self, enabled_only: bool = False):
        return self._db.list_workflows(enabled_only=enabled_only)


def _assert_deleted_filter_present(spy: _SearchDBSpy) -> None:
    assert spy.executed_sql, "Expected at least one SQL query to execute"
    assert "deleted_at IS NULL" in spy.executed_sql[0]


def test_api_agents_search_excludes_deleted_rows():
    try:
        import core.entrypoints.admin.agents as agent_routes
    except Exception as exc:
        _skip_only_missing_optional_dep(exc, context="agent routes import")

    mock_request = MagicMock()
    mock_request.state.auth_user = None  # unauthenticated — skip auth enforcement
    spy = _SearchDBSpy()
    with patch.object(agent_routes, "_get_db", return_value=spy):
        rows = agent_routes.list_agents(mock_request, search="dev")

    assert rows == []
    assert spy.calls == [("dev", "agent")]
    _assert_deleted_filter_present(spy)


def test_api_workflows_search_excludes_deleted_rows():
    try:
        import core.entrypoints.admin.workflows as workflow_routes
    except Exception as exc:
        _skip_only_missing_optional_dep(exc, context="workflow routes import")

    spy = _SearchDBSpy()
    mock_request = MagicMock()
    mock_request.state.auth_user = None
    with patch.object(workflow_routes, "_get_db", return_value=spy):
        rows = workflow_routes.list_workflows(mock_request, search="wf")

    assert rows == []
    assert spy.calls == [("wf", "workflow")]
    _assert_deleted_filter_present(spy)


def test_api_skills_search_excludes_deleted_rows():
    try:
        import core.entrypoints.admin.skills as skill_routes
    except Exception as exc:
        _skip_only_missing_optional_dep(exc, context="skill routes import")

    spy = _SearchDBSpy()
    mock_request = MagicMock()
    mock_request.state.auth_user = {"account_id": "acc-owner"}
    with patch.object(skill_routes, "_get_db", return_value=spy):
        rows = skill_routes.list_skills(mock_request, search="proofreader")

    assert rows == []
    assert spy.calls == []
    _assert_deleted_filter_present(spy)
    assert "account_id = %(account_id)s" in spy.executed_sql[0]


def test_api_changelog_search_excludes_deleted_rows():
    try:
        import core.entrypoints.admin.changelog as changelog_routes
    except Exception as exc:
        _skip_only_missing_optional_dep(exc, context="changelog routes import")

    spy = _SearchDBSpy()
    mock_request = MagicMock()
    mock_request.state.auth_user = None  # unauthenticated — skip admin enforcement
    with patch.object(changelog_routes, "_get_db", return_value=spy):
        rows = changelog_routes.search_entities(mock_request, q="search", entity_type="tool")

    assert rows == []
    assert spy.calls == [("search", "tool")]
    _assert_deleted_filter_present(spy)


def test_api_tools_search_excludes_deleted_rows():
    try:
        import core.entrypoints.admin.tools as tool_routes
    except Exception as exc:
        _skip_only_missing_optional_dep(exc, context="tools routes import")

    spy = _SearchDBSpy()
    service = ToolQueryService(
        get_db=lambda: spy,
        get_registry=lambda: MagicMock(),
        get_tool_validation=lambda *_args, **_kwargs: {"valid": True, "errors": [], "warnings": []},
    )

    with patch.object(tool_routes, "_tool_query_service", return_value=service):
        rows = tool_routes.list_tools(
            request=None,
            enabled_only=False,
            category=None,
            search="search",
        )

    assert rows == []
    assert spy.calls == [("search", "tool")]
    _assert_deleted_filter_present(spy)


class _CanonicalToolsDB:
    """DB double returning canonical tool rows plus a generic search projection."""

    _ROWS: ClassVar[list[dict]] = [
        {
            "tool_id": "document_search",
            "display_name": "Document Search",
            "description": "search documents",
            "category": "search",
            "enabled": True,
            "scope_kind": "global",
            "project_id": None,
            "visible_in_catalog": True,
        },
        {
            "tool_id": "http_request",
            "display_name": "HTTP Request",
            "description": "call an endpoint",
            "category": "network",
            "enabled": False,
            "scope_kind": "global",
            "project_id": None,
            "visible_in_catalog": True,
        },
    ]

    def list_tools(self, enabled_only: bool = False):
        return [dict(row) for row in self._ROWS if not enabled_only or row["enabled"]]

    def search_entities(self, query: str, entity_type: str | None = None):
        # Mirrors the real projection: entity_id, no tool_id, few columns.
        return [
            {"entity_id": row["tool_id"], "display_name": row["display_name"]}
            for row in self._ROWS
            if query.lower() in row["tool_id"]
        ]


def _tool_service(db):
    return ToolQueryService(
        get_db=lambda: db,
        get_registry=lambda: MagicMock(),
        get_tool_validation=lambda *_a, **_k: {"valid": True, "errors": [], "warnings": []},
    )


def test_tools_search_returns_the_same_row_schema_as_an_unfiltered_list():
    """B16 regression.

    ?search= was served straight from search_entities, a generic cross-entity
    projection. The response silently changed shape: 11 fields disappeared and
    tool_id came back null, so any client that searched got unusable rows.
    """
    db = _CanonicalToolsDB()
    service = _tool_service(db)

    unfiltered = [row.dump() for row in service.list_tools().tools]
    searched = [row.dump() for row in service.list_tools(search="document").tools]

    assert unfiltered
    assert searched
    assert set(searched[0]) == set(unfiltered[0]), "search changed the row schema"
    assert all(row.get("tool_id") for row in searched), "search returned rows without tool_id"
    assert [row["tool_id"] for row in searched] == ["document_search"]


def test_tools_search_still_honours_enabled_only():
    """The search branch bypassed enabled_only entirely, returning disabled tools."""
    service = _tool_service(_CanonicalToolsDB())

    rows = service.list_tools(enabled_only=True, search="http").tools

    assert [row.dump()["tool_id"] for row in rows] == []


def test_public_agent_skill_and_tool_searches_include_active_extension_projections():
    import core.entrypoints.admin.agents as agent_routes
    import core.entrypoints.admin.skills as skill_routes
    from core.application.registry import projected_artifact_rows as projection_rows

    projected = {
        "agent": {
            "agent_id": "extension.install-1.showcase_agent",
            "display_name": "Showcase Agent",
            "description": "Sample catalogue agent",
            "enabled": True,
        },
        "skill": {
            "skill_id": "extension.install-1.showcase_skill",
            "display_name": "Showcase Skill",
            "description": "Sample catalogue skill",
            "enabled": True,
        },
        "tool": {
            "tool_id": "extension.install-1.showcase_tool",
            "display_name": "Showcase Tool",
            "description": "Sample catalogue tool",
            "enabled": True,
        },
    }
    db = MagicMock()
    db.list_agents.return_value = []
    db.list_skills.return_value = []
    db.list_tools.return_value = []
    db.search_entities.return_value = []
    request = MagicMock()
    request.state.auth_user = None

    with (
        patch.object(agent_routes, "_get_db", return_value=db),
        patch.object(skill_routes, "_get_db", return_value=db),
        patch.object(
            projection_rows,
            "projected_artifact_rows",
            side_effect=lambda _db, kind, _id_key, **_kwargs: [dict(projected[kind])],
        ),
    ):
        agents = agent_routes.list_agents(request, search="showcase")
        skills = skill_routes.list_skills(request, search="showcase")
        tools = _tool_service(db).list_tools(search="showcase").tools

    assert [row["agent_id"] for row in agents] == [projected["agent"]["agent_id"]]
    assert [row["skill_id"] for row in skills] == [projected["skill"]["skill_id"]]
    assert [row.tool_id for row in tools] == [projected["tool"]["tool_id"]]


def test_public_agent_skill_and_tool_details_resolve_active_extension_projections():
    import core.entrypoints.admin.agents as agent_routes
    import core.entrypoints.admin.skills as skill_routes
    from core.application.registry import projected_artifact_rows as projection_rows

    projected = {
        "agent": {"agent_id": "extension.install-1.agent", "enabled": True},
        "skill": {"skill_id": "extension.install-1.skill", "enabled": True},
        "tool": {"tool_id": "extension.install-1.tool", "enabled": True},
    }
    db = MagicMock()
    db.get_agent.return_value = None
    db.get_skill.return_value = None
    db.get_tool.return_value = None
    request = MagicMock()
    request.state.auth_user = None

    with (
        patch.object(agent_routes, "_get_db", return_value=db),
        patch.object(skill_routes, "_get_db", return_value=db),
        patch.object(
            projection_rows,
            "projected_artifact_rows",
            side_effect=lambda _db, kind, _id_key, **_kwargs: [dict(projected[kind])],
        ),
    ):
        agent = agent_routes.get_agent(projected["agent"]["agent_id"], request)
        skill = skill_routes.get_skill(projected["skill"]["skill_id"], request)
        tool = _tool_service(db).get_tool(projected["tool"]["tool_id"])

    assert agent["agent_id"] == projected["agent"]["agent_id"]
    assert skill["skill_id"] == projected["skill"]["skill_id"]
    assert tool.tool_id == projected["tool"]["tool_id"]
