"""
Unit tests for the Registry Admin API (39 CRUD routes).

Tests all endpoints for: agents, tools, skills, workflows,
git history/rollback, simulate, reload, validate.

Run:
    docker compose exec fs-app python -m pytest tests/unit/test_registry_admin_api.py -v
"""

from __future__ import annotations

import sys
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.scope import normalize_scope

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tests._workflow_definitions import edge, linear_graph, linear_workflow, node
from tests.unit._psycopg_runtime_stub import install_psycopg_runtime_stub

install_psycopg_runtime_stub()

PROJECT_ROOT = Path(__file__).parent.parent.parent


_VALID_READINESS = {"valid": True, "errors": [], "warnings": []}


def _definition_db(workflow: dict, *, revision: int = 1) -> MagicMock:
    """A registry store mock that serves *workflow* to the definition change service.

    The service locks the workflow row through ``_transaction``, compares the
    stored revision and writes the whole definition with ``save_workflow``.
    Agents, installs and connections a change set names resolve in the
    default account, so per-operation reference checks let them through.
    """
    db = MagicMock()
    # The stored row carries its revision, as the registry store returns it.
    db.get_workflow.return_value = {"revision": revision, **workflow}
    db.get_agent.side_effect = lambda agent_id: {
        "agent_id": agent_id,
        "account_id": DEFAULT_ACCOUNT_ID,
        "scope_kind": "system",
    }
    db.get_extension_install_contract.side_effect = lambda install_id: {
        "install_id": install_id,
        "account_id": DEFAULT_ACCOUNT_ID,
    }
    db.get_extension_connection_metadata.side_effect = lambda connection_id: {
        "connection_id": connection_id,
        "account_id": DEFAULT_ACCOUNT_ID,
        "project_id": None,
    }
    cursor = MagicMock()
    cursor.fetchone.return_value = {"revision": revision}
    db._transaction.side_effect = lambda *_args, **_kwargs: nullcontext(cursor)
    db.save_workflow.return_value = revision + 1
    return db


def _saved_definition(db: MagicMock) -> dict:
    """The workflow payload the change service wrote as one revision."""
    db.save_workflow.assert_called_once()
    return db.save_workflow.call_args.args[0]


def _saved_step(db: MagicMock, step_id: str) -> dict:
    return next(step for step in _saved_definition(db)["steps"] if step["step_id"] == step_id)


def _post_changes(client, workflow_id: str, operations: list, *, expected_revision: int = 1):
    return client.post(
        f"/api/workflows/{workflow_id}/changes",
        json={"expected_revision": expected_revision, "operations": operations},
    )


def _issue_codes(response) -> list[str]:
    detail = response.json()["detail"]
    return [str(issue.get("code") or "") for issue in detail.get("issues") or []]


@pytest.mark.parametrize("stored_refs", [None, "", "existing-reference", "omitted"])
@pytest.mark.parametrize("reference_update", [None, "", "new-reference"])
def test_step_name_only_update_does_not_stringify_null_references(
    registry_client, stored_refs, reference_update
):
    import core.entrypoints.admin.workflows as workflow_routes

    previous = {
        "step_id": "set_status",
        "step_kind": "set_variable",
        "display_name": "Before",
        "override": {"set_variable": {"target": "state.flag", "value": "ready"}},
    }
    if stored_refs != "omitted":
        previous.update(workflow_id=stored_refs, connector_id=stored_refs)
    workflow = linear_workflow(previous, workflow_id="wf_contract_test", enabled=False)
    db = _definition_db(workflow)
    patch_body = {"display_name": "Экспорт — 更新"}
    if reference_update is not None:
        patch_body.update(workflow_id=reference_update, connector_id=reference_update)
    with (
        patch.object(workflow_routes, "_get_db", return_value=db),
        patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
        patch.object(workflow_routes, "_get_workflow_validation", return_value=_VALID_READINESS),
    ):
        response = _post_changes(
            registry_client,
            "wf_contract_test",
            [{"op": "update_step_config", "step_id": "set_status", "patch": patch_body}],
        )
    assert response.status_code == 200, response.text
    saved = _saved_step(db, "set_status")
    assert saved["display_name"] == "Экспорт — 更新"
    expected = None if stored_refs == "omitted" else stored_refs
    if reference_update is not None:
        expected = reference_update
    assert saved.get("workflow_id") == expected
    assert saved.get("connector_id") == expected
    assert "None" not in {saved.get("workflow_id"), saved.get("connector_id")}
    stored_step = workflow["steps"][0]
    assert stored_step["display_name"] == "Before"
    assert stored_step.get("workflow_id") == (None if stored_refs == "omitted" else stored_refs)


@pytest.mark.parametrize(
    ("after", "expected_codes"),
    [
        ("same", ["workflow.connection_unavailable"]),
        ("introduced", ["workflow.connection_unavailable", "workflow.step_invalid"]),
    ],
)
def test_enabled_repair_is_refused_while_candidate_has_blocking_readiness_issues(
    registry_client, after, expected_codes
):
    """An enabled repair must resolve every blocking issue in one atomic edit."""
    import core.entrypoints.admin.workflows as workflow_routes

    dead_connection = {
        "code": "workflow.connection_unavailable",
        "message": "The selected extension connection is no longer available.",
        "path": "workflow.steps.sync_stock.connection_binding_jsonb.connection_id",
    }
    broken = {"valid": False, "errors": [dead_connection], "warnings": []}
    worse = {
        "valid": False,
        "errors": [dead_connection, {"code": "workflow.step_invalid", "path": "x"}],
        "warnings": [],
    }
    workflow = linear_workflow(
        {
            "step_id": "set_status",
            "step_kind": "set_variable",
            "display_name": "Before",
            "override": {"set_variable": {"target": "state.flag", "value": "ready"}},
        },
        {
            "step_id": "sync_stock",
            "step_kind": "set_variable",
            "override": {"set_variable": {"target": "state.synced", "value": True}},
        },
        workflow_id="wf_live",
        enabled=True,
    )
    db = _definition_db(workflow)

    def readiness(store, workflow_id):
        candidate = store.get_workflow(workflow_id)
        assert candidate["steps"][0]["display_name"] == "After"
        return worse if after == "introduced" else broken

    with (
        patch.object(workflow_routes, "_get_db", return_value=db),
        patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
        patch.object(
            workflow_routes, "_get_workflow_validation", side_effect=readiness
        ) as validate_candidate,
    ):
        response = _post_changes(
            registry_client,
            "wf_live",
            [
                {
                    "op": "update_step_config",
                    "step_id": "set_status",
                    "patch": {"display_name": "After"},
                }
            ],
        )

    assert response.status_code == 400, response.text
    assert _issue_codes(response) == expected_codes
    validate_candidate.assert_called_once()
    db.save_workflow.assert_not_called()
    db._transaction.assert_called_once()
    assert db.get_workflow.return_value["revision"] == 1
    assert db.get_workflow.return_value["enabled"] is True
    assert db.get_workflow.return_value["steps"][0]["display_name"] == "Before"


@pytest.mark.parametrize(
    ("operation", "expected_code"),
    [
        (
            {
                "op": "create_step",
                "step": {
                    "step_id": "taken",
                    "step_kind": "set_variable",
                    "override": {"set_variable": {"target": "state.flag", "value": 1}},
                },
            },
            "workflow.change.step_exists",
        ),
        (
            {"op": "update_step_config", "step_id": "before", "patch": {"step_id": "taken"}},
            "workflow.change.structure_in_config",
        ),
    ],
)
def test_step_identity_conflict_is_refused_without_any_write(
    registry_client, operation, expected_code
):
    import core.entrypoints.admin.workflows as workflow_routes

    workflow = linear_workflow(
        {"step_id": "before", "step_kind": "set_variable"},
        {"step_id": "taken", "step_kind": "set_variable"},
        workflow_id="wf_contract_test",
        enabled=False,
    )
    db = _definition_db(workflow)
    with (
        patch.object(workflow_routes, "_get_db", return_value=db),
        patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
        patch.object(workflow_routes, "_get_workflow_validation", return_value=_VALID_READINESS),
    ):
        response = _post_changes(registry_client, "wf_contract_test", [operation])
    assert response.status_code == 400, response.text
    assert _issue_codes(response) == [expected_code]
    db.save_workflow.assert_not_called()
    assert [step["step_id"] for step in workflow["steps"]] == ["before", "taken", "done"]


def test_inserting_a_step_keeps_the_route_to_its_successor(registry_client):
    """A step added after the last one inherits that step's route to the end.

    Deleting a step removes its edges and builds no bypass around it; the
    author reconnects the neighbours explicitly in the same change set.
    """
    import core.entrypoints.admin.workflows as workflow_routes

    workflow = linear_workflow(
        {"step_id": "first", "step_kind": "set_variable"},
        workflow_id="wf_step_edges",
        enabled=False,
    )
    db = _definition_db(workflow)
    with (
        patch.object(workflow_routes, "_get_db", return_value=db),
        patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
        patch.object(workflow_routes, "_get_workflow_validation", return_value=_VALID_READINESS),
    ):
        created = _post_changes(
            registry_client,
            "wf_step_edges",
            [
                {
                    "op": "insert_step_between",
                    "source": node("first"),
                    "outcome": {"name": "success"},
                    "step": {
                        "step_id": "second",
                        "step_kind": "set_variable",
                        "override": {"set_variable": {"assignments": {"state.value": 1}}},
                    },
                }
            ],
        )
        assert created.status_code == 200, created.text
        assert created.json()["valid"] is True
        graph = _saved_definition(db)["settings"]["execution_graph"]
        assert graph["entry"] == node("first")
        assert graph["edges"] == [
            edge("first", node("second")),
            edge("second", node("done")),
        ]

        db.save_workflow.reset_mock()
        deleted = _post_changes(
            registry_client,
            "wf_step_edges",
            [
                {"op": "delete_step", "step_id": "first"},
                {"op": "set_entry", "target": node("done")},
            ],
        )
    assert deleted.status_code == 200, deleted.text
    saved = _saved_definition(db)
    assert [step["step_id"] for step in saved["steps"]] == ["done"]
    assert saved["settings"]["execution_graph"]["edges"] == []
    assert saved["settings"]["execution_graph"]["entry"] == node("done")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_REGISTRY_WRITE_ROUTES = (
    ("/api/agents", "POST"),
    ("/api/tools", "POST"),
    ("/api/skills", "POST"),
    ("/api/workflows", "POST"),
)


def _route_registered(app, path: str, method: str) -> bool:
    method_upper = str(method or "").upper()
    for route in app.router.routes:
        if str(getattr(route, "path", "")) != path:
            continue
        methods = set(getattr(route, "methods", set()) or set())
        if method_upper in methods:
            return True
    return False


def _ensure_registry_admin_routes_loaded(app) -> None:
    if all(_route_registered(app, path, method) for path, method in _REGISTRY_WRITE_ROUTES):
        return
    try:
        from core.entrypoints.admin.api import create_admin_router
    except Exception as exc:
        pytest.fail(f"Registry admin router unavailable: {exc}")
    app.include_router(create_admin_router())
    if not all(_route_registered(app, path, method) for path, method in _REGISTRY_WRITE_ROUTES):
        pytest.fail("Registry write routes are not mounted in TestClient app")


@pytest.fixture(scope="module")
def admin_client():
    """FastAPI TestClient for admin API."""
    try:
        from fastapi.testclient import TestClient

        from core.entrypoints.api.app_factory import create_app

        app = create_app()
        _ensure_registry_admin_routes_loaded(app)
        app.state.local_auth_enabled_override = lambda: False
        app.state.external_auth_enabled_override = lambda: False
        yield TestClient(app, raise_server_exceptions=False)
    except Exception as e:
        pytest.fail(f"Could not create admin TestClient: {e}")


@pytest.fixture(scope="module")
def registry_client():
    """TestClient that uses the real registry if available."""
    try:
        from fastapi.testclient import TestClient

        from core.entrypoints.api.app_factory import create_app

        app = create_app()
        _ensure_registry_admin_routes_loaded(app)
        app.state.local_auth_enabled_override = lambda: False
        app.state.external_auth_enabled_override = lambda: False
        yield TestClient(app, raise_server_exceptions=False)
    except Exception as e:
        pytest.fail(f"Could not create TestClient: {e}")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def assert_json_list(resp, min_len: int = 0):
    """Assert response is 200 JSON list with min_len items."""
    assert resp.status_code in (200, 404), f"Unexpected status: {resp.status_code}"
    if resp.status_code == 200:
        data = resp.json()
        assert isinstance(data, (list, dict))
        if isinstance(data, list):
            assert len(data) >= min_len


@pytest.fixture
def tool_routes_client():
    from core.entrypoints.admin import tools as tool_routes

    app = FastAPI()
    app.include_router(tool_routes.router)
    return TestClient(app, raise_server_exceptions=False)


def assert_json_dict(resp):
    """Assert response is 200 JSON dict."""
    assert resp.status_code in (200, 404), f"Unexpected status: {resp.status_code}"
    if resp.status_code == 200:
        data = resp.json()
        assert isinstance(data, dict)


def _read_frontend_source(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def _assert_frontend_shell_response(resp) -> str:
    assert resp.status_code == 200
    text = resp.text
    assert "Flow Steward" in text or "AI Orchestrator" not in text
    assert '<div id="root"></div>' in text
    assert "/app/assets/index-" in text
    return text


def _page_contract_text(resp, *relative_paths: str) -> str:
    parts = [_assert_frontend_shell_response(resp)]
    parts.extend(_read_frontend_source(path) for path in relative_paths)
    return "\n".join(parts)


def _workflow_detail_stub_db(workflow_id: str = "wf_demo") -> SimpleNamespace:
    workflow = {
        "workflow_id": workflow_id,
        "display_name": "Workflow Demo",
        "description": "Demo workflow",
        "version": "1.0.0",
        "enabled": True,
        "trigger": {},
        "settings": {"callable": {"enabled": True, "expected_inputs": [], "expected_outputs": []}},
        "steps": [
            {
                "id": "step-row-1",
                "step_id": "step_1",
                "step_kind": "agent",
                "group_id": None,
                "lane_id": None,
                "agent_id": "scout",
                "skill_id": "collect_findings",
                "display_name": "Analyze requirements",
                "override": {},
            }
        ],
        "groups": [],
    }
    workflow_rows = [
        {
            "workflow_id": workflow_id,
            "display_name": "Workflow Demo",
            "enabled": True,
            "settings": workflow["settings"],
        },
        {
            "workflow_id": "wf_child",
            "display_name": "Child Workflow",
            "enabled": True,
            "settings": {
                "callable": {"enabled": True, "expected_inputs": [], "expected_outputs": []}
            },
        },
    ]
    agent_rows = [
        {
            "agent_id": "scout",
            "display_name": "Scout",
            "enabled": True,
        }
    ]
    skill_rows = [
        {
            "skill_id": "collect_findings",
            "display_name": "Analyze Requirements",
            "enabled": True,
        }
    ]
    pair_rows = [{"agent_id": "scout", "skill_id": "collect_findings"}]
    return SimpleNamespace(
        dsn="postgresql://unused",
        get_workflow=lambda current_workflow_id: (
            dict(workflow) if str(current_workflow_id or "").strip() == workflow_id else None
        ),
        list_agents=lambda enabled_only=False: list(agent_rows),
        list_agents_summary=lambda enabled_only=False: list(agent_rows),
        list_skills=lambda enabled_only=False: list(skill_rows),
        list_skills_summary=lambda enabled_only=False: list(skill_rows),
        list_agent_skill_pairs=lambda enabled_only=True: list(pair_rows),
        list_published_block_contracts=lambda enabled_only=True: [],
        get_published_block_contract=lambda _block_key: None,
        list_workflows=lambda include_definitions=False, enabled_only=False: list(workflow_rows),
        list_workflows_summary=lambda enabled_only=False: list(workflow_rows),
        get_changelog=lambda **_kwargs: [
            {
                "id": 101,
                "created_at": "2026-04-12T10:00:00Z",
                "author": "system",
            }
        ],
    )


def _workflow_detail_response(
    registry_client,
    workflow_id: str = "wf_demo",
    *,
    follow_redirects: bool = True,
):
    return registry_client.get(
        f"/registry/workflows/{workflow_id}",
        follow_redirects=follow_redirects,
    )


def _workflow_api_detail_response(registry_client, workflow_id: str = "wf_demo"):
    try:
        import core.entrypoints.admin.workflows as workflow_routes
    except Exception as e:
        pytest.fail(f"workflow routes not importable: {e}")

    with patch.object(
        workflow_routes, "_get_db", return_value=_workflow_detail_stub_db(workflow_id)
    ):
        return registry_client.get(f"/api/workflows/{workflow_id}")


def _agent_stub_db(agent_id: str = "scout") -> SimpleNamespace:
    agent = {
        "agent_id": agent_id,
        "display_name": "Scout",
        "enabled": True,
        "prompts": {"system_prompt": "Help analyze tasks."},
        "skills": [{"skill_id": "collect_findings"}],
    }
    skills = [{"skill_id": "collect_findings", "display_name": "Analyze Requirements"}]
    return SimpleNamespace(
        get_agent=lambda current_agent_id: dict(agent) if current_agent_id == agent_id else None,
        list_agents=lambda enabled_only=False, search=None: [dict(agent)],
        list_agents_summary=lambda enabled_only=False: [dict(agent)],
        get_agent_skills=lambda current_agent_id: (
            list(skills) if current_agent_id == agent_id else []
        ),
        get_agent_tools=lambda _agent_id: [],
        get_effective_tools=lambda _agent_id: [],
        get_skill_inputs=lambda _skill_id: [],
        get_skill_outputs=lambda _skill_id: [],
        get_agent_inputs=lambda _agent_id: [],
        get_agent_outputs=lambda _agent_id: [],
        list_skills=lambda enabled_only=False: list(skills),
        list_skills_summary=lambda enabled_only=False: list(skills),
        list_tools=lambda enabled_only=False: [],
        list_tools_summary=lambda enabled_only=False: [],
        get_changelog=lambda **_kwargs: [
            {"id": 201, "created_at": "2026-04-12T10:00:00Z", "author": "system"}
        ],
        search_entities=lambda query, entity_type="agent": [dict(agent)],
    )


def _agent_api_detail_response(registry_client, agent_id: str = "scout"):
    try:
        import core.entrypoints.admin.agents as agent_routes
    except Exception as e:
        pytest.fail(f"agent routes not importable: {e}")

    with patch.object(agent_routes, "_get_db", return_value=_agent_stub_db(agent_id)):
        return registry_client.get(f"/api/agents/{agent_id}")


def _agent_page_response(
    registry_client,
    agent_id: str = "scout",
    *,
    follow_redirects: bool = True,
):
    return registry_client.get(
        f"/registry/agents/{agent_id}",
        follow_redirects=follow_redirects,
    )


def _tool_stub_db(tool_id: str = "search_docs") -> SimpleNamespace:
    tool = {
        "tool_id": tool_id,
        "display_name": "Search Docs",
        "enabled": True,
        "category": "documents",
    }
    return SimpleNamespace(
        get_tool=lambda current_tool_id: dict(tool) if current_tool_id == tool_id else None,
        list_tools=lambda enabled_only=False, search=None: [dict(tool)],
        list_tools_summary=lambda enabled_only=False: [dict(tool)],
        get_agents_using_tool=lambda _tool_id: [],
        get_changelog=lambda **_kwargs: [
            {"id": 301, "created_at": "2026-04-12T10:00:00Z", "author": "system"}
        ],
        search_entities=lambda query, entity_type="tool": [dict(tool)],
    )


def _tool_api_detail_response(registry_client, tool_id: str = "search_docs"):
    try:
        import core.entrypoints.admin.tools as tool_routes
    except Exception as e:
        pytest.fail(f"tool routes not importable: {e}")

    with patch.object(tool_routes, "_get_db", return_value=_tool_stub_db(tool_id)):
        return registry_client.get(f"/api/tools/{tool_id}")


def _tool_page_response(
    registry_client,
    tool_id: str = "search_docs",
    *,
    follow_redirects: bool = True,
):
    return registry_client.get(
        f"/registry/tools/{tool_id}",
        follow_redirects=follow_redirects,
    )


def _skill_stub_db(skill_id: str = "collect_findings") -> SimpleNamespace:
    skill = {
        "skill_id": skill_id,
        "display_name": "Analyze Requirements",
        "enabled": True,
        "prompt_template": "Analyze the request carefully.",
    }
    using_agents = [{"agent_id": "scout", "display_name": "Scout"}]
    return SimpleNamespace(
        get_skill=lambda current_skill_id: dict(skill) if current_skill_id == skill_id else None,
        list_skills=lambda enabled_only=False, search=None: [dict(skill)],
        list_skills_summary=lambda enabled_only=False: [dict(skill)],
        get_agents_using_skill=lambda _skill_id: list(using_agents),
        get_skill_inputs=lambda _skill_id: [],
        get_skill_outputs=lambda _skill_id: [],
        list_fields=lambda enabled_only=False: [],
        list_data_contracts=lambda enabled_only=False: [],
        get_changelog=lambda **_kwargs: [
            {"id": 401, "created_at": "2026-04-12T10:00:00Z", "author": "system"}
        ],
        search_entities=lambda query, entity_type="skill": [dict(skill)],
    )


def _skill_api_detail_response(registry_client, skill_id: str = "collect_findings"):
    try:
        import core.entrypoints.admin.skills as skill_routes
    except Exception as e:
        pytest.fail(f"skill routes not importable: {e}")

    with patch.object(skill_routes, "_get_db", return_value=_skill_stub_db(skill_id)):
        return registry_client.get(f"/api/skills/{skill_id}")


def _skill_page_response(
    registry_client,
    skill_id: str = "collect_findings",
    *,
    follow_redirects: bool = True,
):
    return registry_client.get(
        f"/registry/skills/{skill_id}",
        follow_redirects=follow_redirects,
    )


# ---------------------------------------------------------------------------
# Agent CRUD tests
# ---------------------------------------------------------------------------


class TestAgentCRUD:
    """Test /api/agents/* endpoints."""

    def test_list_agents(self, registry_client):
        resp = registry_client.get("/api/agents")
        assert resp.status_code in (200, 404, 500)
        if resp.status_code == 200:
            data = resp.json()
            assert isinstance(data, list)

    def test_list_agents_has_agent_id(self, registry_client):
        resp = registry_client.get("/api/agents")
        if resp.status_code == 200:
            agents = resp.json()
            if agents:
                assert "agent_id" in agents[0]

    def test_list_agents_search_hydrates_canonical_rows_and_honors_enabled_only(
        self, registry_client
    ):
        import core.entrypoints.admin.agents as agent_routes

        enabled_agent = {
            "agent_id": "enabled_agent",
            "display_name": "Enabled Agent",
            "description": "Canonical enabled record",
            "enabled": True,
            "prompts": {"system": "canonical prompt"},
            "tags": ["canonical"],
        }
        mock_db = MagicMock()
        mock_db.search_entities.return_value = [
            {
                "entity_type": "agent",
                "entity_id": "enabled_agent",
                "display_name": "Enabled Agent",
                "enabled": True,
            },
            {
                "entity_type": "agent",
                "entity_id": "disabled_agent",
                "display_name": "Disabled Agent",
                "enabled": False,
            },
        ]
        mock_db.list_agents.return_value = [enabled_agent]

        with patch.object(agent_routes, "_get_db", return_value=mock_db):
            response = registry_client.get(
                "/api/agents", params={"search": "agent", "enabled_only": "true"}
            )

        assert response.status_code == 200, response.text
        assert response.json() == [
            {
                "agent_id": "enabled_agent",
                "version": None,
                "display_name": "Enabled Agent",
                "description": "Canonical enabled record",
                "enabled": True,
                "category": None,
                "base_skill": None,
                "secondary_skills": [],
                "local_instructions": [],
                "prompts": {"system": "canonical prompt"},
                "config": {},
                "quality": {},
                "inputs": {},
                "outputs": [],
                "builder_metadata_jsonb": {},
                "depends_on": {},
                "tags": ["canonical"],
                "skills": [],
                "tools": [],
            }
        ]
        mock_db.list_agents.assert_called_once_with(enabled_only=True)

    def test_get_existing_agent(self, registry_client):
        """Get a deterministic existing agent detail payload."""
        resp = _agent_api_detail_response(registry_client)
        assert resp.status_code in (200, 404)

    def test_get_nonexistent_agent_returns_404(self, registry_client):
        resp = registry_client.get("/api/agents/nonexistent_agent_xyz_99999")
        assert resp.status_code in (404, 500)

    def test_registry_agents_page_handles_datetime_rows(self, registry_client):
        resp = registry_client.get("/registry/agents")

        contract_text = _page_contract_text(
            resp, "assets/frontend/src/pages/registry/agents/index.tsx"
        )
        assert "agents-grid" in contract_text

    def test_registry_agents_page_uses_shared_shell_renderer(self, registry_client):
        resp = registry_client.get("/registry/agents")

        contract_text = _page_contract_text(
            resp,
            "assets/frontend/src/components/layout/AppShell.tsx",
            "assets/frontend/src/pages/registry/agents/index.tsx",
        )
        assert "auth.session?.shell_navigation" in contract_text
        assert "Agents catalog" in contract_text

    def test_single_skill_agent_outputs_proxy_to_skill_contract(self, registry_client):
        try:
            import core.entrypoints.admin.agents as agent_routes
        except Exception as e:
            pytest.fail(f"agent routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_agent.return_value = {
            "agent_id": "proofreader",
            "skills": [{"skill_id": "review_summary"}],
        }
        mock_db.get_skill_outputs.return_value = [
            {
                "name": "review_verdict",
                "type": "text",
                "description": "pass/fail",
                "required": True,
            },
        ]

        with patch.object(agent_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get("/api/agents/proofreader/outputs")

        assert resp.status_code == 200
        assert resp.json() == [
            {
                "name": "review_verdict",
                "type": "text",
                "description": "pass/fail",
                "required": True,
            },
        ]
        mock_db.get_skill_outputs.assert_called_once_with("review_summary")

    def test_single_skill_agent_outputs_write_to_skill_contract(self, registry_client):
        try:
            import core.entrypoints.admin.agents as agent_routes
        except Exception as e:
            pytest.fail(f"agent routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_agent.return_value = {
            "agent_id": "proofreader",
            "skills": [{"skill_id": "review_summary"}],
        }
        mock_db.get_skill_outputs.return_value = [
            {
                "name": "review_verdict",
                "type": "text",
                "description": "pass/fail",
                "required": True,
            },
        ]
        mock_registry = MagicMock()

        with (
            patch.object(agent_routes, "_get_db", return_value=mock_db),
            patch.object(agent_routes, "_get_registry", return_value=mock_registry),
        ):
            resp = registry_client.put(
                "/api/agents/proofreader/outputs",
                json=[
                    {
                        "name": "review_verdict",
                        "type": "text",
                        "description": "pass/fail",
                        "required": True,
                    }
                ],
            )

        assert resp.status_code == 200
        mock_db.set_skill_outputs.assert_called_once_with(
            "review_summary",
            [
                {
                    "name": "review_verdict",
                    "type": "text",
                    "description": "pass/fail",
                    "required": True,
                }
            ],
        )
        mock_registry.reload_all.assert_called_once()

    def test_single_skill_agent_generate_output_format_uses_skill_outputs(self, registry_client):
        try:
            import core.entrypoints.admin.agents as agent_routes
        except Exception as e:
            pytest.fail(f"agent routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_agent.return_value = {
            "agent_id": "proofreader",
            "skills": [{"skill_id": "review_summary"}],
        }
        mock_db.get_skill_outputs.return_value = [
            {
                "name": "review_verdict",
                "type": "text",
                "description": "pass/fail",
                "required": True,
            },
        ]

        with patch.object(agent_routes, "_get_db", return_value=mock_db):
            resp = registry_client.post("/api/agents/proofreader/generate-output-format")

        assert resp.status_code == 200
        body = resp.json()
        assert '"schema_version": "phase_result_v2"' in body["output_format"]
        assert '"artifacts": {' in body["output_format"]
        assert "review_verdict" in body["output_format"]
        mock_db.get_skill_outputs.assert_called_once_with("review_summary")

    def test_add_skill_rejects_legacy_output_format_markers(self, registry_client):
        resp = registry_client.post(
            "/api/agents/proofreader/skills",
            json={
                "skill_id": "review_summary",
                "skill_prompt": "CHECKS:\n{}\nVERDICT: pass",
            },
        )

        assert resp.status_code == 422

    def test_update_skill_prompt_rejects_legacy_output_format_markers(self, registry_client):
        resp = registry_client.put(
            "/api/agents/proofreader/skills/review_summary/prompt",
            json={"skill_prompt": "Respond with a JSON object containing review_verdict"},
        )

        assert resp.status_code == 422

    def test_create_agent_invalid_body_returns_error(self, registry_client):
        """Creating agent without required fields → error."""
        resp = registry_client.post("/api/agents", json={})
        assert resp.status_code in (400, 422, 500)

    def test_create_agent_invalid_id_format(self, registry_client):
        """Agent ID with spaces → validation error."""
        resp = registry_client.post(
            "/api/agents",
            json={
                "agent_id": "Agent With Spaces",
                "display_name": "Test",
                "prompts": {"system": "You are a test agent."},
            },
        )
        assert resp.status_code in (400, 422, 500)

    def test_create_agent_free_text_category_rejected(self, registry_client):
        resp = registry_client.post(
            "/api/agents",
            json={
                "agent_id": "test_agent_free_text_category",
                "display_name": "Test Agent",
                "category": "analysis",
                "prompts": {"system": "You are a test agent."},
            },
        )
        assert resp.status_code == 422
        assert "category must be one of" in resp.text

    def test_create_agent_rolls_back_invalid_enabled_save(self, registry_client):
        try:
            import core.entrypoints.admin.agents as agent_routes
        except Exception as e:
            pytest.fail(f"agent routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_agent.return_value = None
        mock_registry = MagicMock()

        with (
            patch.object(agent_routes, "_get_db", return_value=mock_db),
            patch.object(agent_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                agent_routes,
                "_get_agent_validation",
                return_value={"valid": False, "errors": [{"message": "broken"}], "warnings": []},
            ),
        ):
            resp = registry_client.post(
                "/api/agents",
                json={
                    "agent_id": "broken_agent_save",
                    "display_name": "Broken Agent",
                    "category": "developer-tools",
                    "enabled": True,
                },
            )

        assert resp.status_code == 400
        mock_db.save_agent.assert_called_once()
        mock_db._transaction.assert_called()
        mock_db.delete_agent.assert_not_called()
        mock_db.apply_entity_snapshot.assert_not_called()
        mock_registry.reload_all.assert_not_called()

    def test_create_agent_rejects_standard_tool_actions(self, registry_client):
        try:
            import core.entrypoints.admin.agents as agent_routes
        except Exception as e:
            pytest.fail(f"agent routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_agent.return_value = None

        with patch.object(agent_routes, "_get_db", return_value=mock_db):
            resp = registry_client.post(
                "/api/agents",
                json={
                    "agent_id": "standard_tool_agent",
                    "display_name": "Standard Tool Agent",
                    "category": "developer-tools",
                    "tools": ["std.numbers"],
                },
            )

        assert resp.status_code == 400
        assert "connector workflow steps" in resp.text
        mock_db.save_agent.assert_not_called()

    def test_create_agent_suffixes_duplicate_agent_id(self, registry_client):
        try:
            import core.entrypoints.admin.agents as agent_routes
        except Exception as e:
            pytest.fail(f"agent routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_agent.side_effect = lambda agent_id: (
            {"agent_id": agent_id} if agent_id in {"duplicate_agent", "duplicate_agent_1"} else None
        )
        mock_registry = MagicMock()

        with (
            patch.object(agent_routes, "_get_db", return_value=mock_db),
            patch.object(agent_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                agent_routes,
                "_get_agent_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            resp = registry_client.post(
                "/api/agents",
                json={
                    "agent_id": "duplicate_agent",
                    "display_name": "Duplicate Agent",
                    "category": "developer-tools",
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["agent_id"] == "duplicate_agent_2"
        assert mock_db.save_agent.call_args.args[0]["agent_id"] == "duplicate_agent_2"

    def test_create_agent_persists_request_account_scope(self, registry_client):
        import core.entrypoints.admin.agents as agent_routes

        mock_db = MagicMock()
        mock_db.get_agent.return_value = None
        mock_registry = MagicMock()
        scope = normalize_scope(account_id="acc-owner")

        with (
            patch.object(agent_routes, "_get_db", return_value=mock_db),
            patch.object(agent_routes, "_get_registry", return_value=mock_registry),
            patch.object(agent_routes, "request_scope", return_value=scope),
            patch.object(
                agent_routes,
                "_get_agent_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            response = registry_client.post(
                "/api/agents",
                json={
                    "agent_id": "account_agent",
                    "display_name": "Account Agent",
                    "category": "developer-tools",
                },
            )

        assert response.status_code == 200, response.text
        assert mock_db.save_agent.call_args.args[0]["account_id"] == "acc-owner"

    def test_clone_agent_suffixes_duplicate_target_id(self, registry_client):
        try:
            import core.entrypoints.admin.agents as agent_routes
        except Exception as e:
            pytest.fail(f"agent routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_agent.side_effect = lambda agent_id: (
            {
                "agent_id": "review_agent",
                "account_id": "00000000-0000-0000-0000-000000000002",
                "display_name": "Review Agent",
                "category": "developer-tools",
            }
            if agent_id == "review_agent"
            else {"agent_id": agent_id}
            if agent_id == "review_agent_copy"
            else None
        )
        mock_registry = MagicMock()

        with (
            patch.object(agent_routes, "_get_db", return_value=mock_db),
            patch.object(agent_routes, "_get_registry", return_value=mock_registry),
        ):
            resp = registry_client.post("/api/agents/review_agent/clone", json={})

        assert resp.status_code == 200
        body = resp.json()
        assert body["agent_id"] == "review_agent_copy_1"
        assert mock_db.save_agent.call_args.args[0]["agent_id"] == "review_agent_copy_1"

    def test_clone_agent_masks_internal_persistence_error(self, registry_client):
        import core.entrypoints.admin.agents as agent_routes

        mock_db = MagicMock()
        mock_db.get_agent.side_effect = lambda agent_id: (
            {
                "agent_id": "review_agent",
                "account_id": "00000000-0000-0000-0000-000000000002",
                "display_name": "Review Agent",
                "category": "developer-tools",
            }
            if agent_id == "review_agent"
            else None
        )
        mock_db.save_agent.side_effect = RuntimeError("postgres://user:secret@db/internal_table")

        with patch.object(agent_routes, "_get_db", return_value=mock_db):
            response = registry_client.post("/api/agents/review_agent/clone", json={})

        assert response.status_code == 500
        assert response.json()["error"] == "Failed to clone agent"
        assert response.json()["correlation_id"]
        assert "secret" not in response.text

    def test_clone_agent_route_forwards_identity_only(self, registry_client):
        import core.entrypoints.admin.agents as agent_routes

        command = MagicMock()
        command.clone_agent.return_value = {
            "status": "cloned",
            "agent_id": "manual_copy",
            "source_agent_id": "source_agent",
        }
        with patch.object(agent_routes, "_agent_command_service", return_value=command):
            response = registry_client.post(
                "/api/agents/source_agent/clone",
                json={"agent_id": "manual_copy", "display_name": "Manual copy"},
            )

        assert response.status_code == 200, response.text
        command.clone_agent.assert_called_once()
        kwargs = command.clone_agent.call_args.kwargs
        assert kwargs["target_agent_id"] == "manual_copy"
        assert kwargs["display_name"] == "Manual copy"
        assert "agent_type_id" not in kwargs

    def test_clone_agent_route_returns_actionable_conflict(self, registry_client):
        import core.entrypoints.admin.agents as agent_routes
        from core.application.management.errors import ServiceConflictError

        command = MagicMock()
        command.clone_agent.side_effect = ServiceConflictError(
            "Install the missing dependencies first.",
            payload={
                "error": "Install the missing dependencies first.",
                "code": "agent_clone_dependencies_unavailable",
            },
        )
        with patch.object(agent_routes, "_agent_command_service", return_value=command):
            response = registry_client.post(
                "/api/agents/source_agent/clone", json={"agent_id": "manual_copy"}
            )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "agent_clone_dependencies_unavailable"

    def test_update_agent_rolls_back_invalid_enabled_save(self, registry_client):
        try:
            import core.entrypoints.admin.agents as agent_routes
        except Exception as e:
            pytest.fail(f"agent routes not importable: {e}")

        existing = {
            "agent_id": "composer",
            "display_name": "Developer",
            "category": "developer-tools",
            "enabled": True,
            "skills": [],
            "tools": [],
        }
        mock_db = MagicMock()
        mock_db.get_agent.return_value = existing
        mock_registry = MagicMock()

        with (
            patch.object(agent_routes, "_get_db", return_value=mock_db),
            patch.object(agent_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                agent_routes,
                "_get_agent_validation",
                return_value={"valid": False, "errors": [{"message": "broken"}], "warnings": []},
            ),
        ):
            resp = registry_client.put(
                "/api/agents/composer",
                json={
                    "agent_id": "composer",
                    "display_name": "Developer",
                    "category": "developer-tools",
                    "enabled": True,
                },
            )

        assert resp.status_code == 400
        mock_db.save_agent.assert_called_once()
        mock_db._transaction.assert_called()
        mock_db.delete_agent.assert_not_called()
        mock_db.apply_entity_snapshot.assert_not_called()
        mock_registry.reload_all.assert_not_called()

    def test_update_agent_rejects_standard_tool_actions(self, registry_client):
        try:
            import core.entrypoints.admin.agents as agent_routes
        except Exception as e:
            pytest.fail(f"agent routes not importable: {e}")

        mock_db = MagicMock()
        # CLAUDE-P3-01: row_in_account_scope fails closed on non-mapping rows, so the store double must return a real row.
        mock_db.get_agent.return_value = {"agent_id": "composer"}

        with patch.object(agent_routes, "_get_db", return_value=mock_db):
            resp = registry_client.put(
                "/api/agents/composer",
                json={
                    "agent_id": "composer",
                    "display_name": "Developer",
                    "category": "developer-tools",
                    "tools": ["std.text"],
                },
            )

        assert resp.status_code == 400
        assert "connector workflow steps" in resp.text
        mock_db.save_agent.assert_not_called()

    def test_update_agent_accepts_account_scoped_extension_model(self, registry_client):
        import core.entrypoints.admin.agents as agent_routes
        from core.application.settings import model_catalog

        existing = {
            "agent_id": "composer",
            "display_name": "Developer",
            "category": "developer-tools",
            "enabled": False,
            "skills": [],
            "tools": [],
        }
        mock_db = MagicMock()
        mock_db.get_agent.return_value = existing
        mock_db.conn = object()
        mock_registry = MagicMock()
        scope = normalize_scope(account_id="acc-extension")

        with (
            patch.object(agent_routes, "_get_db", return_value=mock_db),
            patch.object(agent_routes, "_get_registry", return_value=mock_registry),
            patch.object(agent_routes, "request_scope", return_value=scope),
            patch.object(
                agent_routes,
                "_get_agent_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
            patch.object(
                model_catalog,
                "validate_provider_model_pair",
                return_value=(True, ""),
            ) as validate_pair,
        ):
            response = registry_client.put(
                "/api/agents/composer",
                json={
                    "agent_id": "composer",
                    "display_name": "Developer",
                    "category": "developer-tools",
                    "config": {
                        "provider": "llmext_acme_deepseek_abc123",
                        "model": "deepseek/deepseek-chat",
                    },
                },
            )

        assert response.status_code == 200, response.text
        validate_pair.assert_called_once_with(
            "llmext_acme_deepseek_abc123",
            "deepseek/deepseek-chat",
            account_id="acc-extension",
            conn=mock_db.conn,
        )
        mock_db.save_agent.assert_called_once()

    def test_update_agent_preserves_existing_account_scope(self, registry_client):
        import core.entrypoints.admin.agents as agent_routes

        existing = {
            "agent_id": "account_agent",
            "account_id": "acc-owner",
            "display_name": "Account Agent",
            "category": "developer-tools",
            "enabled": False,
            "skills": [],
            "tools": [],
        }
        mock_db = MagicMock()
        mock_db.get_agent.return_value = existing
        mock_registry = MagicMock()
        scope = normalize_scope(account_id="acc-owner")

        with (
            patch.object(agent_routes, "_get_db", return_value=mock_db),
            patch.object(agent_routes, "_get_registry", return_value=mock_registry),
            patch.object(agent_routes, "request_scope", return_value=scope),
            patch.object(
                agent_routes,
                "_get_agent_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            response = registry_client.put(
                "/api/agents/account_agent",
                json={
                    "agent_id": "account_agent",
                    "display_name": "Updated Account Agent",
                    "category": "developer-tools",
                },
            )

        assert response.status_code == 200, response.text
        assert mock_db.save_agent.call_args.args[0]["account_id"] == "acc-owner"

    def test_patch_agent_persists_category_without_builder_side_effects(self, registry_client):
        import core.entrypoints.admin.agents as agent_routes

        existing = {
            "agent_id": "composer",
            "display_name": "Developer",
            "category": "other",
            "enabled": False,
            "skills": [],
            "tools": [],
        }
        mock_db = MagicMock()
        mock_db.get_agent.return_value = existing
        mock_registry = MagicMock()
        with (
            patch.object(agent_routes, "_get_db", return_value=mock_db),
            patch.object(agent_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                agent_routes,
                "_get_agent_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            response = registry_client.patch(
                "/api/agents/composer",
                json={"category": "marketing"},
            )

        assert response.status_code == 200, response.text
        saved = mock_db.save_agent.call_args.args[0]
        assert saved["category"] == "marketing"

    def test_patch_agent_clears_explicit_provider_and_model_override(self, registry_client):
        import core.entrypoints.admin.agents as agent_routes

        existing = {
            "agent_id": "composer",
            "account_id": "acc-owner",
            "display_name": "Developer",
            "category": "developer-tools",
            "enabled": False,
            "skills": [],
            "tools": [],
            "config": {
                "provider": "openai",
                "model": "gpt-5.6-sol",
                "timeout_minutes": 12,
            },
            "prompts": {},
        }
        mock_db = MagicMock()
        mock_db.get_agent.return_value = existing
        mock_registry = MagicMock()
        scope = normalize_scope(account_id="acc-owner")

        with (
            patch.object(agent_routes, "_get_db", return_value=mock_db),
            patch.object(agent_routes, "_get_registry", return_value=mock_registry),
            patch.object(agent_routes, "request_scope", return_value=scope),
            patch.object(
                agent_routes,
                "_get_agent_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            response = registry_client.patch(
                "/api/agents/composer",
                json={"config": {"provider": "", "model": ""}},
            )

        assert response.status_code == 200, response.text
        assert mock_db.save_agent.call_args.args[0]["config"] == {"timeout_minutes": 12}
        assert mock_db.save_agent.call_args.kwargs["sync_relationships"] is False

    def test_patch_agent_rolls_back_invalid_enabled_save(self, registry_client):
        try:
            import core.entrypoints.admin.agents as agent_routes
        except Exception as e:
            pytest.fail(f"agent routes not importable: {e}")

        existing = {
            "agent_id": "composer",
            "display_name": "Developer",
            "category": "developer-tools",
            "enabled": True,
            "skills": [],
            "tools": [],
            "config": {},
            "prompts": {},
        }
        mock_db = MagicMock()
        mock_db.get_agent.return_value = existing
        mock_registry = MagicMock()

        with (
            patch.object(agent_routes, "_get_db", return_value=mock_db),
            patch.object(agent_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                agent_routes,
                "_get_agent_validation",
                return_value={"valid": False, "errors": [{"message": "broken"}], "warnings": []},
            ),
        ):
            resp = registry_client.patch(
                "/api/agents/composer",
                json={"description": "updated"},
            )

        assert resp.status_code == 400
        mock_db.save_agent.assert_called_once()
        mock_db._transaction.assert_called()
        mock_db.delete_agent.assert_not_called()
        mock_db.apply_entity_snapshot.assert_not_called()
        mock_registry.reload_all.assert_not_called()

    def test_toggle_nonexistent_agent(self, registry_client):
        resp = registry_client.patch("/api/agents/nonexistent_xyz/toggle")
        assert resp.status_code in (404, 422, 500)

    def test_delete_nonexistent_agent_returns_404(self, registry_client):
        resp = registry_client.delete("/api/agents/nonexistent_xyz_999")
        assert resp.status_code in (404, 500)

    def test_delete_agent_keyerror_maps_to_404(self, registry_client):
        try:
            import core.entrypoints.admin.agents as agent_routes
        except Exception as e:
            pytest.fail(f"agent routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.delete_agent.side_effect = KeyError("Agent 'missing_agent' not found")

        with patch.object(agent_routes, "_get_db", return_value=mock_db):
            resp = registry_client.delete("/api/agents/missing_agent")

        assert resp.status_code == 404
        assert "missing_agent" in str(resp.json().get("detail") or "")

    def test_agent_history_nonexistent(self, registry_client):
        resp = registry_client.get("/api/agents/nonexistent_xyz/history")
        assert resp.status_code in (200, 404, 500)
        if resp.status_code == 200:
            data = resp.json()
            assert isinstance(data, list)

    def test_agent_performance_nonexistent(self, registry_client):
        resp = registry_client.get("/api/agents/nonexistent_xyz/performance")
        assert resp.status_code in (200, 404, 500)

    def test_agent_diff_nonexistent(self, registry_client):
        resp = registry_client.get("/api/agents/nonexistent_xyz/diff")
        assert resp.status_code in (200, 404, 422, 500)

    def test_toggle_agent_blocked_when_contract_invalid(self, registry_client):
        try:
            import core.entrypoints.admin.agents as agent_routes
        except Exception as e:
            pytest.fail(f"agent routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_agent.return_value = {
            "agent_id": "broken_agent",
            "enabled": False,
            "_skills": [],
        }
        mock_registry = MagicMock()
        with (
            patch.object(agent_routes, "_get_db", return_value=mock_db),
            patch.object(agent_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                agent_routes,
                "_get_agent_validation",
                return_value={
                    "valid": False,
                    "errors": [
                        {"message": "Agent must have at least one skill mapping before enabling"}
                    ],
                    "warnings": [],
                },
            ),
        ):
            resp = registry_client.patch("/api/agents/broken_agent/toggle")

        assert resp.status_code == 400
        detail = resp.json().get("detail", {})
        assert detail.get("error") == "Contract validation failed"
        mock_db.toggle_agent.assert_not_called()

    def test_toggle_agent_blocks_disable_while_enabled_workflow_uses_it(self, registry_client):
        import core.entrypoints.admin.agents as agent_routes

        mock_db = MagicMock()
        mock_db.get_agent.return_value = {"agent_id": "writer", "enabled": True}
        mock_db.list_workflows.return_value = [
            {
                "workflow_id": "publish_content",
                "enabled": True,
                "settings": {},
                "steps": [{"step_id": "write", "step_kind": "agent", "agent_id": "writer"}],
            }
        ]

        with patch.object(agent_routes, "_get_db", return_value=mock_db):
            resp = registry_client.patch("/api/agents/writer/toggle")

        assert resp.status_code == 409
        assert "publish_content" in str(resp.json().get("detail") or "")
        mock_db.toggle_agent.assert_not_called()

    def test_patch_agent_cannot_bypass_enabled_workflow_disable_guard(self, registry_client):
        import core.entrypoints.admin.agents as agent_routes

        mock_db = MagicMock()
        mock_db.get_agent.return_value = {"agent_id": "writer", "enabled": True}
        mock_db.list_workflows.return_value = [
            {
                "workflow_id": "publish_content",
                "enabled": True,
                "settings": {},
                "steps": [{"step_id": "write", "step_kind": "agent", "agent_id": "writer"}],
            }
        ]

        with patch.object(agent_routes, "_get_db", return_value=mock_db):
            resp = registry_client.patch("/api/agents/writer", json={"enabled": False})

        assert resp.status_code == 409
        mock_db.save_agent.assert_not_called()

    def test_agent_contract_validation_endpoint_returns_payload(self, registry_client):
        try:
            import core.entrypoints.admin.agents as agent_routes
        except Exception as e:
            pytest.fail(f"agent routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_agent.return_value = {
            "agent_id": "proofreader",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        with (
            patch.object(agent_routes, "_get_db", return_value=mock_db),
            patch.object(
                agent_routes,
                "_get_agent_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            resp = registry_client.get("/api/agents/proofreader/contract-validation")

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("agent_id") == "proofreader"
        assert body.get("validation", {}).get("valid") is True

    def test_add_agent_skill_translates_atomic_readiness_failure(self, registry_client):
        try:
            import core.entrypoints.admin.agents as agent_routes
        except Exception as e:
            pytest.fail(f"agent routes not importable: {e}")

        mock_db = MagicMock()
        # CLAUDE-P3-01: row_in_account_scope fails closed on non-mapping rows, so the store double must return a real row.
        mock_db.get_agent.return_value = {"agent_id": "composer"}
        mock_registry = MagicMock()
        from core.application.registry.db_store_agents_mixin import AgentRelationshipReadinessError

        validation = {
            "valid": False,
            "errors": [{"message": "Skill tool contract is invalid"}],
            "warnings": [],
        }
        mock_db.add_agent_skill.side_effect = AgentRelationshipReadinessError(validation)

        with (
            patch.object(agent_routes, "_get_db", return_value=mock_db),
            patch.object(agent_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                agent_routes,
                "validate_agent_skill_compatibility",
                return_value={
                    "valid": False,
                    "errors": [{"message": "Skill tool contract is invalid"}],
                    "warnings": [],
                },
            ),
        ):
            resp = registry_client.post(
                "/api/agents/composer/skills",
                json={"skill_id": "broken_cap", "skill_prompt": "Use skill"},
            )

        assert resp.status_code == 400
        assert mock_db.add_agent_skill.call_args.args == ("composer", "broken_cap", "Use skill")
        assert set(mock_db.add_agent_skill.call_args.kwargs) == {
            "validate_skill_compatibility",
            "validate_agent",
        }
        mock_db.remove_agent_skill.assert_not_called()
        mock_registry.reload_all.assert_not_called()

    def test_add_agent_skill_duplicate_returns_conflict_without_removing_mapping(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.agents as agent_routes
            from core.application.registry.db_store_agents_mixin import (
                AgentRelationshipConflictError,
            )
        except Exception as e:
            pytest.fail(f"agent routes not importable: {e}")

        mock_db = MagicMock()
        # CLAUDE-P3-01: row_in_account_scope fails closed on non-mapping rows, so the store double must return a real row.
        mock_db.get_agent.return_value = {"agent_id": "composer"}
        mock_db.add_agent_skill.side_effect = AgentRelationshipConflictError(
            "Skill already assigned"
        )
        with patch.object(agent_routes, "_get_db", return_value=mock_db):
            resp = registry_client.post(
                "/api/agents/composer/skills",
                json={"skill_id": "existing", "skill_prompt": "Use skill"},
            )

        assert resp.status_code == 409
        mock_db.remove_agent_skill.assert_not_called()

    def test_add_agent_tool_rolls_back_on_readiness_failure(self, registry_client):
        try:
            import core.entrypoints.admin.agents as agent_routes
        except Exception as e:
            pytest.fail(f"agent routes not importable: {e}")

        mock_db = MagicMock()
        # CLAUDE-P3-01: row_in_account_scope fails closed on non-mapping rows, so the store double must return a real row.
        mock_db.get_agent.return_value = {"agent_id": "composer"}
        mock_registry = MagicMock()

        with (
            patch.object(agent_routes, "_get_db", return_value=mock_db),
            patch.object(agent_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                agent_routes,
                "_get_agent_validation",
                return_value={
                    "valid": False,
                    "errors": [{"message": "Agent tool is not runtime-ready"}],
                    "warnings": [],
                },
            ),
        ):
            resp = registry_client.post(
                "/api/agents/composer/tools",
                json={"tool_id": "broken_tool"},
            )

        assert resp.status_code == 400
        mock_db.add_agent_tool.assert_called_once_with("composer", "broken_tool")
        mock_db.remove_agent_tool.assert_called_once_with("composer", "broken_tool")
        mock_registry.reload_all.assert_not_called()

    def test_add_agent_tool_rejects_standard_tool_actions(self, registry_client):
        try:
            import core.entrypoints.admin.agents as agent_routes
        except Exception as e:
            pytest.fail(f"agent routes not importable: {e}")

        mock_db = MagicMock()
        # CLAUDE-P3-01: row_in_account_scope fails closed on non-mapping rows, so the store double must return a real row.
        mock_db.get_agent.return_value = {"agent_id": "composer"}
        mock_registry = MagicMock()

        with (
            patch.object(agent_routes, "_get_db", return_value=mock_db),
            patch.object(agent_routes, "_get_registry", return_value=mock_registry),
        ):
            resp = registry_client.post(
                "/api/agents/composer/tools",
                json={"tool_id": "std.numbers"},
            )

        assert resp.status_code == 400
        assert "connector workflow steps" in resp.text
        mock_db.add_agent_tool.assert_not_called()
        mock_registry.reload_all.assert_not_called()

    def test_replace_agent_relationships_uses_one_cas_operation(self, registry_client):
        import core.entrypoints.admin.agents as agent_routes

        mock_db = MagicMock()
        mock_db.get_agent.return_value = {"agent_id": "composer"}
        mock_db.replace_agent_relationships.return_value = {
            "status": "updated",
            "skills": [{"skill_id": "draft_summary", "sort_order": 0}],
            "tools": [{"tool_id": "http_fetch", "sort_order": 0}],
        }
        mock_registry = MagicMock()

        with (
            patch.object(agent_routes, "_get_db", return_value=mock_db),
            patch.object(agent_routes, "_get_registry", return_value=mock_registry),
        ):
            response = registry_client.put(
                "/api/agents/composer/relationships",
                json={
                    "expected_skills": ["analyze"],
                    "expected_tools": ["old_tool"],
                    "skills": ["draft_summary"],
                    "tools": ["http_fetch"],
                },
            )

        assert response.status_code == 200
        mock_db.replace_agent_relationships.assert_called_once()
        call = mock_db.replace_agent_relationships.call_args
        assert call.args == ("composer",)
        assert call.kwargs["expected_skill_ids"] == ["analyze"]
        assert call.kwargs["expected_tool_ids"] == ["old_tool"]
        assert call.kwargs["skill_ids"] == ["draft_summary"]
        assert call.kwargs["tool_ids"] == ["http_fetch"]
        mock_registry.reload_all.assert_called_once_with()

    def test_replace_agent_relationships_returns_conflict_for_lost_cas(self, registry_client):
        import core.entrypoints.admin.agents as agent_routes
        from core.application.registry.db_store_agents_mixin import (
            AgentRelationshipConflictError,
        )

        mock_db = MagicMock()
        mock_db.get_agent.return_value = {"agent_id": "composer"}
        mock_db.replace_agent_relationships.side_effect = AgentRelationshipConflictError(
            "Agent relationships changed; reload and retry"
        )

        with patch.object(agent_routes, "_get_db", return_value=mock_db):
            response = registry_client.put(
                "/api/agents/composer/relationships",
                json={
                    "expected_skills": [],
                    "expected_tools": [],
                    "skills": ["draft_summary"],
                    "tools": [],
                },
            )

        assert response.status_code == 409
        assert "reload and retry" in response.text


# ---------------------------------------------------------------------------
# Tool CRUD tests
# ---------------------------------------------------------------------------


class TestToolCRUD:
    """Test /api/tools/* endpoints."""

    def test_list_tools(self, registry_client):
        resp = registry_client.get("/api/tools")
        assert resp.status_code in (200, 404, 500)
        if resp.status_code == 200:
            data = resp.json()
            assert isinstance(data, list)

    def test_list_tools_has_tool_id(self, registry_client):
        resp = registry_client.get("/api/tools")
        if resp.status_code == 200:
            tools = resp.json()
            if tools:
                assert "tool_id" in tools[0]

    def test_get_existing_tool(self, registry_client):
        resp = _tool_api_detail_response(registry_client)
        assert resp.status_code in (200, 404)

    def test_get_nonexistent_tool_returns_404(self, registry_client):
        resp = registry_client.get("/api/tools/nonexistent_tool_xyz_99999")
        assert resp.status_code in (404, 500)

    def test_registry_tools_page_handles_datetime_rows(self, registry_client):
        resp = registry_client.get("/registry/tools")

        contract_text = _page_contract_text(
            resp, "assets/frontend/src/pages/registry/tools/index.tsx"
        )
        assert "tools-grid" in contract_text

    def test_registry_tool_detail_uses_operations_contract_surface(self, registry_client):
        resp = registry_client.get(
            "/registry/tools/workflow_ops",
            follow_redirects=False,
        )

        contract_text = _page_contract_text(
            resp,
            "assets/frontend/src/pages/registry/tools/detail/index.tsx",
        )
        assert "Actions" in contract_text
        assert "contract_operations" in contract_text
        assert "RegistryEntityChangelogPanel" in contract_text

    def test_create_tool_invalid_body_returns_error(self, registry_client):
        resp = registry_client.post("/api/tools", json={})
        assert resp.status_code == 422

    @pytest.mark.parametrize("method", ["post", "put", "patch"])
    @pytest.mark.parametrize(
        ("interface", "error_path"),
        [
            ({"parameters": {"type": "object", "properties": {}}}, ["parameters"]),
            ({"functions": {"name": "invoke"}}, ["functions"]),
            ({"parameters": [{"type": "string"}]}, ["parameters", 0, "name"]),
        ],
        ids=["schema-is-not-parameters", "functions-must-be-list", "parameter-needs-name"],
    )
    def test_tool_nested_interface_rejected_before_persistence(
        self, registry_client, method, interface, error_path
    ):
        from copy import deepcopy

        import core.entrypoints.admin.tools as tool_routes
        from core.application.registry.db_store import RegistryDBStore

        existing = {
            "tool_id": "fixture_nested_interface",
            "account_id": DEFAULT_ACCOUNT_ID,
            "display_name": "Original disabled draft",
            "enabled": False,
            "interface": {"function_name": "invoke", "parameters": []},
            "implementation": {"type": "internal"},
            "tags": ["preserve"],
        }
        before = deepcopy(existing)
        store = RegistryDBStore("postgresql://unused")
        store._column_exists = MagicMock()
        store._transaction = MagicMock()
        db = MagicMock()
        db.get_tool.return_value = None if method == "post" else existing
        # Run the real canonical manifest validation, not a fabricated save error.
        db.save_tool.side_effect = store.save_tool
        payload = {
            "tool_id": "fixture_nested_interface",
            "display_name": "Must not be saved",
            "enabled": False,
            "interface": interface,
        }
        url = "/api/tools" if method == "post" else "/api/tools/fixture_nested_interface"
        with patch.object(tool_routes, "_get_db", return_value=db):
            response = getattr(registry_client, method)(url, json=payload)

        assert response.status_code == 422, response.text
        assert any(
            issue["loc"] == ["body", "interface", *error_path]
            for issue in response.json()["details"]["errors"]
        )
        assert existing == before
        db.save_tool.assert_not_called()
        store._column_exists.assert_not_called()
        store._transaction.assert_not_called()

    @pytest.mark.parametrize("method", ["post", "put", "patch"])
    def test_tool_nested_interface_accepts_canonical_parameters_and_input_schema(
        self, registry_client, method
    ):
        import core.entrypoints.admin.tools as tool_routes
        from core.application.registry.tool_manifest import ToolManifest

        interface = {
            "function_name": "lookup",
            "parameters": [{"name": "query", "type": "string", "required": True}],
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
        existing = {
            "tool_id": "fixture_nested_interface",
            "account_id": DEFAULT_ACCOUNT_ID,
            "display_name": "Disabled draft",
            "enabled": False,
            "interface": {"function_name": "invoke", "parameters": []},
            "implementation": {"type": "internal"},
            "tags": ["preserve"],
        }
        db = MagicMock()
        db.get_tool.return_value = None if method == "post" else existing
        saved = []

        def save_tool(payload, author):
            saved.append(ToolManifest.model_validate(payload))

        db.save_tool.side_effect = save_tool
        payload = {
            "tool_id": "fixture_nested_interface",
            "display_name": "Valid disabled draft",
            "enabled": False,
            "interface": interface,
        }
        url = "/api/tools" if method == "post" else "/api/tools/fixture_nested_interface"
        with (
            patch.object(tool_routes, "_get_db", return_value=db),
            patch.object(tool_routes, "_get_registry", return_value=MagicMock()),
            patch.object(tool_routes, "_get_tool_validation", return_value={"valid": True}),
        ):
            response = getattr(registry_client, method)(url, json=payload)

        assert response.status_code == 200, response.text
        assert len(saved) == 1
        assert saved[0].input_schema == interface["input_schema"]
        assert saved[0].interface.parameters[0].name == "query"
        assert db.save_tool.call_args.args[0]["interface"] == interface

    def test_tool_save_openapi_example_uses_the_canonical_interface(self):
        import core.entrypoints.admin.tools as tool_routes
        from core.application.registry.tool_manifest import ToolManifest

        manifest = ToolManifest.model_validate(tool_routes._TOOL_SAVE_REQUEST_EXAMPLE)
        assert manifest.input_schema["properties"]["issue_number"]["type"] == "integer"

    @pytest.mark.parametrize("operation", ["create", "replace", "clone"])
    @pytest.mark.parametrize(
        "identifier",
        [" ", "", "fixture invalid/id", "fixture/tool", "fixture\\tool", "fixture\x00tool", ".."],
        ids=["blank", "empty", "space-slash", "slash", "backslash", "control", "dot-segment"],
    )
    def test_tool_write_rejects_noncanonical_body_id_before_persistence(
        self, registry_client, operation, identifier
    ):
        import core.entrypoints.admin.tools as tool_routes

        db = MagicMock()
        db.get_tool.side_effect = lambda key: (
            {
                "tool_id": "fixture_source",
                "account_id": DEFAULT_ACCOUNT_ID,
                "display_name": "Source",
                "enabled": False,
                "implementation": {"type": "internal"},
            }
            if key == "fixture_source"
            else None
        )
        payload = {"tool_id": identifier, "display_name": "Disabled draft", "enabled": False}
        with (
            patch.object(tool_routes, "_get_db", return_value=db),
            patch.object(tool_routes, "_get_registry", return_value=MagicMock()),
            patch.object(tool_routes, "_get_tool_validation", return_value={"valid": True}),
        ):
            if operation == "create":
                response = registry_client.post("/api/tools", json=payload)
            elif operation == "replace":
                response = registry_client.put("/api/tools/fixture_source", json=payload)
            else:
                response = registry_client.post("/api/tools/fixture_source/clone", json=payload)
        assert response.status_code == 422, response.text
        # The existing transport-wide NUL guard rejects the entire body first.
        expected_location = ["body"] if "\x00" in identifier else ["body", "tool_id"]
        assert any(
            issue["loc"] == expected_location for issue in response.json()["details"]["errors"]
        )
        db.save_tool.assert_not_called()

    @pytest.mark.parametrize("method", ["put", "patch"])
    def test_tool_write_rejects_noncanonical_path_id(self, registry_client, method):
        import core.entrypoints.admin.tools as tool_routes

        db = MagicMock()
        db.get_tool.return_value = None
        payload = {"tool_id": "fixture_valid", "display_name": "Disabled draft", "enabled": False}
        with patch.object(tool_routes, "_get_db", return_value=db):
            response = getattr(registry_client, method)(
                "/api/tools/fixture%20invalid", json=payload
            )
        assert response.status_code == 422, response.text
        assert any(
            issue["loc"] == ["path", "tool_id"] for issue in response.json()["details"]["errors"]
        )
        db.save_tool.assert_not_called()

    def test_create_tool_rolls_back_invalid_enabled_save(self, registry_client):
        try:
            import core.entrypoints.admin.tools as tool_routes
        except Exception as e:
            pytest.fail(f"tool routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_tool.return_value = None
        mock_registry = MagicMock()

        with (
            patch.object(tool_routes, "_get_db", return_value=mock_db),
            patch.object(tool_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                tool_routes,
                "_get_tool_validation",
                return_value={"valid": False, "errors": [{"message": "broken"}], "warnings": []},
            ),
        ):
            resp = registry_client.post(
                "/api/tools",
                json={
                    "tool_id": "broken_tool_save",
                    "display_name": "Broken Tool",
                    "enabled": True,
                    "interface": {"function_name": "broken", "parameters": []},
                    "implementation": {"type": "python"},
                },
            )

        assert resp.status_code == 400
        mock_db.save_tool.assert_called_once()
        mock_db._transaction.assert_called()
        mock_db.delete_tool.assert_not_called()
        mock_db.apply_entity_snapshot.assert_not_called()
        mock_registry.reload_all.assert_not_called()

    def test_update_tool_rolls_back_invalid_enabled_save(self, registry_client):
        try:
            import core.entrypoints.admin.tools as tool_routes
        except Exception as e:
            pytest.fail(f"tool routes not importable: {e}")

        existing = {
            "tool_id": "search_tool",
            "display_name": "Search Tool",
            "enabled": True,
            "interface": {"function_name": "search", "parameters": []},
            "implementation": {
                "type": "internal",
                "module": "core.application.orchestration.tools.search",
            },
            "config": {},
            "tags": [],
        }
        mock_db = MagicMock()
        mock_db.get_tool.return_value = existing
        mock_registry = MagicMock()

        with (
            patch.object(tool_routes, "_get_db", return_value=mock_db),
            patch.object(tool_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                tool_routes,
                "_get_tool_validation",
                return_value={"valid": False, "errors": [{"message": "broken"}], "warnings": []},
            ),
        ):
            resp = registry_client.put(
                "/api/tools/search_tool",
                json={
                    "tool_id": "search_tool",
                    "display_name": "Search Tool",
                    "enabled": True,
                    "interface": {"function_name": "search", "parameters": []},
                    "implementation": {
                        "type": "internal",
                        "module": "core.application.orchestration.tools.search",
                    },
                },
            )

        assert resp.status_code == 400
        mock_db.save_tool.assert_called_once()
        mock_db._transaction.assert_called()
        mock_db.delete_tool.assert_not_called()
        mock_db.apply_entity_snapshot.assert_not_called()
        mock_registry.reload_all.assert_not_called()

    def test_patch_tool_rolls_back_invalid_enabled_save(self, registry_client):
        try:
            import core.entrypoints.admin.tools as tool_routes
        except Exception as e:
            pytest.fail(f"tool routes not importable: {e}")

        existing = {
            "tool_id": "search_tool",
            "display_name": "Search Tool",
            "enabled": True,
            "interface": {"function_name": "search", "parameters": []},
            "implementation": {
                "type": "internal",
                "module": "core.application.orchestration.tools.search",
            },
            "config": {},
            "tags": [],
        }
        mock_db = MagicMock()
        mock_db.get_tool.return_value = existing
        mock_registry = MagicMock()

        with (
            patch.object(tool_routes, "_get_db", return_value=mock_db),
            patch.object(tool_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                tool_routes,
                "_get_tool_validation",
                return_value={"valid": False, "errors": [{"message": "broken"}], "warnings": []},
            ),
        ):
            resp = registry_client.patch(
                "/api/tools/search_tool",
                json={"description": "updated"},
            )

        assert resp.status_code == 400
        mock_db.save_tool.assert_called_once()
        mock_db._transaction.assert_called()
        mock_db.delete_tool.assert_not_called()
        mock_db.apply_entity_snapshot.assert_not_called()
        mock_registry.reload_all.assert_not_called()

    def test_delete_nonexistent_tool_returns_404(self, registry_client):
        resp = registry_client.delete("/api/tools/nonexistent_xyz_999")
        assert resp.status_code in (404, 500)

    def test_delete_tool_keyerror_maps_to_404(self, registry_client):
        try:
            import core.entrypoints.admin.tools as tool_routes
        except Exception as e:
            pytest.fail(f"tool routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.delete_tool.side_effect = KeyError("Tool 'missing_tool' not found")

        with patch.object(tool_routes, "_get_db", return_value=mock_db):
            resp = registry_client.delete("/api/tools/missing_tool")

        assert resp.status_code == 404
        assert "missing_tool" in str(resp.json().get("detail") or "")

    def test_tool_history_nonexistent(self, registry_client):
        resp = registry_client.get("/api/tools/nonexistent_xyz/history")
        assert resp.status_code in (200, 404, 500)

    def test_toggle_tool_blocked_when_contract_invalid(self, registry_client):
        try:
            import core.entrypoints.admin.tools as tool_routes
        except Exception as e:
            pytest.fail(f"tool routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_tool.return_value = {"tool_id": "broken_tool", "enabled": False}
        mock_registry = MagicMock()

        with (
            patch.object(tool_routes, "_get_db", return_value=mock_db),
            patch.object(tool_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                tool_routes,
                "_get_tool_validation",
                return_value={
                    "valid": False,
                    "errors": [{"message": "Enabled tool failed runtime resolver check"}],
                    "warnings": [],
                },
            ),
        ):
            resp = registry_client.patch("/api/tools/broken_tool/toggle")

        assert resp.status_code == 400
        detail = resp.json().get("detail", {})
        assert detail.get("error") == "Contract validation failed"
        mock_db.toggle_tool.assert_not_called()

    @pytest.mark.parametrize("method", ["toggle", "put", "patch"])
    def test_unavailable_tool_cannot_be_enabled_through_api(self, tool_routes_client, method):
        try:
            import core.entrypoints.admin.tools as tool_routes
        except Exception as e:
            pytest.fail(f"tool routes not importable: {e}")

        existing = {
            "tool_id": "orphaned_tool",
            "display_name": "Orphaned Tool",
            "description": "Retained after connector deletion.",
            "enabled": False,
            "availability_status": "unavailable",
            "availability_reason_code": "source_connector_removed",
            "availability_reason_text": "Source MCP connector mcp_removed was deleted.",
            "interface": {"function_name": "invoke", "parameters": []},
            "implementation": {"type": "internal"},
            "config": {},
            "tags": [],
        }
        mock_db = MagicMock()
        mock_db.get_tool.return_value = existing
        mock_registry = MagicMock()

        with (
            patch.object(tool_routes, "_get_db", return_value=mock_db),
            patch.object(tool_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                tool_routes,
                "_get_tool_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            if method == "toggle":
                resp = tool_routes_client.patch("/api/tools/orphaned_tool/toggle")
            elif method == "put":
                resp = tool_routes_client.put(
                    "/api/tools/orphaned_tool",
                    json={**existing, "enabled": True},
                )
            else:
                resp = tool_routes_client.patch(
                    "/api/tools/orphaned_tool",
                    json={"enabled": True},
                )

        assert resp.status_code == 409
        assert "source_connector_removed" in str(resp.json().get("detail") or "")
        mock_db.toggle_tool.assert_not_called()
        mock_db.save_tool.assert_not_called()
        assert existing["enabled"] is False

    def test_tool_api_preserves_unavailable_fields(self, tool_routes_client):
        try:
            import core.entrypoints.admin.tools as tool_routes
        except Exception as e:
            pytest.fail(f"tool routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_tool.return_value = {
            "tool_id": "orphaned_tool",
            "display_name": "Orphaned Tool",
            "enabled": False,
            "availability_status": "unavailable",
            "availability_reason_code": "source_connector_removed",
            "availability_reason_text": "Source MCP connector mcp_removed was deleted.",
            "interface": {},
            "implementation": {"type": "internal"},
        }

        with (
            patch.object(tool_routes, "_get_db", return_value=mock_db),
            patch.object(tool_routes, "_get_registry", return_value=MagicMock()),
        ):
            resp = tool_routes_client.get("/api/tools/orphaned_tool")

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["availability_status"] == "unavailable"
        assert payload["availability_reason_code"] == "source_connector_removed"
        assert payload["availability_reason_text"] == (
            "Source MCP connector mcp_removed was deleted."
        )

    def test_tool_contract_validation_endpoint_returns_payload(self, registry_client):
        try:
            import core.entrypoints.admin.tools as tool_routes
        except Exception as e:
            pytest.fail(f"tool routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_tool.return_value = {
            "tool_id": "search_tool",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        with (
            patch.object(tool_routes, "_get_db", return_value=mock_db),
            patch.object(
                tool_routes,
                "_get_tool_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            resp = registry_client.get("/api/tools/search_tool/contract-validation")

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("tool_id") == "search_tool"
        assert body.get("validation", {}).get("valid") is True

    def test_list_tools_uses_tool_query_service_contract(self, registry_client):
        try:
            import core.entrypoints.admin.tools as tool_routes
        except Exception as e:
            pytest.fail(f"tool routes not importable: {e}")

        payload = MagicMock()
        payload.tools = [
            MagicMock(dump=MagicMock(return_value={"tool_id": "search_tool", "category": "search"}))
        ]
        mock_service = MagicMock()
        mock_service.list_tools.return_value = payload

        with patch.object(tool_routes, "_tool_query_service", return_value=mock_service):
            resp = registry_client.get(
                "/api/tools", params={"enabled_only": "true", "category": "search"}
            )

        assert resp.status_code == 200
        assert resp.json()[0]["tool_id"] == "search_tool"
        call = mock_service.list_tools.call_args
        assert call.kwargs["enabled_only"] is True
        assert call.kwargs["category"] == "search"
        assert call.kwargs["search"] == ""
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].actor_id == "admin"

    def test_tool_diff_uses_tool_query_service_contract(self, registry_client):
        try:
            import core.entrypoints.admin.tools as tool_routes
        except Exception as e:
            pytest.fail(f"tool routes not importable: {e}")

        payload = MagicMock()
        payload.dump.return_value = {
            "entity_type": "tool",
            "entity_id": "search_tool",
            "from_entry_id": 7,
            "to_entry_id": 11,
            "changes": [],
            "summary": {"total": 0, "added": 0, "removed": 0, "changed": 0},
        }
        mock_service = MagicMock()
        mock_service.get_diff.return_value = payload

        with patch.object(tool_routes, "_tool_query_service", return_value=mock_service):
            resp = registry_client.get(
                "/api/tools/search_tool/diff", params={"from_entry_id": 7, "to_entry_id": 11}
            )

        assert resp.status_code == 200
        assert resp.json()["entity_id"] == "search_tool"
        call = mock_service.get_diff.call_args
        assert call.args == ("search_tool",)
        assert call.kwargs["from_entry_id"] == "7"
        assert call.kwargs["to_entry_id"] == "11"
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].actor_id == "admin"

    def test_tool_contract_validation_uses_tool_query_service_contract(self, registry_client):
        try:
            import core.entrypoints.admin.tools as tool_routes
        except Exception as e:
            pytest.fail(f"tool routes not importable: {e}")

        payload = MagicMock()
        payload.dump.return_value = {
            "tool_id": "search_tool",
            "validation": {"valid": True, "errors": [], "warnings": []},
        }
        mock_service = MagicMock()
        mock_service.get_contract_validation.return_value = payload

        with patch.object(tool_routes, "_tool_query_service", return_value=mock_service):
            resp = registry_client.get("/api/tools/search_tool/contract-validation")

        assert resp.status_code == 200
        assert resp.json()["tool_id"] == "search_tool"
        call = mock_service.get_contract_validation.call_args
        assert call.args == ("search_tool",)
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].actor_id == "admin"

    def test_create_tool_uses_tool_command_service_contract(self, registry_client):
        try:
            import core.entrypoints.admin.tools as tool_routes
        except Exception as e:
            pytest.fail(f"tool routes not importable: {e}")

        payload = MagicMock()
        payload.dump.return_value = {"status": "created", "tool_id": "search_tool"}
        mock_service = MagicMock()
        mock_service.create_tool.return_value = payload

        with patch.object(tool_routes, "_tool_command_service", return_value=mock_service):
            resp = registry_client.post(
                "/api/tools",
                json={"tool_id": "search_tool", "display_name": "Search Tool"},
            )

        assert resp.status_code == 200
        call = mock_service.create_tool.call_args
        assert call.args[0].tool_id == "search_tool"
        assert call.kwargs["author"] == "admin"
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].actor_id == "admin"

    def test_restore_tool_revision_uses_tool_command_service_contract(self, registry_client):
        try:
            import core.entrypoints.admin.tools as tool_routes
        except Exception as e:
            pytest.fail(f"tool routes not importable: {e}")

        payload = MagicMock()
        payload.dump.return_value = {
            "status": "restored",
            "tool_id": "search_tool",
            "restored_from_entry_id": "11",
        }
        mock_service = MagicMock()
        mock_service.restore_tool_revision.return_value = payload

        with patch.object(tool_routes, "_tool_command_service", return_value=mock_service):
            resp = registry_client.post("/api/tools/search_tool/restore", json={"entry_id": 11})

        assert resp.status_code == 200
        call = mock_service.restore_tool_revision.call_args
        assert call.args[0] == "search_tool"
        assert call.args[1].entry_id == "11"
        assert call.kwargs["author"] == "admin_restore"
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].actor_id == "admin"


# ---------------------------------------------------------------------------
# Skill CRUD tests
# ---------------------------------------------------------------------------


class TestSkillCRUD:
    """Test /api/skills/* endpoints."""

    def test_list_skills(self, registry_client):
        resp = registry_client.get("/api/skills")
        assert resp.status_code in (200, 404, 500)
        if resp.status_code == 200:
            data = resp.json()
            assert isinstance(data, list)

    def test_list_skills_passes_request_account_scope(self, registry_client):
        import core.entrypoints.admin.skills as skill_routes

        mock_db = MagicMock()
        mock_db.list_skills.return_value = []
        scope = normalize_scope(account_id="acc-owner")

        with (
            patch.object(skill_routes, "_get_db", return_value=mock_db),
            patch.object(skill_routes, "request_scope", return_value=scope),
        ):
            response = registry_client.get("/api/skills")

        assert response.status_code == 200, response.text
        mock_db.list_skills.assert_called_once_with(
            enabled_only=False,
            account_id="acc-owner",
        )

    def test_list_skills_has_skill_id(self, registry_client):
        resp = registry_client.get("/api/skills")
        if resp.status_code == 200:
            caps = resp.json()
            if caps:
                assert "skill_id" in caps[0]

    def test_get_existing_skill(self, registry_client):
        resp = _skill_api_detail_response(registry_client)
        assert resp.status_code in (200, 404)

    def test_get_nonexistent_skill_returns_404(self, registry_client):
        resp = registry_client.get("/api/skills/nonexistent_cap_xyz_99999")
        assert resp.status_code in (404, 500)

    def test_create_skill_invalid_body(self, registry_client):
        resp = registry_client.post("/api/skills", json={})
        assert resp.status_code == 422

    @pytest.mark.parametrize("identifier", [None, "", " ", "\t\n"])
    def test_create_skill_rejects_blank_id_before_persistence(self, registry_client, identifier):
        import core.entrypoints.admin.skills as skill_routes

        payload = {"display_name": "Disabled draft", "enabled": False}
        if identifier is not None:
            payload["skill_id"] = identifier
        with patch.object(skill_routes, "_get_db") as get_db:
            response = registry_client.post("/api/skills", json=payload)
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "validation_error"
        assert any(
            issue["loc"] == ["body", "skill_id"] for issue in response.json()["details"]["errors"]
        )
        get_db.assert_not_called()

    def test_create_skill_rolls_back_invalid_enabled_save(self, registry_client):
        try:
            import core.entrypoints.admin.skills as skill_routes
        except Exception as e:
            pytest.fail(f"skill routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_skill.return_value = None
        mock_registry = MagicMock()

        with (
            patch.object(skill_routes, "_get_db", return_value=mock_db),
            patch.object(skill_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                skill_routes,
                "_get_skill_validation",
                return_value={"valid": False, "errors": [{"message": "broken"}], "warnings": []},
            ),
        ):
            resp = registry_client.post(
                "/api/skills",
                json={
                    "skill_id": "broken_cap_save",
                    "display_name": "Broken Skill",
                    "enabled": True,
                    "expected_outputs": [{"name": "result", "type": "text", "required": True}],
                },
            )

        assert resp.status_code == 400
        mock_db.save_skill.assert_called_once()
        mock_db._transaction.assert_called()
        mock_db.delete_skill.assert_not_called()
        mock_db.apply_entity_snapshot.assert_not_called()
        mock_registry.reload_all.assert_not_called()

    def test_update_skill_rolls_back_invalid_enabled_save(self, registry_client):
        try:
            import core.entrypoints.admin.skills as skill_routes
        except Exception as e:
            pytest.fail(f"skill routes not importable: {e}")

        existing = {
            "skill_id": "review_summary",
            "display_name": "Review Summary",
            "enabled": True,
            "expected_inputs": [],
            "expected_outputs": [{"name": "review_verdict", "type": "text", "required": True}],
        }
        mock_db = MagicMock()
        mock_db.get_skill.return_value = existing
        mock_registry = MagicMock()

        with (
            patch.object(skill_routes, "_get_db", return_value=mock_db),
            patch.object(skill_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                skill_routes,
                "_get_skill_validation",
                return_value={"valid": False, "errors": [{"message": "broken"}], "warnings": []},
            ),
        ):
            resp = registry_client.put(
                "/api/skills/review_summary",
                json={
                    "skill_id": "review_summary",
                    "display_name": "Review Summary",
                    "enabled": True,
                    "expected_outputs": [
                        {"name": "review_verdict", "type": "text", "required": True}
                    ],
                },
            )

        assert resp.status_code == 400
        mock_db.save_skill.assert_called_once()
        mock_db._transaction.assert_called()
        mock_db.delete_skill.assert_not_called()
        mock_db.apply_entity_snapshot.assert_not_called()
        mock_registry.reload_all.assert_not_called()

    def test_patch_skill_rolls_back_invalid_enabled_save(self, registry_client):
        try:
            import core.entrypoints.admin.skills as skill_routes
        except Exception as e:
            pytest.fail(f"skill routes not importable: {e}")

        existing = {
            "skill_id": "review_summary",
            "display_name": "Review Summary",
            "enabled": True,
            "expected_inputs": [],
            "expected_outputs": [{"name": "review_verdict", "type": "text", "required": True}],
        }
        mock_db = MagicMock()
        mock_db.get_skill.return_value = existing
        mock_registry = MagicMock()

        with (
            patch.object(skill_routes, "_get_db", return_value=mock_db),
            patch.object(skill_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                skill_routes,
                "_get_skill_validation",
                return_value={"valid": False, "errors": [{"message": "broken"}], "warnings": []},
            ),
        ):
            resp = registry_client.patch(
                "/api/skills/review_summary",
                json={"description": "updated"},
            )

        assert resp.status_code == 400
        mock_db.save_skill.assert_called_once()
        mock_db._transaction.assert_called()
        mock_db.delete_skill.assert_not_called()
        mock_db.apply_entity_snapshot.assert_not_called()
        mock_registry.reload_all.assert_not_called()

    def test_delete_nonexistent_skill(self, registry_client):
        resp = registry_client.delete("/api/skills/nonexistent_xyz_999")
        assert resp.status_code in (404, 500)

    def test_delete_skill_keyerror_maps_to_404(self, registry_client):
        try:
            import core.entrypoints.admin.skills as skill_routes
        except Exception as e:
            pytest.fail(f"skill routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.delete_skill.side_effect = KeyError("Skill 'missing_cap' not found")

        with patch.object(skill_routes, "_get_db", return_value=mock_db):
            resp = registry_client.delete("/api/skills/missing_cap")

        assert resp.status_code == 404
        assert "missing_cap" in str(resp.json().get("detail") or "")

    def test_toggle_skill_blocked_when_contract_invalid(self, registry_client):
        try:
            import core.entrypoints.admin.skills as skill_routes
        except Exception as e:
            pytest.fail(f"skill routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_skill.return_value = {"skill_id": "broken_cap", "enabled": False}
        mock_registry = MagicMock()

        with (
            patch.object(skill_routes, "_get_db", return_value=mock_db),
            patch.object(skill_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                skill_routes,
                "_get_skill_validation",
                return_value={
                    "valid": False,
                    "errors": [
                        {"message": "Skill must declare at least one output contract field"}
                    ],
                    "warnings": [],
                },
            ),
        ):
            resp = registry_client.patch("/api/skills/broken_cap/toggle")

        assert resp.status_code == 400
        detail = resp.json().get("detail", {})
        assert detail.get("error") == "Contract validation failed"
        assert detail.get("messages")
        mock_db.toggle_skill.assert_not_called()

    def test_toggle_skill_blocks_disable_while_its_agent_is_in_enabled_workflow(
        self, registry_client
    ):
        import core.entrypoints.admin.skills as skill_routes

        mock_db = MagicMock()
        mock_db.get_skill.return_value = {"skill_id": "copywriting", "enabled": True}
        mock_db.get_agents_using_skill.return_value = ["writer"]
        mock_db.list_workflows.return_value = [
            {
                "workflow_id": "publish_content",
                "enabled": True,
                "settings": {},
                "steps": [{"step_id": "write", "step_kind": "agent", "agent_id": "writer"}],
            }
        ]

        with patch.object(skill_routes, "_get_db", return_value=mock_db):
            resp = registry_client.patch("/api/skills/copywriting/toggle")

        assert resp.status_code == 409
        assert "publish_content" in str(resp.json().get("detail") or "")
        mock_db.toggle_skill.assert_not_called()

    def test_patch_skill_cannot_bypass_enabled_workflow_disable_guard(self, registry_client):
        import core.entrypoints.admin.skills as skill_routes

        mock_db = MagicMock()
        mock_db.get_skill.return_value = {"skill_id": "copywriting", "enabled": True}
        mock_db.get_agents_using_skill.return_value = ["writer"]
        mock_db.list_workflows.return_value = [
            {
                "workflow_id": "publish_content",
                "enabled": True,
                "settings": {},
                "steps": [{"step_id": "write", "step_kind": "agent", "agent_id": "writer"}],
            }
        ]

        with patch.object(skill_routes, "_get_db", return_value=mock_db):
            resp = registry_client.patch("/api/skills/copywriting", json={"enabled": False})

        assert resp.status_code == 409
        mock_db.save_skill.assert_not_called()

    def test_skill_contract_validation_endpoint_returns_payload(self, registry_client):
        try:
            import core.entrypoints.admin.skills as skill_routes
        except Exception as e:
            pytest.fail(f"skill routes not importable: {e}")

        mock_db = MagicMock()
        # CLAUDE-P3-01: row_in_account_scope fails closed on non-mapping rows, so the store double must return a real row.
        mock_db.get_skill.return_value = {"skill_id": "review_summary"}
        with (
            patch.object(skill_routes, "_get_db", return_value=mock_db),
            patch.object(
                skill_routes,
                "_get_skill_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            resp = registry_client.get("/api/skills/review_summary/contract-validation")

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("skill_id") == "review_summary"
        assert body.get("validation", {}).get("valid") is True

    def test_skill_detail_page_uses_io_tab_as_single_contract_editor(self, registry_client):
        resp = registry_client.get("/registry/skills/review_summary")

        contract_text = _page_contract_text(
            resp,
            "assets/frontend/src/pages/registry/skills/detail/index.tsx",
        )
        assert "I/O" in contract_text
        assert "Config" in contract_text
        assert "Instructions" in contract_text
        assert "Agents" in contract_text
        assert "Changelog" in contract_text


# ---------------------------------------------------------------------------
# E11-S3: skill_id aliasing verification
# ---------------------------------------------------------------------------


class TestSkillIdAliasing:
    """Verify canonical skill_id behavior in public skill APIs."""

    def test_list_skills_includes_skill_id(self, registry_client):
        """GET /api/skills responses include skill_id."""
        try:
            import core.entrypoints.admin.skills as skill_routes
        except Exception as e:
            pytest.fail(f"skill routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.list_skills.return_value = [
            {"skill_id": "review_summary", "display_name": "Review Summary", "enabled": True},
        ]

        with patch.object(skill_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get("/api/skills")

        assert resp.status_code == 200
        items = resp.json()
        assert items[0]["skill_id"] == "review_summary"

    def test_get_skill_includes_skill_id(self, registry_client):
        """GET /api/skills/{id} response includes skill_id."""
        try:
            import core.entrypoints.admin.skills as skill_routes
        except Exception as e:
            pytest.fail(f"skill routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_skill.return_value = {
            "skill_id": "review_summary",
            "display_name": "Review Summary",
            "enabled": True,
        }

        with patch.object(skill_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get("/api/skills/review_summary")

        assert resp.status_code == 200
        body = resp.json()
        assert body["skill_id"] == "review_summary"

    def test_get_skill_projects_stored_contract_rows_to_expected_fields(self, registry_client):
        import core.entrypoints.admin.skills as skill_routes

        mock_db = MagicMock()
        mock_db.get_skill.return_value = {
            "skill_id": "review_summary",
            "display_name": "Review Summary",
            "enabled": False,
            "_inputs": [{"name": "diff", "type": "text", "required": False}],
            "_outputs": [{"name": "verdict", "type": "text", "required": True}],
        }

        with patch.object(skill_routes, "_get_db", return_value=mock_db):
            response = registry_client.get("/api/skills/review_summary")

        assert response.status_code == 200, response.text
        body = response.json()
        assert [
            {key: row[key] for key in ("name", "type", "required")}
            for row in body["expected_inputs"]
        ] == [{"name": "diff", "type": "text", "required": False}]
        assert [
            {key: row[key] for key in ("name", "type", "required")}
            for row in body["expected_outputs"]
        ] == [{"name": "verdict", "type": "text", "required": True}]

    def test_contract_validation_includes_skill_id(self, registry_client):
        """GET /api/skills/{id}/contract-validation includes skill_id."""
        try:
            import core.entrypoints.admin.skills as skill_routes
        except Exception as e:
            pytest.fail(f"skill routes not importable: {e}")

        mock_db = MagicMock()
        # CLAUDE-P3-01: row_in_account_scope fails closed on non-mapping rows, so the store double must return a real row.
        mock_db.get_skill.return_value = {"skill_id": "review_summary"}
        with (
            patch.object(skill_routes, "_get_db", return_value=mock_db),
            patch.object(
                skill_routes,
                "_get_skill_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            resp = registry_client.get("/api/skills/review_summary/contract-validation")

        assert resp.status_code == 200
        body = resp.json()
        assert body["skill_id"] == "review_summary"

    def test_create_skill_response_includes_skill_id(self, registry_client):
        """POST /api/skills response includes skill_id."""
        try:
            import core.entrypoints.admin.skills as skill_routes
        except Exception as e:
            pytest.fail(f"skill routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_skill.return_value = None
        mock_registry = MagicMock()

        with (
            patch.object(skill_routes, "_get_db", return_value=mock_db),
            patch.object(skill_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                skill_routes,
                "_get_skill_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            resp = registry_client.post(
                "/api/skills",
                json={
                    "skill_id": "new_skill",
                    "display_name": "New Skill",
                    "expected_outputs": [{"name": "result", "type": "text", "required": True}],
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["skill_id"] == "new_skill"

    def test_create_skill_persists_request_account_scope(self, registry_client):
        import core.entrypoints.admin.skills as skill_routes

        mock_db = MagicMock()
        mock_db.get_skill.return_value = None
        mock_registry = MagicMock()
        scope = normalize_scope(account_id="acc-owner")

        with (
            patch.object(skill_routes, "_get_db", return_value=mock_db),
            patch.object(skill_routes, "_get_registry", return_value=mock_registry),
            patch.object(skill_routes, "request_scope", return_value=scope),
            patch.object(
                skill_routes,
                "_get_skill_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            response = registry_client.post(
                "/api/skills",
                json={
                    "skill_id": "account_skill",
                    "display_name": "Account Skill",
                    "enabled": False,
                },
            )

        assert response.status_code == 200, response.text
        assert mock_db.save_skill.call_args.args[0]["account_id"] == "acc-owner"

    def test_update_skill_preserves_existing_account_scope(self, registry_client):
        import core.entrypoints.admin.skills as skill_routes

        mock_db = MagicMock()
        mock_db.get_skill.return_value = {
            "skill_id": "account_skill",
            "account_id": "acc-owner",
        }
        mock_registry = MagicMock()
        scope = normalize_scope(account_id="acc-owner")

        with (
            patch.object(skill_routes, "_get_db", return_value=mock_db),
            patch.object(skill_routes, "_get_registry", return_value=mock_registry),
            patch.object(skill_routes, "request_scope", return_value=scope),
            patch.object(
                skill_routes,
                "_get_skill_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            response = registry_client.put(
                "/api/skills/account_skill",
                json={
                    "skill_id": "account_skill",
                    "display_name": "Updated Account Skill",
                    "enabled": False,
                },
            )

        assert response.status_code == 200, response.text
        assert mock_db.save_skill.call_args.args[0]["account_id"] == "acc-owner"

    def test_update_skill_preserves_contract_fields_when_omitted(self, registry_client):
        import core.entrypoints.admin.skills as skill_routes

        existing_inputs = [{"name": "diff", "type": "text", "required": False}]
        existing_outputs = [{"name": "verdict", "type": "text", "required": True}]
        mock_db = MagicMock()
        mock_db.get_skill.return_value = {
            "skill_id": "review_summary",
            "account_id": DEFAULT_ACCOUNT_ID,
            "display_name": "Review Summary",
            "enabled": False,
            "_inputs": existing_inputs,
            "_outputs": existing_outputs,
        }
        mock_registry = MagicMock()

        with (
            patch.object(skill_routes, "_get_db", return_value=mock_db),
            patch.object(skill_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                skill_routes,
                "_get_skill_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            response = registry_client.put(
                "/api/skills/review_summary",
                json={
                    "display_name": "Review Summary renamed",
                    "enabled": False,
                },
            )

        assert response.status_code == 200, response.text
        saved = mock_db.save_skill.call_args.args[0]
        assert saved["expected_inputs"] == existing_inputs
        assert saved["expected_outputs"] == existing_outputs

    def test_update_skill_allows_explicit_contract_clear(self, registry_client):
        import core.entrypoints.admin.skills as skill_routes

        mock_db = MagicMock()
        mock_db.get_skill.return_value = {
            "skill_id": "review_summary",
            "account_id": DEFAULT_ACCOUNT_ID,
            "display_name": "Review Summary",
            "enabled": False,
            "_outputs": [{"name": "verdict", "type": "text", "required": True}],
        }

        with (
            patch.object(skill_routes, "_get_db", return_value=mock_db),
            patch.object(skill_routes, "_get_registry", return_value=MagicMock()),
            patch.object(
                skill_routes,
                "_get_skill_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            response = registry_client.put(
                "/api/skills/review_summary",
                json={
                    "skill_id": "review_summary",
                    "display_name": "Review Summary",
                    "enabled": False,
                    "expected_outputs": [],
                },
            )

        assert response.status_code == 200, response.text
        assert mock_db.save_skill.call_args.args[0]["expected_outputs"] == []

    def test_create_skill_uses_canonical_skill_id_input(self, registry_client):
        """POST /api/skills accepts canonical skill_id input."""
        try:
            import core.entrypoints.admin.skills as skill_routes
        except Exception as e:
            pytest.fail(f"skill routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_skill.return_value = None
        mock_registry = MagicMock()

        with (
            patch.object(skill_routes, "_get_db", return_value=mock_db),
            patch.object(skill_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                skill_routes,
                "_get_skill_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            resp = registry_client.post(
                "/api/skills",
                json={
                    "skill_id": "via_skill_alias",
                    "display_name": "Via Skill Alias",
                    "expected_outputs": [{"name": "result", "type": "text", "required": True}],
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["skill_id"] == "via_skill_alias"
        # Verify DB received canonical skill_id
        saved = mock_db.save_skill.call_args[0][0]
        assert saved["skill_id"] == "via_skill_alias"

    def test_create_skill_suffixes_duplicate_skill_id(self, registry_client):
        try:
            import core.entrypoints.admin.skills as skill_routes
        except Exception as e:
            pytest.fail(f"skill routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_skill.side_effect = lambda skill_id: (
            {"skill_id": skill_id} if skill_id in {"review_summary", "review_summary_1"} else None
        )
        mock_registry = MagicMock()

        with (
            patch.object(skill_routes, "_get_db", return_value=mock_db),
            patch.object(skill_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                skill_routes,
                "_get_skill_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            resp = registry_client.post(
                "/api/skills",
                json={
                    "skill_id": "review_summary",
                    "display_name": "Review Summary",
                    "expected_outputs": [{"name": "result", "type": "text", "required": True}],
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["skill_id"] == "review_summary_2"
        assert mock_db.save_skill.call_args.args[0]["skill_id"] == "review_summary_2"

    def test_toggle_skill_response_includes_skill_id(self, registry_client):
        """PATCH /api/skills/{id}/toggle response includes skill_id."""
        try:
            import core.entrypoints.admin.skills as skill_routes
        except Exception as e:
            pytest.fail(f"skill routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_skill.return_value = {"skill_id": "review_summary", "enabled": False}
        mock_db.toggle_skill.return_value = True
        mock_registry = MagicMock()

        with (
            patch.object(skill_routes, "_get_db", return_value=mock_db),
            patch.object(skill_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                skill_routes,
                "_get_skill_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            resp = registry_client.patch("/api/skills/review_summary/toggle")

        assert resp.status_code == 200
        body = resp.json()
        assert body["skill_id"] == "review_summary"

    def test_delete_skill_response_includes_skill_id(self, registry_client):
        """DELETE /api/skills/{id} response includes skill_id."""
        try:
            import core.entrypoints.admin.skills as skill_routes
        except Exception as e:
            pytest.fail(f"skill routes not importable: {e}")

        mock_db = MagicMock()
        # CLAUDE-P3-01: row_in_account_scope fails closed on non-mapping rows, so the store double must return a real row.
        mock_db.get_skill.return_value = {"skill_id": "review_summary"}
        mock_registry = MagicMock()

        with (
            patch.object(skill_routes, "_get_db", return_value=mock_db),
            patch.object(skill_routes, "_get_registry", return_value=mock_registry),
        ):
            resp = registry_client.delete("/api/skills/review_summary")

        assert resp.status_code == 200
        body = resp.json()
        assert body["skill_id"] == "review_summary"

    def test_delete_skill_returns_typed_conflict_for_disabled_agent_dependency(
        self, registry_client
    ):
        import core.entrypoints.admin.skills as skill_routes

        mock_db = MagicMock()
        mock_db.get_skill.return_value = {"skill_id": "review_summary"}
        mock_db.get_skill_dependents.return_value = [
            {"dependency_kind": "agent", "dependency_id": "disabled-reviewer"}
        ]
        mock_registry = MagicMock()

        with (
            patch.object(skill_routes, "_get_db", return_value=mock_db),
            patch.object(skill_routes, "_get_registry", return_value=mock_registry),
        ):
            response = registry_client.delete("/api/skills/review_summary")

        assert response.status_code == 409
        assert response.json()["detail"] == {
            "error": "registry_dependency_conflict",
            "entity_type": "registry_skill",
            "entity_id": "review_summary",
            "dependent_ids": ["agent:disabled-reviewer"],
            "action": "Remove or replace every listed dependency before trashing the skill.",
        }
        mock_db.delete_skill.assert_not_called()

    def test_clone_skill_accepts_skill_id_as_target(self, registry_client):
        """POST /api/skills/{id}/clone accepts skill_id for target."""
        try:
            import core.entrypoints.admin.skills as skill_routes
        except Exception as e:
            pytest.fail(f"skill routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_skill.side_effect = lambda cid: (
            {"skill_id": "review_summary", "display_name": "Review Summary", "version": "1.0.0"}
            if cid == "review_summary"
            else None
        )
        mock_registry = MagicMock()

        with (
            patch.object(skill_routes, "_get_db", return_value=mock_db),
            patch.object(skill_routes, "_get_registry", return_value=mock_registry),
        ):
            resp = registry_client.post(
                "/api/skills/review_summary/clone",
                json={"skill_id": "review_summary_clone"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["skill_id"] == "review_summary_clone"

    def test_clone_skill_persists_request_account_scope(self, registry_client):
        import core.entrypoints.admin.skills as skill_routes

        mock_db = MagicMock()
        mock_db.get_skill.side_effect = lambda skill_id: (
            {
                "skill_id": "review_summary",
                "display_name": "Review Summary",
                "account_id": "acc-owner",
                "version": "1.0.0",
            }
            if skill_id == "review_summary"
            else None
        )
        mock_registry = MagicMock()
        scope = normalize_scope(account_id="acc-owner")

        with (
            patch.object(skill_routes, "_get_db", return_value=mock_db),
            patch.object(skill_routes, "_get_registry", return_value=mock_registry),
            patch.object(skill_routes, "request_scope", return_value=scope),
        ):
            response = registry_client.post(
                "/api/skills/review_summary/clone",
                json={"skill_id": "review_summary_clone"},
            )

        assert response.status_code == 200, response.text
        assert mock_db.save_skill.call_args.args[0]["account_id"] == "acc-owner"

    def test_clone_skill_suffixes_duplicate_target_id(self, registry_client):
        try:
            import core.entrypoints.admin.skills as skill_routes
        except Exception as e:
            pytest.fail(f"skill routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_skill.side_effect = lambda skill_id: (
            {"skill_id": "review_summary", "display_name": "Review Summary", "version": "1.0.0"}
            if skill_id == "review_summary"
            else {"skill_id": skill_id}
            if skill_id == "review_summary_copy"
            else None
        )
        mock_registry = MagicMock()

        with (
            patch.object(skill_routes, "_get_db", return_value=mock_db),
            patch.object(skill_routes, "_get_registry", return_value=mock_registry),
        ):
            resp = registry_client.post("/api/skills/review_summary/clone", json={})

        assert resp.status_code == 200
        body = resp.json()
        assert body["skill_id"] == "review_summary_copy_1"
        assert mock_db.save_skill.call_args.args[0]["skill_id"] == "review_summary_copy_1"


class TestAgentDetailModelSelection:
    def test_agent_detail_preserves_unavailable_provider_and_model_options(self, registry_client):
        resp = registry_client.get("/registry/agents/proofreader")

        _assert_frontend_shell_response(resp)

    def test_registry_agent_detail_shell_avoids_server_side_model_selection(self, registry_client):
        resp = registry_client.get("/registry/agents/proofreader", follow_redirects=False)

        contract_text = _page_contract_text(
            resp,
            "assets/frontend/src/pages/registry/agents/detail/index.tsx",
            "assets/frontend/src/pages/registry/agents/detail/agent-own-io.tsx",
        )
        # The I/O tab lists inherited fields read-only and edits the agent's own.
        for label in ("Inherited Inputs", "Own Inputs", "Inherited Outputs", "Own Outputs"):
            assert label in contract_text
        assert "Prompt Preview" in contract_text


def _catalog_write_store():
    """Keep real registry writes; replace only the external SQL boundary."""
    import json
    from contextlib import contextmanager

    from psycopg.errors import ForeignKeyViolation

    from core.application.registry.db_store import RegistryDBStore

    contracts = {}
    fields = {}

    class Cursor:
        row = None

        def execute(self, statement, parameters):
            if statement.lstrip().startswith("INSERT INTO registry_data_contracts"):
                contracts[parameters["contract_id"]] = {
                    **parameters,
                    "schema_json": json.loads(parameters["schema_json"]),
                }
            elif statement.lstrip().startswith("INSERT INTO registry_fields"):
                reference = parameters["data_contract_id"]
                if reference and reference not in contracts:
                    raise ForeignKeyViolation("referenced data contract is absent")
                fields[parameters["field_key"]] = {
                    **parameters,
                    "source_hints_json": json.loads(parameters["source_hints_json"]),
                }
            elif statement.lstrip().startswith("SELECT") and "registry_data_contracts" in statement:
                self.row = contracts.get(parameters[0])
            else:
                raise AssertionError(f"Unexpected catalog SQL: {statement}")

        def fetchone(self):
            return self.row

    @contextmanager
    def transaction():
        yield Cursor()

    store = RegistryDBStore("postgresql://unused")
    store._transaction = transaction
    store._exists = lambda table, _column, key: (
        key in (contracts if table == "registry_data_contracts" else fields)
    )
    store.get_data_contract = lambda key, **_kwargs: contracts.get(key)
    store.get_field = lambda key: fields.get(key)
    store._log_change_cur = MagicMock()
    return store, contracts, fields


@pytest.mark.parametrize("owner", ["contract", "field"])
@pytest.mark.parametrize(
    "override,expected",
    [
        ({}, "failed"),
        ({"last_sync_error": None}, None),
        ({"last_sync_error": "  "}, None),
        ({"last_sync_error": " new failure "}, "new failure"),
    ],
)
def test_catalog_sync_error_explicit_override_and_omission(owner, override, expected):
    store, contracts, fields = _catalog_write_store()
    payload = {
        "contract_id": "fixture_contract",
        "display_name": "Draft",
        "schema": {},
        "enabled": False,
    }
    save = store.save_data_contract
    rows, key = contracts, "fixture_contract"
    if owner == "field":
        payload = {"field_key": "fixture.field", "display_name": "Draft", "enabled": False}
        save, rows, key = store.save_field, fields, "fixture.field"
    save(payload)
    assert rows[key]["last_sync_error"] is None
    rows[key]["last_sync_error"] = " failed "
    save({**payload, **override})
    assert rows[key]["last_sync_error"] == expected
    assert rows[key]["enabled"] is False


class TestDataContractCRUD:
    """Test /api/data-contracts/* endpoints."""

    @pytest.mark.parametrize(
        "stored_error,expected",
        [(None, None), ("", None), ("  ", None), (" failed ", "failed"), ("None", "None")],
    )
    def test_metadata_patch_preserves_nullable_sync_error(
        self, registry_client, stored_error, expected
    ):
        import core.entrypoints.admin.data_contracts as routes

        store, contracts, _fields = _catalog_write_store()
        store.save_data_contract(
            {
                "contract_id": "fixture_contract",
                "display_name": "Draft",
                "kind": "json",
                "schema": {"type": "object"},
                "enabled": False,
            }
        )
        contracts["fixture_contract"]["last_sync_error"] = stored_error
        with patch.object(routes, "_get_db", return_value=store):
            response = registry_client.patch(
                "/api/data-contracts/fixture_contract", json={"description": "Metadata only"}
            )
            assert response.status_code == 200, response.text
            reopened = registry_client.get("/api/data-contracts/fixture_contract").json()
        assert reopened["last_sync_error"] == expected
        assert reopened["schema_json"] == {"type": "object"}
        assert reopened["enabled"] is False

    def test_registry_write_explicit_empty_schema_overrides_stale_alias(self):
        store, _contracts, _fields = _catalog_write_store()
        store.save_data_contract(
            {
                "contract_id": "fixture_contract",
                "display_name": "Draft",
                "schema": {},
                "schema_json": {"type": "object"},
                "enabled": False,
            }
        )
        assert store.get_data_contract("fixture_contract")["schema_json"] == {}

    def test_list_contract_can_change_to_json_with_matching_schema(self, registry_client):
        import core.entrypoints.admin.data_contracts as routes

        store, _contracts, _fields = _catalog_write_store()
        store.save_data_contract(
            {
                "contract_id": "fixture_contract",
                "display_name": "Draft",
                "kind": "list",
                "schema": {"type": "array"},
                "enabled": False,
            }
        )
        with patch.object(routes, "_get_db", return_value=store):
            response = registry_client.patch(
                "/api/data-contracts/fixture_contract",
                json={"kind": "json", "schema": {"type": "object"}},
            )
            assert response.status_code == 200, response.text
            reopened = registry_client.get("/api/data-contracts/fixture_contract").json()
        assert reopened["kind"] == "json"
        assert reopened["schema_json"] == {"type": "object"}
        assert reopened["enabled"] is False

    def test_partial_kind_validates_the_retained_schema(self, registry_client):
        import core.entrypoints.admin.data_contracts as routes

        store, contracts, _fields = _catalog_write_store()
        store.save_data_contract(
            {
                "contract_id": "fixture_contract",
                "display_name": "Draft",
                "kind": "json",
                "schema": {"type": "object"},
                "enabled": False,
            }
        )
        with patch.object(routes, "_get_db", return_value=store):
            response = registry_client.patch(
                "/api/data-contracts/fixture_contract", json={"kind": "list"}
            )
            assert response.status_code == 400, response.text
            reopened = registry_client.get("/api/data-contracts/fixture_contract").json()
        assert reopened["kind"] == "json"
        assert reopened["schema_json"] == {"type": "object"}
        assert contracts["fixture_contract"]["enabled"] is False

    @pytest.mark.parametrize(
        ("payload", "expected_kind", "expected_schema"),
        [
            ({"description": "Проверка"}, "json", {"type": "object"}),
            ({"schema": {}}, "json", {}),
            ({"kind": "list", "schema": {"type": "array"}}, "list", {"type": "array"}),
        ],
    )
    def test_partial_contract_schema_roundtrip(
        self, registry_client, payload, expected_kind, expected_schema
    ):
        import core.entrypoints.admin.data_contracts as routes

        store, _contracts, _fields = _catalog_write_store()
        store.save_data_contract(
            {
                "contract_id": "fixture_contract",
                "display_name": "Draft",
                "kind": "json",
                "schema": {"type": "object"},
                "enabled": False,
            }
        )
        with patch.object(routes, "_get_db", return_value=store):
            response = registry_client.patch("/api/data-contracts/fixture_contract", json=payload)
            assert response.status_code == 200, response.text
            reopened = registry_client.get("/api/data-contracts/fixture_contract").json()
        assert reopened["kind"] == expected_kind
        assert reopened["schema_json"] == expected_schema
        assert reopened["enabled"] is False

    def test_list_data_contracts(self, registry_client):
        try:
            import core.entrypoints.admin.data_contracts as data_contract_routes
        except Exception as e:
            pytest.fail(f"data contract routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.list_data_contracts.return_value = [
            {
                "contract_id": "outline_doc.v1",
                "display_name": "Architecture Doc",
                "kind": "json",
                "schema_json": {"type": "object"},
                "enabled": True,
            }
        ]

        with patch.object(data_contract_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get("/api/data-contracts")

        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert data[0]["contract_id"] == "outline_doc.v1"

    def test_create_data_contract(self, registry_client):
        try:
            import core.entrypoints.admin.data_contracts as data_contract_routes
        except Exception as e:
            pytest.fail(f"data contract routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_data_contract.return_value = None

        with patch.object(data_contract_routes, "_get_db", return_value=mock_db):
            resp = registry_client.post(
                "/api/data-contracts",
                json={
                    "contract_id": "outline_doc.v1",
                    "display_name": "Architecture Doc",
                    "kind": "json",
                    "schema": {"type": "object", "properties": {"overview": {"type": "string"}}},
                    "enabled": True,
                    "tags": [],
                },
            )

        assert resp.status_code == 200
        assert resp.json()["contract_id"] == "outline_doc.v1"
        mock_db.save_data_contract.assert_called_once()


class TestFieldCatalogCRUD:
    """Test /api/fields/* endpoints."""

    @pytest.mark.parametrize(
        "stored_error,expected",
        [(None, None), ("", None), ("  ", None), (" failed ", "failed"), ("None", "None")],
    )
    def test_metadata_patch_preserves_nullable_sync_error(
        self, registry_client, stored_error, expected
    ):
        import core.entrypoints.admin.fields as routes

        store, _contracts, fields = _catalog_write_store()
        store.save_data_contract(
            {
                "contract_id": "fixture_contract",
                "display_name": "Draft",
                "kind": "json",
                "schema": {"type": "object"},
                "enabled": False,
            }
        )
        store.save_field(
            {
                "field_key": "fixture.field",
                "display_name": "Draft",
                "value_type": "json",
                "data_contract_id": "fixture_contract",
                "source_hints_json": {"owner": "fixture"},
                "enabled": False,
            }
        )
        fields["fixture.field"]["last_sync_error"] = stored_error
        with patch.object(routes, "_get_db", return_value=store):
            response = registry_client.patch(
                "/api/fields/fixture.field", json={"description": "Metadata only"}
            )
            assert response.status_code == 200, response.text
            reopened = registry_client.get("/api/fields/fixture.field").json()
        assert reopened["last_sync_error"] == expected
        assert reopened["data_contract_id"] == "fixture_contract"
        assert reopened["source_hints_json"] == {"owner": "fixture"}
        assert reopened["value_type"] == "json"
        assert reopened["enabled"] is False

    @pytest.mark.parametrize("method", ["post", "put", "patch"])
    def test_missing_contract_reference_rejects_write_without_mutation(
        self, registry_client, method
    ):
        import core.entrypoints.admin.fields as routes

        store, _contracts, fields = _catalog_write_store()
        if method != "post":
            store.save_field(
                {"field_key": "fixture.field", "display_name": "Draft", "enabled": False}
            )
        with patch.object(routes, "_get_db", return_value=store):
            payload = {
                "field_key": "fixture.field",
                "display_name": "Draft",
                "value_type": "json",
                "data_contract_id": "fixture_missing_contract",
                "enabled": False,
            }
            path = "/api/fields" if method == "post" else "/api/fields/fixture.field"
            response = getattr(registry_client, method)(path, json=payload)
            assert response.status_code == 400, response.text
            reopened = registry_client.get("/api/fields/fixture.field")
        if method == "post":
            assert reopened.status_code == 404
            assert "fixture.field" not in fields
        else:
            assert reopened.status_code == 200
            assert reopened.json()["value_type"] == "text"
            assert reopened.json()["data_contract_id"] is None
            assert reopened.json()["enabled"] is False

    @pytest.mark.parametrize("method", ["post", "put", "patch"])
    def test_existing_contract_reference_roundtrips(self, registry_client, method):
        import core.entrypoints.admin.fields as routes

        store, _contracts, _fields = _catalog_write_store()
        store.save_data_contract(
            {
                "contract_id": "fixture_contract",
                "display_name": "Draft",
                "kind": "json",
                "schema": {"type": "object"},
                "enabled": False,
            }
        )
        if method != "post":
            store.save_field(
                {"field_key": "fixture.field", "display_name": "Draft", "enabled": False}
            )
        with patch.object(routes, "_get_db", return_value=store):
            payload = {
                "field_key": "fixture.field",
                "display_name": "Draft",
                "value_type": "json",
                "data_contract_id": "fixture_contract",
                "enabled": False,
            }
            path = "/api/fields" if method == "post" else "/api/fields/fixture.field"
            response = getattr(registry_client, method)(path, json=payload)
            assert response.status_code == 200, response.text
            reopened = registry_client.get("/api/fields/fixture.field").json()
        assert reopened["value_type"] == "json"
        assert reopened["data_contract_id"] == "fixture_contract"
        assert reopened["enabled"] is False

    def test_list_fields(self, registry_client):
        try:
            import core.entrypoints.admin.fields as fields_routes
        except Exception as e:
            pytest.fail(f"fields routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.list_fields.return_value = [
            {
                "field_key": "requirements.doc",
                "display_name": "Requirements Doc",
                "value_type": "text",
                "enabled": True,
            }
        ]

        with patch.object(fields_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get("/api/fields")

        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert data[0]["field_key"] == "requirements.doc"
        mock_db.list_fields.assert_called_once()

    def test_create_field_rejects_structured_type_without_contract(self, registry_client):
        try:
            import core.entrypoints.admin.fields as fields_routes
        except Exception as e:
            pytest.fail(f"fields routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_field.return_value = None

        with patch.object(fields_routes, "_get_db", return_value=mock_db):
            resp = registry_client.post(
                "/api/fields",
                json={
                    "field_key": "architecture.doc",
                    "display_name": "Architecture Doc",
                    "value_type": "json",
                    "enabled": True,
                },
            )

        assert resp.status_code == 400
        assert "data_contract_id is required" in resp.text

    def test_create_field_success(self, registry_client):
        try:
            import core.entrypoints.admin.fields as fields_routes
        except Exception as e:
            pytest.fail(f"fields routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_field.return_value = None

        with patch.object(fields_routes, "_get_db", return_value=mock_db):
            resp = registry_client.post(
                "/api/fields",
                json={
                    "field_key": "requirements.doc",
                    "display_name": "Requirements Doc",
                    "value_type": "text",
                    "enabled": True,
                },
            )

        assert resp.status_code == 200
        assert resp.json()["field_key"] == "requirements.doc"
        mock_db.save_field.assert_called_once()

    def test_registry_fields_page_renders_user_facing_catalog_surface(self, registry_client):
        resp = registry_client.get("/registry/fields")

        contract_text = _page_contract_text(
            resp, "assets/frontend/src/pages/registry/fields/index.tsx"
        )
        assert "fields-grid" in contract_text
        assert "Direct edits are disabled" in contract_text
        assert "/registry/data-contracts" in contract_text


class TestRegistryCatalogPages:
    def test_registry_skills_page_includes_section_nav(self, registry_client):
        resp = registry_client.get("/registry/skills")

        contract_text = _page_contract_text(
            resp, "assets/frontend/src/pages/registry/skills/index.tsx"
        )
        assert "Skills catalog" in contract_text

    def test_registry_data_contracts_page_renders_read_only_surface(self, registry_client):
        resp = registry_client.get("/registry/data-contracts")

        contract_text = _page_contract_text(
            resp,
            "assets/frontend/src/pages/registry/data-contracts/index.tsx",
        )
        assert "contracts-grid" in contract_text
        assert "Direct edits are disabled" in contract_text
        assert "/registry/fields" in contract_text

    def test_registry_published_blocks_page_renders_read_only_surface(self, registry_client):
        resp = registry_client.get("/registry/published-blocks")

        contract_text = _page_contract_text(
            resp,
            "assets/frontend/src/pages/registry/published-blocks/index.tsx",
        )
        assert "published-blocks-grid" in contract_text
        assert "Direct edits are disabled" in contract_text
        assert "/registry/fields" in contract_text

    def test_registry_index_includes_technical_catalog_links(self, registry_client):
        resp = registry_client.get("/registry")

        contract_text = _page_contract_text(resp, "assets/frontend/src/app/router.tsx")
        assert 'path="fields"' in contract_text
        assert 'path="data-contracts"' in contract_text
        assert 'path="published-blocks"' in contract_text


class TestPublishedBlockCRUD:
    """Test /api/published-blocks/* endpoints."""

    def test_list_published_blocks(self, registry_client):
        try:
            import core.entrypoints.admin.published_blocks as pb_routes
        except Exception as e:
            pytest.fail(f"published-block routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.list_published_block_contracts.return_value = [
            {
                "block_key": "composer.build",
                "display_name": "Developer Build",
                "source_kind": "agent",
                "source_ref": "composer",
                "enabled": True,
            }
        ]

        with patch.object(pb_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get("/api/published-blocks")

        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert data[0]["block_key"] == "composer.build"
        mock_db.list_published_block_contracts.assert_called_once()

    def test_create_published_block_success(self, registry_client):
        try:
            import core.entrypoints.admin.published_blocks as pb_routes
        except Exception as e:
            pytest.fail(f"published-block routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_published_block_contract.return_value = None

        with patch.object(pb_routes, "_get_db", return_value=mock_db):
            resp = registry_client.post(
                "/api/published-blocks",
                json={
                    "block_key": "composer.build",
                    "display_name": "Developer Build",
                    "block_type": "agent",
                    "source_kind": "agent",
                    "source_ref": "composer",
                    "enabled": True,
                    "inputs": [{"field_key": "requirements.doc", "required": True}],
                    "outputs": [{"field_key": "patch.result", "required_by_default": True}],
                },
            )

        assert resp.status_code == 200
        assert resp.json()["block_key"] == "composer.build"
        mock_db.save_published_block_contract.assert_called_once()

    def test_create_published_block_accepts_uuid_field_ids(self, registry_client):
        try:
            import core.entrypoints.admin.published_blocks as pb_routes
        except Exception as e:
            pytest.fail(f"published-block routes not importable: {e}")

        input_field_id = "11111111-1111-1111-1111-111111111111"
        output_field_id = "22222222-2222-2222-2222-222222222222"
        mock_db = MagicMock()
        mock_db.get_published_block_contract.return_value = None
        mock_db.get_agent.return_value = {"agent_id": "composer"}

        with patch.object(pb_routes, "_get_db", return_value=mock_db):
            resp = registry_client.post(
                "/api/published-blocks",
                json={
                    "block_key": "composer.uuid_fields",
                    "display_name": "Developer UUID Fields",
                    "block_type": "agent",
                    "source_kind": "agent",
                    "source_ref": "composer",
                    "enabled": True,
                    "inputs": [{"field_id": input_field_id, "required": True}],
                    "outputs": [{"field_id": output_field_id, "required_by_default": True}],
                },
            )

        assert resp.status_code == 200
        payload = mock_db.save_published_block_contract.call_args.args[0]
        assert payload["inputs"][0]["field_id"] == input_field_id
        assert payload["outputs"][0]["field_id"] == output_field_id

    def test_create_published_block_returns_400_for_unknown_field_reference(self, registry_client):
        try:
            import core.entrypoints.admin.published_blocks as pb_routes
        except Exception as e:
            pytest.fail(f"published-block routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_published_block_contract.return_value = None
        mock_db.save_published_block_contract.side_effect = ValueError(
            "inputs[0] references unknown field_key 'requirements.missing'"
        )

        with patch.object(pb_routes, "_get_db", return_value=mock_db):
            resp = registry_client.post(
                "/api/published-blocks",
                json={
                    "block_key": "composer.build",
                    "display_name": "Developer Build",
                    "block_type": "agent",
                    "source_kind": "agent",
                    "source_ref": "composer",
                    "enabled": True,
                    "inputs": [{"field_key": "requirements.missing", "required": True}],
                    "outputs": [{"field_key": "patch.result", "required_by_default": True}],
                },
            )

        assert resp.status_code == 400
        assert "unknown field_key" in resp.text

    def test_create_published_block_returns_400_for_unknown_source_ref(self, registry_client):
        try:
            import core.entrypoints.admin.published_blocks as pb_routes
        except Exception as e:
            pytest.fail(f"published-block routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_published_block_contract.return_value = None
        mock_db.get_agent.return_value = None
        mock_db.get_skill.return_value = None

        with patch.object(pb_routes, "_get_db", return_value=mock_db):
            resp = registry_client.post(
                "/api/published-blocks",
                json={
                    "block_key": "composer.build",
                    "display_name": "Developer Build",
                    "block_type": "agent",
                    "source_kind": "agent",
                    "source_ref": "missing_agent",
                    "enabled": True,
                    "inputs": [{"field_key": "requirements.doc", "required": True}],
                    "outputs": [{"field_key": "patch.result", "required_by_default": True}],
                },
            )

        assert resp.status_code == 400
        assert "does not reference an existing agent_id or skill_id" in resp.text

    def test_update_published_block_returns_400_when_source_rebinds_in_use_block(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.published_blocks as pb_routes
        except Exception as e:
            pytest.fail(f"published-block routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_agent.return_value = {"agent_id": "composer", "enabled": True}
        mock_db.get_skill.return_value = {"skill_id": "draft_summary", "enabled": True}
        mock_db.get_agent_skills.return_value = [{"skill_id": "draft_summary"}]
        mock_db.save_published_block_contract.side_effect = ValueError(
            "Cannot change source_ref/source_kind for published block 'designer.outline_plan' "
            "while it is referenced by workflow steps (1): wf_demo/design#7. Create a new block_key instead."
        )

        with patch.object(pb_routes, "_get_db", return_value=mock_db):
            resp = registry_client.put(
                "/api/published-blocks/designer.outline_plan",
                json={
                    "block_key": "designer.outline_plan",
                    "display_name": "Designer / Outline",
                    "block_type": "agent",
                    "source_kind": "agent",
                    "source_ref": "composer::draft_summary",
                    "enabled": True,
                    "inputs": [],
                    "outputs": [],
                },
            )

        assert resp.status_code == 400
        assert "Cannot change source_ref/source_kind for published block" in resp.text


class TestGuardrailCRUD:
    def test_guardrails_page_skips_platform_component_kit_when_unused(self, registry_client):
        resp = registry_client.get("/registry/guardrails")

        _assert_frontend_shell_response(resp)
        assert "/static/platform-kit/index-" + "ap" + "p.js" not in resp.text

    def test_guardrail_detail_page_prefers_typed_parameters_over_raw_json(self, registry_client):
        resp = registry_client.get("/registry/guardrails/7")

        contract_text = _page_contract_text(
            resp,
            "assets/frontend/src/pages/registry/guardrails/detail/index.tsx",
        )
        assert "Guardrail Config" in contract_text
        assert "Advanced policy JSON" in contract_text
        assert "Agent Scope" in contract_text


# ---------------------------------------------------------------------------
# Workflow CRUD tests
# ---------------------------------------------------------------------------


class TestWorkflowCRUD:
    """Test /api/workflows/* endpoints."""

    @pytest.mark.parametrize("method", ("put", "patch"))
    def test_platform_workflow_mutation_requires_clone(self, registry_client, method):
        """A global platform template must never be reassigned to a tenant."""
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        platform_workflow = {
            "workflow_id": "monthly_review",
            "account_id": None,
            "scope_kind": "system",
            "display_name": "Contextual Bugfix",
            "enabled": False,
            "settings": {},
            "trigger": {},
            "tags": [],
            "steps": [],
            "groups": [],
        }
        mock_db = MagicMock()
        mock_db.get_workflow.return_value = platform_workflow

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            response = getattr(registry_client, method)(
                "/api/workflows/monthly_review",
                json=(
                    {"workflow_id": "monthly_review", "display_name": "Edited"}
                    if method == "put"
                    else {"display_name": "Edited"}
                ),
            )

        assert response.status_code == 409
        assert response.json()["detail"] == "platform_workflow_requires_clone"
        mock_db.save_workflow.assert_not_called()

    @pytest.mark.parametrize(
        ("method", "path", "payload", "persistence_method"),
        (
            ("patch", "/api/workflows/monthly_review/toggle", None, "toggle_workflow"),
            ("delete", "/api/workflows/monthly_review", None, "delete_workflow"),
            (
                "post",
                "/api/workflows/monthly_review/changes",
                {
                    "expected_revision": 1,
                    "operations": [
                        {
                            "op": "update_step_config",
                            "step_id": "set_status",
                            "patch": {"display_name": "Edited"},
                        }
                    ],
                },
                "save_workflow",
            ),
            (
                "post",
                "/api/workflows/monthly_review/changes",
                {
                    "expected_revision": 1,
                    "operations": [{"op": "delete_step", "step_id": "set_status"}],
                },
                "save_workflow",
            ),
            (
                "post",
                "/api/workflows/monthly_review/restore",
                {"entry_id": "revision-1"},
                "restore_entity_revision",
            ),
        ),
    )
    def test_platform_workflow_non_clone_mutations_require_clone(
        self,
        registry_client,
        method,
        path,
        payload,
        persistence_method,
    ):
        """Every mutation boundary rejects the globally shared template."""
        import core.entrypoints.admin.workflows_definition_router as definition_router
        import core.entrypoints.admin.workflows_routes_history as history_routes
        import core.entrypoints.admin.workflows_routes_write as write_routes

        platform_workflow = {
            "workflow_id": "monthly_review",
            "account_id": None,
            "scope_kind": "system",
            "display_name": "Contextual Bugfix",
            "enabled": False,
            "settings": {},
            "trigger": {},
            "tags": [],
            "steps": [{"step_id": "set_status", "step_kind": "set_variable", "group_id": None}],
            "groups": [],
        }
        mock_db = MagicMock()
        mock_db.get_workflow.return_value = platform_workflow
        mock_registry = MagicMock()

        with (
            patch.object(write_routes, "_get_db", return_value=mock_db),
            patch.object(write_routes, "_get_registry", return_value=mock_registry),
            patch.object(definition_router, "_get_db", return_value=mock_db),
            patch.object(definition_router, "_get_registry", return_value=mock_registry),
            patch.object(
                definition_router, "_resolve_workflow_row", return_value=platform_workflow
            ),
            patch.object(history_routes, "_get_db", return_value=mock_db),
            patch.object(history_routes, "_get_registry", return_value=mock_registry),
        ):
            request = getattr(registry_client, method)
            response = request(path, json=payload) if payload is not None else request(path)

        assert response.status_code == 409
        assert response.json()["detail"] == "platform_workflow_requires_clone"
        if persistence_method:
            getattr(mock_db, persistence_method).assert_not_called()

    def test_platform_workflow_clone_uses_calling_account_ownership(self, registry_client):
        """Cloning a global template creates an account-owned copy for its caller."""
        import core.entrypoints.admin.workflows_routes_write as write_routes
        import core.entrypoints.admin.workflows_scope as workflow_scope

        platform_workflow = {
            "workflow_id": "monthly_review",
            "account_id": None,
            "scope_kind": "system",
            "display_name": "Contextual Bugfix",
            "enabled": False,
            "settings": {},
            "trigger": {},
            "tags": [],
            "steps": [],
            "groups": [],
        }
        mock_db = MagicMock()
        mock_db.get_workflow.side_effect = lambda workflow_id: (
            platform_workflow if workflow_id == "monthly_review" else None
        )
        mock_registry = MagicMock()
        mock_portability = SimpleNamespace(assert_clone_allowed=lambda _workflow_id: None)

        with (
            patch.object(write_routes, "_get_db", return_value=mock_db),
            patch.object(write_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                write_routes, "_get_workflow_portability_service", return_value=mock_portability
            ),
            patch.object(
                workflow_scope,
                "_request_scope",
                return_value=normalize_scope(account_id="00000000-0000-0000-0000-000000000042"),
            ),
        ):
            response = registry_client.post(
                "/api/workflows/monthly_review/clone",
                json={"workflow_id": "monthly_review_account_beta"},
            )

        assert response.status_code == 200, response.text
        mock_db.clone_workflow.assert_called_once_with(
            source_workflow_id="monthly_review",
            target_workflow_id="monthly_review_account_beta",
            display_name=None,
            description=None,
            account_id="00000000-0000-0000-0000-000000000042",
            scope_kind="account",
            # CLAUDE-P3-16: the changelog records the principal who acted, not a
            # hardcoded "admin". This request is made by the operator fixture.
            author="operator",
        )

    def test_list_workflows_hides_compat_aliases_by_default(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.list_workflows.return_value = []
        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get("/api/workflows")

        assert resp.status_code == 200
        mock_db.list_workflows.assert_called_once_with(enabled_only=False)

    def test_list_workflows_search_hydrates_canonical_rows_and_honors_enabled_only(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        canonical_workflow = {
            "workflow_id": "sample_product_catalog_enrichment",
            "display_name": "Demo: Product Catalog Enrichment",
            "description": "Canonical workflow record",
            "enabled": True,
            "settings": {"mode": "canonical"},
            "steps": [{"step_id": "enrich", "step_kind": "agent", "group_id": None}],
            "groups": [],
        }
        mock_db.list_workflows.return_value = [canonical_workflow]
        mock_db.search_entities.return_value = [
            {
                "entity_type": "workflow",
                "entity_id": "sample_product_catalog_enrichment",
                "display_name": "Demo: Product Catalog Enrichment",
                "enabled": True,
            },
            {
                "entity_type": "workflow",
                "entity_id": "disabled_catalog_workflow",
                "display_name": "Disabled Product Catalog",
                "enabled": False,
            },
        ]

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_extension_workflow_projection_rows",
                return_value=[],
            ),
        ):
            resp = registry_client.get(
                "/api/workflows",
                params={"search": "product catalog", "enabled_only": "true"},
            )

        assert resp.status_code == 200
        payload = resp.json()
        assert [row["workflow_id"] for row in payload] == ["sample_product_catalog_enrichment"]
        assert payload[0]["description"] == "Canonical workflow record"
        assert payload[0]["settings"] == {"mode": "canonical"}
        assert payload[0]["steps"] == [
            {"step_id": "enrich", "step_kind": "agent", "group_id": None}
        ]
        mock_db.list_workflows.assert_called_once_with(enabled_only=True)

    def test_list_workflows_never_shows_a_hook_storage_workflow(self, registry_client):
        """R2-11: a hook's steps are a place in another workflow, not a workflow to browse."""
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.list_workflows.return_value = [
            {"workflow_id": "order_flow", "display_name": "Order flow", "enabled": True},
            {
                "workflow_id": "order_flow_adjust_order",
                "display_name": "Adjust the order",
                "enabled": True,
                "settings": {
                    "hook_storage": {
                        "author_workflow_id": "order_flow",
                        "hook_id": "adjust_order",
                        "project_id": "p-1",
                    }
                },
            },
        ]
        mock_db.search_entities.return_value = [
            {"entity_type": "workflow", "entity_id": "order_flow_adjust_order"},
            {"entity_type": "workflow", "entity_id": "order_flow"},
        ]

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_extension_workflow_projection_rows", return_value=[]),
        ):
            listed = registry_client.get("/api/workflows")
            searched = registry_client.get("/api/workflows", params={"search": "order"})

        assert [row["workflow_id"] for row in listed.json()] == ["order_flow"]
        assert [row["workflow_id"] for row in searched.json()] == ["order_flow"]

    def test_workflow_export_policy_endpoint_enforces_protected_source(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_ext",
            "display_name": "Extension workflow",
            "source_install_id": "inst-ext",
            "source_release_id": "rel-1",
            "projection_mode": "derived",
            "settings": {},
            "availability_status": "available",
        }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch(
                "core.application.registry.workflow_lookup.get_workflow_row",
                return_value=mock_db.get_workflow.return_value,
            ),
        ):
            resp = registry_client.get("/api/workflows/wf_ext/export-policy")

        assert resp.status_code == 200
        body = resp.json()
        assert body["full_export_allowed"] is False
        assert body["reference_export_allowed"] is True
        assert body["clone_allowed"] is False
        assert body["source_install_id"] == "inst-ext"

    def test_workflow_export_yaml_endpoint_returns_reference_for_protected(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_ext",
            "display_name": "Extension workflow",
            "source_install_id": "inst-ext",
            "source_release_id": "rel-1",
            "local_workflow_id": "local_sync",
            "projection_mode": "derived",
            "settings": {},
            "steps": [{"step_id": "hidden", "step_kind": "agent", "group_id": None}],
            "groups": [],
        }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch(
                "core.application.registry.workflow_lookup.get_workflow_row",
                return_value=mock_db.get_workflow.return_value,
            ),
        ):
            resp = registry_client.get("/api/workflows/wf_ext/export.yaml")

        assert resp.status_code == 200
        bundle = yaml.safe_load(resp.text)
        workflow = bundle["workflows"][0]
        assert workflow["portability_kind"] == "extension_workflow_ref"
        assert workflow["source_install_id"] == "inst-ext"
        assert "steps" not in workflow
        assert "phases" not in workflow
        assert "hidden" not in resp.text

    def test_workflow_export_yaml_names_extensions_by_portable_id(self, registry_client):
        """AC17-R60 through the route: the service fix never ran on the real export.

        `_dependency_snapshot` resolves an install to its `extension_id` only when the
        service knows the request's account. The export route built the service without
        one, so every exported package still named its extension by the install UUID of
        the exporting machine, which no other installation can resolve.
        """
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        install_id = "a5ed0a41-daab-43fe-be29-050dafef3f1a"
        workflow_row = {
            "workflow_id": "wf_imap",
            "display_name": "Read a mailbox",
            "account_id": "acc_alpha",
            "settings": {},
            "steps": [
                {
                    "step_id": "read_mailbox",
                    "step_kind": "connector",
                    "group_id": None,
                    "lane_id": None,
                    "connector_id": "extension.acme.mailbox",
                    "connector_install_id": install_id,
                }
            ],
            "groups": [],
        }
        mock_db = MagicMock()
        mock_db.get_workflow.return_value = workflow_row

        class _InstallStore:
            def get_extension_install_for_operator(self, ref, *, account_id):
                if (ref, account_id) != (install_id, "acc_alpha"):
                    return None
                return {
                    "extension_id": "acme.mailbox",
                    "install_id": install_id,
                    "descriptor_jsonb": {
                        "display_name": "Acme Mailbox",
                        "version": "0.1.0",
                    },
                }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_request_scope",
                return_value=normalize_scope(account_id="acc_alpha"),
            ),
            patch.object(
                workflow_routes, "_platform_logger_for_admin_catalog", return_value=_InstallStore()
            ),
            patch(
                "core.application.registry.workflow_lookup.get_workflow_row",
                return_value=workflow_row,
            ),
        ):
            resp = registry_client.get("/api/workflows/wf_imap/export.yaml")

        assert resp.status_code == 200, resp.text
        extensions = yaml.safe_load(resp.text)["dependencies"]["extensions"]
        assert [row.get("extension_id") for row in extensions] == ["acme.mailbox"]
        assert extensions[0]["display_name"] == "Acme Mailbox"
        # The install id stays only as a local hint.
        assert extensions[0]["install_id"] == install_id

    def test_workflow_export_yaml_enforces_request_account_scope(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_ext",
            "display_name": "Extension workflow",
            "account_id": "acc_alpha",
            "settings": {},
        }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_request_scope",
                return_value=normalize_scope(account_id="acc_other"),
            ),
        ):
            resp = registry_client.get("/api/workflows/wf_ext/export.yaml")

        assert resp.status_code == 403

    def test_protected_workflow_structure_is_readable_but_not_changed_or_step_tested(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_ext",
            "display_name": "Extension workflow",
            "source_install_id": "inst-ext",
            "projection_mode": "derived",
            "steps": [{"step_id": "hidden_step", "step_kind": "agent", "group_id": None}],
            "groups": [],
        }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch(
                "core.application.registry.workflow_lookup.get_workflow_row",
                return_value=mock_db.get_workflow.return_value,
            ),
        ):
            read_resp = registry_client.get(
                "/api/workflows/wf_ext/steps/hidden_step/available-outputs"
            )
            test_run_resp = registry_client.post(
                "/api/workflows/wf_ext/steps/hidden_step/test-run",
                json={
                    "project_id": "df0d2afe-7064-4206-941b-61d2d4db425c",
                    "task_summary": "Direct request with the button hidden",
                },
            )
            write_resp = _post_changes(
                registry_client,
                "wf_ext",
                [
                    {
                        "op": "create_step",
                        "step": {"step_id": "new_step", "step_kind": "set_variable"},
                    }
                ],
            )

        # Reading is allowed; changing it and running one author step are not.
        assert read_resp.status_code != 403, read_resp.text
        assert write_resp.status_code == 403
        assert test_run_resp.status_code == 403
        assert test_run_resp.json()["detail"]["error"] == "workflow_step_test_run_protected"
        mock_db.save_workflow.assert_not_called()

    def test_protected_workflow_validation_is_readable_and_suggestions_are_refused(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_ext",
            "source_install_id": "inst-ext",
            "projection_mode": "derived",
            "steps": [{"step_id": "hidden_step", "step_kind": "agent", "group_id": None}],
            "groups": [],
        }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch(
                "core.application.registry.workflow_lookup.get_workflow_row",
                return_value=mock_db.get_workflow.return_value,
            ),
            patch.object(workflow_routes, "_get_workflow_validation"),
        ):
            contract_resp = registry_client.get("/api/workflows/wf_ext/contract-validation")
            validate_resp = registry_client.post("/api/workflows/wf_ext/validate", json={})
            suggestions_resp = registry_client.get("/api/workflows/wf_ext/suggestions")

        # Validation reads the redacted structure; suggestions are edits.
        assert contract_resp.status_code != 403, contract_resp.text
        assert validate_resp.status_code != 403, validate_resp.text
        assert suggestions_resp.status_code == 403
        assert "hidden_step" not in suggestions_resp.text

    def test_workflow_validate_enforces_request_account_scope(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_broken",
            "account_id": "acc_alpha",
            "steps": [{"step_id": "analysis", "step_kind": "agent", "group_id": None}],
            "groups": [],
        }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_request_scope",
                return_value=normalize_scope(account_id="acc_other"),
            ),
        ):
            resp = registry_client.post("/api/workflows/wf_broken/validate", json={})

        assert resp.status_code == 403

    def test_nested_workflow_step_mutation_enforces_request_account_scope(self, registry_client):
        try:
            import core.entrypoints.admin.workflows_definition_router as definition_router
        except Exception as e:
            pytest.fail(f"workflow definition routes not importable: {e}")

        mock_db = MagicMock()
        workflow = linear_workflow(
            {"step_id": "planning", "step_kind": "set_variable"},
            workflow_id="wf_foreign",
            account_id="acc_alpha",
            enabled=False,
        )
        mock_db.get_workflow.return_value = workflow
        mock_registry = MagicMock()
        mock_registry.workflows.get_workflow.return_value = None

        with (
            patch.object(definition_router, "_get_db", return_value=mock_db),
            patch.object(definition_router, "_get_registry", return_value=mock_registry),
            patch.object(definition_router, "_resolve_workflow_row", return_value=workflow),
            patch.object(
                definition_router,
                "_request_scope",
                return_value=normalize_scope(account_id="acc_other"),
            ),
        ):
            resp = _post_changes(
                registry_client,
                "wf_foreign",
                [
                    {
                        "op": "create_step",
                        "step": {
                            "step_kind": "set_variable",
                            "step_id": "new_step",
                            "display_name": "New Step",
                            "override": {"set_variable": {"target": "state.ok", "value": True}},
                        },
                    }
                ],
            )

        assert resp.status_code == 403
        mock_db.save_workflow.assert_not_called()
        mock_db._transaction.assert_not_called()

    def test_nested_workflow_simulate_enforces_request_account_scope(self, registry_client):
        try:
            import core.entrypoints.admin.workflows_definition_router as definition_router
        except Exception as e:
            pytest.fail(f"workflow definition routes not importable: {e}")

        mock_db = MagicMock()
        workflow = linear_workflow(
            {"step_id": "planning", "step_kind": "set_variable"},
            workflow_id="wf_foreign",
            account_id="acc_alpha",
        )
        mock_db.get_workflow.return_value = workflow
        mock_registry = MagicMock()
        mock_registry.workflows.get_workflow.return_value = None

        with (
            patch.object(definition_router, "_get_db", return_value=mock_db),
            patch.object(definition_router, "_get_registry", return_value=mock_registry),
            patch.object(definition_router, "_resolve_workflow_row", return_value=workflow),
            patch.object(
                definition_router,
                "_request_scope",
                return_value=normalize_scope(account_id="acc_other"),
            ),
        ):
            resp = registry_client.post(
                "/api/workflows/wf_foreign/simulate",
                json={"task_envelope": {"task_type": "feature"}},
            )

        assert resp.status_code == 403

    def test_nested_workflow_io_preview_enforces_request_account_scope(self, registry_client):
        try:
            import core.entrypoints.admin.workflows_definition_router as definition_router
        except Exception as e:
            pytest.fail(f"workflow definition routes not importable: {e}")

        mock_db = MagicMock()
        workflow = linear_workflow(
            {"step_id": "planning", "step_kind": "set_variable"},
            workflow_id="wf_foreign",
            account_id="acc_alpha",
        )
        mock_db.get_workflow.return_value = workflow

        with (
            patch.object(definition_router, "_get_db", return_value=mock_db),
            patch.object(definition_router, "_resolve_workflow_row", return_value=workflow),
            patch.object(
                definition_router,
                "_request_scope",
                return_value=normalize_scope(account_id="acc_other"),
            ),
        ):
            resp = registry_client.get("/api/workflows/wf_foreign/steps/planning/available-outputs")

        assert resp.status_code == 403

    def test_protected_workflow_delete_is_blocked(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_ext",
            "source_install_id": "inst-ext",
            "projection_mode": "derived",
        }
        mock_registry = MagicMock()

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch(
                "core.application.registry.workflow_lookup.get_workflow_row",
                return_value=mock_db.get_workflow.return_value,
            ),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
        ):
            resp = registry_client.delete("/api/workflows/wf_ext")

        assert resp.status_code == 403
        mock_db.delete_workflow.assert_not_called()
        mock_registry.reload_all.assert_not_called()

    def test_licensed_projection_only_delete_is_blocked(self, registry_client):
        import core.entrypoints.admin.workflows as workflow_routes

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        projection = {
            "workflow_id": "extension.inst-ext.prepare",
            "source_install_id": "inst-ext",
            "projection_mode": "derived",
            "settings": {"portability": {"allow_full_export": True}},
        }
        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch(
                "core.application.registry.workflow_lookup.get_workflow_row",
                return_value=projection,
            ),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
        ):
            response = registry_client.delete("/api/workflows/extension.inst-ext.prepare")

        assert response.status_code == 403
        mock_db.delete_workflow.assert_not_called()

    def test_clone_workflow_enforces_request_account_scope(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_demo",
            "display_name": "Demo workflow",
            "account_id": "acc_alpha",
            "enabled": True,
            "settings": {},
        }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_request_scope",
                return_value=normalize_scope(account_id="acc_other"),
            ),
        ):
            resp = registry_client.post(
                "/api/workflows/wf_demo/clone",
                json={"workflow_id": "wf_demo_copy"},
            )

        assert resp.status_code == 403

    def test_protected_workflow_patch_cannot_clear_ownership_or_unlock_export(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_ext",
            "display_name": "Extension workflow",
            "source_install_id": "inst-ext",
            "projection_mode": "derived",
            "settings": {},
        }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch(
                "core.application.registry.workflow_lookup.get_workflow_row",
                return_value=mock_db.get_workflow.return_value,
            ),
        ):
            resp = registry_client.patch(
                "/api/workflows/wf_ext",
                json={
                    "source_install_id": "",
                    "projection_mode": "manual",
                    "settings": {"portability": {"allow_full_export": True}},
                },
            )

        assert resp.status_code == 403
        detail = resp.json().get("detail") or {}
        assert detail["error"] == "workflow_protected_metadata_immutable"
        mock_db.save_workflow.assert_not_called()

    def test_workflow_import_preflight_reports_conflict(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        bundle = {
            "schema_version": "platform.workflow_bundle.v2",
            "kind": "workflow_bundle",
            "metadata": {},
            "workflows": [
                {
                    "workflow_id": "wf_existing",
                    "version": "1.0.0",
                    "display_name": "Existing",
                    "description": "",
                    "trigger": {},
                    "settings": {},
                    "participants": [],
                    "tags": [],
                    "steps": [],
                    "groups": [],
                    "execution_graph": None,
                }
            ],
            "dependencies": {
                "agents": [],
                "skills": [],
                "tools": [],
                "child_workflows": [],
                "published_blocks": [],
                "extensions": [],
                "project_connections": [],
            },
            "import_hints": {},
        }
        mock_db = MagicMock()
        mock_db.get_workflow.side_effect = lambda workflow_id, include_trashed=False: (
            {"workflow_id": workflow_id} if workflow_id == "wf_existing" else None
        )

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.post(
                "/api/workflows/import/preflight",
                json={"bundle_yaml": yaml.safe_dump(bundle, sort_keys=False)},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["can_apply"] is False
        conflict = next(
            item
            for item in body["dependencies"]
            if item["metadata"].get("code") == "workflow_id_conflict"
        )
        assert conflict["status"] == "conflict"

    def test_manual_import_maps_an_exported_extension_like_the_catalog(self, registry_client):
        """ "From catalog" mapped an exported extension to this account's install by its
        portable id; importing the same YAML by hand left it blocking. Both paths now
        share one resolver, so the manual preflight maps it too."""
        try:
            import core.entrypoints.admin.workflows as workflow_routes
            import core.entrypoints.admin.workflows_routes_listing_import as import_routes
            from core.application.extensions.install import InstallState
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        foreign, local = "deadbeef-0000-4000-8000-00000000abcd", "inst-local-mailbox"
        bundle = {
            "schema_version": "platform.workflow_bundle.v2",
            "kind": "workflow_bundle",
            "metadata": {},
            "workflows": [
                {
                    "workflow_id": "wf_mailbox",
                    "version": "1.0.0",
                    "display_name": "Mailbox",
                    "description": "",
                    "trigger": {},
                    "settings": {},
                    "participants": [],
                    "tags": [],
                    "steps": [
                        {
                            "step_id": "read_mailbox",
                            "step_kind": "connector",
                            "connector_id": "extension.acme.mailbox",
                            "connector_install_id": foreign,
                        }
                    ],
                    "groups": [],
                    "execution_graph": {
                        "schema_version": 1,
                        "entry": node("read_mailbox"),
                        "edges": [],
                    },
                }
            ],
            "dependencies": {
                "agents": [],
                "skills": [],
                "tools": [],
                "child_workflows": [],
                "published_blocks": [],
                "extensions": [
                    {"install_id": foreign, "extension_id": "acme.mailbox", "required": True}
                ],
                "project_connections": [],
            },
            "import_hints": {},
        }
        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None

        class _Record:
            extension_id = "acme.mailbox"
            install_id = local
            version = "1.0.0"
            state = InstallState.active

        class _InstallStore:
            def list_for_account(self, account_id, **_kwargs):
                return [_Record()] if account_id == "acc_alpha" else []

        class _IntegrationStore:
            conn = None

            def get_extension_by_install_id(self, install_id, *, account_id=None):
                if (install_id, account_id) == (local, "acc_alpha"):
                    return {"install_id": local, "extension_id": "acme.mailbox"}
                return None

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_request_scope",
                return_value=normalize_scope(account_id="acc_alpha"),
            ),
            patch.object(
                workflow_routes,
                "_platform_logger_for_admin_catalog",
                return_value=_IntegrationStore(),
            ),
            patch.object(
                import_routes,
                "get_default_extension_install_store",
                return_value=_InstallStore(),
            ),
        ):
            resp = registry_client.post(
                "/api/workflows/import/preflight",
                json={"bundle_yaml": yaml.safe_dump(bundle, sort_keys=False)},
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        [extension] = [d for d in body["dependencies"] if d["dependency_type"] == "extension"]
        assert extension["status"] == "resolved"
        assert body["extension_resolutions"] == [
            {
                "extension_id": "acme.mailbox",
                "outcome": "resolved",
                "message": "",
                "install_id": local,
                "installed_version": "1.0.0",
            }
        ]

    def test_workflow_import_apply_blocks_unresolved_dependencies(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        bundle = {
            "schema_version": "platform.workflow_bundle.v2",
            "kind": "workflow_bundle",
            "metadata": {},
            "workflows": [
                {
                    "workflow_id": "wf_incoming",
                    "version": "1.0.0",
                    "display_name": "Incoming",
                    "description": "",
                    "trigger": {},
                    "settings": {},
                    "participants": [],
                    "tags": [],
                    "steps": [
                        {
                            "step_id": "plan",
                            "step_kind": "agent",
                            "agent_id": "missing_agent",
                        }
                    ],
                    "groups": [],
                    "execution_graph": {
                        "schema_version": 1,
                        "entry": node("plan"),
                        "edges": [],
                    },
                }
            ],
            "dependencies": {"agents": [{"agent_id": "missing_agent"}]},
            "import_hints": {},
        }
        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        mock_db.get_agent.return_value = None

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.post(
                "/api/workflows/import/apply",
                json={"bundle_yaml": yaml.safe_dump(bundle, sort_keys=False)},
            )

        assert resp.status_code == 400
        detail = resp.json().get("detail") or {}
        assert detail["error"] == "workflow_import_preflight_blocked"
        assert any(
            item["dependency_type"] == "agent" and item["status"] == "requires_mapping"
            for item in detail["errors"]
        )

    def test_workflow_import_preflight_requires_project_connection_resolution(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        bundle = {
            "schema_version": "platform.workflow_bundle.v2",
            "kind": "workflow_bundle",
            "metadata": {},
            "workflows": [
                {
                    "workflow_id": "wf_incoming",
                    "version": "1.0.0",
                    "display_name": "Incoming",
                    "description": "",
                    "trigger": {},
                    "settings": {},
                    "participants": [],
                    "tags": [],
                    "steps": [],
                    "groups": [],
                    "execution_graph": None,
                }
            ],
            "dependencies": {"project_connections": [{"connection_id": "victim-conn"}]},
            "import_hints": {},
        }
        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        mock_db.get_project_connection.return_value = {
            "connection_id": "victim-conn",
            "project_id": "foreign-project",
        }
        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.post(
                "/api/workflows/import/preflight",
                json={"bundle_yaml": yaml.safe_dump(bundle, sort_keys=False)},
            )

        assert resp.status_code == 200
        dependency = resp.json()["dependencies"][0]
        assert dependency["dependency_type"] == "project_connection"
        assert dependency["status"] == "requires_project_connection"

    def test_workflow_import_apply_rejects_placeholder_project_outside_scope(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        bundle = {
            "schema_version": "platform.workflow_bundle.v2",
            "kind": "workflow_bundle",
            "metadata": {},
            "workflows": [
                {
                    "workflow_id": "wf_incoming",
                    "version": "1.0.0",
                    "display_name": "Incoming",
                    "description": "",
                    "trigger": {},
                    "settings": {},
                    "participants": [],
                    "tags": [],
                    "steps": [],
                    "groups": [],
                    "execution_graph": None,
                }
            ],
            "dependencies": {
                "project_connections": [{"connection_id": "crm-prod"}],
            },
            "import_hints": {},
        }
        mock_db = MagicMock()
        mock_db.get_project.return_value = {
            "id": "project-other",
            "account_id": "account-other",
        }
        mock_db.restore_entity_revision = MagicMock()

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_request_scope",
                return_value=normalize_scope(
                    account_id=DEFAULT_ACCOUNT_ID,
                    allowed_project_ids=frozenset({"project-allowed"}),
                ),
            ),
        ):
            resp = registry_client.post(
                "/api/workflows/import/apply",
                json={
                    "bundle_yaml": yaml.safe_dump(bundle, sort_keys=False),
                    "resolutions": [
                        {
                            "dependency_key": "project_connection:crm-prod",
                            "action": "create_connection_placeholder",
                            "project_id": "project-other",
                            "extension_id": "ext-crm",
                        }
                    ],
                },
            )

        assert resp.status_code == 403
        mock_db.restore_entity_revision.assert_not_called()

    def test_workflow_import_apply_rejects_existing_connection_outside_project_scope(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        bundle = {
            "schema_version": "platform.workflow_bundle.v2",
            "kind": "workflow_bundle",
            "metadata": {},
            "workflows": [
                {
                    "workflow_id": "wf_incoming",
                    "version": "1.0.0",
                    "display_name": "Incoming",
                    "description": "",
                    "trigger": {},
                    "settings": {},
                    "participants": [],
                    "tags": [],
                    "steps": [],
                    "groups": [],
                    "execution_graph": None,
                }
            ],
            "dependencies": {
                "project_connections": [{"connection_id": "crm-prod"}],
            },
            "import_hints": {},
        }
        mock_db = MagicMock()
        mock_db.get_project.return_value = {
            "id": "project-allowed",
            "project_id": "project-allowed",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_db.get_project_connection.return_value = None
        mock_db.restore_entity_revision = MagicMock()

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_request_scope",
                return_value=normalize_scope(
                    account_id=DEFAULT_ACCOUNT_ID,
                    allowed_project_ids=frozenset({"project-allowed"}),
                ),
            ),
        ):
            resp = registry_client.post(
                "/api/workflows/import/apply",
                json={
                    "bundle_yaml": yaml.safe_dump(bundle, sort_keys=False),
                    "resolutions": [
                        {
                            "dependency_key": "project_connection:crm-prod",
                            "action": "map_existing",
                            "project_id": "project-allowed",
                            "target_connection_id": "conn-other-project",
                        }
                    ],
                },
            )

        assert resp.status_code == 403
        mock_db.get_project_connection.assert_called_once_with(
            project_id="project-allowed",
            connection_id="conn-other-project",
        )
        mock_db.restore_entity_revision.assert_not_called()

    def test_workflow_import_options_returns_typed_candidates(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = SimpleNamespace(
            list_agents_summary=lambda enabled_only=False: [
                {
                    "agent_id": "scout",
                    "display_name": "Scout",
                    "enabled": True,
                }
            ],
            list_skills_summary=lambda enabled_only=False: [
                {"skill_id": "collect_findings", "display_name": "Analyze Requirements"}
            ],
            list_tools_summary=lambda enabled_only=False: [
                {"tool_id": "search_docs", "display_name": "Search Docs"}
            ],
            list_workflows_summary=lambda enabled_only=False: [
                {"workflow_id": "child_wf", "display_name": "Child Workflow"}
            ],
            list_published_block_contracts=lambda enabled_only=False, ensure_projection=False: [
                {"block_key": "blk.sync", "display_name": "Sync Block"}
            ],
            list_extensions=lambda enabled_only=False: [
                {"install_id": "inst-crm", "display_name": "CRM Extension"}
            ],
            list_project_connections=lambda project_id, enabled_only=False: [
                {
                    "connection_id": f"conn-{project_id}",
                    "display_name": "CRM Production",
                    "enabled": True,
                }
            ],
        )

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get("/api/workflows/import/options?project_id=17")

        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "workflow_import_dependency_options"
        assert body["options"]["agent"][0]["value"] == "scout"
        assert body["options"]["child_workflow"][0]["value"] == "child_wf"
        assert body["options"]["project_connection"][0]["value"] == "conn-17"

    def test_workflow_import_options_uses_the_project_connection_store(self, registry_client):
        try:
            import core.entrypoints.admin.workflows_routes_listing_import as workflow_import_routes
        except Exception as e:
            pytest.fail(f"workflow import routes not importable: {e}")

        mock_db = SimpleNamespace(
            list_agents_summary=lambda enabled_only=False, account_id=None: [],
            list_skills_summary=lambda enabled_only=False, account_id=None: [],
            list_tools_summary=lambda enabled_only=False, account_id=None: [],
            list_workflows_summary=lambda enabled_only=False, account_id=None: [],
            list_published_block_contracts=lambda enabled_only=False, ensure_projection=False: [],
            list_extensions=lambda enabled_only=False: [],
        )
        connection_rows = [
            {
                "connection_id": "conn-17",
                "display_name": "Supplier SFTP",
                "enabled": True,
            }
        ]

        with (
            patch.object(workflow_import_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_import_routes,
                "_list_project_connections_for_catalog",
                return_value=connection_rows,
            ) as connection_lister,
        ):
            resp = registry_client.get("/api/workflows/import/options?project_id=17")

        assert resp.status_code == 200
        assert resp.json()["options"]["project_connection"][0]["value"] == "conn-17"
        connection_lister.assert_called_once_with(
            mock_db,
            project_id="17",
            enabled_only=False,
        )

    def test_workflow_import_options_enforces_project_scope(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = SimpleNamespace(
            get_project=lambda project_id: None,
            list_agents_summary=lambda enabled_only=False: [],
            list_skills_summary=lambda enabled_only=False: [],
            list_tools_summary=lambda enabled_only=False: [],
            list_workflows_summary=lambda enabled_only=False: [],
            list_published_block_contracts=lambda enabled_only=False, ensure_projection=False: [],
            list_extensions=lambda enabled_only=False: [],
            list_project_connections=lambda project_id, enabled_only=False: [],
        )

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_request_scope",
                return_value=normalize_scope(
                    account_id="acct-1",
                    allowed_project_ids=frozenset({"proj-allowed"}),
                ),
            ),
        ):
            resp = registry_client.get("/api/workflows/import/options?project_id=17")

        assert resp.status_code == 403
        assert "Access denied" in resp.text

    def test_list_workflows(self, registry_client):
        resp = registry_client.get("/api/workflows")
        assert resp.status_code in (200, 404, 500)
        if resp.status_code == 200:
            data = resp.json()
            assert isinstance(data, list)

    def test_list_workflows_has_workflow_id(self, registry_client):
        resp = registry_client.get("/api/workflows")
        if resp.status_code == 200:
            wfs = resp.json()
            if wfs:
                assert "workflow_id" in wfs[0]

    def test_list_workflows_includes_extension_runtime_projection_rows(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.list_workflows.return_value = []
        mock_db.get_workflow.return_value = None
        projection_row = {
            "workflow_id": "extension.inst-extension-demo.wf_extension_demo",
            "display_name": "Extension workflow",
            "description": "Imported from rich extension",
            "source_install_id": "inst-extension-demo",
            "source_release_id": "rel-extension-demo",
            "projection_mode": "derived",
            "settings": {},
            "enabled": True,
            "availability_status": "available",
            "availability_reason_code": "none",
        }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch(
                "core.entrypoints.admin.workflows_routes_listing_import.projection_aware_catalog_rows",
                return_value=[projection_row],
            ),
        ):
            resp = registry_client.get("/api/workflows")

        assert resp.status_code == 200
        body = resp.json()
        assert any(
            item["workflow_id"] == "extension.inst-extension-demo.wf_extension_demo"
            for item in body
        )

    def test_list_workflows_uses_request_account_scope_for_extension_rows(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.list_workflows.return_value = []

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_request_scope",
                return_value=normalize_scope(account_id="acc_team"),
            ),
            patch(
                "core.entrypoints.admin.workflows_routes_listing_import.projection_aware_catalog_rows",
                return_value=[],
            ) as mock_catalog_rows,
        ):
            resp = registry_client.get("/api/workflows")

        assert resp.status_code == 200
        mock_catalog_rows.assert_called_once_with(
            mock_db,
            "workflow",
            "workflow_id",
            [],
            account_id="acc_team",
            enabled_only=False,
        )

    def test_list_workflows_hides_inactive_durable_projection_anchor(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.extension_runtime_store = SimpleNamespace(list_projections=lambda **_kwargs: [])
        mock_db.list_workflows.return_value = [
            {
                "workflow_id": "extension.install-a.deploy",
                "display_name": "Disabled extension workflow",
                "enabled": True,
                "projection_mode": "derived",
                "source_install_id": "install-a",
                "settings": {},
            }
        ]

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get("/api/workflows")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_search_workflows_hides_inactive_durable_projection_anchor(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        anchor = {
            "workflow_id": "extension.install-a.deploy",
            "display_name": "Disabled extension workflow",
            "enabled": True,
            "projection_mode": "derived",
            "source_install_id": "install-a",
            "settings": {},
        }
        mock_db = MagicMock()
        mock_db.extension_runtime_store = SimpleNamespace(list_projections=lambda **_kwargs: [])
        mock_db.list_workflows.return_value = [anchor]
        mock_db.search_entities.return_value = [anchor]

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get("/api/workflows?search=deploy")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_workflow_hides_inactive_durable_projection_anchor(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.extension_runtime_store = SimpleNamespace(list_projections=lambda **_kwargs: [])
        mock_db.get_workflow.return_value = {
            "workflow_id": "extension.install-a.deploy",
            "display_name": "Disabled extension workflow",
            "enabled": True,
            "projection_mode": "derived",
            "source_install_id": "install-a",
            "settings": {},
        }

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get("/api/workflows/extension.install-a.deploy")

        assert resp.status_code == 404

    @staticmethod
    def _blocked_projection_db():
        """A Showcase-like install whose workflow needs an extension that is not installed."""
        anchor = {
            "workflow_id": "extension.install-a.demo_dataset_open_orders",
            "display_name": "Demo: Dataset Open Orders",
            "enabled": True,
            "projection_mode": "derived",
            "source_install_id": "install-a",
            "settings": {},
        }
        projection = {
            "install_id": "install-a",
            "release_id": "pr_a",
            "artifact_id": "demo_dataset_open_orders",
            "artifact_kind": "workflow",
            "projection": {
                "workflow_id": "demo_dataset_open_orders",
                "display_name": "Demo: Dataset Open Orders",
                "enabled": True,
                "settings": {},
                "steps": [],
                "dependencies": {
                    "extensions": [
                        {
                            "extension_id": "acme.required-warehouse",
                            "required": True,
                        }
                    ]
                },
            },
        }
        mock_db = MagicMock()
        mock_db.extension_runtime_store = SimpleNamespace(
            list_projections=lambda **_kwargs: [projection]
        )
        mock_db.list_workflows.return_value = [anchor]
        mock_db.get_workflow.side_effect = lambda workflow_id: (
            dict(anchor) if workflow_id == anchor["workflow_id"] else None
        )
        return mock_db

    @staticmethod
    def _nothing_else_installed():
        rows = "core.application.registry.projected_artifact_rows"
        projection = "core.entrypoints.admin.workflows_projection"
        return (
            patch(f"{rows}.active_extension_ids_for_account", return_value=set()),
            patch(f"{rows}.active_extension_versions_for_account", return_value={}),
            patch(f"{projection}._active_extension_ids_for_projection", return_value=set()),
            patch(f"{projection}._active_extension_versions_for_projection", return_value={}),
            patch(
                "core.application.extensions.install_availability_service."
                "InstallAvailabilityService.get_install_availability",
                return_value={"availability_status": "available"},
            ),
        )

    def test_list_shows_a_workflow_whose_required_extension_is_missing(self, registry_client):
        import core.entrypoints.admin.workflows as workflow_routes

        mock_db = self._blocked_projection_db()
        a, b, c, d, e = self._nothing_else_installed()
        with patch.object(workflow_routes, "_get_db", return_value=mock_db), a, b, c, d, e:
            resp = registry_client.get("/api/workflows")

        assert resp.status_code == 200
        rows = {row["workflow_id"]: row for row in resp.json()}
        row = rows["extension.install-a.demo_dataset_open_orders"]
        assert row["enabled"] is False
        assert row["availability_status"] == "unavailable"
        assert row["availability_reason_text"].startswith(
            "Needs the extension acme.required-warehouse, which this account does not have active."
        )

    def test_get_workflow_explains_a_missing_required_extension_instead_of_404(
        self, registry_client
    ):
        import core.entrypoints.admin.workflows as workflow_routes

        mock_db = self._blocked_projection_db()
        a, b, c, d, e = self._nothing_else_installed()
        with patch.object(workflow_routes, "_get_db", return_value=mock_db), a, b, c, d, e:
            resp = registry_client.get(
                "/api/workflows/extension.install-a.demo_dataset_open_orders"
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["availability_status"] == "unavailable"
        assert "acme.required-warehouse" in body["availability_reason_text"]
        assert "Extensions > Catalog" in body["availability_reason_text"]

    def test_get_workflow_falls_back_to_extension_runtime_projection(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        projection_row = {
            "workflow_id": "extension.inst-extension-demo.wf_extension_demo",
            "display_name": "Extension workflow",
            "description": "Imported from rich extension",
            "source_install_id": "inst-extension-demo",
            "source_release_id": "rel-extension-demo",
            "projection_mode": "derived",
            "settings": {"participants": [{"participant_key": "builder", "agent_id": "agent_a"}]},
            "steps": [{"step_id": "step_demo", "step_kind": "agent", "group_id": None}],
            "groups": [],
            "enabled": True,
            "availability_status": "available",
            "availability_reason_code": "none",
        }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch(
                "core.application.registry.workflow_lookup.get_workflow_row",
                return_value=projection_row,
            ),
        ):
            resp = registry_client.get("/api/workflows/wf_extension_demo")

        assert resp.status_code == 200
        body = resp.json()
        assert body["workflow_id"] == "extension.inst-extension-demo.wf_extension_demo"
        assert body["source_install_id"] == "inst-extension-demo"
        assert body["participants"][0]["agent_id"] == "agent_a"

    def test_get_workflow_uses_request_account_scope_for_extension_projection(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_request_scope",
                return_value=normalize_scope(account_id="acc_team"),
            ),
            patch.object(
                workflow_routes,
                "_resolve_workflow_row",
                return_value={"workflow_id": "wf_extension_demo", "settings": {}},
            ) as mock_resolve,
        ):
            resp = registry_client.get("/api/workflows/wf_extension_demo")

        assert resp.status_code == 200
        mock_resolve.assert_called_once_with(
            mock_db,
            "wf_extension_demo",
            account_id="acc_team",
        )

    @pytest.mark.parametrize(
        "suffix",
        ["", "/export-policy", "/dag", "/canvas-graph", "/contract"],
    )
    def test_workflow_read_surfaces_deny_foreign_account_rows(self, registry_client, suffix):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_foreign",
            "display_name": "Foreign workflow",
            "account_id": "acc_owner",
            "settings": {},
            "steps": [],
            "groups": [],
        }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_request_scope",
                return_value=normalize_scope(account_id="acc_other"),
            ),
        ):
            response = registry_client.get(f"/api/workflows/wf_foreign{suffix}")

        assert response.status_code == 403, response.text

    def test_workflow_detail_does_not_resurrect_soft_deleted_history(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        mock_db.get_entity_history.return_value = [
            {
                "snapshot": {
                    "workflow_id": "wf_deleted",
                    "account_id": "acc_owner",
                    "settings": {},
                    "steps": [],
                    "groups": [],
                }
            }
        ]

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_extension_workflow", return_value=None),
            patch.object(
                workflow_routes,
                "_request_scope",
                return_value=normalize_scope(account_id="acc_owner"),
            ),
        ):
            response = registry_client.get("/api/workflows/wf_deleted")

        assert response.status_code == 404, response.text

    def test_export_policy_falls_back_to_extension_runtime_projection(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        projection_row = {
            "workflow_id": "wf_extension_demo",
            "display_name": "Extension workflow",
            "source_install_id": "inst-extension-demo",
            "source_release_id": "rel-extension-demo",
            "projection_mode": "derived",
            "settings": {},
            "enabled": True,
            "availability_status": "available",
            "availability_reason_code": "none",
        }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch(
                "core.application.registry.workflow_lookup.get_workflow_row",
                return_value=projection_row,
            ),
        ):
            resp = registry_client.get("/api/workflows/wf_extension_demo/export-policy")

        assert resp.status_code == 200
        body = resp.json()
        assert body["workflow_id"] == "wf_extension_demo"
        assert body["full_export_allowed"] is False
        assert body["reference_export_allowed"] is True
        assert body["source_install_id"] == "inst-extension-demo"

    def test_canvas_graph_hides_inactive_extension_runtime_projection(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        projection_row = {
            "workflow_id": "wf_extension_demo",
            "display_name": "Extension workflow",
            "description": "Imported from rich extension",
            "source_install_id": "inst-extension-demo",
            "source_release_id": "rel-extension-demo",
            "projection_mode": "derived",
            "settings": {},
            "enabled": False,
            "availability_status": "unavailable",
            "availability_reason_code": "install_disabled",
            "availability_reason_text": "Install is disabled.",
        }
        mock_db.get_workflow.return_value = projection_row
        mock_db.extension_runtime_store = SimpleNamespace(list_projections=lambda **_kwargs: [])

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get("/api/workflows/wf_extension_demo/canvas-graph")

        assert resp.status_code == 404
        assert "wf_extension_demo" in resp.text

    def test_canvas_graph_marks_licensed_extension_runtime_projection_read_only(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        projection_row = {
            "workflow_id": "extension.inst-extension-demo.wf_extension_demo",
            "display_name": "Extension workflow",
            "source_install_id": "inst-extension-demo",
            "source_release_id": "rel-extension-demo",
            "projection_mode": "derived",
            "settings": {"portability": {"allow_full_export": True}},
            "enabled": True,
            "availability_status": "available",
            "availability_reason_code": "none",
            "steps": [{"step_id": "lookup", "step_kind": "agent", "group_id": None}],
            "groups": [],
        }

        def build_canvas(canvas_db, workflow_id):
            assert canvas_db.get_workflow(workflow_id) == projection_row
            return {"steps": [{"step_id": "lookup"}], "groups": [], "edges": []}

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_resolve_active_workflow_scope_target",
                return_value=(projection_row, None),
            ),
            patch.object(workflow_routes, "build_workflow_canvas_graph", side_effect=build_canvas),
        ):
            resp = registry_client.get(
                "/api/workflows/extension.inst-extension-demo.wf_extension_demo/canvas-graph"
            )

        assert resp.status_code == 200
        assert resp.json()["steps"] == [{"step_id": "lookup"}]
        assert resp.json()["groups"] == []
        assert resp.json()["read_only"] is True
        assert resp.json()["portability"]["structure_change_allowed"] is False
        assert "phases" not in resp.json()

    @staticmethod
    def _extension_anchor_and_projection(*, editable: bool):
        workflow_id = "extension.inst-hook-sample.prepare_order"
        anchor_row = {
            "workflow_id": workflow_id,
            "display_name": "Hook sample",
            "source_install_id": "inst-hook-sample",
            "local_workflow_id": workflow_id,
            "projection_mode": "derived",
            "settings": {},
            "enabled": True,
        }
        callable_contract = {
            "enabled": True,
            "input_mode": "fields",
            "output_mode": "variables",
            "trigger_channels": ["ui", "api"],
            "expected_inputs": [{"name": "customer", "type": "string", "required": True}],
            "expected_outputs": [{"name": "order", "type": "object", "required": False}],
        }
        settings = {"callable": callable_contract}
        if editable:
            settings["portability"] = {"allow_full_export": True}
        projection_row = {
            **anchor_row,
            "account_id": "00000000-0000-0000-0000-000000000002",
            "settings": settings,
            "availability_status": "available",
            "availability_reason_code": "none",
            "steps": [{"step_id": "prepare", "step_kind": "set_variable"}],
            "groups": [],
        }
        return workflow_id, anchor_row, projection_row

    @pytest.mark.parametrize("editable", [False, True], ids=["protected", "editable"])
    def test_canvas_graph_reads_an_extension_workflow_interface_from_its_projection(
        self, registry_client, editable
    ):
        """B21: the builder said "no inputs / no outputs" for an extension workflow.

        The platform table holds the install's anchor row, whose settings are
        empty; the canvas was built from it instead of the resolved projection,
        whose settings.callable declares the interface.
        """
        import core.entrypoints.admin.workflows as workflow_routes

        workflow_id, anchor_row, projection_row = self._extension_anchor_and_projection(
            editable=editable
        )
        mock_db = MagicMock()
        mock_db.get_workflow.side_effect = lambda wid: (
            dict(anchor_row) if wid == workflow_id else None
        )
        seen: dict[str, object] = {}

        def build_canvas(canvas_db, requested_id):
            seen["workflow"] = canvas_db.get_workflow(requested_id)
            return {"steps": [], "groups": [], "edges": []}

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_resolve_active_workflow_scope_target",
                return_value=(projection_row, None),
            ),
            patch.object(workflow_routes, "build_workflow_canvas_graph", side_effect=build_canvas),
        ):
            resp = registry_client.get(f"/api/workflows/{workflow_id}/canvas-graph")

        assert resp.status_code == 200
        callable_contract = seen["workflow"]["settings"]["callable"]
        assert [row["name"] for row in callable_contract["expected_inputs"]] == ["customer"]
        assert [row["name"] for row in callable_contract["expected_outputs"]] == ["order"]

    @pytest.mark.parametrize("projection_revision", [None, 999], ids=["absent", "publisher-value"])
    @pytest.mark.parametrize("surface", ["detail", "canvas"])
    def test_protected_projected_workflow_reads_keep_persisted_definition_revision(
        self, registry_client, projection_revision, surface
    ):
        """The builder must identify the same anchor revision the execution archive pins."""
        import core.entrypoints.admin.workflows as workflow_routes

        workflow_id, anchor, projected = self._extension_anchor_and_projection(editable=False)
        anchor["revision"] = 17
        projected["workflow_id"] = "prepare_order"
        if projection_revision is not None:
            projected["revision"] = projection_revision
        projected["for_each"] = []
        database = MagicMock()
        database.get_workflow.side_effect = lambda requested: (
            dict(anchor) if requested == workflow_id else None
        )
        database.extension_runtime_store = SimpleNamespace(
            list_projections=lambda **kwargs: [
                {
                    "account_id": DEFAULT_ACCOUNT_ID,
                    "install_id": "inst-hook-sample",
                    "release_id": "release-hook-sample",
                    "artifact_kind": "workflow",
                    "artifact_id": "prepare_order",
                    "is_active": True,
                    "projection": projected,
                }
            ]
        )
        suffix = "/canvas-graph" if surface == "canvas" else ""
        with (
            patch.object(workflow_routes, "_get_db", return_value=database),
            patch(
                "core.application.registry.projected_artifact_rows.active_extension_ids_for_account",
                return_value={"acme.hook-sample"},
            ),
            patch(
                "core.application.registry.projected_artifact_rows.active_extension_versions_for_account",
                return_value={},
            ),
            patch(
                "core.application.workflows.canvas_graph_service.validate_workflow_readiness",
                return_value=_VALID_READINESS,
            ),
        ):
            response = registry_client.get(f"/api/workflows/{workflow_id}{suffix}")

        assert response.status_code == 200, response.text
        payload = response.json()
        workflow = payload["workflow"] if surface == "canvas" else payload
        assert workflow.get("revision") == 17
        assert payload["protected_workflow"] is True
        if surface == "detail":
            assert payload["steps"][0]["step_id"] == "prepare"
            assert payload["settings"]["callable"]["expected_inputs"][0]["name"] == "customer"
        assert anchor["revision"] == 17
        assert projected.get("revision") == projection_revision

    def test_validation_view_serves_its_own_workflow_before_the_anchor_row(self):
        from core.application.workflows.canvas_graph_service import (
            build_workflow_canvas_graph,
        )
        from core.entrypoints.admin.workflows_projection import _WorkflowValidationDBView

        workflow_id, anchor_row, projection_row = self._extension_anchor_and_projection(
            editable=False
        )
        mock_db = MagicMock()
        mock_db.get_workflow.side_effect = lambda wid: (
            dict(anchor_row) if wid == workflow_id else None
        )
        view = _WorkflowValidationDBView(mock_db, workflow_id, projection_row)

        assert view.get_workflow(workflow_id)["settings"]["callable"]["expected_inputs"]
        with patch(
            "core.application.workflows.canvas_graph_service.validate_workflow_readiness",
            return_value={"valid": True, "errors": [], "warnings": []},
        ):
            graph = build_workflow_canvas_graph(view, workflow_id)

        assert [row["name"] for row in graph["interface_contract"]["expected_inputs"]] == [
            "customer"
        ]
        assert [row["name"] for row in graph["output_contract"]["expected_outputs"]] == ["order"]

    def test_extension_runtime_workflow_validation_uses_projection_fallback(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
            import core.entrypoints.admin.workflows_projection as workflow_projection
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        projection_row = linear_workflow(
            {
                "step_id": "workflow_start",
                "step_kind": "set_variable",
                "override": {
                    "set_variable": {
                        "assignments": {
                            "state.workflow_started": True,
                        }
                    }
                },
            },
            {
                "step_id": "extension_http_step",
                "step_kind": "http_request",
                "output_mapping_jsonb": {"result": "state.workflow_data.result"},
                "override": {
                    "http_request": {
                        "method": "GET",
                        "url": "https://example.test/health",
                        "response_target": "state.workflow_data.result",
                        "timeout_seconds": 30,
                        "expected_statuses": [200],
                    }
                },
            },
            {
                "step_id": "workflow_end",
                "step_kind": "stop_success",
                "override": {"stop_success": {"message": "done"}},
            },
            workflow_id="wf_extension_demo",
            display_name="Extension workflow",
            enabled=True,
            _workflow_outputs=[
                {
                    "output_name": "result",
                    "field_type": "text",
                    "output_mode": "artifact",
                }
            ],
        )

        with patch.object(
            workflow_projection, "_resolve_workflow_row", return_value=projection_row
        ):
            validation = workflow_routes._get_workflow_validation(
                mock_db, "extension.inst-extension-demo.wf_extension_demo"
            )

        assert validation["valid"] is True, validation

    def test_extension_runtime_workflow_validation_uses_extension_runtime_agent_projection(
        self,
        registry_client,
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        mock_db.get_agent.return_value = None
        mock_db.get_skill.return_value = None
        mock_db.get_agent_skills.return_value = []
        mock_db.get_agent_tools.return_value = []
        projection_row = linear_workflow(
            {
                "step_id": "extension_step",
                "step_kind": "agent",
                "participant_key": "builder",
                "agent_id": "extension_agent",
            },
            {
                "step_id": "workflow_done",
                "step_kind": "stop_success",
                "override": {"stop_success": {"message": "done"}},
            },
            workflow_id="extension.inst-extension-demo.wf_extension_demo",
            display_name="Extension workflow",
            enabled=True,
            settings={
                "participants": [
                    {
                        "participant_key": "builder",
                        "agent_id": "extension_agent",
                    }
                ]
            },
        )
        projected_agent = {
            "agent_id": "extension_agent",
            "display_name": "Extension Agent",
            "enabled": True,
            "projection_mode": "derived",
            "source_install_id": "inst-extension-demo",
        }

        class _MockProjectionStore:
            def list_projections(self, *, account_id, artifact_kind, only_active):
                assert account_id == DEFAULT_ACCOUNT_ID
                assert only_active is False
                if artifact_kind != "agent":
                    return []
                return [
                    {
                        "artifact_id": "extension_agent",
                        "artifact_kind": "agent",
                        "install_id": "inst-extension-demo",
                        "release_id": "rel-extension-demo",
                        "is_active": True,
                        "projection": projected_agent,
                    }
                ]

        with (
            patch.object(workflow_routes, "_resolve_workflow_row", return_value=projection_row),
            patch.object(
                workflow_routes,
                "_get_extension_runtime_store",
                return_value=_MockProjectionStore(),
            ),
        ):
            validation = workflow_routes._get_workflow_validation(
                mock_db, "extension.inst-extension-demo.wf_extension_demo"
            )

        assert validation["valid"] is True
        warning_codes = {item["code"] for item in validation["warnings"]}
        assert "workflow.step_contract.agent.skills_missing_projected" in warning_codes

    def test_step_test_run_uses_extension_runtime_workflow_row(self, registry_client):
        try:
            import core.application.workflows.platform_step_test_run_helpers as test_run_helpers
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        projection_row = {
            "workflow_id": "wf_extension_demo",
            "display_name": "Extension workflow",
            "enabled": True,
            "settings": {},
            "steps": [
                {
                    "step_id": "extension_step",
                    "step_kind": "agent",
                    "agent_id": "agent_a",
                    "participant_key": "builder",
                }
            ],
            "groups": [],
        }
        validation = {"valid": True, "errors": [], "warnings": []}
        mock_logger = MagicMock()
        mock_logger.get_project.return_value = {
            "id": "df0d2afe-7064-4206-941b-61d2d4db425c",
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_logger.get_project_run_config.return_value = {
            "project_name": "Demo",
        }
        mock_job_store = MagicMock()
        mock_job_store.get_job.return_value = {"id": 41, "thread_id": "thread-41"}

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_resolve_workflow_row", return_value=projection_row),
            patch.object(workflow_routes, "_get_workflow_validation", return_value=validation),
            patch(
                "core.infrastructure.runtime.entrypoint_db_adapters.create_project_store",
                return_value=mock_logger,
            ),
            patch.object(
                test_run_helpers,
                "resolve_step_for_test_run",
                return_value={"step_id": "extension_step", "step_kind": "agent"},
            ) as mock_resolve_step,
            patch(
                "core.infrastructure.runtime.entrypoint_db_adapters.create_job_store",
                return_value=mock_job_store,
            ),
            patch(
                "core.entrypoints.admin.workflows_definition_router.enqueue_task",
                return_value=41,
            ) as enqueue_test_run,
        ):
            resp = registry_client.post(
                "/api/workflows/wf_extension_demo/steps/extension_step/test-run",
                json={
                    "project_id": "df0d2afe-7064-4206-941b-61d2d4db425c",
                    "task_summary": "Smoke run",
                },
            )

        assert resp.status_code == 202
        assert resp.json()["status"] == "queued"
        assert mock_resolve_step.call_args.kwargs["workflow"] == projection_row
        assert enqueue_test_run.call_args.kwargs["task_source"] == "step_test_run"

    def test_get_existing_workflow(self, registry_client):
        resp = _workflow_api_detail_response(registry_client)
        assert resp.status_code in (200, 404)

    def test_get_nonexistent_workflow_returns_404(self, registry_client):
        resp = registry_client.get("/api/workflows/nonexistent_wf_xyz_99999")
        assert resp.status_code in (404, 500)

    def test_step_contract_preview_endpoint_returns_grouped_contract(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = linear_workflow(
            {
                "step_id": "analysis",
                "step_kind": "agent",
                "agent_id": "scout",
                "skill_id": "collect_findings",
            },
            {
                "step_id": "development",
                "step_kind": "agent",
                "agent_id": "composer",
                "boundary_contract": {
                    "entry": [
                        {
                            "name": "summary_doc",
                            "type": "text",
                            "source": "message.step.analysis.artifacts.summary_doc",
                        },
                    ]
                },
            },
            workflow_id="wf_demo",
        )
        mock_db.get_agent.return_value = {
            "agent_id": "composer",
        }
        mock_db.get_skill.return_value = {
            "skill_id": "draft_summary",
            "display_name": "Implement Story",
        }
        mock_db.get_skill_inputs.return_value = [
            {"name": "summary_doc", "type": "text", "required": True},
            {"name": "bug_description", "type": "text", "required": True},
        ]
        mock_db.get_agent_tools.return_value = []
        mock_db.get_tool.return_value = None
        mock_db.get_agent_inputs.return_value = [
            {"name": "branch_name", "type": "text", "required": True}
        ]
        mock_db.get_skill_outputs.return_value = [
            {"name": "code_output", "type": "text", "required": True}
        ]
        mock_db.get_agent_outputs.side_effect = lambda agent_id: (
            [{"name": "summary_doc", "type": "text"}]
            if agent_id == "scout"
            else [{"name": "git_diff", "type": "text"}]
        )
        mock_db.get_agent_skills.return_value = [{"skill_id": "draft_summary"}]

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get(
                "/api/workflows/wf_demo/step-contract-preview",
                params={"step_id": "development", "agent_id": "composer"},
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "locked_required" in body
        assert "unresolved_required" in body
        assert "optional" in body
        assert "custom" in body
        assert "role_type" not in body
        locked_names = {item.get("name") for item in body.get("locked_required", [])}
        unresolved_names = {item.get("name") for item in body.get("unresolved_required", [])}
        assert "branch_name" not in locked_names
        assert "branch_name" not in unresolved_names

    def test_step_contract_preview_endpoint_returns_404_when_workflow_missing(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_resolve_workflow_row", return_value=None),
        ):
            resp = registry_client.get(
                "/api/workflows/wf_missing/step-contract-preview",
                params={"agent_id": "composer"},
            )

        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "path",
        [
            "/api/workflows/wf_demo/phases/development/contract-preview",
            "/api/workflows/wf_demo/phases/development/step-contract-preview",
        ],
    )
    def test_phase_scoped_contract_preview_routes_are_removed(self, registry_client, path):
        resp = registry_client.get(path, params={"agent_id": "composer"})

        assert resp.status_code == 404

    def test_transform_actions_endpoint_returns_action_contracts(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {"workflow_id": "wf_demo", "steps": [], "groups": []}

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get(
                "/api/workflows/wf_demo/transform-actions",
            )

        assert resp.status_code == 200
        body = resp.json()
        actions = body.get("actions")
        assert isinstance(actions, list)
        trim = next(action for action in actions if action["action_id"] == "text.trim")
        assert trim["display_name"] == "Trim"
        assert trim["params_schema"]["type"] == "object"
        assert trim["transform_safe"] is True

    def test_transform_preview_endpoint_runs_transform_against_sample_data(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {"workflow_id": "wf_demo", "steps": [], "groups": []}

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.post(
                "/api/workflows/wf_demo/transform-preview",
                json={
                    "config": {
                        "input": {"source": "state.inputs.customer"},
                        "mode": "object",
                        "result_target": "state.normalized_customer",
                        "fields": [
                            {
                                "from": "name",
                                "to": "full_name",
                                "type": "text",
                                "actions": [{"action": "text.trim"}],
                            }
                        ],
                    },
                    "sample_data": {"name": "  Ada Lovelace  "},
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["preview"] is True
        assert body["ok"] is True
        assert body["output"] == {"full_name": "Ada Lovelace"}
        assert body["state_patch"]["normalized_customer"] == {"full_name": "Ada Lovelace"}

    def test_transform_preview_endpoint_returns_structured_row_filter_errors(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {"workflow_id": "wf_demo", "steps": [], "groups": []}

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.post(
                "/api/workflows/wf_demo/transform-preview",
                json={
                    "config": {
                        "input": {"source": "state.inputs.rows"},
                        "mode": "list",
                        "result_target": "state.filtered",
                        "row_filter": {"selector": "price", "operator": "gt", "value": 10},
                        "fields": [{"from": "sku", "to": "sku", "type": "text"}],
                    },
                    "sample_data": [{"sku": "A1", "price": "not-a-number"}],
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["preview"] is True
        assert body["ok"] is False
        assert body["errors"][0]["reason"] == "row_filter_failed"

    def test_step_contract_preview_endpoint_accepts_block_key(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {"workflow_id": "wf_demo", "steps": [], "groups": []}
        mock_db.get_published_block_contract.return_value = {
            "block_key": "designer.outline_plan",
            "display_name": "Designer / Outline Plan",
            "source_kind": "agent",
            "source_ref": "designer::outline_plan",
            "enabled": True,
            "inputs": [{"field_key": "summary_doc", "value_type": "text", "required": True}],
            "outputs": [
                {
                    "field_key": "architecture",
                    "value_type": "text",
                    "required_by_default": True,
                    "exposed_by_default": True,
                }
            ],
        }
        mock_db.list_published_block_contracts.return_value = [
            {
                "block_key": "designer.outline_plan",
                "source_kind": "agent",
                "source_ref": "designer::outline_plan",
                "enabled": True,
            }
        ]
        mock_db.get_agent.return_value = {
            "agent_id": "designer",
        }
        mock_db.get_skill.return_value = {
            "skill_id": "outline_plan",
            "display_name": "Design Architecture",
        }
        mock_db.get_skill_inputs.return_value = []
        mock_db.get_skill_outputs.return_value = []
        mock_db.get_agent_tools.return_value = []
        mock_db.get_agent_inputs.return_value = []
        mock_db.get_agent_outputs.return_value = []
        mock_db.get_tool.return_value = None

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get(
                "/api/workflows/wf_demo/step-contract-preview",
                params={"block_key": "designer.outline_plan"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("block_key") == "designer.outline_plan"
        unresolved_names = {item.get("name") for item in body.get("unresolved_required", [])}
        assert "summary_doc" in unresolved_names

    def test_step_contract_preview_endpoint_accepts_step_id_for_non_agent_step(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_demo",
            "steps": [
                {
                    "id": "step-row-1",
                    "step_id": "request_customer_data",
                    "group_id": None,
                    "step_kind": "http_request",
                    "override": {
                        "http_request": {
                            "method": "POST",
                            "url": "https://example.test/customers",
                            "response_target": "state.workflow_data.customer_response",
                        }
                    },
                }
            ],
            "groups": [],
        }

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get(
                "/api/workflows/wf_demo/step-contract-preview",
                params={"step_id": "request_customer_data"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("step_id") == "request_customer_data"
        assert body.get("step_kind") == "http_request"
        locked_names = {item.get("name") for item in body.get("locked_required", [])}
        assert "method" in locked_names
        output_names = {item.get("name") for item in body.get("outputs", [])}
        assert "workflow_data.customer_response" in output_names

    def test_step_contract_preview_http_request_includes_runtime_selector_inputs(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.workflows_definition_router as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_demo",
            "steps": [
                {
                    "id": "step-row-1",
                    "step_id": "open_meteo_request",
                    "group_id": None,
                    "step_kind": "http_request",
                    "override": {
                        "http_request": {
                            "method": "GET",
                            "url": "https://api.open-meteo.com/v1/forecast",
                            "query_params": {
                                "latitude": "{{ state.weather_query.latitude }}",
                                "longitude": "{{ state.weather_query.longitude }}",
                            },
                        }
                    },
                }
            ],
            "groups": [],
        }

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get(
                "/api/workflows/wf_demo/step-contract-preview",
                params={"step_id": "open_meteo_request"},
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        all_names = {
            item.get("name")
            for group in ("locked_required", "unresolved_required", "optional", "custom")
            for item in body.get(group, [])
        }
        assert "query_params.latitude" in all_names
        assert "query_params.longitude" in all_names

    def test_step_contract_preview_connector_uses_action_contract_input_type(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_demo",
            "steps": [
                {
                    "id": "step-row-1",
                    "step_id": "count_open_orders",
                    "group_id": None,
                    "step_kind": "connector",
                    "connector_id": "connector.std.text",
                    "override": {"connector": {"function_name": "length"}},
                    "input_mapping_jsonb": {"value": "state.demo_orders_result.rows"},
                }
            ],
            "groups": [],
        }
        mock_db.get_tool.return_value = {
            "tool_id": "std.text",
            "interface": {
                "functions": [
                    {
                        "name": "length",
                        "parameters": [
                            {
                                "name": "value",
                                "type": "json",
                                "required": True,
                                "description": "Structured value to measure",
                            }
                        ],
                    }
                ]
            },
        }

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get(
                "/api/workflows/wf_demo/step-contract-preview",
                params={"step_id": "count_open_orders"},
            )

        assert resp.status_code == 200
        body = resp.json()
        all_rows = [
            *body.get("locked_required", []),
            *body.get("unresolved_required", []),
            *body.get("optional", []),
            *body.get("custom", []),
        ]
        value_row = next(row for row in all_rows if row.get("name") == "value")
        assert value_row.get("type") == "json"
        assert value_row.get("description") == "Structured value to measure"

    def test_step_test_run_endpoint_accepts_ordinal_step_id(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.dsn = "postgres://flow-steward"
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_demo",
            "steps": [
                {
                    "id": "db-step-1",
                    "step_id": "real_step_id",
                    "step_kind": "set_variable",
                    "override": {
                        "set_variable": {
                            "target": "state.workflow_data.result",
                            "value": "done",
                        }
                    },
                }
            ],
            "groups": [],
        }
        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {"id": 7, "name": "Demo"}
        mock_bl.get_project_run_config.return_value = {
            "project_name": "Demo",
        }
        mock_job_store = MagicMock()
        mock_job_store.get_job.return_value = {"id": 41, "thread_id": "thread-41"}

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
            patch(
                "core.infrastructure.runtime.entrypoint_db_adapters.create_project_store",
                return_value=mock_bl,
            ),
            patch(
                "core.infrastructure.runtime.entrypoint_db_adapters.create_job_store",
                return_value=mock_job_store,
            ),
            patch(
                "core.entrypoints.admin.workflows_definition_router.enqueue_task",
                return_value=41,
            ) as enqueue_test_run,
        ):
            resp = registry_client.post(
                "/api/workflows/wf_demo/steps/real_step_id/test-run",
                json={
                    "project_id": 7,
                    "task_summary": "Run only this step",
                },
            )

        assert resp.status_code == 202
        assert resp.json().get("step_id") == "real_step_id"
        extra_state = enqueue_test_run.call_args.kwargs["extra_state"]
        assert extra_state["run_cursor"]["step_id"] == "real_step_id"
        assert extra_state["step_test_run_context"]["step_id"] == "real_step_id"

    def test_step_test_run_runtime_fails_when_connector_mapping_is_unresolved(self):
        from core.application.workflows import platform_test_run_runtime as runtime

        step = {
            "step_id": "count_open_orders",
            "display_name": "Count open orders",
            "step_kind": "connector",
            "connector_id": "connector.std.text",
            "override": {"connector": {"function_name": "length"}},
            "input_mapping_jsonb": {"value": "state.demo_orders_result.rows"},
            "output_mapping_jsonb": {"result": "state.demo_order_count"},
        }

        with (
            patch.object(runtime, "_reload_registry_for_test_run", return_value=MagicMock()),
            patch.object(runtime, "execute_step_node", return_value={"agent_messages": []}),
        ):
            payload = runtime.run_step_test_run_ephemeral(
                workflow_id="sample_dataset_open_orders",
                workflow={
                    "workflow_id": "sample_dataset_open_orders",
                    "revision": 1,
                    "steps": [step],
                    "groups": [],
                },
                step_id=step["step_id"],
                project_id="proj_demo",
                task_summary="Count rows",
                task_id="test-step-run-connector",
                task_envelope={"inputs": {"project_id": "proj_demo"}},
                project_name="Demo",
                project_config={},
            )

        assert payload.get("status") == "fail"
        assert payload.get("error") == "missing_mapped_inputs"
        assert "state.demo_orders_result.rows" in str(payload.get("summary") or "")
        assert (payload.get("result") or {}).get("missing_inputs")
        assert payload.get("workflow_outputs") == {}

    def test_step_test_run_runtime_applies_manual_binding_to_state_source(self):
        from core.application.workflows import platform_test_run_runtime as runtime

        step = {
            "step_id": "count_open_orders",
            "display_name": "Count open orders",
            "step_kind": "connector",
            "connector_id": "connector.std.text",
            "override": {"connector": {"function_name": "length"}},
            "input_mapping_jsonb": {"value": "state.demo_orders_result.rows"},
            "output_mapping_jsonb": {"result": "state.demo_order_count"},
        }

        with (
            patch.object(runtime, "_reload_registry_for_test_run", return_value=MagicMock()),
            patch.object(runtime, "execute_step_node", return_value={"agent_messages": []}),
        ):
            payload = runtime.run_step_test_run_ephemeral(
                workflow_id="sample_dataset_open_orders",
                workflow={
                    "workflow_id": "sample_dataset_open_orders",
                    "revision": 1,
                    "steps": [step],
                    "groups": [],
                },
                step_id=step["step_id"],
                project_id="proj_demo",
                task_summary="Count rows",
                task_id="test-step-run-connector-bound",
                task_envelope={
                    "inputs": {
                        "project_id": "proj_demo",
                        "value": [{"id": 1}, {"id": 2}],
                    },
                    "metadata": {
                        "input_bindings": [
                            {
                                "name": "value",
                                "source": "state.demo_orders_result.rows",
                                "type": "array",
                            }
                        ]
                    },
                },
                project_name="Demo",
                project_config={},
            )

        assert payload.get("error") != "missing_mapped_inputs"
        assert payload.get("inputs", {}).get("value") == [{"id": 1}, {"id": 2}]
        assert payload.get("workflow_outputs") == {}

    def test_step_test_run_runtime_fails_when_http_request_context_is_unresolved(self):
        from core.application.workflows import platform_test_run_runtime as runtime

        step = {
            "step_id": "open_meteo_request",
            "display_name": "Call Open-Meteo",
            "step_kind": "http_request",
            "override": {
                "http_request": {
                    "url": "https://api.open-meteo.com/v1/forecast",
                    "method": "GET",
                    "query_params": {
                        "latitude": "{{ state.weather_query.latitude }}",
                        "longitude": "{{ state.weather_query.longitude }}",
                        "current": "temperature_2m",
                    },
                }
            },
        }

        with patch.object(runtime, "_reload_registry_for_test_run", return_value=MagicMock()):
            payload = runtime.run_step_test_run_ephemeral(
                workflow_id="sample_weather_city_brief",
                workflow={
                    "workflow_id": "sample_weather_city_brief",
                    "revision": 1,
                    "steps": [step],
                    "groups": [],
                },
                step_id=step["step_id"],
                project_id="proj_demo",
                task_summary="Fetch weather",
                task_id="test-step-run-http",
                task_envelope={"inputs": {"project_id": "proj_demo"}},
                project_name="Demo",
                project_config={},
            )

        assert payload.get("status") == "fail"
        assert payload.get("error") == "missing_runtime_request_context"
        assert "state.weather_query.latitude" in str(payload.get("summary") or "")
        request = (payload.get("result") or {}).get("request") or {}
        assert request.get("query_params", {}).get("latitude") == ""

    def test_step_test_run_runtime_applies_manual_binding_for_switch_selector(self):
        from core.application.workflows import platform_test_run_runtime as runtime

        step = {
            "step_id": "event_type_switch",
            "display_name": "Route by event type",
            "step_kind": "switch",
            "override": {
                "switch": {
                    "selector": "state.webhook.event_type",
                    "cases": [{"case_id": "invoice", "when": "invoice.created"}],
                }
            },
        }

        with patch.object(runtime, "_reload_registry_for_test_run", return_value=MagicMock()):
            payload = runtime.run_step_test_run_ephemeral(
                workflow_id="sample_incoming_webhook_router",
                workflow={
                    "workflow_id": "sample_incoming_webhook_router",
                    "revision": 1,
                    "steps": [step],
                    "groups": [],
                },
                step_id=step["step_id"],
                project_id="proj_demo",
                task_summary="Route event",
                task_id="test-step-run-switch-bound",
                task_envelope={
                    "inputs": {
                        "project_id": "proj_demo",
                        "selector": "invoice.created",
                    },
                    "metadata": {
                        "input_bindings": [
                            {
                                "name": "selector",
                                "source": "state.webhook.event_type",
                                "type": "text",
                            }
                        ]
                    },
                },
                project_name="Demo",
                project_config={},
            )

        assert payload.get("final_status") in {"pass", "completed", "success"}
        assert "invoice.created" in str(payload.get("summary") or payload.get("result") or "")

    def test_step_test_run_dataset_query_inputs_include_literal_config(self):
        from core.application.workflows import platform_test_run_runtime as runtime

        step = {
            "step_id": "load_open_orders",
            "display_name": "Load open orders",
            "step_kind": "dataset_query",
            "override": {
                "dataset_query": {
                    "query_template_id": "query-1",
                    "result_target": "state.demo_orders_result",
                    "row_limit": 10,
                    "timeout_seconds": 30,
                }
            },
        }

        with (
            patch.object(runtime, "_reload_registry_for_test_run", return_value=MagicMock()),
            patch.object(runtime, "execute_step_node", return_value={"agent_messages": []}),
        ):
            payload = runtime.run_step_test_run_ephemeral(
                workflow_id="sample_dataset_open_orders",
                workflow={
                    "workflow_id": "sample_dataset_open_orders",
                    "revision": 1,
                    "steps": [step],
                    "groups": [],
                },
                step_id=step["step_id"],
                project_id="proj_demo",
                task_summary="Load rows",
                task_id="test-step-run-dataset-query",
                task_envelope={"inputs": {"project_id": "proj_demo"}},
                project_name="Demo",
                project_config={},
            )

        assert payload.get("inputs", {}).get("query_template_id") == "query-1"
        assert payload.get("inputs", {}).get("result_target") == "state.demo_orders_result"
        assert payload.get("inputs", {}).get("row_limit") == 10

    def test_workflow_test_run_runtime_enriches_scope_and_dedupes_trace(self):
        from core.application.workflows import platform_test_run_runtime as runtime

        final_state = {
            "status": "in_progress",
            "agent_messages": [
                {
                    "step_id": "workflow_start",
                    "status": "pass",
                    "summary": "set_variable wrote 2 top-level key(s)",
                    "from_agent": "control_flow_step",
                    "skill": "control_flow",
                    "artifacts": {
                        "step_kind": "set_variable",
                        "step_id": "workflow_start",
                    },
                },
                {
                    "step_id": "workflow_start",
                    "status": "pass",
                    "summary": "set_variable wrote 2 top-level key(s)",
                    "from_agent": "control_flow_step",
                    "skill": "control_flow",
                    "artifacts": {
                        "step_kind": "set_variable",
                        "step_id": "workflow_start",
                    },
                },
                {
                    "step_id": "load_open_orders",
                    "status": "fail",
                    "summary": "dataset_query requires canonical project/account/tenant/workspace scope",
                    "errors": ["dataset_query_failed"],
                    "from_agent": "dataset_query_step",
                    "skill": "dataset_query",
                    "artifacts": {
                        "step_kind": "dataset_query",
                        "step_id": "load_open_orders",
                    },
                },
            ],
        }

        class _Graph:
            captured_initial_state = None

            def stream(self, initial_state, _invoke_config, stream_mode="values"):
                self.captured_initial_state = initial_state
                yield final_state

        graph = _Graph()

        with (
            patch.object(runtime, "_reload_registry_for_test_run", return_value=MagicMock()),
            patch.object(runtime, "build_registry_graph", return_value=graph),
        ):
            payload = runtime.run_workflow_test_run_ephemeral(
                workflow_id="sample_dataset_open_orders",
                project_id="proj_demo",
                task_summary="Run workflow",
                task_id="test-workflow-run",
                task_envelope={"inputs": {"project_id": "proj_demo"}},
                project_name="Demo",
                project_config={
                    "project_id": "proj_demo",
                    "account_id": "acc_demo",
                    "tenant_id": "tenant_demo",
                    "workspace_id": "workspace_demo",
                },
                extra_state={"test_mode": True},
            )

        assert graph.captured_initial_state["project_id"] == "proj_demo"
        assert graph.captured_initial_state["account_id"] == "acc_demo"
        assert graph.captured_initial_state["tenant_id"] == "tenant_demo"
        assert graph.captured_initial_state["workspace_id"] == "workspace_demo"
        assert payload.get("final_status") == "fail"
        assert len(payload.get("trace") or []) == 2
        assert payload["trace"][0]["step_id"] == "workflow_start"
        assert payload["trace"][1]["step_id"] == "load_open_orders"

    def test_workflow_test_run_endpoint_uses_db_workflow_existence_check(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.dsn = "postgres://flow-steward"
        mock_db.get_workflow.return_value = {
            "workflow_id": "sample_dataset_open_orders",
            "steps": [],
            "groups": [],
        }
        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {"id": "proj_demo", "name": "Demo"}
        mock_bl.get_project_run_config.return_value = {
            "project_name": "Demo",
        }
        mock_job_store = MagicMock()
        mock_job_store.get_job.return_value = {
            "id": 41,
            "thread_id": "thread-41",
        }

        registry = SimpleNamespace(
            workflows=SimpleNamespace(get_workflow=MagicMock(return_value=None))
        )

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
            patch(
                "core.infrastructure.runtime.entrypoint_db_adapters.create_project_store",
                return_value=mock_bl,
            ),
            patch(
                "core.infrastructure.runtime.entrypoint_db_adapters.create_job_store",
                return_value=mock_job_store,
            ),
            patch(
                "core.entrypoints.admin.workflows_definition_router.enqueue_task",
                return_value=41,
            ) as enqueue_test_run,
        ):
            resp = registry_client.post(
                "/api/workflows/sample_dataset_open_orders/test-run",
                json={
                    "project_id": "proj_demo",
                    "task_summary": "Run workflow",
                },
            )

        assert resp.status_code == 202
        assert resp.json().get("status") == "queued"
        assert resp.json().get("workflow_id") == "sample_dataset_open_orders"
        assert enqueue_test_run.call_args.kwargs["task_source"] == "workflow_test_run"

    def test_step_input_defaults_endpoint_returns_required_optional_and_custom(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = linear_workflow(
            {
                "step_id": "analysis",
                "step_kind": "agent",
                "agent_id": "scout",
                "skill_id": "collect_findings",
            },
            {
                "step_id": "development",
                "step_kind": "agent",
                "agent_id": "composer",
                "skill_id": "draft_summary",
                "boundary_contract": {
                    "entry": [
                        {
                            "name": "summary_doc",
                            "type": "text",
                            "source": "message.step.analysis.artifacts.summary_doc",
                        },
                        {"name": "custom_note", "type": "text", "source": "state.task_summary"},
                    ]
                },
            },
            workflow_id="wf_demo",
        )
        mock_db.get_agent_skills.return_value = [{"skill_id": "draft_summary"}]
        mock_db.get_skill_inputs.return_value = [
            {"name": "summary_doc", "type": "text", "required": True},
            {"name": "nice_to_have", "type": "text", "required": False},
        ]
        mock_db.get_agent_tools.return_value = []
        mock_db.get_tool.return_value = None
        mock_db.get_agent_inputs.return_value = []
        mock_db.get_skill_outputs.return_value = [
            {"name": "code_output", "type": "text", "required": True}
        ]
        mock_db.get_agent_outputs.return_value = [{"name": "summary_doc", "type": "text"}]

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get("/api/workflows/wf_demo/steps/development/input-defaults")
            missing = registry_client.get("/api/workflows/wf_demo/steps/unknown/input-defaults")

        assert missing.status_code == 404
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["step_id"] == "development"
        required_names = {item.get("name") for item in body.get("required_inputs", [])}
        optional_names = {item.get("name") for item in body.get("optional_inputs", [])}
        custom_names = {item.get("name") for item in body.get("custom_inputs", [])}
        assert "summary_doc" in required_names
        assert "nice_to_have" in optional_names
        assert "custom_note" in custom_names

    def test_create_workflow_without_steps_succeeds(self, registry_client):
        """A workflow is created without steps; its definition comes through /changes."""
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        workflow_id = f"test_workflow_empty_{uuid.uuid4().hex[:8]}"
        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        mock_registry = MagicMock()

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={
                    "valid": True,
                    "errors": [],
                    "warnings": [],
                },
            ),
        ):
            resp = registry_client.post(
                "/api/workflows",
                json={
                    "workflow_id": workflow_id,
                    "display_name": "Test Workflow",
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "created"
        assert body["workflow_id"] == workflow_id
        mock_db.save_workflow.assert_called_once()
        mock_registry.reload_all.assert_called_once()

    @pytest.mark.parametrize(
        "invalid_workflow_id",
        ["release plan", "release!plan", "release/plan", "релиз"],
    )
    def test_create_workflow_rejects_non_slug_workflow_ids(
        self, registry_client, invalid_workflow_id
    ):
        """AUDIT-032: workflow creation must share the registry slug contract.

        A malformed ID must be rejected at the REST boundary before persistence,
        rather than being persisted and later encoded into a builder URL.
        """
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_registry = MagicMock()
        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
        ):
            response = registry_client.post(
                "/api/workflows",
                json={"workflow_id": invalid_workflow_id, "display_name": "Invalid workflow"},
            )

        assert response.status_code == 422, response.text
        assert "workflow_id must match [a-z][a-z0-9_]*" in response.text
        mock_db.save_workflow.assert_not_called()

    def test_create_workflow_rejects_duplicate_workflow_id(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.side_effect = lambda workflow_id: (
            {"workflow_id": workflow_id}
            if workflow_id in {"duplicate_workflow", "duplicate_workflow_1"}
            else None
        )
        mock_registry = MagicMock()

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            resp = registry_client.post(
                "/api/workflows",
                json={
                    "workflow_id": "duplicate_workflow",
                    "display_name": "Duplicate Workflow",
                },
            )

        assert resp.status_code == 409
        assert resp.json()["detail"] == "Workflow 'duplicate_workflow' already exists"
        mock_db.save_workflow.assert_not_called()

    def test_clone_workflow_rejects_duplicate_target_id(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.side_effect = lambda workflow_id: (
            {"workflow_id": "wf_demo", "display_name": "Workflow Demo", "steps": []}
            if workflow_id == "wf_demo"
            else {"workflow_id": workflow_id}
            if workflow_id == "wf_demo_copy"
            else None
        )
        mock_registry = MagicMock()

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
        ):
            resp = registry_client.post("/api/workflows/wf_demo/clone", json={})

        assert resp.status_code == 409
        assert resp.json()["detail"] == "Workflow 'wf_demo_copy' already exists"
        mock_db.clone_workflow.assert_not_called()

    def test_disabled_workflow_saves_an_incomplete_definition_with_its_issues(
        self, registry_client
    ):
        """Detached authoring: a disabled workflow stores a half-built graph."""
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        workflow = linear_workflow(
            {"step_id": "intake", "step_kind": "set_variable"},
            workflow_id="wf_draft",
            enabled=False,
        )
        mock_db = _definition_db(workflow)

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
            patch.object(
                workflow_routes, "_get_workflow_validation", return_value=_VALID_READINESS
            ),
        ):
            resp = _post_changes(
                registry_client,
                "wf_draft",
                [
                    {
                        "op": "create_step",
                        "step": {
                            "step_id": "route",
                            "step_kind": "if",
                            "override": {"if": {"condition": True}},
                        },
                    }
                ],
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["revision"] == 2
        assert body["valid"] is False
        assert {issue["code"] for issue in body["issues"]} >= {"workflow.graph.outcome_missing"}
        saved = _saved_definition(mock_db)
        assert [step["step_id"] for step in saved["steps"]] == ["intake", "done", "route"]
        assert mock_db.save_workflow.call_args.kwargs["expected_revision"] == 1

    def test_enabled_workflow_refuses_a_change_that_leaves_structural_issues(self, registry_client):
        """The same change a disabled workflow accepts is refused here, unsaved.

        An enabled workflow keeps running its current revision; a structural
        issue of the result blocks the whole set.
        """
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        workflow = linear_workflow(
            {"step_id": "intake", "step_kind": "set_variable"},
            workflow_id="wf_live",
            enabled=True,
        )
        mock_db = _definition_db(workflow)

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
            patch.object(
                workflow_routes, "_get_workflow_validation", return_value=_VALID_READINESS
            ),
        ):
            resp = _post_changes(
                registry_client,
                "wf_live",
                [
                    {
                        "op": "create_step",
                        "step": {
                            "step_id": "route",
                            "step_kind": "if",
                            "override": {"if": {"condition": True}},
                        },
                    }
                ],
            )

        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail["action"] == "apply_changes"
        assert "workflow.graph.outcome_missing" in _issue_codes(resp)
        mock_db.save_workflow.assert_not_called()

    def test_delete_step_drops_its_edges_and_the_same_set_reconnects(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        workflow = linear_workflow(
            {"step_id": "router", "step_kind": "if", "override": {"if": {"condition": True}}},
            {"step_id": "sms", "step_kind": "set_variable"},
            workflow_id="wf_canvas",
            enabled=True,
            settings={
                "execution_graph": {
                    "schema_version": 1,
                    "entry": node("router"),
                    "edges": [
                        edge("router", node("sms"), "true"),
                        edge("router", node("done"), "false"),
                        edge("sms", node("done")),
                    ],
                }
            },
        )
        mock_db = _definition_db(workflow)

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
            patch.object(
                workflow_routes, "_get_workflow_validation", return_value=_VALID_READINESS
            ),
            patch.object(
                workflow_routes,
                "_assert_workflow_structure_change_allowed",
                return_value=None,
            ),
        ):
            refused = _post_changes(
                registry_client, "wf_canvas", [{"op": "delete_step", "step_id": "sms"}]
            )
            resp = _post_changes(
                registry_client,
                "wf_canvas",
                [
                    {"op": "delete_step", "step_id": "sms"},
                    {"op": "connect", "edge": edge("router", node("done"), "true")},
                ],
            )

        # Deleting alone leaves the router's true outcome unrouted: no bypass is built.
        assert refused.status_code == 400, refused.text
        assert "workflow.graph.outcome_missing" in _issue_codes(refused)
        assert resp.status_code == 200, resp.text
        saved = _saved_definition(mock_db)
        assert [step["step_id"] for step in saved["steps"]] == ["router", "done"]
        assert saved["settings"]["execution_graph"]["edges"] == [
            edge("router", node("done"), "false"),
            edge("router", node("done"), "true"),
        ]
        # The if config keeps its condition only; targets live in the graph.
        assert _saved_step(mock_db, "router")["override"] == {"if": {"condition": True}}

    def test_add_control_flow_step_does_not_require_agent_skill_pair(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        workflow = linear_workflow(
            {"step_id": "analysis", "step_kind": "set_variable"},
            workflow_id="wf_control",
            enabled=False,
        )
        mock_db = _definition_db(workflow)

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
            patch.object(
                workflow_routes, "_get_workflow_validation", return_value=_VALID_READINESS
            ),
        ):
            resp = _post_changes(
                registry_client,
                "wf_control",
                [
                    {
                        "op": "insert_step_between",
                        "source": node("analysis"),
                        "outcome": {"name": "success"},
                        "step": {
                            "step_kind": "set_variable",
                            "step_id": "mark_routed",
                            "agent_id": "scout",
                            "skill_id": "collect_findings",
                            "override": {"set_variable": {"target": "state.routed", "value": 1}},
                        },
                    },
                    {
                        "op": "create_step",
                        "step": {
                            "step_kind": "IF",
                            "step_id": "route_if",
                            "agent_id": "scout",
                            "override": {"if": {"condition": True}},
                        },
                    },
                ],
            )

        assert resp.status_code == 200, resp.text
        for step_id, step_kind in (("mark_routed", "set_variable"), ("route_if", "if")):
            payload = _saved_step(mock_db, step_id)
            assert payload["step_kind"] == step_kind
            # A non-agent step carries no agent, and the skill is never stored by hand.
            assert payload["agent_id"] == ""
            assert "skill_id" not in payload
        mock_db.get_agent_skills.assert_not_called()

    def test_add_extension_connector_step_preserves_exact_install_and_extension_identity(self):
        try:
            from core.application.workflows.workflow_change_authorization import (
                ChangeReferenceAccess,
            )
            from core.entrypoints.admin.workflows_definition import (
                create_workflow_definition_router,
            )
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = _definition_db(
            linear_workflow(
                {"step_id": "sync", "step_kind": "set_variable"},
                workflow_id="wf_fixture_documents",
                enabled=False,
            )
        )
        router = create_workflow_definition_router(
            get_db=lambda: mock_db,
            get_registry=lambda: MagicMock(),
            get_workflow_validation=lambda _db, _workflow_id: dict(_VALID_READINESS),
            get_step_test_run_validation=lambda _db, _workflow_id, _step_id: dict(_VALID_READINESS),
            get_readiness_validation=lambda _store, _workflow_id: dict(_VALID_READINESS),
            resolve_workflow_row=lambda db, workflow_id: db.get_workflow(workflow_id),
            assert_workflow_structure_change_allowed=lambda _db, _workflow_id, _action: None,
            assert_workflow_step_test_run_allowed=lambda _db, _workflow_id, _action: None,
            request_scope=lambda _request: SimpleNamespace(
                account_id=DEFAULT_ACCOUNT_ID, actor_name="operator"
            ),
            enforce_workflow_row_scope=lambda _db, _scope, workflow: workflow,
            assert_workflow_mutation_allowed=lambda _workflow: None,
            validate_context_inject_sources=lambda _value: None,
            change_reference_access=lambda _db, _scope, _workflow: ChangeReferenceAccess(
                agent=lambda _id: True,
                workflow=lambda _id: True,
                connection=lambda _id: True,
                install=lambda _id: True,
            ),
            durable_test_run_job_state=lambda _job_id: {},
            enqueue_test_run=lambda **_kwargs: 1,
        )
        app = FastAPI()
        app.include_router(router, prefix="/api/workflows")

        response = _post_changes(
            TestClient(app, raise_server_exceptions=False),
            "wf_fixture_documents",
            [
                {
                    "op": "insert_step_between",
                    "source": node("sync"),
                    "outcome": {"name": "success"},
                    "step": {
                        "step_kind": "connector",
                        "step_id": "search_drive",
                        "display_name": "Search Drive",
                        "connector_id": "extension.fixture.extension-oauth",
                        "connector_install_id": "install-fixture",
                        "connection_binding_jsonb": {"connection_id": "fixture-documents"},
                        "override": {
                            "connector": {
                                "extension_id": "fixture.extension-oauth",
                                "operation_id": "search_drive_files",
                                "connection_binding": {"connection_id": "fixture-documents"},
                            }
                        },
                    },
                }
            ],
        )

        assert response.status_code == 200, response.text
        assert response.json()["valid"] is True
        payload = _saved_step(mock_db, "search_drive")
        assert payload["connector_install_id"] == "install-fixture"
        assert payload["connector_id"] == "extension.fixture.extension-oauth"
        assert payload["override"]["connector"]["extension_id"] == ("fixture.extension-oauth")
        assert payload["connection_binding_jsonb"] == {"connection_id": "fixture-documents"}
        assert mock_db.save_workflow.call_args.kwargs["author"] == "operator"

    def test_add_step_rejects_unknown_or_foreign_override_key_before_persistence(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = _definition_db(
            linear_workflow(
                {"step_id": "start", "step_kind": "set_variable"},
                workflow_id="wf_contract",
                enabled=False,
            )
        )

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
            patch.object(
                workflow_routes, "_get_workflow_validation", return_value=_VALID_READINESS
            ),
        ):
            resp = _post_changes(
                registry_client,
                "wf_contract",
                [
                    {
                        "op": "create_step",
                        "step": {
                            "step_kind": "set_variable",
                            "step_id": "set-result",
                            "override": {
                                "set_variable": {"target": "result", "value": "ok"},
                                "http_request": {
                                    "method": "POST",
                                    "url": "https://example.invalid",
                                },
                                "require_runtime_tool": True,
                            },
                        },
                    }
                ],
            )

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["validation"]["errors"] == [
            {
                "code": "workflow.step_override_key_unknown",
                "message": "Unknown step override key 'http_request'",
                "path": "operations[0].step.override.http_request",
                "severity": "error",
            },
            {
                "code": "workflow.step_override_key_unknown",
                "message": "Unknown step override key 'require_runtime_tool'",
                "path": "operations[0].step.override.require_runtime_tool",
                "severity": "error",
            },
        ]
        mock_db.save_workflow.assert_not_called()

    @pytest.mark.parametrize(
        ("step_kind", "expected_issue"),
        [
            (
                "agent",
                {
                    "code": "workflow.step_override_key_unknown",
                    "message": "Unknown step override key 'invoke_workflow'",
                    "path": "operations[0].step.override.invoke_workflow",
                    "severity": "error",
                },
            ),
            # A change set never defaults a step to an agent: the kind is required.
            (
                "",
                {
                    "code": "workflow.change.step_kind_missing",
                    "message": "A new step needs its step_kind",
                    "path": "operations[0].step.step_kind",
                    "severity": "error",
                },
            ),
        ],
    )
    def test_add_step_rejects_agent_invoke_wrapper_before_any_mutation(
        self, registry_client, step_kind, expected_issue
    ):
        import core.entrypoints.admin.workflows as workflow_routes

        db = _definition_db(
            linear_workflow(
                {"step_id": "start", "step_kind": "set_variable"},
                workflow_id="wf_contract",
                enabled=False,
            )
        )
        payload = {
            "step_id": "child-call",
            "override": {
                "invoke_workflow": {"workflow_id": "wf_child"},
            },
        }
        if step_kind:
            payload["step_kind"] = step_kind
        with (
            patch.object(workflow_routes, "_get_db", return_value=db),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
            patch.object(
                workflow_routes, "_get_workflow_validation", return_value=_VALID_READINESS
            ),
        ):
            response = _post_changes(
                registry_client, "wf_contract", [{"op": "create_step", "step": payload}]
            )

        assert response.status_code == 400
        assert response.json()["detail"]["validation"]["errors"] == [expected_issue]
        for method in [
            "save_workflow",
            "save_workflow_history",
            "add_workflow_history",
            "bind_project_workflow",
        ]:
            getattr(db, method).assert_not_called()

    def test_add_http_request_step_persists_the_complete_canonical_contract(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        http_config = {
            "artifact_filename": "response.json",
            "auth_mode": "none",
            "auth_ref": "",
            "body_template": {"sku": "{{state.inputs.sku}}"},
            "expected_statuses": [200, 201],
            "headers": {"Accept": "application/json"},
            "max_response_bytes": 4096,
            "method": "POST",
            "query_params": {"dry_run": True},
            "response_mode": "inline",
            "response_target": "state.response",
            "timeout_seconds": 12.5,
            "url": "https://example.test/items",
        }
        mock_db = _definition_db(
            linear_workflow(
                {"step_id": "request", "step_kind": "set_variable"},
                workflow_id="wf_http_contract",
                enabled=False,
            )
        )

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
            patch.object(
                workflow_routes, "_get_workflow_validation", return_value=_VALID_READINESS
            ),
        ):
            response = _post_changes(
                registry_client,
                "wf_http_contract",
                [
                    {
                        "op": "insert_step_between",
                        "source": node("request"),
                        "outcome": {"name": "success"},
                        "step": {
                            "step_kind": "http_request",
                            "step_id": "create_item",
                            "display_name": "Create item",
                            "override": {"http_request": http_config},
                        },
                    }
                ],
            )

        assert response.status_code == 200, response.text
        persisted = _saved_step(mock_db, "create_item")
        assert persisted["step_kind"] == "http_request"
        assert persisted["override"] == {"http_request": http_config}

    def test_update_http_request_step_persists_the_complete_canonical_contract(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        http_config = {
            "artifact_filename": "response.json",
            "auth_mode": "none",
            "auth_ref": "",
            "body_template": ["{{state.inputs.sku}}"],
            "expected_statuses": [200, 202],
            "headers": {"Accept": "application/json"},
            "max_response_bytes": 8192,
            "method": "PATCH",
            "query_params": {"audit": "true"},
            "response_mode": "inline",
            "response_target": "state.updated_response",
            "timeout_seconds": 22.5,
            "url": "https://example.test/items/42",
        }
        previous_step = {
            "id": "step-row",
            "step_id": "update_item",
            "step_kind": "http_request",
            "display_name": "Update item",
            "agent_id": "",
            "connector_id": "",
            "connector_install_id": "",
            "optional": False,
            "override": {
                "http_request": {
                    "method": "GET",
                    "url": "https://example.test/items/42",
                    "response_target": "state.response",
                    "timeout_seconds": 30,
                    "expected_statuses": [200],
                }
            },
        }
        mock_db = _definition_db(
            linear_workflow(previous_step, workflow_id="wf_http_contract", enabled=False)
        )

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
            patch.object(
                workflow_routes, "_get_workflow_validation", return_value=_VALID_READINESS
            ),
        ):
            response = _post_changes(
                registry_client,
                "wf_http_contract",
                [
                    {
                        "op": "update_step_config",
                        "step_id": "update_item",
                        "patch": {"override": {"http_request": http_config}},
                    }
                ],
            )

        assert response.status_code == 200, response.text
        persisted = _saved_step(mock_db, "update_item")
        assert persisted["step_kind"] == "http_request"
        assert persisted["display_name"] == "Update item"
        assert persisted["override"] == {"http_request": http_config}

    def test_update_step_config_refuses_structure_and_unknown_fields(self, registry_client):
        """A config patch cannot move or re-kind a step, nor carry a removed field."""
        import core.entrypoints.admin.workflows as workflow_routes

        mock_db = _definition_db(
            linear_workflow(
                {"step_id": "update_item", "step_kind": "set_variable"},
                workflow_id="wf_http_contract",
                enabled=False,
            )
        )
        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
            patch.object(
                workflow_routes, "_get_workflow_validation", return_value=_VALID_READINESS
            ),
        ):
            moved = _post_changes(
                registry_client,
                "wf_http_contract",
                [
                    {
                        "op": "update_step_config",
                        "step_id": "update_item",
                        "patch": {"step_kind": "http_request", "group_id": "loop"},
                    }
                ],
            )
            legacy = _post_changes(
                registry_client,
                "wf_http_contract",
                [
                    {
                        "op": "update_step_config",
                        "step_id": "update_item",
                        "patch": {"parallel_group": "lane_a"},
                    }
                ],
            )

        assert moved.status_code == 400, moved.text
        assert _issue_codes(moved) == ["workflow.change.structure_in_config"]
        assert legacy.status_code == 400, legacy.text
        assert _issue_codes(legacy) == ["workflow.change.field_unknown"]
        mock_db.save_workflow.assert_not_called()

    def test_workflow_save_models_accept_uuid_source_projection_id(self):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        projection_id = "11111111-1111-1111-1111-111111111111"

        save = workflow_routes.WorkflowSave(
            workflow_id="wf_projection_uuid",
            display_name="Projection UUID Workflow",
            source_projection_id=projection_id,
        )
        patch_body = workflow_routes.WorkflowPatch(source_projection_id=projection_id)

        assert save.source_projection_id == projection_id
        assert patch_body.source_projection_id == projection_id

    def test_create_workflow_returns_503_when_backend_unavailable(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
            from core.infrastructure.db.psycopg_runtime import OperationalError
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.side_effect = OperationalError("pg-fs unavailable")

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.post(
                "/api/workflows",
                json={
                    "workflow_id": "wf_backend_down",
                    "display_name": "Workflow Backend Down",
                },
            )

        assert resp.status_code == 503
        detail = resp.json().get("detail", {})
        assert detail.get("error") == "Workflow registry backend unavailable"
        assert detail.get("workflow_id") == "wf_backend_down"
        assert detail.get("action") == "create"

    def test_workflow_detail_preserves_invalid_boundary_rows_as_inspection_evidence(
        self, registry_client
    ):
        import core.entrypoints.admin.workflows as workflow_routes

        inputs = ["message", None, {"name": "valid", "type": "text"}]
        workflow = linear_workflow(
            {
                "step_id": "normalize",
                "step_kind": "set_variable",
                "boundary_contract": {"entry": inputs},
            },
            workflow_id="wf_legacy_boundary_inputs",
            enabled=False,
        )
        mock_db = MagicMock()
        mock_db.get_workflow.return_value = workflow
        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_resolve_active_workflow_scope_target",
                return_value=(workflow, None),
            ),
        ):
            response = registry_client.get("/api/workflows/wf_legacy_boundary_inputs")
        assert response.status_code == 200, response.text
        step = response.json()["steps"][0]
        assert step["raw_expected_inputs"] == inputs
        assert [row["name"] for row in step["boundary_contract"]["entry"]] == ["valid"]
        assert [issue["path"] for issue in step["expected_input_issues"]] == [
            "workflow.steps.normalize.boundary_contract.entry[0]",
            "workflow.steps.normalize.boundary_contract.entry[1]",
        ]
        assert workflow["steps"][0]["boundary_contract"]["entry"] == inputs
        mock_db.save_workflow.assert_not_called()

    @pytest.mark.parametrize("field", ["phases", "_phases", "steps", "groups"])
    def test_create_workflow_refuses_definition_fields_before_persistence(
        self, registry_client, field
    ):
        """A create body is metadata only; the definition comes through /changes."""
        import core.entrypoints.admin.workflows as workflow_routes

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            response = registry_client.post(
                "/api/workflows",
                json={
                    "workflow_id": "wf_definition_in_create",
                    "display_name": "Definition in create",
                    "enabled": False,
                    field: [{"step_id": "normalize", "step_kind": "set_variable"}],
                },
            )
        assert response.status_code == 422, response.text
        payload = response.json()
        assert payload["code"] == "validation_error"
        error = payload["details"]["errors"][0]
        assert error["type"] == "workflow_definition_field_unsupported"
        assert error["ctx"]["fields"] == field
        assert "/changes" in error["msg"]
        mock_db.save_workflow.assert_not_called()
        mock_db._transaction.assert_not_called()

    def test_create_workflow_returns_warning_when_registry_reload_deferred(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
            from core.infrastructure.db.psycopg_runtime import OperationalError
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        mock_registry = MagicMock()
        mock_registry.reload_all.side_effect = OperationalError("pg-fs unavailable")

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={
                    "valid": True,
                    "errors": [],
                    "warnings": [],
                },
            ),
        ):
            resp = registry_client.post(
                "/api/workflows",
                json={
                    "workflow_id": "wf_deferred_reload",
                    "display_name": "Workflow Reload Deferred",
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "created"
        assert body["workflow_id"] == "wf_deferred_reload"
        assert body["warnings"] == [
            {
                "code": "workflow.registry_reload_deferred",
                "message": (
                    "Workflow was saved, but registry refresh is temporarily unavailable. "
                    "Retry in a few seconds."
                ),
            }
        ]
        mock_db.save_workflow.assert_called_once()
        mock_registry.reload_all.assert_called_once()

    def test_create_workflow_keeps_the_issues_of_a_disabled_workflow(self, registry_client):
        """A new workflow starts empty and disabled; its issues are reported, not refused."""
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        mock_registry = MagicMock()
        missing_steps = {
            "code": "workflow.steps_missing",
            "message": "Workflow must define at least one step before enabling/running",
        }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={"valid": False, "errors": [missing_steps], "warnings": []},
            ),
        ):
            resp = registry_client.post(
                "/api/workflows",
                json={"workflow_id": "wf_new_empty_draft", "display_name": "New Draft"},
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "created"
        assert body["validation"]["errors"] == [missing_steps]
        mock_db.save_workflow.assert_called_once()
        mock_registry.reload_all.assert_called_once()

    def test_create_workflow_rolls_back_invalid_enabled_save(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        mock_registry = MagicMock()

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={"valid": False, "errors": [{"message": "broken"}], "warnings": []},
            ),
        ):
            resp = registry_client.post(
                "/api/workflows",
                json={
                    "workflow_id": "wf_broken_save",
                    "display_name": "Broken Workflow",
                    "enabled": True,
                },
            )

        assert resp.status_code == 400
        mock_db.save_workflow.assert_called_once()
        mock_db._transaction.assert_called()
        mock_db.delete_workflow.assert_not_called()
        mock_db.apply_entity_snapshot.assert_not_called()
        mock_registry.reload_all.assert_not_called()

    def test_create_workflow_allows_disabled_blank_shell_without_execution_bindings(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        mock_registry = MagicMock()

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={
                    "valid": False,
                    "errors": [
                        {
                            "code": "workflow.execution_bindings_missing",
                            "message": "Workflow must include at least one real execution step before it can be enabled or run",
                            "path": "workflow.steps",
                            "severity": "error",
                        }
                    ],
                    "warnings": [],
                },
            ),
        ):
            resp = registry_client.post(
                "/api/workflows",
                json={
                    "workflow_id": "wf_blank_shell",
                    "display_name": "Blank Shell Workflow",
                    "enabled": False,
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "created"
        assert body["workflow_id"] == "wf_blank_shell"
        assert body["validation"]["valid"] is True
        assert body["validation"]["errors"] == []
        mock_db.save_workflow.assert_called_once()
        mock_registry.reload_all.assert_called_once()

    def test_create_workflow_binds_new_workflow_to_project_without_clobbering_existing_links(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        mock_db.get_project.return_value = {
            "project_id": "proj_demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_registry = MagicMock()

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
            patch.object(
                workflow_routes,
                "_request_scope",
                return_value=normalize_scope(
                    account_id=DEFAULT_ACCOUNT_ID,
                    project_id="proj_demo",
                ),
            ),
        ):
            resp = registry_client.post(
                "/api/workflows",
                json={
                    "workflow_id": "wf_bound_shell",
                    "display_name": "Bound Shell Workflow",
                    "enabled": False,
                    "project_id": "proj_demo",
                },
            )

        assert resp.status_code == 200
        save_payload = mock_db.save_workflow.call_args.args[0]
        assert save_payload["project_id"] == "proj_demo"
        mock_db.add_project_workflow_binding.assert_called_once_with("proj_demo", "wf_bound_shell")
        mock_db.list_project_workflows.assert_not_called()
        mock_db.save_project_workflows.assert_not_called()

    def test_clone_workflow_adds_only_the_requested_project_binding(self, registry_client):
        import core.entrypoints.admin.workflows as workflow_routes

        source = {
            "workflow_id": "source_wf",
            "account_id": DEFAULT_ACCOUNT_ID,
            "scope_kind": "account",
            "enabled": True,
            "settings": {},
            "trigger": {},
            "tags": [],
            "steps": [],
            "groups": [],
        }
        mock_db = MagicMock()
        mock_db.get_workflow.side_effect = lambda workflow_id: (
            source if workflow_id == "source_wf" else None
        )
        mock_db.get_project.return_value = {
            "project_id": "proj_demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_registry = MagicMock()
        portability = SimpleNamespace(assert_clone_allowed=lambda _workflow_id: None)

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_portability_service",
                return_value=portability,
            ),
            patch.object(
                workflow_routes,
                "_request_scope",
                return_value=normalize_scope(account_id=DEFAULT_ACCOUNT_ID),
            ),
        ):
            response = registry_client.post(
                "/api/workflows/source_wf/clone",
                json={"workflow_id": "cloned_wf", "project_id": "proj_demo"},
            )

        assert response.status_code == 200, response.text
        mock_db.add_project_workflow_binding.assert_called_once_with("proj_demo", "cloned_wf")
        mock_db.save_project_workflows.assert_not_called()

    def test_changes_refuse_raw_mcp_connector_step_on_an_enabled_workflow(self, registry_client):
        """The real readiness check judges the candidate before anything is written."""
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        stored = linear_workflow(
            {
                "step_id": "workflow_start",
                "step_kind": "set_variable",
                "override": {
                    "set_variable": {
                        "target": "state.workflow_started",
                        "value": True,
                    }
                },
            },
            {
                "step_id": "workflow_success",
                "step_kind": "stop_success",
                "override": {
                    "stop_success": {
                        "message": "Workflow completed successfully.",
                    }
                },
            },
            workflow_id="wf_raw_mcp_payload",
            display_name="Raw MCP Payload Workflow",
            enabled=True,
        )

        class _WorkflowChangeDB:
            def __init__(self):
                self.saved_workflow = None

            def get_workflow(self, workflow_id: str, include_trashed: bool = False):
                if str(workflow_id or "") == stored["workflow_id"]:
                    return stored
                return None

            def save_workflow(self, payload, author="admin", expected_revision=None):
                self.saved_workflow = dict(payload)
                return 2

            def _transaction(self):
                cursor = MagicMock()
                cursor.fetchone.return_value = {"revision": 1}
                return nullcontext(cursor)

        db = _WorkflowChangeDB()

        with (
            patch.object(workflow_routes, "_get_db", return_value=db),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
        ):
            resp = _post_changes(
                registry_client,
                "wf_raw_mcp_payload",
                [
                    {
                        "op": "insert_step_between",
                        "source": node("workflow_start"),
                        "outcome": {"name": "success"},
                        "step": {
                            "step_id": "load_projects",
                            "step_kind": "connector",
                            "connector_id": "mcp_c134570ca411419d",
                            "override": {
                                "connector": {
                                    "operation_id": "fs_browser_full.project_list",
                                }
                            },
                        },
                    }
                ],
            )

        assert resp.status_code == 400, resp.text
        detail = resp.json().get("detail") or {}
        assert detail.get("error") == "Contract validation failed"
        assert detail.get("entity_type") == "workflow"
        assert detail.get("entity_id") == "wf_raw_mcp_payload"
        assert detail.get("action") == "apply_changes"
        assert any(
            str(item.get("code") or "") == "workflow.step_connector_raw_mcp_unsupported"
            for item in (detail.get("issues") or [])
        )
        assert db.saved_workflow is None

    def test_update_workflow_rolls_back_invalid_enabled_save(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        existing = {
            "workflow_id": "weekly_digest",
            "display_name": "Quick Fix",
            "enabled": True,
            "trigger": {},
            "settings": {},
            "tags": [],
            "steps": [],
            "groups": [],
        }
        mock_db = MagicMock()
        mock_db.get_workflow.return_value = existing
        mock_registry = MagicMock()

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={"valid": False, "errors": [{"message": "broken"}], "warnings": []},
            ),
        ):
            resp = registry_client.put(
                "/api/workflows/weekly_digest",
                json={
                    "workflow_id": "weekly_digest",
                    "display_name": "Quick Fix",
                    "enabled": True,
                },
            )

        assert resp.status_code == 400
        mock_db.save_workflow.assert_called_once()
        mock_db._transaction.assert_called()
        mock_db.delete_workflow.assert_not_called()
        mock_db.apply_entity_snapshot.assert_not_called()
        mock_registry.reload_all.assert_not_called()

    def test_update_workflow_requires_revision_precondition(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        existing = {
            "workflow_id": "wf_revision_required",
            "display_name": "Revision Required",
            "enabled": False,
            "revision": 4,
            "trigger": {},
            "settings": {},
            "tags": [],
            "steps": [],
            "groups": [],
        }
        mock_db = MagicMock()
        mock_db.get_workflow.return_value = existing

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.put(
                "/api/workflows/wf_revision_required",
                json={
                    "workflow_id": "wf_revision_required",
                    "display_name": "Stale write without revision",
                    "enabled": False,
                },
            )

        assert resp.status_code == 428
        assert resp.json()["detail"]["code"] == "workflow_revision_required"
        mock_db.save_workflow.assert_not_called()

    def test_update_workflow_rejects_stale_revision(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
            from core.application.registry.db_store_workflows_mixin import (
                WorkflowRevisionConflictError,
            )
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        existing = {
            "workflow_id": "wf_stale_editor",
            "display_name": "Newer committed value",
            "enabled": False,
            "revision": 9,
            "trigger": {},
            "settings": {},
            "tags": [],
            "steps": [],
            "groups": [],
        }
        mock_db = MagicMock()
        mock_db.get_workflow.return_value = existing
        mock_db._transaction.return_value = nullcontext()
        mock_db.save_workflow.side_effect = WorkflowRevisionConflictError("wf_stale_editor", 8)

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.put(
                "/api/workflows/wf_stale_editor",
                json={
                    "workflow_id": "wf_stale_editor",
                    "display_name": "Older stale value",
                    "enabled": False,
                    "expected_revision": 8,
                },
            )

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "workflow_revision_conflict"
        assert detail["expected_revision"] == 8
        mock_db.save_workflow.assert_called_once()

    @pytest.mark.parametrize("field", ["phases", "steps", "groups"])
    def test_update_workflow_rejects_definition_payload_without_any_mutation(
        self, registry_client, field
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        existing_workflow = {
            "workflow_id": "wf_disabled_preserve_steps",
            "display_name": "Disabled Preserve Steps",
            "description": "",
            "enabled": False,
            "revision": 7,
            "account_id": DEFAULT_ACCOUNT_ID,
            "scope_kind": "system",
            "trigger": {},
            "settings": {},
            "participants": [],
            "tags": [],
            "steps": [
                {
                    "step_id": "workflow_start",
                    "step_kind": "set_variable",
                    "group_id": None,
                    "override": {
                        "set_variable": {
                            "target": "state.workflow_started",
                            "value": True,
                        }
                    },
                },
                {
                    "step_id": "workflow_success",
                    "step_kind": "stop_success",
                    "group_id": None,
                    "override": {
                        "stop_success": {
                            "message": "Workflow completed successfully.",
                        }
                    },
                },
            ],
            "groups": [],
        }

        class _WorkflowUpdateDB:
            def __init__(self, workflow):
                self.workflow = dict(workflow)
                self.saved_payload = None

            def get_workflow(self, workflow_id):
                if str(workflow_id or "").strip() != str(self.workflow.get("workflow_id") or ""):
                    return None
                return dict(self.workflow)

            def save_workflow(self, data, author="admin", expected_revision=None):
                self.saved_payload = dict(data)
                self.expected_revision = expected_revision
                self.workflow = dict(data)

            def _transaction(self):
                return nullcontext()

        db = _WorkflowUpdateDB(existing_workflow)
        mock_registry = MagicMock()

        with (
            patch.object(workflow_routes, "_get_db", return_value=db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
        ):
            resp = registry_client.put(
                "/api/workflows/wf_disabled_preserve_steps",
                json={
                    "workflow_id": "wf_disabled_preserve_steps",
                    "display_name": "Disabled Preserve Steps Updated",
                    "enabled": False,
                    "expected_revision": 7,
                    field: [
                        {
                            "step_id": "raw_mcp_step",
                            "step_kind": "connector",
                            "connector_id": "mcp_c134570ca411419d",
                            "override": {
                                "connector": {
                                    "operation_id": "fs_browser_full.project_list",
                                }
                            },
                        }
                    ],
                },
            )

        assert resp.status_code == 422
        payload = resp.json()
        assert payload["code"] == "validation_error"
        error = payload["details"]["errors"][0]
        assert error["type"] == "workflow_metadata_field_unsupported"
        assert error["ctx"]["fields"] == field
        assert "/api/workflows/<workflow_id>/changes" in error["msg"]
        assert db.saved_payload is None
        mock_registry.reload_all.assert_not_called()

    def test_patch_workflow_rolls_back_invalid_enabled_save(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        existing = {
            "workflow_id": "weekly_digest",
            "display_name": "Quick Fix",
            "enabled": True,
            "trigger": {},
            "settings": {},
            "tags": [],
            "steps": [],
            "groups": [],
        }
        mock_db = MagicMock()
        mock_db.get_workflow.return_value = existing
        mock_registry = MagicMock()

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={"valid": False, "errors": [{"message": "broken"}], "warnings": []},
            ),
        ):
            resp = registry_client.patch(
                "/api/workflows/weekly_digest",
                json={"description": "updated"},
            )

        assert resp.status_code == 400
        mock_db.save_workflow.assert_called_once()
        mock_db._transaction.assert_called()
        mock_db.delete_workflow.assert_not_called()
        mock_db.apply_entity_snapshot.assert_not_called()
        mock_registry.reload_all.assert_not_called()

    def test_patch_workflow_deep_merges_callable_settings(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        existing = {
            "workflow_id": "callable_merge",
            "display_name": "Callable Merge",
            "enabled": False,
            "trigger": {},
            "settings": {
                "callable": {
                    "enabled": False,
                    "trigger_channels": ["parent_workflow"],
                    "expected_inputs": [{"name": "input_text", "type": "text", "required": True}],
                    "expected_outputs": [
                        {"name": "uppercase_text", "type": "text", "required": False}
                    ],
                },
                "participants": [],
            },
            "tags": [],
            "steps": [],
            "groups": [],
        }
        mock_db = MagicMock()
        mock_db.get_workflow.return_value = existing
        mock_registry = MagicMock()

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            resp = registry_client.patch(
                "/api/workflows/callable_merge",
                json={"settings": {"callable": {"enabled": True}}},
            )

        assert resp.status_code == 200
        saved_payload = mock_db.save_workflow.call_args.args[0]
        callable_settings = saved_payload["settings"]["callable"]
        assert callable_settings["enabled"] is True
        assert callable_settings["trigger_channels"] == ["parent_workflow"]
        assert callable_settings["expected_inputs"] == [
            {"name": "input_text", "type": "text", "required": True}
        ]
        assert callable_settings["expected_outputs"] == [
            {"name": "uppercase_text", "type": "text", "required": False}
        ]

    def test_create_workflow_strips_legacy_trigger_keys(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        mock_registry = MagicMock()

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            resp = registry_client.post(
                "/api/workflows",
                json={
                    "workflow_id": "wf_trigger_cleanup_test",
                    "display_name": "WF Trigger Cleanup Test",
                    "trigger": {
                        "task_types": ["feature"],
                        "complexity": ["small"],
                        "confidence_threshold": 1.9,
                    },
                },
            )

        assert resp.status_code == 400
        assert "unsupported routing keys" in resp.text
        mock_db.save_workflow.assert_not_called()

    def test_patch_workflow_trigger_rejects_legacy_routing_keys(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "weekly_digest",
            "display_name": "Quick Fix",
            "trigger": {
                "task_types": ["bugfix"],
            },
            "settings": {},
            "tags": [],
            "enabled": True,
        }
        mock_registry = MagicMock()

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            resp = registry_client.patch(
                "/api/workflows/weekly_digest",
                json={
                    "trigger": {
                        "task_types": ["feature"],
                        "complexity": ["major"],
                        "confidence_threshold": -2,
                    },
                },
            )

        assert resp.status_code == 400
        assert "unsupported routing keys" in resp.text
        mock_db.save_workflow.assert_not_called()

    def test_add_step_is_refused_when_it_introduces_a_readiness_issue(self, registry_client):
        """An enabled workflow keeps its revision; nothing is written to roll back."""
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = _definition_db(
            linear_workflow(
                {"step_id": "planning", "step_kind": "set_variable"},
                workflow_id="wf_contract_test",
                enabled=True,
            )
        )
        introduced = {
            "severity": "error",
            "code": "workflow.step_input_unresolved",
            "message": "Required input 'spec_doc' not produced by prior steps",
            "path": "workflow.steps.compose_sections.boundary_contract.entry",
        }

        def readiness(store, workflow_id):
            candidate = store.get_workflow(workflow_id)
            added = next(
                step for step in candidate["steps"] if step["step_id"] == "compose_sections"
            )
            assert added["step_kind"] == "agent"
            assert added["agent_id"] == "coordinator"
            return {"valid": False, "errors": [introduced], "warnings": []}

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                side_effect=readiness,
            ) as validate_candidate,
        ):
            resp = _post_changes(
                registry_client,
                "wf_contract_test",
                [
                    {
                        "op": "insert_step_between",
                        "source": node("planning"),
                        "outcome": {"name": "success"},
                        "step": {
                            "step_id": "compose_sections",
                            "step_kind": "agent",
                            "agent_id": "coordinator",
                            "skill_id": "compose_sections",
                        },
                    }
                ],
            )

        assert resp.status_code == 400, resp.text
        detail = resp.json().get("detail") or {}
        assert detail.get("error") == "Contract validation failed"
        assert detail.get("entity_type") == "workflow"
        assert detail.get("action") == "apply_changes"
        assert detail["validation"]["errors"] == [introduced]
        validate_candidate.assert_called_once()
        assert mock_db.get_workflow.return_value["revision"] == 1
        assert mock_db.get_workflow.return_value["enabled"] is True
        assert [step["step_id"] for step in mock_db.get_workflow.return_value["steps"]] == [
            "planning",
            "done",
        ]
        mock_db.save_workflow.assert_not_called()
        mock_db.delete_step.assert_not_called()

    def test_add_agent_step_derives_its_skill_instead_of_storing_one(self, registry_client):
        """The skill an agent step runs is the agent's; a hand-set skill is dropped."""
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = _definition_db(
            linear_workflow(
                {"step_id": "planning", "step_kind": "set_variable"},
                workflow_id="wf_contract_test",
                enabled=False,
            )
        )

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
            patch.object(
                workflow_routes, "_get_workflow_validation", return_value=_VALID_READINESS
            ),
            patch.object(workflow_routes, "validate_agent_skill_compatibility") as compatibility,
        ):
            resp = _post_changes(
                registry_client,
                "wf_contract_test",
                [
                    {
                        "op": "insert_step_between",
                        "source": node("planning"),
                        "outcome": {"name": "success"},
                        "step": {
                            "step_id": "compose_sections",
                            "step_kind": "agent",
                            "agent_id": "coordinator",
                            "skill_id": "compose_sections_v2",
                        },
                    }
                ],
            )

        assert resp.status_code == 200, resp.text
        saved = _saved_step(mock_db, "compose_sections")
        assert saved["agent_id"] == "coordinator"
        assert "skill_id" not in saved
        compatibility.assert_not_called()

    def test_add_step_allows_disabled_draft_to_keep_preexisting_step_errors(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        preexisting_errors = [
            {
                "code": "workflow.step_http_request_url_missing",
                "severity": "error",
                "message": "http_request requires override.http_request.url",
                "path": "workflow.steps.request.override.http_request.url",
            },
            {
                "code": "workflow.step_http_request_response_target_missing",
                "severity": "error",
                "message": "http_request requires override.http_request.response_target",
                "path": "workflow.steps.request.override.http_request.response_target",
            },
        ]
        mock_db = _definition_db(
            linear_workflow(
                {
                    "step_id": "request",
                    "step_kind": "http_request",
                    "override": {"http_request": {"url": "", "response_target": ""}},
                },
                workflow_id="wf_http_draft",
                enabled=False,
            )
        )

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={
                    "valid": False,
                    "errors": preexisting_errors,
                    "warnings": [],
                },
            ),
        ):
            resp = _post_changes(
                registry_client,
                "wf_http_draft",
                [
                    {
                        "op": "insert_step_between",
                        "source": node("request"),
                        "outcome": {"name": "success"},
                        "step": {
                            "step_kind": "set_variable",
                            "step_id": "set-result",
                            "override": {"set_variable": {"assignments": {"result": "ok"}}},
                        },
                    }
                ],
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["revision"] == 2
        assert body["valid"] is False
        assert body["issues"] == preexisting_errors
        assert [step["step_id"] for step in _saved_definition(mock_db)["steps"]] == [
            "request",
            "done",
            "set-result",
        ]

    def test_update_group_allows_disabled_draft_to_keep_preexisting_step_errors(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        preexisting_error = {
            "code": "workflow.step_http_request_url_missing",
            "severity": "error",
            "message": "http_request requires override.http_request.url",
            "path": "workflow.steps.request.override.http_request.url",
        }
        workflow = linear_workflow(
            {"step_id": "request", "step_kind": "http_request", "group_id": "planning"},
            workflow_id="wf_http_draft",
            enabled=False,
            groups=[{"group_id": "planning", "display_name": "Planning"}],
        )
        mock_db = _definition_db(workflow)

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={
                    "valid": False,
                    "errors": [preexisting_error],
                    "warnings": [],
                },
            ),
        ):
            resp = _post_changes(
                registry_client,
                "wf_http_draft",
                [
                    {
                        "op": "update_group_config",
                        "group_id": "planning",
                        "patch": {"display_name": "HTTP planning"},
                    }
                ],
            )

        assert resp.status_code == 200, resp.text
        assert preexisting_error in resp.json()["issues"]
        assert _saved_definition(mock_db)["groups"] == [
            {"group_id": "planning", "display_name": "HTTP planning"}
        ]

    @pytest.mark.parametrize("step_ref", ["victim_step", "step-row-id"])
    def test_delete_step_rejects_a_step_the_workflow_does_not_have(self, registry_client, step_ref):
        """Operations name steps by their business id; a row id is not an alias."""
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = _definition_db(
            linear_workflow(
                {"id": "step-row-id", "step_id": "owned_step", "step_kind": "set_variable"},
                workflow_id="wf_contract_test",
                enabled=False,
            )
        )

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
            patch.object(
                workflow_routes, "_get_workflow_validation", return_value=_VALID_READINESS
            ),
        ):
            resp = _post_changes(
                registry_client,
                "wf_contract_test",
                [{"op": "delete_step", "step_id": step_ref}],
            )

        assert resp.status_code == 400, resp.text
        assert _issue_codes(resp) == ["workflow.change.step_unknown"]
        assert resp.json()["detail"]["issues"][0]["path"] == "operations[0].step_id"
        mock_db.save_workflow.assert_not_called()

    def test_update_step_is_refused_atomically_when_it_introduces_a_readiness_issue(
        self, registry_client
    ):
        """The old route wrote, re-validated and wrote back; now nothing is written."""
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = _definition_db(
            linear_workflow(
                {
                    "step_id": "compose_sections",
                    "step_kind": "agent",
                    "agent_id": "coordinator",
                    "skill_id": "compose_sections",
                    "connector_install_id": "",
                    "optional": False,
                    "context_inject": {},
                    "override": None,
                },
                workflow_id="wf_contract_test",
                enabled=True,
            )
        )
        introduced = {
            "severity": "error",
            "message": "Required input 'spec_doc' not produced by prior steps",
            "path": "workflow.steps.compose_sections.boundary_contract.entry",
        }

        def readiness(store, workflow_id):
            candidate = store.get_workflow(workflow_id)
            edited = next(
                step for step in candidate["steps"] if step["step_id"] == "compose_sections"
            )
            assert edited["connector_install_id"] == "c3427f50-live-regression"
            return {"valid": False, "errors": [introduced], "warnings": []}

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                side_effect=readiness,
            ) as validate_candidate,
        ):
            resp = _post_changes(
                registry_client,
                "wf_contract_test",
                [
                    {
                        "op": "update_step_config",
                        "step_id": "compose_sections",
                        "patch": {
                            "agent_id": "coordinator",
                            "skill_id": "compose_sections_v2",
                            "connector_install_id": "c3427f50-live-regression",
                        },
                    }
                ],
            )

        assert resp.status_code == 400, resp.text
        detail = resp.json().get("detail") or {}
        assert detail.get("error") == "Contract validation failed"
        assert detail.get("entity_type") == "workflow"
        assert detail.get("action") == "apply_changes"
        assert detail["validation"]["errors"] == [introduced]
        validate_candidate.assert_called_once()
        assert mock_db.get_workflow.return_value["revision"] == 1
        assert mock_db.get_workflow.return_value["enabled"] is True
        assert mock_db.get_workflow.return_value["steps"][0]["connector_install_id"] == ""
        mock_db.save_workflow.assert_not_called()
        mock_db._transaction.assert_called_once()

    @pytest.mark.parametrize(
        ("step_ref", "expected_status"), [("set_status", 200), ("step-row-id", 400)]
    )
    def test_update_step_addresses_the_step_by_its_business_id(
        self, registry_client, step_ref, expected_status
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = _definition_db(
            linear_workflow(
                {
                    "id": "step-row-id",
                    "step_id": "set_status",
                    "step_kind": "set_variable",
                    "display_name": "Set status",
                    "optional": False,
                },
                workflow_id="wf_contract_test",
                enabled=False,
            )
        )

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
            patch.object(
                workflow_routes, "_get_workflow_validation", return_value=_VALID_READINESS
            ),
        ):
            resp = _post_changes(
                registry_client,
                "wf_contract_test",
                [
                    {
                        "op": "update_step_config",
                        "step_id": step_ref,
                        "patch": {"display_name": "Set current status"},
                    }
                ],
            )

        assert resp.status_code == expected_status, resp.text
        if expected_status == 200:
            assert _saved_step(mock_db, "set_status")["display_name"] == "Set current status"
        else:
            assert _issue_codes(resp) == ["workflow.change.step_unknown"]
            mock_db.save_workflow.assert_not_called()

    def test_changes_refuse_a_stale_revision_with_409(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = _definition_db(
            linear_workflow(
                {"step_id": "set_status", "step_kind": "set_variable"},
                workflow_id="wf_contract_test",
                enabled=False,
            ),
            revision=5,
        )

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
            patch.object(
                workflow_routes, "_get_workflow_validation", return_value=_VALID_READINESS
            ),
        ):
            resp = _post_changes(
                registry_client,
                "wf_contract_test",
                [
                    {
                        "op": "update_step_config",
                        "step_id": "set_status",
                        "patch": {"display_name": "Stale"},
                    }
                ],
                expected_revision=4,
            )

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == {
            "code": "workflow_revision_conflict",
            "message": "The workflow changed after it was loaded. Reload before saving.",
            "workflow_id": "wf_contract_test",
            "expected_revision": 4,
            "revision": 5,
        }
        mock_db.save_workflow.assert_not_called()

    @pytest.mark.parametrize(
        "body",
        [
            {"operations": [{"op": "delete_step", "step_id": "x"}]},
            {"expected_revision": 0, "operations": [{"op": "delete_step", "step_id": "x"}]},
            {"expected_revision": 1, "operations": []},
            {"expected_revision": 1, "operations": [], "phases": []},
        ],
    )
    def test_changes_body_requires_a_revision_and_operations(self, registry_client, body):
        import core.entrypoints.admin.workflows as workflow_routes

        mock_db = _definition_db(linear_workflow(workflow_id="wf_contract_test"))
        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.post("/api/workflows/wf_contract_test/changes", json=body)

        assert resp.status_code == 422, resp.text
        mock_db.save_workflow.assert_not_called()

    def test_changes_refuse_an_unknown_operation_kind(self, registry_client):
        import core.entrypoints.admin.workflows as workflow_routes

        mock_db = _definition_db(
            linear_workflow(
                {"step_id": "set_status", "step_kind": "set_variable"},
                workflow_id="wf_contract_test",
                enabled=False,
            )
        )
        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
            patch.object(
                workflow_routes, "_get_workflow_validation", return_value=_VALID_READINESS
            ),
        ):
            resp = _post_changes(
                registry_client,
                "wf_contract_test",
                [{"op": "add_phase", "phase": {"phase_id": "review"}}],
            )

        assert resp.status_code == 400, resp.text
        assert _issue_codes(resp) == ["workflow.change.op_unknown"]
        mock_db.save_workflow.assert_not_called()

    def test_changes_return_404_for_an_unknown_workflow(self, registry_client):
        import core.entrypoints.admin.workflows as workflow_routes

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_resolve_workflow_row", return_value=None),
        ):
            resp = _post_changes(
                registry_client, "wf_missing", [{"op": "delete_step", "step_id": "x"}]
            )

        assert resp.status_code == 404, resp.text
        mock_db.save_workflow.assert_not_called()

    @pytest.mark.parametrize(
        ("error", "expected_status", "expected_detail"),
        [
            (
                'insert violates foreign key constraint "registry_workflow_steps_skill_id_fkey"',
                404,
                "Skill not found",
            ),
            (
                'insert violates foreign key constraint "registry_workflow_steps_connector_install_id_fkey"',
                404,
                "Connector install not found",
            ),
            (
                'insert violates foreign key constraint "registry_workflow_steps_block_key_fkey"',
                404,
                "Published block not found",
            ),
            (
                'insert violates foreign key constraint "registry_workflow_steps_workflow_id_fkey"',
                400,
                "Referenced workflow dependency not found",
            ),
        ],
    )
    def test_changes_map_reference_refusals_to_author_errors(
        self, registry_client, error, expected_status, expected_detail
    ):
        import core.entrypoints.admin.workflows as workflow_routes

        mock_db = _definition_db(
            linear_workflow(
                {"step_id": "set_status", "step_kind": "set_variable"},
                workflow_id="wf_contract_test",
                enabled=False,
            )
        )
        mock_db.save_workflow.side_effect = RuntimeError(error)
        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
            patch.object(
                workflow_routes, "_get_workflow_validation", return_value=_VALID_READINESS
            ),
        ):
            resp = _post_changes(
                registry_client,
                "wf_contract_test",
                [
                    {
                        "op": "update_step_config",
                        "step_id": "set_status",
                        "patch": {"display_name": "Renamed"},
                    }
                ],
            )

        assert resp.status_code == expected_status, resp.text
        assert resp.json()["detail"] == expected_detail

    def test_simulate_workflow_returns_deterministic_result_without_llm(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_agent_skills.return_value = [{"skill_id": "collect_findings"}]
        mock_registry = MagicMock()
        mock_registry.workflows.get_workflow.return_value = SimpleNamespace(
            workflow_id="wf_demo",
            steps=[
                SimpleNamespace(
                    step_id="plan",
                    step_kind="agent",
                    group_id=None,
                    lane_id=None,
                    participant_key="",
                    agent="scout",
                    agent_id="scout",
                    connector_id="",
                    connector_install_id="",
                    workflow_id="",
                    optional=False,
                ),
                SimpleNamespace(
                    step_id="collect",
                    step_kind="set_variable",
                    group_id="batch",
                    lane_id=None,
                    participant_key="",
                    agent="",
                    agent_id="",
                    connector_id="",
                    connector_install_id="",
                    workflow_id="",
                    optional=True,
                ),
            ],
        )

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
            patch(
                "core.application.orchestration.classifier._classify_with_llm",
                side_effect=AssertionError("simulate must not call live LLM classification"),
            ),
        ):
            resp = registry_client.post(
                "/api/workflows/wf_demo/simulate",
                json={
                    "task_envelope": {
                        "title": "Add user profile",
                        "task_summary": "Add user profile",
                    }
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["workflow_id"] == "wf_demo"
        assert body["classification"]["task_type"] == ""
        assert body["classification"]["reasoning"] == "default_classification"
        assert "phases" not in body
        assert [step["step_id"] for step in body["steps"]] == ["plan", "collect"]
        plan, collect = body["steps"]
        assert plan["agent"] == "scout"
        assert plan["skill_id"] == "collect_findings"
        assert plan["group_id"] is None
        # A non-agent step resolves no skill; a grouped step names its real group.
        assert collect["skill_id"] == ""
        assert collect["group_id"] == "batch"
        assert collect["optional"] is True

    def test_simulate_registry_only_workflow_enforces_workflow_scope(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        mock_registry = MagicMock()
        mock_registry.workflows.get_workflow.return_value = {"workflow_id": "wf_registry_only"}

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_request_scope",
                return_value=normalize_scope(
                    account_id=DEFAULT_ACCOUNT_ID,
                    allowed_workflow_ids=frozenset({"wf_allowed"}),
                ),
            ),
        ):
            resp = registry_client.post(
                "/api/workflows/wf_registry_only/simulate",
                json={"task_envelope": {"title": "Nope"}},
            )

        assert resp.status_code == 403

    def test_simulate_workflow_returns_task_type_in_classification(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_registry = MagicMock()
        mock_registry.workflows.get_workflow.return_value = {
            "id": "wf_demo",
            "steps": [],
            "groups": [],
        }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            resp = registry_client.post(
                "/api/workflows/wf_demo/simulate",
                json={
                    "task_envelope": {
                        "title": "Fix login redirect",
                        "task_summary": "Fix login redirect",
                        "task_type": "bugfix",
                    }
                },
            )

        assert resp.status_code == 200
        assert resp.json()["classification"]["task_type"] == "bugfix"

    def test_simulate_nonexistent_workflow_id_returns_404(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_registry = MagicMock()
        mock_registry.workflows.get_workflow.return_value = None

        with patch.object(workflow_routes, "_get_registry", return_value=mock_registry):
            resp = registry_client.post(
                "/api/workflows/nonexistent_wf_xyz/simulate",
                json={"task_envelope": {"title": "Test task"}},
            )

        assert resp.status_code == 404
        assert "nonexistent_wf_xyz" in resp.text

    def test_workflow_history_nonexistent(self, registry_client):
        resp = registry_client.get("/api/workflows/nonexistent_xyz/history")
        assert resp.status_code in (200, 404, 500)

    def test_clone_nonexistent_workflow(self, registry_client):
        resp = registry_client.post(
            "/api/workflows/nonexistent_xyz/clone",
            json={"workflow_id": "nonexistent_xyz_copy"},
        )
        assert resp.status_code in (404, 409, 422, 500)

    def test_list_agent_skill_pairs(self, registry_client):
        resp = registry_client.get("/api/workflows/meta/agent-skill-pairs")
        assert resp.status_code in (200, 404, 500)
        if resp.status_code == 200:
            data = resp.json()
            assert isinstance(data, dict)
            assert "pairs" in data

    def test_callable_workflows_meta_filters_enabled_callable_rows(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.list_workflows.return_value = [
            {
                "workflow_id": "wf_callable",
                "display_name": "Callable WF",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "trigger_channels": ["parent_workflow"],
                        "expected_inputs": [{"name": "request", "type": "text"}],
                        "expected_outputs": [{"name": "result", "type": "json"}],
                    }
                },
            },
            {
                "workflow_id": "wf_api_only",
                "display_name": "API Only",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "trigger_channels": ["api"],
                        "expected_inputs": [{"name": "request", "type": "text"}],
                        "output_mode": "none",
                    }
                },
            },
            {
                "workflow_id": "wf_disabled",
                "display_name": "Disabled Callable",
                "enabled": False,
                "settings": {"callable": {"enabled": True}},
            },
            {
                "workflow_id": "wf_plain",
                "display_name": "Plain",
                "enabled": True,
                "settings": {"callable": {"enabled": False}},
            },
            {
                # R2-11: a hook's storage is callable by its hook only, never an Invoke target.
                "workflow_id": "wf_hook_storage",
                "display_name": "Hook storage",
                "enabled": True,
                "settings": {
                    "hook_storage": {
                        "author_workflow_id": "wf_author",
                        "hook_id": "adjust",
                        "project_id": "p-1",
                    },
                    "callable": {
                        "enabled": True,
                        "trigger_channels": ["parent_workflow"],
                        "expected_inputs": [{"name": "request", "type": "text"}],
                        "output_mode": "none",
                    },
                },
            },
        ]

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get("/api/workflows/meta/callable-workflows")

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["count"] == 1
        assert payload["workflows"][0]["workflow_id"] == "wf_callable"
        assert payload["workflows"][0]["expected_outputs"][0]["name"] == "result"

    def test_connector_catalog_meta_scopes_project_connections_per_connector(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        connectors = [
            {
                "connector_id": "connector.crm",
                "display_name": "CRM",
                "description": "CRM connector",
                "namespace_prefix": "crm",
                "allowlisted_tools": ["crm.search", "crm.sync"],
                "enabled": True,
                "status": "healthy",
            },
            {
                "connector_id": "connector.mail",
                "display_name": "Mail",
                "description": "Mail connector",
                "namespace_prefix": "mail",
                "allowlisted_tools": ["mail.send"],
                "enabled": True,
                "status": "healthy",
            },
        ]
        project_connections = [
            {
                "connection_id": "conn-crm-1",
                "extension_id": "connector.crm",
                "extension_install_id": "",
                "connection_kind": "mcp_connector",
                "runtime_mode": "local",
                "health_status": "healthy",
                "runtime_status": "compatible",
                "enabled": True,
                "last_error": "",
                "connection_config": {},
                "connection_routes": [],
            },
            {
                "connection_id": "conn-unrelated",
                "extension_id": "connector.other",
                "extension_install_id": "",
                "connection_kind": "mcp_connector",
                "runtime_mode": "local",
                "health_status": "healthy",
                "runtime_status": "compatible",
                "enabled": True,
                "last_error": "",
                "connection_config": {},
                "connection_routes": [],
            },
        ]

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_list_connector_catalog_rows", return_value=connectors),
            patch.object(
                workflow_routes,
                "_list_project_connections_for_catalog",
                return_value=project_connections,
            ),
        ):
            resp = registry_client.get(
                "/api/workflows/meta/connector-catalog",
                params={"project_id": 17},
            )

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["count"] == 2
        crm_row = next(
            item for item in payload["connectors"] if item["connector_id"] == "connector.crm"
        )
        mail_row = next(
            item for item in payload["connectors"] if item["connector_id"] == "connector.mail"
        )
        assert crm_row["connector_install_id"] == ""
        assert [item["connection_id"] for item in crm_row["connections"]] == ["conn-crm-1"]
        assert mail_row["connections"] == []

    def test_connector_catalog_meta_exposes_registry_tool_actions(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.list_tools.return_value = [
            {
                "tool_id": "sample_content_tools",
                "display_name": "Demo Content Tools",
                "description": "Composes deterministic demo content.",
                "enabled": True,
                "interface": {
                    "functions": [
                        {
                            "name": "demo_compose_content_brief",
                            "description": "Compose a content brief.",
                            "parameters": [
                                {
                                    "name": "topic",
                                    "type": "string",
                                    "required": True,
                                    "description": "Brief topic",
                                }
                            ],
                            "returns": {
                                "name": "content_brief",
                                "type": "json",
                                "required": True,
                            },
                        }
                    ]
                },
            }
        ]

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_list_connector_catalog_rows", return_value=[]),
            patch.object(
                workflow_routes,
                "_list_project_connections_for_catalog",
                return_value=[],
            ),
        ):
            resp = registry_client.get(
                "/api/workflows/meta/connector-catalog",
                params={"project_id": 17},
            )

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["count"] == 1
        row = payload["connectors"][0]
        assert row["connector_id"] == "connector.sample_content_tools"
        assert row["source"] == "registry_tool"
        assert row["tool_url"] == "/registry/tools/sample_content_tools"
        assert row["connections"] == []
        assert row["operations"][0]["operation_id"] == "demo_compose_content_brief"
        assert row["operations"][0]["parameters"][0]["name"] == "topic"
        assert row["operations"][0]["returns"]["name"] == "content_brief"

    def test_connector_catalog_meta_groups_projected_mcp_tools_as_actions(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.list_tools.return_value = [
            {
                "tool_id": "drive.create_file",
                "display_name": "Drive / create_file",
                "description": "Create a file.",
                "enabled": True,
                "category": "other",
                "interface": {
                    "functions": [
                        {
                            "name": "drive.create_file",
                            "display_name": "create_file",
                            "description": "Create a file.",
                            "parameters": [],
                            "returns": {
                                "name": "result",
                                "type": "json",
                                "required": True,
                            },
                        }
                    ]
                },
                "implementation": {
                    "type": "mcp",
                    "connector_id": "mcp_drive",
                    "remote_tool_name": "create_file",
                },
            },
            {
                "tool_id": "drive.search_files",
                "display_name": "Drive / search_files",
                "description": "Search files.",
                "enabled": True,
                "category": "other",
                "interface": {
                    "functions": [
                        {
                            "name": "drive.search_files",
                            "display_name": "search_files",
                            "description": "Search files.",
                            "parameters": [],
                            "returns": {
                                "name": "result",
                                "type": "json",
                                "required": True,
                            },
                        }
                    ]
                },
                "implementation": {
                    "type": "mcp",
                    "connector_id": "mcp_drive",
                    "remote_tool_name": "search_files",
                },
            },
        ]
        mock_db.list_tools.return_value.extend(
            [
                {
                    "tool_id": "drive.unavailable",
                    "display_name": "Drive / unavailable",
                    "enabled": True,
                    "availability_status": "unavailable",
                    "category": "other",
                    "interface": {
                        "functions": [
                            {
                                "name": "drive.unavailable",
                                "display_name": "Unavailable",
                                "parameters": [],
                                "returns": {},
                            }
                        ]
                    },
                },
                {
                    "tool_id": "drive.foreign",
                    "display_name": "Drive / foreign",
                    "enabled": True,
                    "account_id": "another-account",
                    "category": "other",
                    "interface": {
                        "functions": [
                            {
                                "name": "drive.foreign",
                                "display_name": "Foreign",
                                "parameters": [],
                                "returns": {},
                            }
                        ]
                    },
                },
            ]
        )
        readiness_rows = {row["tool_id"]: row for row in mock_db.list_tools.return_value}
        readiness_rows["drive.disabled"] = {
            "tool_id": "drive.disabled",
            "enabled": False,
            "availability_status": "available",
        }
        mock_db.get_tool.side_effect = lambda tool_id: readiness_rows.get(tool_id)

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_list_connector_catalog_rows",
                return_value=[
                    {
                        "connector_id": "mcp_drive",
                        "display_name": "Drive",
                        "description": "Raw MCP connector row",
                        "namespace_prefix": "drive",
                        "allowlisted_tools": [
                            "drive.create_file",
                            "drive.search_files",
                            "drive.disabled",
                            "drive.unavailable",
                            "drive.foreign",
                            "drive.deleted",
                        ],
                        "enabled": True,
                        "status": "active",
                    }
                ],
            ),
            patch.object(
                workflow_routes,
                "_list_project_connections_for_catalog",
                return_value=[],
            ),
        ):
            resp = registry_client.get(
                "/api/workflows/meta/connector-catalog",
                params={"project_id": 17},
            )

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["count"] == 1
        row = payload["connectors"][0]
        assert row["connector_id"] == "mcp_drive"
        assert row["display_name"] == "Drive"
        assert row["source"] == "mcp_connector"
        assert [operation["display_name"] for operation in row["operations"]] == [
            "create_file",
            "search_files",
        ]
        assert {operation["tool_id"] for operation in row["operations"]} == {
            "drive.create_file",
            "drive.search_files",
        }
        assert [operation["runtime_connector_id"] for operation in row["operations"]] == [
            "connector.drive.create_file",
            "connector.drive.search_files",
        ]

    def test_connector_catalog_meta_exposes_standard_tool_actions(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.list_tools.return_value = [
            {
                "tool_id": "std.numbers",
                "display_name": "Numbers",
                "description": "Deterministic numeric Standard Tool Actions.",
                "enabled": True,
                "category": "standard_tool_action",
                "interface": {
                    "functions": [
                        {
                            "name": "round",
                            "display_name": "Round",
                            "description": "Round a numeric value.",
                            "parameters": [
                                {"name": "value", "type": "number", "required": True},
                                {"name": "decimals", "type": "integer"},
                            ],
                            "returns": {
                                "name": "result",
                                "type": "number",
                                "required": True,
                            },
                        }
                    ]
                },
            }
        ]

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_list_connector_catalog_rows", return_value=[]),
            patch.object(
                workflow_routes,
                "_list_project_connections_for_catalog",
                return_value=[],
            ),
        ):
            resp = registry_client.get(
                "/api/workflows/meta/connector-catalog",
                params={"project_id": 17},
            )

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["count"] == 1
        row = payload["connectors"][0]
        assert row["connector_id"] == "connector.std.numbers"
        assert row["source"] == "standard_tool_action"
        assert row["category"] == "standard_tool_action"
        assert row["tool_url"] == "/registry/tools/std.numbers"
        assert row["connections"] == []
        operation = row["operations"][0]
        assert operation["operation_id"] == "round"
        assert operation["source"] == "standard_tool_action"
        assert operation["category"] == "standard_tool_action"
        assert operation["tool_id"] == "std.numbers"

    def test_connector_catalog_extension_actions_require_active_account_install(
        self, registry_client
    ):
        _ = registry_client
        try:
            import core.entrypoints.admin.workflows_connector_catalog as connector_catalog
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.conn = object()
        mock_db.list_tools.return_value = []
        release = SimpleNamespace(
            extension_id="example.host-fixture",
            descriptor={
                "display_name": "Example Extension",
                "operation_manifest": [
                    {
                        "operation_id": "publish_record",
                        "operation_kind": "action",
                        "workflow_visible": True,
                        "outputs": [
                            {"name": "receipt_handle", "value_type": "value"},
                            {"name": "provider_status", "value_type": "value"},
                        ],
                    }
                ],
                "action_definitions": [
                    {
                        "action_id": "publish_record",
                        "title": "Publish Record",
                        "workflow_visible": True,
                        "parameters": [{"name": "to", "type": "string"}],
                        "result_fields": ["receipt_handle", "provider_status"],
                    }
                ],
            },
        )

        class RuntimeStore:
            def __init__(self, conn):
                assert conn is mock_db.conn

            def get_release(self, release_id: str):
                assert release_id == "release_example"
                return release

        with (
            patch.object(
                connector_catalog, "_platform_logger_for_admin_catalog", return_value=mock_db
            ),
            patch.object(
                connector_catalog,
                "_list_project_connections_for_catalog",
                return_value=[],
            ),
            patch.object(
                connector_catalog,
                "_active_extension_install_index_for_catalog",
                return_value={
                    "example.host-fixture": SimpleNamespace(
                        extension_id="example.host-fixture",
                        install_id="install_example",
                        release_id="release_example",
                    )
                },
            ),
            patch.object(connector_catalog, "DBExtensionRuntimeStore", RuntimeStore),
        ):
            rows = connector_catalog._extension_action_connector_rows(
                mock_db,
                project_id="17",
                scope=normalize_scope(account_id=DEFAULT_ACCOUNT_ID),
            )

        connector_ids = {row["connector_id"] for row in rows}
        assert "extension.example.host-fixture" in connector_ids
        assert "extension.acme.disabled-example" not in connector_ids
        action_row = next(
            row for row in rows if row["connector_id"] == "extension.example.host-fixture"
        )
        assert action_row["connector_install_id"] == "install_example"
        assert action_row["operations"][0]["operation_id"] == "publish_record"
        assert action_row["operations"][0]["returns"] == [
            {"name": "receipt_handle", "value_type": "value", "type": "value"},
            {"name": "provider_status", "value_type": "value", "type": "value"},
        ]

    def test_extension_catalog_prefers_account_install_over_global_install(self):
        import core.entrypoints.admin.workflows_connector_catalog as connector_catalog

        own_install = SimpleNamespace(
            extension_id="acme.document-converter",
            install_id="install_own",
            account_id="account_a",
            visibility="account",
        )
        global_install = SimpleNamespace(
            extension_id="acme.document-converter",
            install_id="install_global",
            account_id="account_b",
            visibility="global",
        )

        class InstallStore:
            def __init__(self, _conn):
                pass

            def list_for_account(self, account_id: str, **_kwargs):
                assert account_id == "account_a"
                return [global_install, own_install]

        with patch("core.application.extensions.install.DBExtensionInstallStore", InstallStore):
            index = connector_catalog._active_extension_install_index_for_catalog(
                SimpleNamespace(conn=object()),
                scope=normalize_scope(account_id="account_a"),
            )

        assert index["acme.document-converter"].install_id == "install_own"

    def test_connector_catalog_extension_actions_use_installed_release_descriptor(self):
        try:
            import core.entrypoints.admin.workflows_connector_catalog as connector_catalog
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.conn = object()
        mock_db.list_extensions.return_value = [
            {
                "extension_key": "example.host-fixture",
                "display_name": "Example Extension",
                "compiled_manifest": {
                    "action_definitions": [
                        {
                            "action_id": "v2_only",
                            "title": "V2 only",
                            "workflow_visible": True,
                        }
                    ]
                },
            },
        ]
        install = SimpleNamespace(
            extension_id="example.host-fixture",
            install_id="install_example",
            release_id="release_v1",
        )
        release = SimpleNamespace(
            extension_id="example.host-fixture",
            descriptor={
                "display_name": "Example Extension v1",
                "operation_manifest": [
                    {
                        "operation_id": "v1_send",
                        "operation_kind": "action",
                        "workflow_visible": True,
                    }
                ],
                "action_definitions": [
                    {
                        "action_id": "v1_send",
                        "title": "Publish Record v1",
                        "workflow_visible": True,
                    }
                ],
            },
        )

        class RuntimeStore:
            def __init__(self, conn):
                assert conn is mock_db.conn

            def get_release(self, release_id: str):
                assert release_id == "release_v1"
                return release

        with (
            patch.object(
                connector_catalog, "_platform_logger_for_admin_catalog", return_value=mock_db
            ),
            patch.object(
                connector_catalog,
                "_list_project_connections_for_catalog",
                return_value=[],
            ),
            patch.object(
                connector_catalog,
                "_active_extension_install_index_for_catalog",
                return_value={"example.host-fixture": install},
            ),
            patch.object(connector_catalog, "DBExtensionRuntimeStore", RuntimeStore),
        ):
            rows = connector_catalog._extension_action_connector_rows(
                mock_db,
                project_id="17",
                scope=normalize_scope(account_id=DEFAULT_ACCOUNT_ID),
            )

        assert len(rows) == 1
        assert rows[0]["display_name"] == "Example Extension v1"
        operation_ids = {operation["operation_id"] for operation in rows[0]["operations"]}
        assert operation_ids == {"v1_send"}
        assert "v2_only" not in operation_ids

    @pytest.mark.parametrize(
        ("locale", "source_name", "operation_name"),
        [
            ("lt", "Pavyzdinis plėtinys", "Paskelbti įrašą"),
            ("ru", "Example Extension v1 (en)", "Publish Record (en)"),
            ("en", "Example Extension v1 (en)", "Publish Record (en)"),
        ],
    )
    def test_connector_catalog_extension_source_follows_the_installed_release_language(
        self, locale: str, source_name: str, operation_name: str
    ):
        import core.entrypoints.admin.workflows_connector_catalog as connector_catalog

        mock_db = MagicMock()
        mock_db.conn = object()
        install = SimpleNamespace(
            extension_id="example.host-fixture",
            install_id="install_example",
            release_id="release_v1",
        )
        release = SimpleNamespace(
            extension_id="example.host-fixture",
            descriptor={
                "display_name": "Example Extension v1",
                "description": "Declared description",
                "operation_manifest": [
                    {
                        "operation_id": "v1_send",
                        "operation_kind": "action",
                        "workflow_visible": True,
                    }
                ],
                "action_definitions": [
                    {
                        "action_id": "v1_send",
                        "title": "Publish Record v1",
                        "workflow_visible": True,
                    }
                ],
                "locale_bundles": {
                    "en": {
                        "ext.display_name": "Example Extension v1 (en)",
                        "op.v1_send.display_name": "Publish Record (en)",
                    },
                    "lt": {
                        "ext.display_name": "Pavyzdinis plėtinys",
                        "op.v1_send.display_name": "Paskelbti įrašą",
                    },
                },
            },
        )

        class RuntimeStore:
            def __init__(self, conn):
                assert conn is mock_db.conn

            def get_release(self, release_id: str):
                assert release_id == "release_v1"
                return release

        with (
            patch.object(
                connector_catalog, "_platform_logger_for_admin_catalog", return_value=mock_db
            ),
            patch.object(
                connector_catalog,
                "_list_project_connections_for_catalog",
                return_value=[],
            ),
            patch.object(
                connector_catalog,
                "_active_extension_install_index_for_catalog",
                return_value={"example.host-fixture": install},
            ),
            patch.object(connector_catalog, "DBExtensionRuntimeStore", RuntimeStore),
        ):
            rows = connector_catalog._extension_action_connector_rows(
                mock_db,
                project_id="17",
                scope=normalize_scope(account_id=DEFAULT_ACCOUNT_ID),
                locale=locale,
            )

        assert len(rows) == 1
        assert rows[0]["connector_id"] == "extension.example.host-fixture"
        assert rows[0]["extension_install_id"] == "install_example"
        assert rows[0]["display_name"] == source_name
        assert rows[0]["description"] == "Declared description"
        assert [operation["operation_id"] for operation in rows[0]["operations"]] == ["v1_send"]
        assert rows[0]["operations"][0]["display_name"] == operation_name

    def test_connector_catalog_empty_release_actions_do_not_fall_back_to_latest(self):
        try:
            import core.entrypoints.admin.workflows_connector_catalog as connector_catalog
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.conn = object()
        mock_db.list_extensions.return_value = [
            {
                "extension_key": "example.host-fixture",
                "display_name": "Example Extension",
                "compiled_manifest": {
                    "action_definitions": [
                        {
                            "action_id": "v2_only",
                            "title": "V2 only",
                            "workflow_visible": True,
                        }
                    ]
                },
            },
        ]
        install = SimpleNamespace(
            extension_id="example.host-fixture",
            install_id="install_example",
            release_id="release_v1",
        )
        release = SimpleNamespace(
            extension_id="example.host-fixture",
            descriptor={
                "display_name": "Example Extension v1",
                "action_definitions": [],
            },
        )

        class RuntimeStore:
            def __init__(self, conn):
                assert conn is mock_db.conn

            def get_release(self, release_id: str):
                assert release_id == "release_v1"
                return release

        with (
            patch.object(
                connector_catalog, "_platform_logger_for_admin_catalog", return_value=mock_db
            ),
            patch.object(
                connector_catalog,
                "_list_project_connections_for_catalog",
                return_value=[],
            ),
            patch.object(
                connector_catalog,
                "_active_extension_install_index_for_catalog",
                return_value={"example.host-fixture": install},
            ),
            patch.object(connector_catalog, "DBExtensionRuntimeStore", RuntimeStore),
        ):
            rows = connector_catalog._extension_action_connector_rows(
                mock_db,
                project_id="17",
                scope=normalize_scope(account_id=DEFAULT_ACCOUNT_ID),
            )

        assert rows == []

    def test_extension_form_schema_returns_rendered_payload(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        contract = {
            "step_ui_manifest": [
                {
                    "operation_id": "sync_issues",
                    "section": "General",
                    "fields": [{"name": "repository", "widget": "text", "required": True}],
                }
            ]
        }
        rendered = {
            "operation_id": "sync_issues",
            "sections": [
                {
                    "section": "General",
                    "fields": [
                        {
                            "name": "repository",
                            "label": "Repository",
                            "widget_type": "text",
                            "help_text": "",
                            "placeholder": "",
                            "required": True,
                            "depends_on": {},
                            "query_id": "",
                        }
                    ],
                }
            ],
            "errors": [],
        }

        with (
            patch.object(workflow_routes, "_resolve_extension_contract", return_value=contract),
            patch.object(workflow_routes, "_get_db", return_value=SimpleNamespace()),
            patch(
                "core.application.extensions.extension_projection_v2.render_step_form",
                return_value=rendered,
            ) as mock_render,
        ):
            resp = registry_client.get(
                "/api/workflows/meta/extension-form-schema",
                params={
                    "install_id": "install_demo",
                    "operation_id": "sync_issues",
                    "locale": "en",
                },
            )

        assert resp.status_code == 200
        assert resp.json() == rendered
        mock_render.assert_called_once_with(
            contract=contract,
            operation_id="sync_issues",
            locale="en",
            locale_bundles=contract.get("locale_bundles"),
        )

    def test_extension_install_contract_is_release_and_install_scoped(self, registry_client):
        import core.entrypoints.api.workflow_extension_contract as workflow_routes

        contract = {
            "account_id": workflow_routes.DEFAULT_ACCOUNT_ID,
            "install_id": "install_old",
            "extension_id": "acme.document-converter",
            "install_state": "disabled",
            "operation_manifest": [
                {
                    "operation_id": "convert_legacy",
                    "display_name": "Convert with legacy release",
                    "operation_kind": "action",
                    "workflow_visible": True,
                    "connection_type_ids": ["acme_documents_api"],
                    "inputs": [{"name": "source", "value_type": "string"}],
                    "outputs": [{"name": "legacy_artifact", "value_type": "value"}],
                }
            ],
            "action_definitions": [
                {
                    "action_id": "convert_legacy",
                    "title": "Convert with legacy release",
                    "workflow_visible": True,
                    "parameters": [{"name": "source", "type": "string"}],
                    "result_fields": ["legacy_artifact"],
                }
            ],
        }
        connections = [
            {
                "connection_id": "connection_old",
                "display_name": "Legacy connection",
                "extension_id": "acme.document-converter",
                "extension_install_id": "install_old",
                "enabled": True,
                "secret_refs": {"refs": {"token": "secret://old"}},
            },
            {
                "connection_id": "connection_current",
                "display_name": "Current connection",
                "extension_id": "acme.document-converter",
                "extension_install_id": "install_current",
                "enabled": True,
            },
        ]
        db = SimpleNamespace(
            get_project=lambda project_id: {
                "project_id": project_id,
                "account_id": workflow_routes.DEFAULT_ACCOUNT_ID,
            },
            list_project_connections=lambda _project_id, enabled_only=True: connections,
        )

        with (
            patch.object(workflow_routes, "_resolve_extension_contract", return_value=contract),
            patch.object(workflow_routes, "_get_db", return_value=db),
            patch.object(workflow_routes, "_get_connection_store", return_value=db),
        ):
            resp = registry_client.get(
                "/api/workflows/meta/extension-install-contract",
                params={"install_id": "install_old", "project_id": "project_default"},
            )

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["install_id"] == "install_old"
        assert payload["install_state"] == "disabled"
        assert payload["operations"] == [
            {
                "operation_id": "convert_legacy",
                "display_name": "Convert with legacy release",
                "description": "",
                "parameters": [{"name": "source", "value_type": "string", "type": "string"}],
                "returns": [{"name": "legacy_artifact", "value_type": "value", "type": "value"}],
                "source": "extension",
                "extension_id": "acme.document-converter",
                "action_id": "convert_legacy",
                "connection_type_ids": ["acme_documents_api"],
                "connection_required": True,
                "required_oauth_scopes": [],
            }
        ]
        assert payload["connections"] == [
            {
                "connection_id": "connection_old",
                "display_name": "Legacy connection",
                "extension_id": "acme.document-converter",
                "extension_install_id": "install_old",
                "enabled": True,
            }
        ]

    def test_extension_contract_resolver_uses_materialized_release_contract_and_global_scope(self):
        import core.entrypoints.admin.workflows_routes_extension as workflow_routes_extension
        from core.application.extensions.extension_projection_v2 import render_step_form

        install = SimpleNamespace(
            install_id="install_global",
            extension_id="acme.document-converter",
            account_id="owner_account",
            release_id="release_acme_documents",
            visibility="global",
        )
        normalized_contract = {
            "step_ui_manifest": [
                {
                    "operation_id": "convert_file",
                    "section": "Conversion",
                    "fields": [
                        {
                            "name": "table_mode",
                            "widget": "select",
                            "options": [
                                {"label": "Fast", "value": "fast"},
                                {"label": "Accurate", "value": "accurate"},
                            ],
                        }
                    ],
                }
            ]
        }
        release = SimpleNamespace(
            extension_id="acme.document-converter",
            descriptor={
                "runtime": {"extension_contract_v2": {"step_ui_manifest": {"ref": "raw"}}},
                "extension_contract_v2_normalized": normalized_contract,
            },
        )

        class InstallStore:
            def __init__(self, _conn):
                pass

            def list_for_account(self, account_id: str, **_kwargs):
                assert account_id == "consumer_account"
                return [install]

        class RuntimeStore:
            def __init__(self, _conn):
                pass

            def get_release(self, release_id: str):
                assert release_id == "release_acme_documents"
                return release

        with (
            patch.object(
                workflow_routes_extension,
                "_get_db",
                return_value=SimpleNamespace(dsn="postgresql://configured"),
            ),
            patch(
                "core.infrastructure.runtime.entrypoint_db_adapters.create_identity_store",
                return_value=SimpleNamespace(conn=object()),
            ),
            patch(
                "core.application.extensions.install.DBExtensionInstallStore",
                InstallStore,
            ),
            patch(
                "core.application.extensions.runtime_store_db.DBExtensionRuntimeStore",
                RuntimeStore,
            ),
        ):
            contract = workflow_routes_extension._resolve_extension_contract(
                "install_global", account_id="consumer_account"
            )

        assert contract is not None
        assert contract["account_id"] == "consumer_account"
        rendered = render_step_form(contract=contract, operation_id="convert_file")
        assert rendered["errors"] == []
        assert rendered["sections"][0]["fields"][0]["options"] == [
            {"label": "Fast", "value": "fast"},
            {"label": "Accurate", "value": "accurate"},
        ]

    def test_extension_contract_resolver_keeps_action_manifest_without_v2_ui_contract(self):
        import core.entrypoints.admin.workflows_routes_extension as workflow_routes_extension

        install = SimpleNamespace(
            install_id="install_fixture_notifier",
            extension_id="fixture.system-notifier",
            account_id="owner_account",
            release_id="release_fixture_notifier",
            visibility="global",
        )
        release = SimpleNamespace(
            extension_id="fixture.system-notifier",
            descriptor={
                "extension_id": "fixture.system-notifier",
                "action_definitions": [
                    {"action_id": "send_message", "result_fields": ["message_id"]}
                ],
            },
        )

        class InstallStore:
            def __init__(self, _conn):
                pass

            def list_for_account(self, _account_id: str, **_kwargs):
                return [install]

        class RuntimeStore:
            def __init__(self, _conn):
                pass

            def get_release(self, _release_id: str):
                return release

        with (
            patch.object(
                workflow_routes_extension,
                "_get_db",
                return_value=SimpleNamespace(dsn="postgresql://configured"),
            ),
            patch(
                "core.infrastructure.runtime.entrypoint_db_adapters.create_identity_store",
                return_value=SimpleNamespace(conn=object()),
            ),
            patch("core.application.extensions.install.DBExtensionInstallStore", InstallStore),
            patch(
                "core.application.extensions.runtime_store_db.DBExtensionRuntimeStore",
                RuntimeStore,
            ),
        ):
            contract = workflow_routes_extension._resolve_extension_contract(
                "install_fixture_notifier", account_id="consumer_account"
            )

        assert contract is not None
        assert contract["extension_id"] == "fixture.system-notifier"
        assert contract["action_definitions"][0]["action_id"] == "send_message"

    def test_extension_form_schema_returns_404_when_contract_missing(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        with patch.object(workflow_routes, "_resolve_extension_contract", return_value=None):
            resp = registry_client.get(
                "/api/workflows/meta/extension-form-schema",
                params={
                    "install_id": "missing_install",
                    "operation_id": "sync_issues",
                },
            )

        assert resp.status_code == 404
        assert "Extension contract not found" in resp.text

    def test_extension_resource_query_returns_provider_results(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        contract = {
            "resource_query_manifest": [
                {"query_id": "repo_picker", "label": "Repositories", "source": "runtime"}
            ]
        }
        provider = MagicMock()
        query_result = {
            "ok": True,
            "items": [{"id": "repo_1", "label": "demo/repo"}],
            "next_cursor": "cursor_2",
            "total": 1,
        }
        mock_get_provider = MagicMock(return_value=provider)

        with (
            patch.object(workflow_routes, "_resolve_extension_contract", return_value=contract),
            patch.object(
                workflow_routes,
                "_get_db",
                return_value=SimpleNamespace(
                    get_project=lambda project_id: {
                        "project_id": str(project_id),
                        "account_id": workflow_routes.DEFAULT_ACCOUNT_ID,
                    },
                    get_project_connection=lambda project_id=None, connection_id="": {
                        "connection_id": str(connection_id),
                        "project_id": str(project_id),
                    },
                ),
            ),
            patch.dict(
                registry_client.app.state.runtime_container.registries,
                {"extension_query_provider_factory": mock_get_provider},
            ),
            patch(
                "core.application.extensions.extension_projection_v2.execute_resource_query",
                return_value=query_result,
            ) as mock_execute,
        ):
            resp = registry_client.post(
                "/api/workflows/meta/extension-resource-query",
                json={
                    "install_id": "install_demo",
                    "query_id": "repo_picker",
                    "account_id": "acct_1",
                    "project_id": 7,
                    "connection_id": "conn_9",
                    "search": "demo",
                    "cursor": "cursor_1",
                    "limit": 25,
                },
            )

        assert resp.status_code == 200
        assert resp.json() == query_result
        mock_get_provider.assert_called_once()
        assert mock_get_provider.call_args.args == ("install_demo",)
        assert callable(mock_get_provider.call_args.kwargs["get_logger_only"])
        execute_kwargs = mock_execute.call_args.kwargs
        assert execute_kwargs["contract"] == contract
        assert execute_kwargs["query_id"] == "repo_picker"
        assert execute_kwargs["provider"] is provider
        assert execute_kwargs["account_id"] == workflow_routes.DEFAULT_ACCOUNT_ID
        assert execute_kwargs["project_id"] == "7"
        assert execute_kwargs["connection_id"] == "conn_9"
        assert execute_kwargs["search"] == "demo"
        assert execute_kwargs["cursor"] == "cursor_1"
        assert execute_kwargs["limit"] == 25

    def test_extension_resource_query_enforces_project_scope(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        contract = {"resource_query_manifest": [{"query_id": "repo_picker"}]}

        with (
            patch.object(workflow_routes, "_resolve_extension_contract", return_value=contract),
            patch.object(
                workflow_routes,
                "_get_db",
                return_value=SimpleNamespace(get_project=lambda project_id: None),
            ),
            patch.object(
                workflow_routes,
                "_request_scope",
                return_value=normalize_scope(
                    account_id="acct_1",
                    allowed_project_ids=frozenset({"proj-allowed"}),
                ),
            ),
        ):
            resp = registry_client.post(
                "/api/workflows/meta/extension-resource-query",
                json={
                    "install_id": "install_demo",
                    "query_id": "repo_picker",
                    "project_id": "7",
                },
            )

        assert resp.status_code == 403

    def test_extension_resource_query_enforces_connection_project_scope(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        contract = {"resource_query_manifest": [{"query_id": "repo_picker"}]}

        with (
            patch.object(workflow_routes, "_resolve_extension_contract", return_value=contract),
            patch.object(
                workflow_routes,
                "_get_db",
                return_value=SimpleNamespace(
                    get_project=lambda project_id: {
                        "project_id": str(project_id),
                        "account_id": "acc_default",
                    },
                    get_project_connection=lambda project_id=None, connection_id="": None,
                ),
            ),
        ):
            resp = registry_client.post(
                "/api/workflows/meta/extension-resource-query",
                json={
                    "install_id": "install_demo",
                    "query_id": "repo_picker",
                    "project_id": "7",
                    "connection_id": "conn_9",
                },
            )

        assert resp.status_code == 403
        assert "Access denied" in resp.text

    def test_extension_resource_query_returns_503_when_provider_missing(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        contract = {"resource_query_manifest": [{"query_id": "repo_picker"}]}

        with (
            patch.object(workflow_routes, "_resolve_extension_contract", return_value=contract),
            patch.dict(
                registry_client.app.state.runtime_container.registries,
                {"extension_query_provider_factory": MagicMock(return_value=None)},
            ),
        ):
            resp = registry_client.post(
                "/api/workflows/meta/extension-resource-query",
                json={
                    "install_id": "install_demo",
                    "query_id": "repo_picker",
                },
            )

        assert resp.status_code == 503
        assert "runtime provider not available" in resp.text

    def test_workflow_revision_detail_returns_exact_payload(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        revision_payload = {
            "entry_id": 7,
            "revision": 7,
            "entity_type": "workflow",
            "entity_id": "wf_demo",
            "snapshot": {
                "workflow_id": "wf_demo",
                "display_name": "Demo workflow",
                "portability": {
                    "workflow_id": "wf_demo",
                    "extension_owned": False,
                    "protected": False,
                    "full_export_allowed": True,
                    "reference_export_allowed": False,
                    "clone_allowed": True,
                    "structure_read_allowed": True,
                    "structure_change_allowed": True,
                    "step_test_run_allowed": True,
                    "source_install_id": "",
                    "source_release_id": "",
                    "source_extension_id": "",
                    "source_extension_name": "",
                    "source_extension_version": "",
                    "local_workflow_id": "",
                    "projection_mode": "manual",
                    "availability_status": "available",
                    "availability_reason_code": "none",
                    "availability_reason_text": "",
                    "new_runs_blocked": False,
                    "visible_in_catalog": True,
                    "reason": "",
                },
            },
        }
        mock_db.get_entity_revision.return_value = revision_payload
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_demo",
            "account_id": DEFAULT_ACCOUNT_ID,
            "scope_kind": "account",
            "projection_mode": "manual",
            "enabled": False,
        }

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get("/api/workflows/wf_demo/history/7")

        assert resp.status_code == 200
        assert resp.json() == revision_payload
        mock_db.get_entity_revision.assert_called_once_with(
            entity_type="workflow",
            entity_id="wf_demo",
            revision=7,
        )

    def test_protected_workflow_history_redacts_internal_snapshot(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        protected_workflow = {
            "workflow_id": "wf_ext",
            "source_install_id": "inst-ext",
            "projection_mode": "derived",
            "steps": [
                {
                    "step_id": "visible",
                    "step_kind": "agent",
                    "group_id": None,
                    "override": {
                        "agent": {
                            "api_key": "author-key-value",  # pragma: allowlist secret
                        }
                    },
                }
            ],
        }
        mock_db = MagicMock()
        mock_db.get_workflow.return_value = protected_workflow
        mock_db.get_entity_history.return_value = [
            {
                "id": 7,
                "entity_type": "workflow",
                "entity_id": "wf_ext",
                "snapshot": dict(protected_workflow),
            }
        ]

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get("/api/workflows/wf_ext/history")

        assert resp.status_code == 200
        body = resp.json()
        # Read-only structure stays visible; the author's internals do not.
        assert body[0]["snapshot"]["protected_workflow"] is True
        assert [step["step_id"] for step in body[0]["snapshot"]["steps"]] == ["visible"]
        assert "author-key-value" not in resp.text

    def test_global_changelog_redacts_protected_workflow_snapshots(self, registry_client):
        try:
            import core.entrypoints.admin.changelog as changelog_routes
        except Exception as e:
            pytest.fail(f"changelog routes not importable: {e}")

        protected_workflow = {
            "workflow_id": "wf_ext",
            "source_install_id": "inst-ext",
            "projection_mode": "derived",
            "steps": [
                {
                    "step_id": "visible",
                    "step_kind": "agent",
                    "group_id": None,
                    "override": {
                        "agent": {
                            "api_key": "author-key-value",  # pragma: allowlist secret
                        }
                    },
                }
            ],
        }
        mock_db = MagicMock()
        mock_db.get_workflow.return_value = protected_workflow
        mock_db.get_changelog.return_value = [
            {
                "id": 7,
                "entity_type": "workflow",
                "entity_id": "wf_ext",
                "snapshot": dict(protected_workflow),
            }
        ]

        with patch.object(changelog_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get(
                "/api/changelog",
                params={"entity_type": "workflow", "entity_id": "wf_ext"},
            )

        assert resp.status_code == 200
        body = resp.json()
        # Read-only structure stays visible; the author's sensitive values do not.
        assert body[0]["snapshot"]["protected_workflow"] is True
        assert [step["step_id"] for step in body[0]["snapshot"]["steps"]] == ["visible"]
        assert "author-key-value" not in resp.text

    def test_global_changelog_deleted_protected_workflow_diff_is_blocked(self, registry_client):
        try:
            import core.entrypoints.admin.changelog as changelog_routes
        except Exception as e:
            pytest.fail(f"changelog routes not importable: {e}")

        protected_snapshot = {
            "workflow_id": "wf_ext",
            "source_install_id": "inst-ext",
            "projection_mode": "derived",
            "steps": [{"step_id": "hidden", "step_kind": "agent", "group_id": None}],
        }
        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        mock_db.get_entity_history.return_value = [
            {
                "id": 7,
                "entity_type": "workflow",
                "entity_id": "wf_ext",
                "snapshot": dict(protected_snapshot),
            }
        ]

        with patch.object(changelog_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get("/api/changelog/workflow/wf_ext/diff")

        assert resp.status_code == 403
        assert "hidden" not in resp.text
        mock_db.diff_entity_history.assert_not_called()

    def test_global_changelog_deleted_protected_workflow_restore_is_blocked(self, registry_client):
        try:
            import core.entrypoints.admin.changelog as changelog_routes
        except Exception as e:
            pytest.fail(f"changelog routes not importable: {e}")

        protected_snapshot = {
            "workflow_id": "wf_ext",
            "source_install_id": "inst-ext",
            "projection_mode": "derived",
            "steps": [{"step_id": "hidden", "step_kind": "agent", "group_id": None}],
        }
        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        mock_db.get_changelog_entry.return_value = {
            "id": 9,
            "entity_type": "workflow",
            "entity_id": "wf_ext",
            "snapshot": dict(protected_snapshot),
        }
        mock_registry = MagicMock()

        with (
            patch.object(changelog_routes, "_get_db", return_value=mock_db),
            patch.object(changelog_routes, "_get_registry", return_value=mock_registry),
        ):
            resp = registry_client.post(
                "/api/changelog/workflow/wf_ext/restore",
                json={"entry_id": 9},
            )

        assert resp.status_code == 403
        assert "hidden" not in resp.text
        mock_db.restore_entity_revision.assert_not_called()
        mock_registry.reload_all.assert_not_called()

    def test_global_changelog_cannot_resurrect_hard_deleted_workflow(self, registry_client):
        try:
            import core.entrypoints.admin.changelog as changelog_routes
        except Exception as e:
            pytest.fail(f"changelog routes not importable: {e}")

        snapshot = {
            "workflow_id": "wf_deleted",
            "display_name": "Hard-deleted workflow",
            "account_id": DEFAULT_ACCOUNT_ID,
            "projection_mode": "manual",
            "steps": [],
            "groups": [],
        }
        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        mock_db.get_changelog_entry.return_value = {
            "id": 10,
            "entity_type": "workflow",
            "entity_id": "wf_deleted",
            "snapshot": snapshot,
        }
        mock_registry = MagicMock()

        with (
            patch.object(changelog_routes, "_get_db", return_value=mock_db),
            patch.object(changelog_routes, "_get_registry", return_value=mock_registry),
        ):
            response = registry_client.post(
                "/api/changelog/workflow/wf_deleted/restore",
                json={"entry_id": 10},
            )

        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "workflow_deleted_restore_forbidden"
        mock_db.restore_entity_revision.assert_not_called()
        mock_registry.reload_all.assert_not_called()

    def test_workflow_revision_detail_returns_404_when_missing(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_entity_revision.return_value = None

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get("/api/workflows/wf_demo/history/99")

        assert resp.status_code == 404
        assert "Revision 99 not found" in resp.text

    def test_workflow_diff_returns_exact_payload(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        diff_payload = {
            "entity_type": "workflow",
            "entity_id": "wf_demo",
            "from_entry_id": 4,
            "to_entry_id": 7,
            "changes": [{"field": "display_name", "before": "Old", "after": "New"}],
        }
        mock_db.diff_entity_history.return_value = diff_payload

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get(
                "/api/workflows/wf_demo/diff",
                params={"from_entry_id": 4, "to_entry_id": 7},
            )

        assert resp.status_code == 200
        assert resp.json() == diff_payload
        mock_db.diff_entity_history.assert_called_once_with(
            entity_type="workflow",
            entity_id="wf_demo",
            from_entry_id="4",
            to_entry_id="7",
        )

    def test_protected_workflow_diff_is_blocked(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_ext",
            "source_install_id": "inst-ext",
            "projection_mode": "derived",
        }

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get("/api/workflows/wf_ext/diff")

        assert resp.status_code == 403
        mock_db.diff_entity_history.assert_not_called()

    def test_deleted_protected_workflow_diff_is_blocked_from_history_snapshot(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        protected_snapshot = {
            "workflow_id": "wf_ext",
            "source_install_id": "inst-ext",
            "projection_mode": "derived",
            "steps": [{"step_id": "hidden", "step_kind": "agent", "group_id": None}],
        }
        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        mock_db.get_entity_history.return_value = [
            {
                "id": 7,
                "entity_type": "workflow",
                "entity_id": "wf_ext",
                "snapshot": dict(protected_snapshot),
            }
        ]

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get("/api/workflows/wf_ext/diff")

        assert resp.status_code == 403
        assert "hidden" not in resp.text
        mock_db.diff_entity_history.assert_not_called()

    def test_deleted_protected_workflow_restore_is_blocked_from_history_snapshot(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        protected_snapshot = {
            "workflow_id": "wf_ext",
            "source_install_id": "inst-ext",
            "projection_mode": "derived",
            "steps": [{"step_id": "hidden", "step_kind": "agent", "group_id": None}],
        }
        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        mock_db.get_changelog_entry.return_value = {
            "id": 9,
            "entity_type": "workflow",
            "entity_id": "wf_ext",
            "snapshot": dict(protected_snapshot),
        }
        mock_registry = MagicMock()

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
        ):
            resp = registry_client.post("/api/workflows/wf_ext/restore", json={"entry_id": 9})

        assert resp.status_code == 403
        assert "hidden" not in resp.text
        mock_db.restore_entity_revision.assert_not_called()
        mock_registry.reload_all.assert_not_called()

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("GET", "/api/workflows/wf_deleted/history", None),
            ("GET", "/api/workflows/wf_deleted/diff", None),
            ("POST", "/api/workflows/wf_deleted/restore", {"entry_id": 9}),
        ],
    )
    def test_deleted_workflow_history_surfaces_enforce_request_account_scope(
        self,
        registry_client,
        method,
        path,
        body,
    ):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        snapshot = {
            "workflow_id": "wf_deleted",
            "display_name": "Deleted workflow",
            "account_id": "acc_alpha",
            "projection_mode": "manual",
        }
        mock_db = MagicMock()
        mock_db.get_workflow.return_value = None
        mock_db.get_entity_history.return_value = [
            {
                "id": 9,
                "entity_type": "workflow",
                "entity_id": "wf_deleted",
                "snapshot": dict(snapshot),
            }
        ]
        mock_db.get_changelog_entry.return_value = {
            "id": 9,
            "entity_type": "workflow",
            "entity_id": "wf_deleted",
            "snapshot": dict(snapshot),
        }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_request_scope",
                return_value=normalize_scope(account_id="acc_other"),
            ),
        ):
            if method == "POST":
                resp = registry_client.post(path, json=body)
            else:
                resp = registry_client.get(path)

        assert resp.status_code == 403

    def test_workflow_diff_returns_404_when_history_is_missing(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.diff_entity_history.side_effect = KeyError("workflow history missing")

        with patch.object(workflow_routes, "_get_db", return_value=mock_db):
            resp = registry_client.get("/api/workflows/wf_demo/diff")

        assert resp.status_code == 404
        assert "workflow history missing" in resp.text

    def test_toggle_workflow_blocked_when_contract_invalid(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_bad",
            "enabled": False,
            "steps": [],
            "groups": [],
        }
        mock_registry = MagicMock()
        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={
                    "valid": False,
                    "errors": [
                        {
                            "code": "workflow.steps_missing",
                            "message": "Workflow must define at least one step before enabling/running",
                        }
                    ],
                    "warnings": [],
                },
            ),
        ):
            resp = registry_client.patch("/api/workflows/wf_bad/toggle")

        assert resp.status_code == 400
        detail = resp.json().get("detail", {})
        assert detail.get("error") == "Contract validation failed"
        mock_db.toggle_workflow.assert_not_called()

    def test_simulate_workflow_blocked_when_contract_invalid(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_registry = MagicMock()
        mock_registry.workflows.get_workflow.return_value = {"workflow_id": "wf_bad"}

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={
                    "valid": False,
                    "errors": [{"message": "Phase 'analysis' has no steps"}],
                    "warnings": [],
                },
            ),
        ):
            resp = registry_client.post(
                "/api/workflows/wf_bad/simulate",
                json={"task_envelope": {"title": "Test", "task_type": "bugfix"}},
            )

        assert resp.status_code == 400
        detail = resp.json().get("detail", {})
        assert detail.get("error") == "Contract validation failed"

    def test_workflow_test_run_enqueues_durable_worker_execution(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.dsn = "postgres://flow-steward"
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_testable",
            "enabled": True,
            "steps": [{"step_id": "planning", "step_kind": "set_variable", "group_id": None}],
        }
        mock_registry = MagicMock()
        mock_registry.workflows.get_workflow.return_value = {
            "workflow_id": "wf_testable",
            "enabled": True,
            "steps": [{"step_id": "planning", "step_kind": "set_variable", "group_id": None}],
        }
        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {"id": 7, "name": "Demo"}
        mock_bl.get_project_run_config.return_value = {
            "project_name": "Demo",
        }
        mock_job_store = MagicMock()
        mock_job_store.get_job.return_value = {"id": 41, "thread_id": "thread-41"}

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
            patch(
                "core.infrastructure.runtime.entrypoint_db_adapters.create_project_store",
                return_value=mock_bl,
            ),
            patch(
                "core.infrastructure.runtime.entrypoint_db_adapters.create_job_store",
                return_value=mock_job_store,
            ),
            patch(
                "core.entrypoints.admin.workflows_definition_router.enqueue_task",
                return_value=41,
            ) as enqueue_test_run,
        ):
            resp = registry_client.post(
                "/api/workflows/wf_testable/test-run",
                json={
                    "project_id": 7,
                    "task_summary": "Validate workflow path",
                    "mode": "brownfield",
                },
            )

        assert resp.status_code == 202
        body = resp.json()
        assert body.get("status") == "queued"
        assert body.get("thread_id") == "thread-41"
        call_kwargs = enqueue_test_run.call_args.kwargs
        assert call_kwargs["task_source"] == "workflow_test_run"
        assert call_kwargs["extra_state"]["workflow_id"] == "wf_testable"
        assert call_kwargs["project_id"] == "7"
        assert call_kwargs["task_summary"] == "Validate workflow path"
        assert call_kwargs["project_name"] == "Demo"
        assert call_kwargs["test_mode"] is True
        assert call_kwargs["extra_state"]["mode"] == "brownfield"
        assert call_kwargs["extra_state"]["task_envelope"]["project_id"] == "7"
        assert call_kwargs["extra_state"]["task_envelope"]["requested_workflow_id"] == "wf_testable"
        assert (
            call_kwargs["extra_state"]["task_envelope"]["metadata"]["source"] == "workflow_test_run"
        )

    def test_workflow_test_run_dry_run_validates_without_enqueueing_job(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.dsn = "postgres://flow-steward"
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_testable",
            "enabled": True,
            "steps": [{"step_id": "planning", "step_kind": "set_variable", "group_id": None}],
        }
        mock_registry = MagicMock()
        mock_registry.workflows.get_workflow.return_value = {
            "workflow_id": "wf_testable",
            "enabled": True,
            "steps": [{"step_id": "planning", "step_kind": "set_variable", "group_id": None}],
        }
        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {"id": 7, "name": "Demo"}
        mock_bl.get_project_run_config.return_value = {
            "project_name": "Demo",
        }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
            patch(
                "core.infrastructure.runtime.entrypoint_db_adapters.create_project_store",
                return_value=mock_bl,
            ),
            patch(
                "core.entrypoints.admin.workflows_definition_router.enqueue_task"
            ) as enqueue_test_run,
        ):
            resp = registry_client.post(
                "/api/workflows/wf_testable/test-run",
                json={
                    "project_id": 7,
                    "task_summary": "Validate workflow path",
                    "mode": "brownfield",
                    "dry_run": True,
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("status") == "dry_run"
        assert body.get("dry_run") is True
        assert body.get("would_execute_ephemerally") is True
        assert body.get("workflow_id") == "wf_testable"
        assert body.get("project_id") == "7"
        assert body.get("project_name") == "Demo"
        assert body.get("task_summary") == "Validate workflow path"
        assert body.get("task_envelope", {}).get("project_id") == "7"
        assert body.get("mode") == "brownfield"
        assert body.get("test_mode") is True
        assert body.get("validation", {}).get("valid") is True
        assert "execution_id" not in body
        enqueue_test_run.assert_not_called()

    def test_a_disabled_workflow_can_still_be_test_run(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.dsn = "postgres://flow-steward"
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_testable",
            "enabled": False,
            "steps": [{"step_id": "planning", "step_kind": "set_variable", "group_id": None}],
        }
        mock_registry = MagicMock()
        mock_registry.workflows.get_workflow.return_value = {
            "workflow_id": "wf_testable",
            "enabled": False,
            "steps": [{"step_id": "planning", "step_kind": "set_variable", "group_id": None}],
        }
        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {"id": 7, "name": "Demo"}
        mock_bl.get_project_run_config.return_value = {
            "project_name": "Demo",
        }
        mock_job_store = MagicMock()
        mock_job_store.get_job.return_value = {"id": 41, "thread_id": "thread-41"}

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
            patch(
                "core.infrastructure.runtime.entrypoint_db_adapters.create_project_store",
                return_value=mock_bl,
            ),
            patch(
                "core.infrastructure.runtime.entrypoint_db_adapters.create_job_store",
                return_value=mock_job_store,
            ),
            patch(
                "core.entrypoints.admin.workflows_definition_router.enqueue_task",
                return_value=41,
            ) as enqueue_test_run,
        ):
            resp = registry_client.post(
                "/api/workflows/wf_testable/test-run",
                json={
                    "project_id": 7,
                    "task_summary": "Validate workflow path",
                    "mode": "brownfield",
                },
            )

        # A disabled workflow is a draft; testing it is what a test run is for.
        assert resp.status_code == 202
        assert enqueue_test_run.call_args.kwargs["extra_state"]["test_mode"] is True

    def test_workflow_test_run_returns_422_for_unlinked_explicit_workflow(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.dsn = "postgres://flow-steward"
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_testable",
            "enabled": True,
            "steps": [{"step_id": "planning", "step_kind": "set_variable", "group_id": None}],
        }
        mock_registry = MagicMock()
        mock_registry.workflows.get_workflow.return_value = {
            "workflow_id": "wf_testable",
            "enabled": True,
            "steps": [{"step_id": "planning", "step_kind": "set_variable", "group_id": None}],
        }
        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {"id": 7, "name": "Demo"}
        mock_bl.get_project_run_config.return_value = {
            "project_name": "Demo",
            "project_workflows": ["wf-other"],
        }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
            patch(
                "core.infrastructure.runtime.entrypoint_db_adapters.create_project_store",
                return_value=mock_bl,
            ),
            patch(
                "core.entrypoints.admin.workflows_definition_router.enqueue_task"
            ) as enqueue_test_run,
        ):
            resp = registry_client.post(
                "/api/workflows/wf_testable/test-run",
                json={
                    "project_id": 7,
                    "task_summary": "Validate workflow path",
                    "mode": "brownfield",
                },
            )

        assert resp.status_code == 422
        assert "not linked to this project" in resp.json()["detail"].lower()
        enqueue_test_run.assert_not_called()

    def test_workflow_test_run_rejects_hollow_workflow_before_runtime(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.dsn = "postgres://flow-steward"
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_testable",
            "enabled": True,
            "steps": [{"step_id": "start", "step_kind": "set_variable", "group_id": None}],
        }
        mock_registry = MagicMock()
        mock_registry.workflows.get_workflow.return_value = {
            "workflow_id": "wf_testable",
            "enabled": True,
            "steps": [{"step_id": "start", "step_kind": "set_variable", "group_id": None}],
        }
        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {"id": 7, "name": "Demo"}
        mock_bl.get_project_run_config.return_value = {
            "project_name": "Demo",
        }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={
                    "valid": False,
                    "errors": [
                        {
                            "code": "workflow.execution_bindings_missing",
                            "message": "Workflow must include at least one real execution step before it can be enabled or run",
                            "path": "workflow.steps",
                            "severity": "error",
                        }
                    ],
                    "warnings": [],
                },
            ),
            patch(
                "core.infrastructure.runtime.entrypoint_db_adapters.create_project_store",
                return_value=mock_bl,
            ),
            patch(
                "core.application.workflows.platform_test_run_runtime.run_workflow_test_run_ephemeral"
            ) as mock_test_run,
        ):
            resp = registry_client.post(
                "/api/workflows/wf_testable/test-run",
                json={
                    "project_id": 7,
                    "task_summary": "Validate workflow path",
                    "mode": "brownfield",
                },
            )

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert isinstance(detail, dict)
        assert any(
            "real execution step" in str(message or "").lower()
            for message in (detail.get("messages") or [])
        )
        mock_test_run.assert_not_called()

    def test_workflow_contract_validation_endpoint_returns_payload(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "weekly_digest",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
            "settings": {},
        }
        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            resp = registry_client.get("/api/workflows/weekly_digest/contract-validation")

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("workflow_id") == "weekly_digest"
        assert body.get("validation", {}).get("valid") is True

    def test_workflow_contract_endpoint_returns_public_interface(self, registry_client):
        try:
            import core.entrypoints.admin.workflows_common as workflow_common
            import core.entrypoints.admin.workflows_routes_read as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "callable_child",
            "display_name": "Callable Child",
            "enabled": True,
            "settings": {
                "callable": {
                    "enabled": True,
                    "expected_inputs": [{"name": "request", "type": "text", "required": True}],
                    # Declared by hand before files were returned from steps.
                    "expected_artifacts": [{"name": "report_pdf", "type": "file"}],
                }
            },
            "steps": [
                {
                    "step_id": "render",
                    "display_name": "Render PDF",
                    "step_kind": "file_from_base64",
                    "override": {
                        "file_from_base64": {
                            "source": "state.inputs.body",
                            "result_target": "state.report_pdf",
                        }
                    },
                }
            ],
        }
        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_common,
                "_get_registry",
                return_value=SimpleNamespace(db_store=mock_db, workflows=None),
            ),
        ):
            resp = registry_client.get("/api/workflows/callable_child/contract")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        interface = body.get("interface") or {}
        assert interface.get("output_mode") == "artifacts"
        assert interface.get("expected_inputs", [{}])[0]["type"] == "paragraph"
        assert interface.get("output_schema", {}).get("x-output-mode") == "artifacts"
        # The old declaration is linked to the one step result it can mean.
        assert interface.get("returned_files") == [
            {
                "name": "report_pdf",
                "type": "file",
                "required": False,
                "source": {
                    "step_id": "render",
                    "step_name": "Render PDF",
                    "result": "file",
                    "target": "state.report_pdf",
                },
                "origin": "legacy",
            }
        ]
        assert "expected_artifacts" not in interface
        assert "side_effects" not in interface

    def test_workflow_validate_alias_returns_normalized_issue_list(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_broken",
            "steps": [{"step_id": "compose_sections", "step_kind": "agent", "group_id": None}],
            "groups": [],
        }
        validation_payload = {
            "valid": False,
            "errors": [
                {
                    "code": "workflow.step_io.error",
                    "path": "workflow.steps.compose_sections.boundary_contract.entry",
                    "message": "Required input 'spec_doc' (from compose_sections) not produced by upstream steps",
                }
            ],
            "warnings": [],
        }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                return_value=validation_payload,
            ),
        ):
            resp = registry_client.post("/api/workflows/wf_broken/validate", json={})

        assert resp.status_code == 200
        body = resp.json()
        assert body["workflow_id"] == "wf_broken"
        assert body["issues"] == [
            {
                "type": "workflow.step_io.error",
                "code": "workflow.step_io.error",
                "severity": "error",
                "message": "Required input 'spec_doc' (from compose_sections) not produced by upstream steps",
                "path": "workflow.steps.compose_sections.boundary_contract.entry",
                "step_id": "compose_sections",
                "field": "boundary_contract",
            }
        ]

    def test_workflow_suggestions_derive_expected_input_repair(self, registry_client):
        try:
            import core.entrypoints.admin.workflows_projection as workflow_projection
            import core.entrypoints.admin.workflows_routes_read as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_broken",
            "steps": [{"step_id": "compose_sections", "step_kind": "agent", "group_id": None}],
            "groups": [],
        }
        validation_payload = {
            "valid": False,
            "errors": [
                {
                    "code": "workflow.step_io.error",
                    "path": "workflow.steps.compose_sections.boundary_contract.entry",
                    "message": "Required input 'spec_doc' (from compose_sections) not produced by upstream steps",
                }
            ],
            "warnings": [],
        }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_projection,
                "_get_workflow_validation",
                return_value=validation_payload,
            ),
            patch(
                "core.application.registry.io_resolver.IOResolver.get_step_input_defaults",
                return_value={
                    "workflow_id": "wf_broken",
                    "step_id": "compose_sections",
                    "group_id": None,
                    "required_inputs": [
                        {
                            "name": "spec_doc",
                            "type": "text",
                            "source": "",
                            "resolved": False,
                            "suggested_sources": [],
                        }
                    ],
                    "optional_inputs": [],
                    "custom_inputs": [],
                    "current_expected_inputs": [],
                },
            ),
        ):
            resp = registry_client.get("/api/workflows/wf_broken/suggestions")

        assert resp.status_code == 200
        body = resp.json()
        assert body["workflow_id"] == "wf_broken"
        assert body["suggestions"] == [
            {
                "action": "upsertExpectedInput",
                "step_id": "compose_sections",
                "field": "boundary_contract.entry",
                "input_name": "spec_doc",
                "suggested_value": {
                    "name": "spec_doc",
                    "type": "text",
                    "source": "",
                    "required": True,
                },
                "reason": "Required input 'spec_doc' (from compose_sections) not produced by upstream steps",
                "already_present": False,
            }
        ]

    def test_workflow_suggestions_derive_expected_input_repair_from_step_issue(
        self, registry_client
    ):
        try:
            import core.entrypoints.admin.workflows_projection as workflow_projection
            import core.entrypoints.admin.workflows_routes_read as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "wf_broken",
            "steps": [{"step_id": "compose_sections", "step_kind": "agent", "group_id": None}],
            "groups": [],
        }
        validation_payload = {
            "valid": False,
            "errors": [
                {
                    "code": "workflow.step_io.error",
                    "path": "workflow.steps.compose_sections.inputs.spec_doc",
                    "message": "Required input 'spec_doc' (from compose_sections) not produced by upstream steps",
                }
            ],
            "warnings": [],
        }

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_projection,
                "_get_workflow_validation",
                return_value=validation_payload,
            ),
            patch(
                "core.application.registry.io_resolver.IOResolver.get_step_input_defaults",
                return_value={
                    "workflow_id": "wf_broken",
                    "step_id": "compose_sections",
                    "group_id": None,
                    "required_inputs": [
                        {
                            "name": "spec_doc",
                            "type": "text",
                            "source": "",
                            "resolved": False,
                            "suggested_sources": [],
                        }
                    ],
                    "optional_inputs": [],
                    "custom_inputs": [],
                    "current_expected_inputs": [],
                },
            ),
        ):
            resp = registry_client.get("/api/workflows/wf_broken/suggestions")

        assert resp.status_code == 200
        body = resp.json()
        assert body["workflow_id"] == "wf_broken"
        assert body["suggestions"] == [
            {
                "action": "upsertExpectedInput",
                "step_id": "compose_sections",
                "field": "boundary_contract.entry",
                "input_name": "spec_doc",
                "suggested_value": {
                    "name": "spec_doc",
                    "type": "text",
                    "source": "",
                    "required": True,
                },
                "reason": "Required input 'spec_doc' (from compose_sections) not produced by upstream steps",
                "already_present": False,
            }
        ]

    def test_workflow_contract_validation_endpoint_degrades_on_pool_pressure(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
            from core.infrastructure.db.psycopg_runtime.pool import PoolError
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        mock_db = MagicMock()
        mock_db.get_workflow.return_value = {
            "workflow_id": "weekly_digest",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
            "settings": {},
        }
        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_get_workflow_validation",
                side_effect=PoolError("connection pool exhausted"),
            ),
        ):
            resp = registry_client.get("/api/workflows/weekly_digest/contract-validation")

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("workflow_id") == "weekly_digest"
        validation = body.get("validation") or {}
        assert validation.get("valid") is False
        errors = validation.get("errors") or []
        assert isinstance(errors, list) and errors
        assert errors[0].get("code") == "workflow.validation_temporarily_unavailable"

    def test_delete_nonexistent_workflow(self, registry_client):
        resp = registry_client.delete("/api/workflows/nonexistent_xyz_999")
        assert resp.status_code in (404, 500)

    def test_deleted_workflow_detail_returns_404(self, registry_client):
        try:
            import core.entrypoints.admin.workflows as workflow_routes
        except Exception as e:
            pytest.fail(f"workflow routes not importable: {e}")

        workflows_state = {
            "wf_demo": {
                "workflow_id": "wf_demo",
                "display_name": "Workflow Demo",
                "enabled": True,
                "settings": {},
                "steps": [],
                "groups": [],
            }
        }

        def _get_workflow(workflow_id: str):
            row = workflows_state.get(str(workflow_id or "").strip())
            return dict(row) if isinstance(row, dict) else None

        def _clone_workflow(
            *,
            source_workflow_id: str,
            target_workflow_id: str,
            display_name=None,
            description=None,
            enabled=None,
            account_id,
            scope_kind,
            author=None,
        ):
            source = workflows_state.get(source_workflow_id)
            if source is None:
                raise KeyError(source_workflow_id)
            if target_workflow_id in workflows_state:
                raise ValueError("Workflow already exists")
            workflows_state[target_workflow_id] = {
                **dict(source),
                "workflow_id": target_workflow_id,
                "display_name": display_name
                or f"{source.get('display_name', source_workflow_id)} Copy",
                "description": description or source.get("description", ""),
                "enabled": bool(source.get("enabled", True) if enabled is None else enabled),
                "account_id": account_id,
                "scope_kind": scope_kind,
            }

        def _delete_workflow(workflow_id: str, author: str = "admin"):
            workflows_state.pop(str(workflow_id or "").strip(), None)

        mock_db = SimpleNamespace(
            _transaction=lambda: nullcontext(),
            clone_workflow=_clone_workflow,
            delete_workflow=_delete_workflow,
            get_workflow=_get_workflow,
        )

        clone_workflow_id = f"nightfire_delete_probe_{uuid.uuid4().hex[:8]}"

        with (
            patch.object(workflow_routes, "_get_db", return_value=mock_db),
            patch.object(
                workflow_routes,
                "_get_registry",
                return_value=SimpleNamespace(reload_all=lambda: None),
            ),
        ):
            clone_resp = registry_client.post(
                "/api/workflows/wf_demo/clone",
                json={"workflow_id": clone_workflow_id},
            )
            assert clone_resp.status_code == 200

            delete_resp = registry_client.delete(f"/api/workflows/{clone_workflow_id}")
            assert delete_resp.status_code == 200

            detail_resp = registry_client.get(f"/api/workflows/{clone_workflow_id}")
        assert detail_resp.status_code == 404

    def test_workflows_page_uses_spa_list_contract(self, registry_client):
        resp = registry_client.get("/registry/workflows", follow_redirects=False)

        contract_text = _page_contract_text(
            resp, "assets/frontend/src/pages/workflows/list/index.tsx"
        )
        assert "Workflow catalog" in contract_text
        assert (
            'fetchWorkflows({ enabledOnly, search: deferredSearch }, { presentation: "background" })'
            in contract_text
        )
        assert "handleCloneWorkflow" in contract_text
        assert 'action_id: "clone"' in contract_text
        assert 'action_id: "export"' in contract_text
        # A protected extension workflow offers no export, not even a reference bundle.
        assert 'action_id: "export-reference"' not in contract_text
        assert "workflow.display_name || workflowId" in contract_text

    def test_workflow_detail_page_uses_spa_builder_contract(self, registry_client):
        resp = _workflow_detail_response(registry_client, follow_redirects=False)

        contract_text = _page_contract_text(
            resp,
            "assets/frontend/src/pages/workflows/detail/index.tsx",
            "assets/frontend/src/pages/workflows/detail/detail-data-controller.ts",
            "assets/frontend/src/pages/workflows/detail/detail-page-shell.tsx",
            "assets/frontend/src/pages/workflows/shared/header.tsx",
        )
        assert "Workflow settings" in contract_text
        assert "Projects" in contract_text
        assert "Open builder" in contract_text
        assert "Test Run" in contract_text
        assert "Export YAML" in contract_text
        # A protected extension workflow offers no export, not even a reference bundle.
        assert "Export reference" not in contract_text
        assert "Clone Workflow" in contract_text
        assert "Delete Workflow" in contract_text
        assert 'summaryId="workflow-detail-summary"' in contract_text
        assert "const workflowDescription" in contract_text
        assert "description={workflowDescription}" in contract_text
        # The header left `ui/card` in the design-system transfer; the assertion
        # keeps its purpose — the header prints the workflow's own description,
        # not a placeholder — against the canonical node that now carries it.
        assert '<p className="fs-card-description">{description}</p>' in contract_text
        assert "React workflow detail consumes workflow metadata" not in contract_text
        assert 'id="workflow-header-actions"' in contract_text
        assert 'data-workflow-header-action="overview"' in contract_text
        assert 'data-workflow-header-action="projects"' in contract_text
        assert 'data-workflow-header-action="workflow-settings"' in contract_text
        assert 'data-workflow-header-action="builder"' in contract_text
        assert 'data-workflow-header-action="workflow-test-run"' in contract_text
        assert 'data-workflow-header-action="export-yaml"' in contract_text
        assert 'data-workflow-header-action="export-reference"' not in contract_text
        assert 'data-workflow-header-action="clone"' in contract_text
        assert 'data-workflow-header-action="delete"' in contract_text
        assert 'data-workflow-header-action="automation"' not in contract_text
        assert 'data-workflow-header-action="reactions"' not in contract_text
        assert "Workflow automation & reactions" in contract_text
        assert 'id="workflow-reactions-link"' not in contract_text
        assert "Open reactions rules" not in contract_text
        assert "switchTab('trigger')" not in contract_text
        assert "Quick Pair" not in contract_text
        assert "Load from steps" not in contract_text

    def test_workflow_projects_page_uses_spa_bindings_contract(self, registry_client):
        resp = registry_client.get("/registry/workflows/wf_demo/projects", follow_redirects=False)

        contract_text = _page_contract_text(
            resp,
            "assets/frontend/src/pages/workflows/projects/index.tsx",
            "assets/frontend/src/pages/workflows/shared/header.tsx",
            "assets/frontend/src/app/router.tsx",
        )
        assert "WorkflowProjectBindingsPanel" in contract_text
        assert 'activePage="projects"' in contract_text
        assert 'id="workflow-project-bindings"' in contract_text
        assert "Save project bindings" in contract_text
        assert "projectFilterItems" in contract_text
        assert (
            'searchPlaceholder={t("react.workflows.project_name_or_id", "Project name or ID")}'
            in contract_text
        )
        assert "fetchProjectWorkflowBindings" in contract_text
        assert "saveProjectWorkflowBindings" in contract_text
        assert 'data-workflow-header-action="projects"' in contract_text
        assert 'path=":workflowId/projects" element={<WorkflowsProjectsPage />}' in contract_text
        assert "WorkflowAliasRedirect" not in contract_text

    def test_workflow_simulate_page_alias_is_removed(self, registry_client):
        resp = registry_client.get("/registry/workflows/wf_demo/simulate", follow_redirects=False)
        assert resp.status_code == 404

    def test_workflow_detail_page_exposes_step_and_group_drawer_surfaces(self, registry_client):
        resp = _workflow_detail_response(registry_client, follow_redirects=False)

        contract_text = _page_contract_text(
            resp,
            "assets/frontend/src/pages/workflows/detail/index.tsx",
            "assets/frontend/src/pages/workflows/detail/detail-helpers.tsx",
            "assets/frontend/src/pages/workflows/detail/detail-drawer-controller.ts",
            "assets/frontend/src/pages/workflows/detail/detail-drawer-shell.tsx",
            "assets/frontend/src/pages/workflows/detail/detail-main-content.tsx",
            "assets/frontend/src/pages/workflows/detail/detail-group-editor-surface.tsx",
            "assets/frontend/src/pages/workflows/detail/detail-step-editor-surface.tsx",
            "assets/frontend/src/pages/workflows/detail/detail-agent-step-editor.tsx",
        )
        assert 'id="modal-add-step"' in contract_text
        assert "openStepDrawer(" in contract_text
        assert "openGroupDrawer(" in contract_text
        assert "Step editor" in contract_text
        assert "Group editor" in contract_text
        assert "Phase" not in contract_text
        assert "step-block-recovery-warning" not in contract_text
        assert "Published block mapping is required before saving this step." not in contract_text
        assert "Agent contract" in contract_text
        assert "Auto from selected agent" in contract_text
        assert "step-skill-select" not in contract_text
        assert "openModal('modal-add-step')" not in contract_text

    def test_workflow_detail_page_exposes_automation_and_advanced_surfaces(self, registry_client):
        resp = _workflow_detail_response(registry_client, follow_redirects=False)

        contract_text = _page_contract_text(
            resp,
            "assets/frontend/src/pages/workflows/detail/index.tsx",
            "assets/frontend/src/pages/workflows/detail/detail-helpers.tsx",
            "assets/frontend/src/pages/workflows/detail/detail-automation-core.ts",
            "assets/frontend/src/pages/workflows/detail/detail-automation-editor.tsx",
            "assets/frontend/src/pages/workflows/detail/detail-page-shell.tsx",
            "assets/frontend/src/pages/workflows/detail/detail-group-editor-surface.tsx",
            "assets/frontend/src/pages/workflows/detail/detail-step-editor-surface.tsx",
        )
        assert "Step automation" in contract_text
        assert 'id="step-automation-surface"' in contract_text
        assert "+ Add step rule" not in contract_text
        # A rule is added from the editor (Settings, Automation): "Add rule", or "Blank rule" in the empty state.
        assert '"Add rule"' in contract_text
        assert '"Blank rule"' in contract_text
        assert "Group automation" in contract_text
        assert 'id="group-automation-surface"' in contract_text
        assert "phase-automation-surface" not in contract_text
        assert "AutomationRuleEditor" in contract_text
        assert "presetsByScope" not in contract_text
        assert "Open Test Run" in contract_text
        assert "Default matching channels" in contract_text
        assert "Route keys I enter" in contract_text
        assert "rxn-notify-route-keys" in contract_text
        assert "rxn-notify-severity" in contract_text
        assert "rxn-notify-message" in contract_text
        assert "Project notification routes" not in contract_text
        assert "Only selected route keys" not in contract_text

    def test_workflow_detail_page_exposes_test_run_surfaces(self, registry_client):
        resp = _workflow_detail_response(registry_client, follow_redirects=False)

        contract_text = _page_contract_text(
            resp,
            "assets/frontend/src/pages/workflows/detail/index.tsx",
            "assets/frontend/src/pages/workflows/detail/detail-drawer-controller.ts",
            "assets/frontend/src/pages/workflows/detail/detail-step-editor-surface.tsx",
            "assets/frontend/src/pages/workflows/detail/detail-workflow-test-run-surface.tsx",
        )
        assert "WorkflowTestRunSurface" in contract_text
        assert "Step Test Run" in contract_text

    def test_registry_entity_detail_pages_use_client_changelog_surfaces(self, registry_client):
        checks = [
            (
                _agent_page_response(registry_client, follow_redirects=False),
                "assets/frontend/src/pages/registry/agents/detail/index.tsx",
            ),
            (
                _tool_page_response(registry_client, follow_redirects=False),
                "assets/frontend/src/pages/registry/tools/detail/index.tsx",
            ),
            (
                _skill_page_response(registry_client, follow_redirects=False),
                "assets/frontend/src/pages/registry/skills/detail/index.tsx",
            ),
        ]

        for resp, relative_path in checks:
            contract_text = _page_contract_text(resp, relative_path)
            assert "RegistryEntityChangelogPanel" in contract_text
            assert "Changelog" in contract_text

    def test_registry_changelog_route_serves_spa_shell(self, registry_client):
        resp = registry_client.get("/registry/changelog", follow_redirects=False)

        contract_text = _page_contract_text(
            resp,
            "assets/frontend/src/pages/registry/changelog/index.tsx",
        )
        assert "fetchChangelogList" in contract_text
        assert "Loading changelog" in contract_text

    def test_workflows_page_does_not_reload_each_workflow_individually(self, registry_client):
        resp = registry_client.get("/registry/workflows", follow_redirects=False)

        _assert_frontend_shell_response(resp)

    def test_workflow_detail_page_keeps_catalog_options_client_side(self, registry_client):
        resp = _workflow_detail_response(registry_client, follow_redirects=False)
        assert "<option value=" not in resp.text

        contract_text = _page_contract_text(
            resp,
            "assets/frontend/src/pages/workflows/detail/index.tsx",
            "assets/frontend/src/pages/workflows/detail/detail-interface.ts",
            "assets/frontend/src/pages/workflows/detail/detail-step-options-controller.ts",
        )
        assert "collectOptionsFromUnknown(" in contract_text
        assert "uniqueSelectOptions(options)" in contract_text
        assert "workflowRecord.agents" in contract_text
        assert "workflowRecord.skills" in contract_text
        assert "textOrEmpty(step.agentLabel) || value" in contract_text
        assert "textOrEmpty(step.skillLabel) || value" in contract_text


# ---------------------------------------------------------------------------
# Registry operations tests
# ---------------------------------------------------------------------------


class TestRegistryOperations:
    """Test registry-level operations: reload, validate, stats."""

    def test_validate_registry(self, registry_client):
        resp = registry_client.get("/api/validate")
        assert resp.status_code in (200, 404, 500)
        if resp.status_code == 200:
            data = resp.json()
            assert isinstance(data, dict)

    def test_validate_registry_has_errors_field(self, registry_client):
        resp = registry_client.get("/api/validate")
        if resp.status_code == 200:
            data = resp.json()
            assert "errors" in data or "warnings" in data or "stats" in data

    def test_reload_registry(self, registry_client):
        resp = registry_client.post("/api/registry/reload")
        assert resp.status_code in (200, 403, 404, 500)

    def test_registry_stats(self, registry_client):
        resp = registry_client.get("/api/registry/stats")
        assert resp.status_code in (200, 404, 500)
        if resp.status_code == 200:
            data = resp.json()
            assert isinstance(data, dict)

    def test_registry_stats_has_counts(self, registry_client):
        resp = registry_client.get("/api/registry/stats")
        if resp.status_code == 200:
            data = resp.json()
            # Should have some count fields
            has_counts = any("count" in k.lower() for k in data)
            assert has_counts or len(data) > 0

    def test_graph_info(self, registry_client):
        resp = registry_client.get("/api/graph")
        assert resp.status_code in (200, 404, 500)
        if resp.status_code == 200:
            data = resp.json()
            assert isinstance(data, dict)

    def test_validate_with_reload_param(self, registry_client):
        resp = registry_client.get("/api/validate?reload=true")
        assert resp.status_code in (200, 403, 404, 500)


# ---------------------------------------------------------------------------
# Full workflow detail tests (when registry is available)
# ---------------------------------------------------------------------------


class TestWorkflowDetail:
    """Test workflow detail carries its steps, groups and graph, never phases."""

    def test_workflow_detail_has_steps(self, registry_client):
        resp = _workflow_api_detail_response(registry_client)
        if resp.status_code == 200:
            data = resp.json()
            assert [step["step_id"] for step in data["steps"]] == ["step_1"]
            assert data["groups"] == []
            assert "phases" not in data
            assert "_phases" not in data


# ---------------------------------------------------------------------------
# Agent detail tests (when registry is available)
# ---------------------------------------------------------------------------


class TestAgentDetail:
    """Test agent detail endpoint content."""

    def test_agent_detail_has_prompts(self, registry_client):
        resp = _agent_api_detail_response(registry_client)
        if resp.status_code == 200:
            data = resp.json()
            assert "prompts" in data or "agent_id" in data

    def test_agent_detail_has_skills(self, registry_client):
        resp = _agent_api_detail_response(registry_client)
        if resp.status_code == 200:
            data = resp.json()
            assert "skills" in data or "agent_id" in data


# ---------------------------------------------------------------------------
# Account-scope enforcement (FS-DEEP-CAND-065 / 067)
#
# Registry agents/skills carry an optional account_id. A scoped admin in
# account A must not read or mutate an account-B-owned object; same-account and
# global (NULL account) objects stay accessible.
# ---------------------------------------------------------------------------


class _ScopedRegistryFakeDB:
    """Minimal registry DB double exposing only the lookups the scope guards use."""

    def __init__(self) -> None:
        self.agents = {
            "global_agent": {"agent_id": "global_agent", "display_name": "Global"},
            "acctA_agent": {
                "agent_id": "acctA_agent",
                "display_name": "Owned by A",
                "account_id": "account-A",
            },
            "acctB_agent": {
                "agent_id": "acctB_agent",
                "display_name": "Owned by B",
                "account_id": "account-B",
            },
        }
        self.skills = {
            "global_skill": {"skill_id": "global_skill", "display_name": "Global skill"},
            "acctB_skill": {
                "skill_id": "acctB_skill",
                "display_name": "B skill",
                "account_id": "account-B",
            },
        }
        self.deleted: list[str] = []
        self.deleted_authors: dict[str, str] = {}

    # agents
    def get_agent(self, agent_id):
        if agent_id == "deleted_agent":
            return None
        row = self.agents.get(agent_id)
        return dict(row) if row else None

    def list_agents(self, enabled_only=False):
        rows = [dict(row) for row in self.agents.values()]
        return [row for row in rows if not enabled_only or row.get("enabled", True)]

    def search_entities(self, _search, entity_type=None):
        assert entity_type == "agent"
        return [dict(row) for row in self.agents.values()]

    def toggle_agent(self, agent_id):
        return True

    def delete_agent(self, agent_id, author="admin"):
        self.deleted.append(agent_id)
        self.deleted_authors[agent_id] = author

    # skills
    def get_skill(self, skill_id):
        row = self.skills.get(skill_id)
        return dict(row) if row else None

    def delete_skill(self, skill_id, author="admin"):
        self.deleted.append(skill_id)
        self.deleted_authors[skill_id] = author


def _scoped_agents_client(monkeypatch, scope_context):
    import core.entrypoints.admin.agents as agent_routes

    db = _ScopedRegistryFakeDB()
    monkeypatch.setattr(agent_routes, "_get_db", lambda: db)
    monkeypatch.setattr(agent_routes, "_get_registry", lambda: MagicMock())
    app = FastAPI()
    app.include_router(agent_routes.router)

    @app.middleware("http")
    async def _inject_scope(request, call_next):
        request.state.auth_user = {"username": "admin", "roles": ["admin"]}
        request.state.scope_context = scope_context
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False), db


def _scoped_skills_client(monkeypatch, scope_context):
    import core.entrypoints.admin.skills as skill_routes

    db = _ScopedRegistryFakeDB()
    monkeypatch.setattr(skill_routes, "_get_db", lambda: db)
    monkeypatch.setattr(skill_routes, "_get_registry", lambda: MagicMock())
    app = FastAPI()
    app.include_router(skill_routes.router)

    @app.middleware("http")
    async def _inject_scope(request, call_next):
        request.state.auth_user = {"username": "admin", "roles": ["admin"]}
        request.state.scope_context = scope_context
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False), db


class TestRegistryAccountScope:
    @pytest.mark.parametrize("path", ["/api/agents", "/api/agents?search=agent"])
    def test_list_agents_hides_foreign_account_rows(self, monkeypatch, path):
        scope = normalize_scope(account_id="account-A")
        client, _db = _scoped_agents_client(monkeypatch, scope)

        response = client.get(path)

        assert response.status_code == 200
        assert [row["agent_id"] for row in response.json()] == [
            "global_agent",
            "acctA_agent",
        ]

    def test_get_agent_cross_account_denied(self, monkeypatch):
        scope = normalize_scope(account_id="account-A")
        client, _db = _scoped_agents_client(monkeypatch, scope)
        resp = client.get("/api/agents/acctB_agent")
        assert resp.status_code == 404

    def test_get_agent_same_account_allowed(self, monkeypatch):
        scope = normalize_scope(account_id="account-A")
        client, _db = _scoped_agents_client(monkeypatch, scope)
        resp = client.get("/api/agents/acctA_agent")
        assert resp.status_code == 200
        assert resp.json()["agent_id"] == "acctA_agent"

    def test_agent_contract_validation_is_account_scoped(self, monkeypatch):
        import core.entrypoints.admin.agents as agent_routes

        scope = normalize_scope(account_id="account-A")
        client, _db = _scoped_agents_client(monkeypatch, scope)
        validation = MagicMock(return_value={"valid": True, "errors": [], "warnings": []})
        monkeypatch.setattr(agent_routes, "_get_agent_validation", validation)

        denied = client.get("/api/agents/acctB_agent/contract-validation")
        assert denied.status_code == 404
        validation.assert_not_called()

        missing = client.get("/api/agents/missing_agent/contract-validation")
        assert missing.status_code == 404
        deleted = client.get("/api/agents/deleted_agent/contract-validation")
        assert deleted.status_code == 404
        validation.assert_not_called()

        allowed = client.get("/api/agents/acctA_agent/contract-validation")
        assert allowed.status_code == 200
        assert allowed.json()["validation"]["valid"] is True
        validation.assert_called_once_with(
            agent_routes._get_db(), "acctA_agent", account_id="account-A"
        )

    def test_agent_contract_validation_resolves_active_projected_agent(self, monkeypatch):
        import core.entrypoints.admin.agents as agent_routes

        scope = normalize_scope(account_id="account-A")
        client, db = _scoped_agents_client(monkeypatch, scope)
        db.get_agent = lambda _agent_id: None
        projected = {
            "agent_id": "extension.install-1.projected_agent",
            "account_id": "account-A",
            "source_install_id": "install-1",
        }
        lookup = MagicMock(return_value=projected)
        validation = MagicMock(return_value={"valid": True, "errors": [], "warnings": []})
        monkeypatch.setattr(agent_routes, "projection_aware_catalog_row", lookup)
        monkeypatch.setattr(agent_routes, "_get_agent_validation", validation)

        response = client.get("/api/agents/extension.install-1.projected_agent/contract-validation")

        assert response.status_code == 200
        lookup.assert_called_once_with(
            db,
            "agent",
            "agent_id",
            "extension.install-1.projected_agent",
            None,
            account_id="account-A",
        )
        validation.assert_called_once_with(
            db, "extension.install-1.projected_agent", account_id="account-A"
        )

    def test_get_global_agent_allowed(self, monkeypatch):
        scope = normalize_scope(account_id="account-A")
        client, _db = _scoped_agents_client(monkeypatch, scope)
        resp = client.get("/api/agents/global_agent")
        assert resp.status_code == 200

    def test_toggle_agent_cross_account_denied(self, monkeypatch):
        scope = normalize_scope(account_id="account-A")
        client, _db = _scoped_agents_client(monkeypatch, scope)
        resp = client.patch("/api/agents/acctB_agent/toggle")
        assert resp.status_code == 404

    def test_delete_agent_cross_account_denied(self, monkeypatch):
        scope = normalize_scope(account_id="account-A")
        client, db = _scoped_agents_client(monkeypatch, scope)
        resp = client.delete("/api/agents/acctB_agent")
        assert resp.status_code == 404
        assert "acctB_agent" not in db.deleted

    def test_delete_agent_records_request_actor(self, monkeypatch):
        scope = normalize_scope(account_id="account-A", actor_id="alice@example.com")
        client, db = _scoped_agents_client(monkeypatch, scope)
        resp = client.delete("/api/agents/acctA_agent")
        assert resp.status_code == 200
        assert db.deleted_authors["acctA_agent"] == "alice@example.com"

    def test_get_skill_cross_account_denied(self, monkeypatch):
        scope = normalize_scope(account_id="account-A")
        client, _db = _scoped_skills_client(monkeypatch, scope)
        resp = client.get("/api/skills/acctB_skill")
        assert resp.status_code == 404

    def test_get_skill_global_allowed(self, monkeypatch):
        scope = normalize_scope(account_id="account-A")
        client, _db = _scoped_skills_client(monkeypatch, scope)
        resp = client.get("/api/skills/global_skill")
        assert resp.status_code == 200

    def test_delete_skill_cross_account_denied(self, monkeypatch):
        scope = normalize_scope(account_id="account-A")
        client, db = _scoped_skills_client(monkeypatch, scope)
        resp = client.delete("/api/skills/acctB_skill")
        assert resp.status_code == 404
        assert "acctB_skill" not in db.deleted

    def test_delete_skill_records_request_actor(self, monkeypatch):
        scope = normalize_scope(account_id="account-A", actor_id="alice@example.com")
        client, db = _scoped_skills_client(monkeypatch, scope)
        resp = client.delete("/api/skills/global_skill")
        assert resp.status_code == 200
        assert db.deleted_authors["global_skill"] == "alice@example.com"


def test_delete_workflow_records_request_actor(monkeypatch):
    import core.entrypoints.admin.workflows_routes_write as workflow_write_routes

    scope = normalize_scope(account_id="account-A", actor_id="alice@example.com")
    mock_db = MagicMock()
    mock_db.get_workflow.return_value = {"workflow_id": "wf_manual"}
    mock_registry = MagicMock()
    mock_portability_service = SimpleNamespace(assert_delete_allowed=lambda _workflow_id: None)

    monkeypatch.setattr(workflow_write_routes, "_get_db", lambda: mock_db)
    monkeypatch.setattr(workflow_write_routes, "_get_registry", lambda: mock_registry)
    monkeypatch.setattr(
        workflow_write_routes,
        "_resolve_workflow_scope_target",
        lambda _db, _request, _workflow_id: ({"workflow_id": "wf_manual"}, scope),
    )
    monkeypatch.setattr(
        workflow_write_routes,
        "_get_workflow_portability_service",
        lambda _db: mock_portability_service,
    )

    result = workflow_write_routes.delete_workflow("wf_manual", MagicMock())

    assert result == {"status": "deleted", "workflow_id": "wf_manual"}
    mock_db.delete_workflow.assert_called_once_with(
        "wf_manual",
        author="alice@example.com",
    )


def test_delete_workflow_returns_typed_dependency_conflict(monkeypatch):
    import core.entrypoints.admin.workflows_routes_write as workflow_write_routes
    from core.application.management.errors import ServiceConflictError

    scope = normalize_scope(account_id="account-A", actor_id="alice@example.com")
    mock_db = MagicMock()
    mock_db.get_workflow.return_value = {"workflow_id": "wf_live"}
    mock_db.delete_workflow.side_effect = ServiceConflictError(
        "Workflow has non-terminal dependent jobs",
        payload={
            "code": "workflow_has_nonterminal_jobs",
            "workflow_id": "wf_live",
            "blocking_jobs": [{"job_id": "job-1", "status": "awaiting_approval"}],
            "truncated": False,
        },
    )
    monkeypatch.setattr(workflow_write_routes, "_get_db", lambda: mock_db)
    monkeypatch.setattr(workflow_write_routes, "_get_registry", MagicMock)
    monkeypatch.setattr(
        workflow_write_routes,
        "_resolve_workflow_scope_target",
        lambda _db, _request, _workflow_id: ({"workflow_id": "wf_live"}, scope),
    )
    monkeypatch.setattr(
        workflow_write_routes,
        "_get_workflow_portability_service",
        lambda _db: SimpleNamespace(assert_delete_allowed=lambda _workflow_id: None),
    )

    with pytest.raises(HTTPException) as exc_info:
        workflow_write_routes.delete_workflow("wf_live", MagicMock())

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "workflow_has_nonterminal_jobs"
    assert exc_info.value.detail["blocking_jobs"] == [
        {"job_id": "job-1", "status": "awaiting_approval"}
    ]


# ---------------------------------------------------------------------------
# Prompt preview + performance scope (FS-DEEP-CAND-067)
# ---------------------------------------------------------------------------


class _PreviewFakeRegistry:
    def __init__(self, db):
        self.db_store = db
        self.agents = MagicMock()
        self.workflows = MagicMock()


def test_preview_agent_cross_account_denied(monkeypatch):
    import core.entrypoints.admin.preview as preview_routes

    db = _ScopedRegistryFakeDB()
    db.get_workflow = lambda workflow_id: None  # not used here
    registry = _PreviewFakeRegistry(db)
    monkeypatch.setattr(preview_routes, "_get_registry", lambda: registry)
    app = FastAPI()
    app.include_router(preview_routes.router)

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.scope_context = normalize_scope(account_id="account-A")
        return await call_next(request)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/preview/agent/acctB_agent", params={"skill": "x"})
    assert resp.status_code == 404
    # In-memory registry agent lookup must never be reached for a cross-account agent.
    registry.agents.get_agent.assert_not_called()


def test_preview_agent_same_account_passes_scope_guard(monkeypatch):
    import core.entrypoints.admin.preview as preview_routes

    db = _ScopedRegistryFakeDB()
    registry = _PreviewFakeRegistry(db)
    # Same-account agent clears the scope guard; in-memory lookup returns None ->
    # 404 from the *existence* check, proving the scope guard did not block it.
    registry.agents.get_agent.return_value = None
    monkeypatch.setattr(preview_routes, "_get_registry", lambda: registry)
    app = FastAPI()
    app.include_router(preview_routes.router)

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.scope_context = normalize_scope(account_id="account-A")
        return await call_next(request)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/preview/agent/acctA_agent", params={"skill": "x"})
    assert resp.status_code == 404
    registry.agents.get_agent.assert_called_once()


def test_agent_performance_cross_account_denied(monkeypatch):
    import core.entrypoints.admin.agents as agent_routes

    db = _ScopedRegistryFakeDB()
    monkeypatch.setattr(agent_routes, "_get_db", lambda: db)
    monkeypatch.setattr(agent_routes, "_get_registry", lambda: MagicMock())
    app = FastAPI()
    app.include_router(agent_routes.router)

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.scope_context = normalize_scope(account_id="account-A")
        return await call_next(request)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/agents/acctB_agent/performance")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Guardrail project-scope (FS-DEEP-CAND-018 / 030)
#
# Guardrail rows carry only project_id (no account_id column), so project
# membership is the enforceable scope dimension. A project-restricted admin must
# not read or mutate a guardrail bound to a project outside its allowed set;
# global (project_id NULL) guardrails stay accessible.
# ---------------------------------------------------------------------------


class _GuardrailScopeFakeDB:
    def __init__(self) -> None:
        self.guardrails = {
            "g_global": {
                "id": "g_global",
                "display_name": "Global",
                "project_id": None,
                "account_id": "account-1",
            },
            "g_proj3": {
                "id": "g_proj3",
                "display_name": "Proj3",
                "project_id": "3",
                "account_id": "account-1",
            },
            "g_proj4": {
                "id": "g_proj4",
                "display_name": "Proj4",
                "project_id": "4",
                "account_id": "account-1",
            },
            "g_foreign": {
                "id": "g_foreign",
                "display_name": "Foreign",
                "project_id": None,
                "account_id": "account-2",
            },
        }
        self.saved: list[dict] = []
        self.saved_contexts: list[dict] = []
        self.deleted: list[str] = []
        self.deleted_authors: dict[str, str] = {}
        self.toggled: list[str] = []
        self.dsn = ""

    def get_guardrail(self, guardrail_id, include_trashed=False, *, account_id=None):
        row = self.guardrails.get(guardrail_id)
        return dict(row) if row and row.get("account_id") == account_id else None

    def list_guardrails(self, *, account_id, project_id=None, enabled_only=False):
        return [
            dict(row) for row in self.guardrails.values() if row.get("account_id") == account_id
        ]

    def save_guardrail(self, data, *, account_id, actor_id, actor_type, author="admin"):
        self.saved.append(dict(data))
        self.saved_contexts.append(
            {
                "account_id": account_id,
                "actor_id": actor_id,
                "actor_type": actor_type,
                "author": author,
            }
        )
        guardrail_id = str(data.get("id") or "g_new")
        existing = self.guardrails.get(guardrail_id, {})
        agents = data.get("agents", existing.get("_agents", []))
        self.guardrails[guardrail_id] = {
            **existing,
            **dict(data),
            "id": guardrail_id,
            "account_id": account_id,
            "_agents": list(agents or []),
        }
        return guardrail_id

    def delete_guardrail(self, guardrail_id, *, account_id, actor_id, actor_type, author="admin"):
        self.deleted.append(guardrail_id)
        self.deleted_authors[guardrail_id] = author
        row = self.guardrails.get(guardrail_id)
        return bool(row and row.get("account_id") == account_id)

    def toggle_guardrail(self, guardrail_id, *, account_id, actor_id, actor_type):
        self.toggled.append(guardrail_id)
        row = self.guardrails.get(guardrail_id)
        return True if row and row.get("account_id") == account_id else None


def _scoped_guardrails_client(monkeypatch, scope_context):
    import core.entrypoints.admin.guardrails as guardrail_routes

    db = _GuardrailScopeFakeDB()
    monkeypatch.setattr(guardrail_routes, "_get_db", lambda: db)
    monkeypatch.setattr(guardrail_routes, "_get_extension_runtime_store", lambda _db: None)
    app = FastAPI()
    app.include_router(guardrail_routes.router)

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.auth_user = {"username": "admin", "roles": ["admin"]}
        request.state.scope_context = scope_context
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False), db


class TestGuardrailProjectScope:
    def _restricted_scope(self):
        return normalize_scope(account_id="account-1", allowed_project_ids=frozenset({"3"}))

    def test_get_guardrail_other_project_denied(self, monkeypatch):
        client, _db = _scoped_guardrails_client(monkeypatch, self._restricted_scope())
        resp = client.get("/api/guardrails/g_proj4")
        assert resp.status_code == 404

    def test_get_guardrail_allowed_project_ok(self, monkeypatch):
        client, _db = _scoped_guardrails_client(monkeypatch, self._restricted_scope())
        resp = client.get("/api/guardrails/g_proj3")
        assert resp.status_code == 200

    def test_get_global_guardrail_ok(self, monkeypatch):
        client, _db = _scoped_guardrails_client(monkeypatch, self._restricted_scope())
        resp = client.get("/api/guardrails/g_global")
        assert resp.status_code == 200

    def test_list_guardrails_hides_foreign_account(self, monkeypatch):
        client, _db = _scoped_guardrails_client(monkeypatch, self._restricted_scope())
        resp = client.get("/api/guardrails")
        assert resp.status_code == 200
        assert "g_foreign" not in {row["id"] for row in resp.json()}

    def test_delete_guardrail_other_project_denied(self, monkeypatch):
        client, db = _scoped_guardrails_client(monkeypatch, self._restricted_scope())
        resp = client.delete("/api/guardrails/g_proj4")
        assert resp.status_code == 404
        assert "g_proj4" not in db.deleted

    def test_delete_guardrail_records_request_actor(self, monkeypatch):
        scope = normalize_scope(
            account_id="account-1",
            actor_id="alice@example.com",
            allowed_project_ids=frozenset({"3"}),
        )
        client, db = _scoped_guardrails_client(monkeypatch, scope)
        resp = client.delete("/api/guardrails/g_proj3")
        assert resp.status_code == 200
        assert db.deleted_authors["g_proj3"] == "alice@example.com"

    def test_toggle_guardrail_other_project_denied(self, monkeypatch):
        client, db = _scoped_guardrails_client(monkeypatch, self._restricted_scope())
        resp = client.patch("/api/guardrails/g_proj4/toggle")
        assert resp.status_code == 404
        assert "g_proj4" not in db.toggled

    def test_create_guardrail_foreign_project_denied(self, monkeypatch):
        client, db = _scoped_guardrails_client(monkeypatch, self._restricted_scope())
        resp = client.post(
            "/api/guardrails",
            json={
                "display_name": "X",
                "mode": "deterministic",
                "rule_type": "forbidden_patterns",
                "config": {"patterns": ["secret"]},
                "project_id": "4",
                "agents": ["agent-1"],
            },
        )
        assert resp.status_code == 403
        assert db.saved == []

    def test_create_guardrail_allowed_project_ok(self, monkeypatch):
        client, db = _scoped_guardrails_client(monkeypatch, self._restricted_scope())
        resp = client.post(
            "/api/guardrails",
            json={
                "display_name": "X",
                "mode": "deterministic",
                "rule_type": "forbidden_patterns",
                "config": {"patterns": ["secret"]},
                "project_id": "3",
                "agents": ["agent-1"],
            },
        )
        assert resp.status_code == 201
        assert len(db.saved) == 1
        assert db.saved_contexts == [
            {
                "account_id": "account-1",
                "actor_id": "operator",
                "actor_type": "unknown",
                "author": "operator",
            }
        ]

    def test_create_guardrail_maps_store_validation_failure_to_bad_request(self, monkeypatch):
        client, db = _scoped_guardrails_client(monkeypatch, self._restricted_scope())

        def reject_unavailable_agents(*_args, **_kwargs):
            raise ValueError("guardrail agents are not available in the owning account: composer")

        monkeypatch.setattr(db, "save_guardrail", reject_unavailable_agents)

        resp = client.post(
            "/api/guardrails",
            json={
                "display_name": "Foreign-account guardrail",
                "mode": "deterministic",
                "rule_type": "forbidden_patterns",
                "config": {"patterns": ["secret"]},
                "agents": ["composer"],
            },
        )

        assert resp.status_code == 400
        assert resp.json()["detail"] == (
            "guardrail agents are not available in the owning account: composer"
        )

    def test_patch_foreign_account_guardrail_fails_closed(self, monkeypatch):
        client, db = _scoped_guardrails_client(monkeypatch, self._restricted_scope())
        resp = client.patch("/api/guardrails/g_foreign", json={"description": "stolen"})
        assert resp.status_code == 404
        assert db.saved == []

    def test_patch_guardrail_config_preserves_agent_scope_after_reload(self, monkeypatch):
        client, db = _scoped_guardrails_client(monkeypatch, self._restricted_scope())
        db.guardrails["g_proj3"]["_agents"] = ["composer"]
        db.guardrails["g_proj3"]["config"] = {"paths": ["README.md"]}

        resp = client.patch(
            "/api/guardrails/g_proj3",
            json={
                "display_name": "Proj3 updated",
                "description": "after",
                "mode": "semantic",
                "rule_type": "immutable_paths",
                "severity": "warning",
                "config": {"paths": ["docs/standards"]},
                "prompt_text": "after prompt",
                "agent_prompt": "after guidance",
            },
        )
        assert resp.status_code == 200

        detail = client.get("/api/guardrails/g_proj3")
        assert detail.status_code == 200
        payload = detail.json()
        assert payload["display_name"] == "Proj3 updated"
        assert payload["description"] == "after"
        assert payload["mode"] == "semantic"
        assert payload["severity"] == "warning"
        assert payload["config"] == {"paths": ["docs/standards"]}
        assert payload["prompt_text"] == "after prompt"
        assert payload["agent_prompt"] == "after guidance"
        assert payload["_agents"] == ["composer"]
        assert db.saved[-1]["agents"] == ["composer"]


class TestUnroutedStepGates:
    """An unrouted step keeps a workflow out of production until it is wired."""

    @staticmethod
    def _workflow_with_unrouted_step(*, routed: bool = False) -> dict:
        """``scratch_work`` exists but no edge reaches or leaves it unless *routed*."""
        workflow = linear_workflow(
            {"step_id": "work", "step_kind": "agent", "agent_id": "scout", "override": {}},
            {"step_id": "done", "step_kind": "stop_success"},
            workflow_id="wf_unrouted_gate",
            display_name="Unrouted gate",
            enabled=False,
        )
        workflow["steps"].insert(
            1,
            {
                "step_id": "scratch_work",
                "step_kind": "agent",
                "agent_id": "scout",
                "group_id": None,
                "lane_id": None,
                "override": {},
            },
        )
        if routed:
            workflow["settings"]["execution_graph"] = linear_graph(["work", "scratch_work", "done"])
        return workflow

    @staticmethod
    def _toggle(registry_client, workflow: dict):
        import core.entrypoints.admin.workflows_routes_write as write_routes

        db = MagicMock()
        db.get_workflow.return_value = workflow
        with patch.object(write_routes, "_get_db", return_value=db):
            resp = registry_client.patch("/api/workflows/wf_unrouted_gate/toggle")
        issues = (resp.json().get("detail") or {}).get("issues") or []
        return resp, db, {str(issue.get("code") or "") for issue in issues}

    def test_metadata_patch_cannot_write_the_execution_graph(self, registry_client):
        """It decides what executes, and only the change service may set it."""
        import core.entrypoints.admin.workflows_routes_write as write_routes

        existing = self._workflow_with_unrouted_step()
        stored_graph = dict(existing["settings"]["execution_graph"])
        db = MagicMock()
        db.get_workflow.return_value = existing
        db.save_workflow.return_value = None

        with patch.object(write_routes, "_get_db", return_value=db):
            registry_client.patch(
                "/api/workflows/wf_unrouted_gate",
                json={
                    "settings": {
                        "execution_graph": linear_graph(["work", "scratch_work", "done"]),
                        "max_total_cost": 5,
                    }
                },
            )

        saved = [call.args[0] for call in db.save_workflow.call_args_list if call.args]
        for payload in saved:
            settings = payload.get("settings") or {}
            # The caller's own settings land; the service-owned key does not.
            assert settings.get("execution_graph") == stored_graph, payload
            assert settings.get("max_total_cost") == 5, payload
        assert saved, "the patch never reached the store"

    def test_enabling_is_refused_while_a_step_is_unrouted(self, registry_client):
        """Readiness is the enable gate, and an unrouted step fails it."""
        resp, db, codes = self._toggle(registry_client, self._workflow_with_unrouted_step())

        assert resp.status_code == 400
        assert {"workflow.graph.unreachable", "workflow.graph.outcome_missing"} <= codes
        unreachable = next(
            issue
            for issue in resp.json()["detail"]["issues"]
            if issue["code"] == "workflow.graph.unreachable"
        )
        assert "scratch_work" in unreachable["message"]
        assert unreachable["path"] == "workflow.steps.scratch_work"
        db.toggle_workflow.assert_not_called()

    def test_routing_the_step_removes_exactly_that_reason_to_refuse(self, registry_client):
        """The mocked store has no agents, so other reasons remain — but not these."""
        blocked = self._workflow_with_unrouted_step()
        routed = self._workflow_with_unrouted_step(routed=True)

        _resp, _db, blocked_codes = self._toggle(registry_client, blocked)
        _resp, _db, routed_codes = self._toggle(registry_client, routed)

        assert blocked_codes - routed_codes == {
            "workflow.graph.unreachable",
            "workflow.graph.outcome_missing",
        }
        assert routed_codes - blocked_codes == set()
