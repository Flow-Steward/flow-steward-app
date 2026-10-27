from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

try:
    import core.entrypoints.admin.agents as agent_routes
    import core.entrypoints.admin.workflows as workflow_routes

    _IMPORT_ERROR = None
except Exception as _e:  # pragma: no cover - environment-dependent optional stack
    agent_routes = None
    workflow_routes = None
    _IMPORT_ERROR = _e


def test_agent_update_rejects_forbidden_context_template_path():
    if agent_routes is None:
        pytest.skip(f"admin routes not importable in this environment: {_IMPORT_ERROR}")
    app = FastAPI()
    app.include_router(agent_routes.router)
    client = TestClient(app)
    mock_db = MagicMock()
    mock_db.get_agent.return_value = {
        "agent_id": "composer",
        "account_id": None,
    }

    body = {
        "agent_id": "composer",
        "display_name": "Developer",
        "agent_type_id": "composer",
        "version": "1.0.0",
        "description": "",
        "enabled": False,
        "prompts": {
            "system": "You are composer",
            "context_template": "Use {{ project_config.credentials }}",
        },
        "config": {},
        "quality": {},
        "inputs": {"required": [], "optional": []},
        "outputs": [],
        "depends_on": {"soft": [], "hard": []},
        "tags": [],
        "skills": [],
        "tools": [],
    }

    with patch.object(agent_routes, "_get_db", return_value=mock_db):
        resp = client.put("/api/agents/composer", json=body)

    assert resp.status_code == 400
    assert "forbidden placeholder path" in resp.text.lower()
    mock_db.save_agent.assert_not_called()


def test_workflow_change_rejects_forbidden_context_inject_source():
    if workflow_routes is None:
        pytest.skip(f"workflow routes not importable in this environment: {_IMPORT_ERROR}")
    app = FastAPI()
    app.include_router(workflow_routes.router)
    client = TestClient(app)
    mock_db = MagicMock()
    mock_db.get_workflow.return_value = {
        "workflow_id": "wf_demo",
        "revision": 3,
        "steps": [],
        "groups": [],
        "settings": {},
    }

    with patch.object(workflow_routes, "_get_db", return_value=mock_db):
        resp = client.post(
            "/api/workflows/wf_demo/changes",
            json={
                "expected_revision": 3,
                "operations": [
                    {
                        "op": "create_step",
                        "step": {
                            "step_id": "draft",
                            "step_kind": "agent",
                            "agent_id": "composer",
                            "context_inject": {
                                "db": {"source": "project_config.credentials", "required": True},
                            },
                        },
                    }
                ],
            },
        )

    assert resp.status_code == 400
    assert "forbidden source" in resp.text.lower()
    mock_db.save_workflow.assert_not_called()
