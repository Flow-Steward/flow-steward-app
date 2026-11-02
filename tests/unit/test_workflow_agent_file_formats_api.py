"""Workflow editor format metadata follows the effective server policy."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.orchestration import agent_file_policy
from core.entrypoints.admin import workflows


@pytest.mark.parametrize(
    ("setting", "extensions"),
    [
        (
            None,
            (".txt", ".md", ".markdown", ".json", ".xml", ".csv", ".tsv", ".yaml", ".yml", ".rtf"),
        ),
        ("TXT, .custom", (".txt", ".custom")),
        (
            "",
            (".txt", ".md", ".markdown", ".json", ".xml", ".csv", ".tsv", ".yaml", ".yml", ".rtf"),
        ),
    ],
    ids=["default", "custom", "blank_uses_default"],
)
def test_metadata_projects_effective_policy(monkeypatch, setting, extensions):
    app = FastAPI()
    app.include_router(workflows.router)
    env = {} if setting is None else {"FS_AGENT_TEXT_FILE_EXTENSIONS": setting}
    monkeypatch.setattr(agent_file_policy, "runtime_env", env)
    response = TestClient(app).get("/api/workflows/meta/agent-file-formats")
    assert response.status_code == 200
    assert response.json() == {
        "text_extensions": list(extensions),
        "text_encoding": "utf-8",
        "rtf_mode": "source",
    }


def test_metadata_requires_authentication():
    from core.entrypoints.api import app_factory
    from core.entrypoints.api import route_registration as api_routes

    app = app_factory.create_app()
    app.state.local_auth_enabled_override = lambda: True
    app.state.external_auth_enabled_override = lambda: False
    with (
        patch.dict(app.state.runtime_container.service_factories, {"db": lambda: MagicMock()}),
        patch.object(api_routes, "_auth_tables_available", return_value=True),
    ):
        response = TestClient(app, raise_server_exceptions=False).get(
            "/api/workflows/meta/agent-file-formats"
        )
    assert response.status_code == 401
