from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from core.application.management.command_models import WorkflowRunInput
from core.application.tooling.testing import smoke_test

_PROJECT_ID = "11111111-1111-4111-8111-111111111111"


class _Response:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        return dict(self._payload)


class _Session:
    def __init__(self, *, workflows: list[dict[str, Any]]) -> None:
        self.workflows = workflows
        self.task_payloads: list[dict[str, Any]] = []

    def get(self, url: str, **_kwargs: Any) -> _Response:
        if url.endswith(f"/api/projects/{_PROJECT_ID}/workflow-catalog"):
            return _Response(200, {"project_id": _PROJECT_ID, "workflows": self.workflows})
        if url.endswith(f"/api/projects/{_PROJECT_ID}/workflows/weekly_report/contract"):
            return _Response(
                200,
                {
                    "workflow_id": "weekly_report",
                    "enabled": True,
                    "interface": {
                        "input_mode": "fields",
                        "expected_inputs": [],
                        "input_schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {},
                        },
                    },
                },
            )
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url: str, *, json: dict[str, Any], **_kwargs: Any) -> _Response:
        if url.endswith("/api/workflow-runs"):
            self.task_payloads.append(dict(json))
            return _Response(202, {"job_id": "job-1"})
        raise AssertionError(f"unexpected POST {url}")


class _Cookies:
    def get(self, key: str, default: str = "") -> str:
        return "csrf-token" if key == "fs_csrf" else default


class _AccountSelectionSession:
    def __init__(self) -> None:
        self.verify: object = True
        self.cookies = _Cookies()
        self.login_payloads: list[dict[str, Any]] = []

    def post(self, url: str, *, json: dict[str, Any], **_kwargs: Any) -> _Response:
        assert url == "http://app.test/api/auth/login"
        self.login_payloads.append(dict(json))
        if len(self.login_payloads) == 1:
            return _Response(
                409,
                {
                    "error": "Account selection required",
                    "requires_account_selection": True,
                    "account_options": [
                        {
                            "account_id": "22222222-2222-4222-8222-222222222222",
                            "is_active": True,
                            "is_default": True,
                        },
                        {
                            "account_id": "33333333-3333-4333-8333-333333333333",
                            "is_active": True,
                            "is_default": False,
                        },
                    ],
                },
            )
        return _Response(200, {"ok": True})


def test_smoke_login_retries_with_active_default_account(monkeypatch: Any) -> None:
    session = _AccountSelectionSession()
    monkeypatch.setattr(smoke_test.requests, "Session", lambda: session)
    monkeypatch.setattr(smoke_test, "_resolve_verify_setting", lambda: False)
    monkeypatch.setattr(smoke_test, "BASE", "http://app.test")
    monkeypatch.setattr(smoke_test, "USERNAME", "admin@example.test")
    monkeypatch.setattr(smoke_test, "PASSWORD", "test-password")

    resolved, headers = smoke_test.setup_session()

    assert resolved is session
    assert headers == {"x-csrf-token": "csrf-token"}
    assert session.login_payloads == [
        {"email": "admin@example.test", "password": "test-password"},
        {
            "email": "admin@example.test",
            "password": "test-password",
            "account_id": "22222222-2222-4222-8222-222222222222",
        },
    ]


def test_active_job_cleanup_is_limited_to_selected_project(monkeypatch: Any) -> None:
    class _JobsSession:
        def get(self, url: str, **_kwargs: Any) -> _Response:
            assert url == "http://app.test/api/jobs?limit=200"
            return _Response(
                200,
                {
                    "jobs": [
                        {
                            "id": "selected-active",
                            "project_id": _PROJECT_ID,
                            "status": "awaiting_approval",
                        },
                        {
                            "id": "other-active",
                            "project_id": "22222222-2222-4222-8222-222222222222",
                            "status": "queued",
                        },
                        {
                            "id": "selected-complete",
                            "project_id": _PROJECT_ID,
                            "status": "completed",
                        },
                    ]
                },
            )

    monkeypatch.setattr(smoke_test, "S", _JobsSession())
    monkeypatch.setattr(smoke_test, "BASE", "http://app.test")
    monkeypatch.setattr(smoke_test, "PROJECT_ID", _PROJECT_ID)
    monkeypatch.setattr(smoke_test, "H", {"x-csrf-token": "csrf"})

    assert smoke_test.get_active_jobs() == [
        {
            "id": "selected-active",
            "project_id": _PROJECT_ID,
            "status": "awaiting_approval",
        }
    ]


def test_job_list_rows_expose_current_job_id_through_legacy_id_key() -> None:
    assert smoke_test._extract_jobs_rows(
        {"jobs": [{"job_id": "job-current", "status": "cancelled"}]}
    ) == [{"job_id": "job-current", "id": "job-current", "status": "cancelled"}]


def _configure_run(monkeypatch: Any, session: _Session) -> None:
    monkeypatch.setattr(
        smoke_test,
        "resolve_test_env",
        lambda *_args, **_kwargs: SimpleNamespace(
            project_id=_PROJECT_ID,
            username="admin@example.test",
            password="test-password",  # pragma: allowlist secret
        ),
    )
    monkeypatch.setattr(
        smoke_test,
        "resolve_base_url",
        lambda *_args, **_kwargs: "http://app.test",
    )
    monkeypatch.setattr(smoke_test, "setup_session", lambda: (session, {"x-csrf-token": "csrf"}))
    monkeypatch.setattr(smoke_test, "get_active_jobs", list)
    monkeypatch.setattr(
        smoke_test,
        "get_workflows",
        lambda: [{"workflow_id": "global-unlinked", "enabled": True}],
    )
    monkeypatch.setattr(smoke_test.time, "sleep", lambda _seconds: None)


def test_backend_smoke_resolves_linked_workflow_and_sends_workflow_run_payload(
    monkeypatch: Any,
) -> None:
    session = _Session(
        workflows=[
            {
                "workflow_id": "weekly_report",
                "enabled": True,
                "availability_status": "available",
                "new_runs_blocked": False,
            }
        ]
    )
    _configure_run(monkeypatch, session)
    monkeypatch.setattr(
        smoke_test,
        "run_workflow_smoke_scenarios",
        lambda context: context["submit_task"]("Fix the current task contract", mode="brownfield"),
    )
    monkeypatch.setattr(smoke_test, "run_admin_smoke_scenarios", lambda _context: None)

    result = smoke_test.run_smoke_test(project_id=_PROJECT_ID, lane="backend")

    assert result == 0
    assert len(session.task_payloads) == 1
    payload = session.task_payloads[0]
    validated = WorkflowRunInput.model_validate(payload)
    assert validated.project_id == _PROJECT_ID
    assert validated.workflow_id == "weekly_report"
    assert validated.inputs == {}
    assert "task_summary" not in payload
    assert "mode" not in payload


def test_backend_smoke_stops_before_scenarios_when_project_has_no_compatible_workflow(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    session = _Session(workflows=[])
    _configure_run(monkeypatch, session)
    scenario_calls: list[str] = []
    monkeypatch.setattr(
        smoke_test,
        "run_workflow_smoke_scenarios",
        lambda _context: scenario_calls.append("workflow"),
    )
    monkeypatch.setattr(
        smoke_test,
        "run_admin_smoke_scenarios",
        lambda _context: scenario_calls.append("admin"),
    )

    result = smoke_test.run_smoke_test(project_id=_PROJECT_ID, lane="backend")

    assert result == 1
    assert scenario_calls == []
    assert session.task_payloads == []
    assert "preflight" in capsys.readouterr().out.lower()
