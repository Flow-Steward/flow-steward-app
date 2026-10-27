from __future__ import annotations

from unittest.mock import Mock

from tests.e2e import helpers


def test_e2e_test_runtime_provider_config_never_uses_environment_provider_keys(monkeypatch):
    monkeypatch.setenv("FS_DEFAULT_PROVIDER", "openai")
    monkeypatch.setenv("FS_DEFAULT_MODEL", "gpt-4o")

    assert helpers._e2e_test_runtime_provider_config() == {}


def test_ensure_e2e_project_runtime_provider_does_not_write_project_provider_config(monkeypatch):
    monkeypatch.setenv("E2E_ENSURE_TEST_PROJECT_API_PROVIDER", "1")
    monkeypatch.setenv("FS_DEFAULT_PROVIDER", "openai")
    monkeypatch.setenv("FS_DEFAULT_MODEL", "gpt-4o")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, url: str, **kwargs):
        calls.append((method, url, kwargs))
        if method == "GET":
            return Mock(
                status_code=200,
                json=Mock(
                    return_value={
                        "name": "Test Project Backend",
                        "workspace_id": "workspace-1",
                        "project_type": "it",
                        "agents_config": {"stack": {"language": "python"}},
                    }
                ),
            )
        return Mock(status_code=200, json=Mock(return_value={"ok": True}))

    monkeypatch.setattr(helpers, "e2e_request", fake_request)
    monkeypatch.setattr(helpers, "BASE_URL", "http://localhost:8099")

    helpers._ensure_e2e_project_runtime_provider("project-1")

    assert calls == []


def test_ensure_e2e_project_runtime_provider_leaves_non_test_projects(monkeypatch):
    monkeypatch.setenv("E2E_ENSURE_TEST_PROJECT_API_PROVIDER", "1")
    monkeypatch.setenv("FS_DEFAULT_PROVIDER", "openai")
    monkeypatch.setenv("FS_DEFAULT_MODEL", "gpt-4o")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, url: str, **kwargs):
        calls.append((method, url, kwargs))
        return Mock(
            status_code=200,
            json=Mock(
                return_value={
                    "name": "Flow Steward App",
                    "workspace_id": "workspace-1",
                    "project_type": "it",
                    "agents_config": {
                        "default_provider": "openai",
                        "default_model": "gpt-5.5",
                    },
                }
            ),
        )

    monkeypatch.setattr(helpers, "e2e_request", fake_request)
    monkeypatch.setattr(helpers, "BASE_URL", "http://localhost:8099")

    helpers._ensure_e2e_project_runtime_provider("project-1")

    assert calls == []


def test_ensure_test_job_refreshes_deleted_cached_seed(monkeypatch):
    pid = "231"
    helpers._seeded_jobs.clear()
    helpers._seeded_jobs[pid] = {"id": 1360, "status": "deleted"}

    responses = [
        Mock(status_code=404),
        Mock(status_code=200, json=Mock(return_value={"jobs": []})),
        Mock(status_code=200, json=Mock(return_value={"job_id": 1379})),
        Mock(status_code=200, json=Mock(return_value={"id": 1379, "status": "pending"})),
    ]

    calls: list[tuple[str, str]] = []

    def fake_request(method: str, url: str, **kwargs):
        calls.append((method, url))
        return responses.pop(0)

    monkeypatch.setattr(helpers, "e2e_request", fake_request)
    monkeypatch.setattr(helpers, "BASE_URL", "http://localhost:8099")
    monkeypatch.setattr(
        helpers,
        "ensure_canonical_project_workflows",
        lambda *_args, **_kwargs: {"workflow_ids": ["weekly_report"]},
    )

    job = helpers.ensure_test_job(pid)

    assert job["id"] == 1379
    assert helpers._seeded_jobs[pid]["id"] == 1379
    assert calls == [
        ("GET", "http://localhost:8099/api/jobs/1360"),
        ("GET", "http://localhost:8099/api/projects/231/jobs"),
        ("POST", "http://localhost:8099/api/workflow-runs"),
        ("GET", "http://localhost:8099/api/jobs/1379"),
    ]
