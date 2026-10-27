from __future__ import annotations

from typing import Any

import pytest
import requests
from pydantic import ValidationError

from core.application.tooling.contracts import ToolRunResult
from core.application.tooling.testing import cutover_smoke, live_auth
from core.application.tooling.testing.models import (
    TestingCutoverSmokeRequest as CutoverSmokeRequest,
)
from core.application.tooling.testing.service import _classify_cutover_failure


class _Response:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.content = b"{}"
        self.text = "response body"

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _AuthenticatedApi:
    def __init__(
        self,
        *,
        me_status: int = 200,
        workflow_input_properties: dict[str, Any] | None = None,
        workflow_input_required: list[str] | None = None,
    ) -> None:
        self.me_status = me_status
        self.workflow_input_properties = (
            {"topic": {"type": "string"}}
            if workflow_input_properties is None
            else workflow_input_properties
        )
        self.workflow_input_required = list(workflow_input_required or [])
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/api/auth/me"):
            return _Response(self.me_status, {"id": "admin-1"})
        if url.endswith("/api/projects/project-1/workflows"):
            return _Response(
                200,
                {
                    "operator_default_workflow_id": "wf.current",
                    "project_workflows": [
                        {
                            "workflow_id": "wf.current",
                            "enabled": True,
                            "availability_status": "available",
                            "new_runs_blocked": False,
                        }
                    ],
                },
            )
        if url.endswith("/api/projects/project-1/workflows/wf.current/contract"):
            return _Response(
                200,
                {
                    "interface": {
                        "input_schema": {
                            "type": "object",
                            "properties": self.workflow_input_properties,
                            "required": self.workflow_input_required,
                        }
                    }
                },
            )
        if "/api/jobs/" in url and url.endswith("/messages"):
            return _Response(
                200,
                {
                    "messages": [
                        {"status": "completed", "from_agent": "designer"},
                        {"status": "completed", "from_agent": "composer"},
                        {"status": "completed", "from_agent": "proofreader"},
                    ]
                },
            )
        if "/api/jobs/" in url:
            return _Response(200, {"status": "completed", "thread_id": "thread-1"})
        raise AssertionError(f"Unexpected GET {url}")

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/api/auth/login"):
            self.cookies["fs_csrf"] = "csrf-token"
            return _Response(200, {"status": "authenticated"})
        if url.endswith("/api/workflow-runs"):
            return _Response(202, {"job_id": "job-1"})
        raise AssertionError(f"Unexpected POST {url}")


def test_live_password_auth_retries_with_the_only_active_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _AuthenticatedApi()
    login_payloads: list[dict[str, Any]] = []

    def _post(url: str, **kwargs: Any) -> _Response:
        assert url == "https://flow.test/api/auth/login"
        payload = dict(kwargs["json"])
        login_payloads.append(payload)
        if len(login_payloads) == 1:
            return _Response(
                409,
                {
                    "requires_account_selection": True,
                    "account_options": [
                        {
                            "account_id": "account-only",
                            "is_active": True,
                            "is_default": False,
                        }
                    ],
                },
            )
        return _Response(200, {"status": "authenticated"})

    api.post = _post  # type: ignore[method-assign]
    monkeypatch.setattr(live_auth.requests, "Session", lambda: api)

    session = live_auth.authenticated_session(
        base_url="https://flow.test",
        verify_ssl=True,
        username="admin@example.test",
        password="protected-password",  # pragma: allowlist secret
        bearer_token="",
    )

    assert session is api
    assert login_payloads == [
        {
            "email": "admin@example.test",
            "password": "protected-password",  # pragma: allowlist secret
        },
        {
            "email": "admin@example.test",
            "password": "protected-password",  # pragma: allowlist secret
            "account_id": "account-only",
        },
    ]


def test_mutating_live_runner_contracts_require_one_explicit_auth_method() -> None:
    with pytest.raises(ValidationError, match="authentication is required"):
        CutoverSmokeRequest(project_id="project-1")
    with pytest.raises(ValidationError, match="either username/password or bearer token"):
        CutoverSmokeRequest(
            project_id="project-1",
            username="admin@example.test",
            password="password",  # pragma: allowlist secret
            bearer_token="token",
        )

    assert (
        CutoverSmokeRequest(project_id="project-1", bearer_token="service-token").bearer_token
        == "service-token"
    )


def test_live_runner_command_secrets_use_only_protected_inputs() -> None:
    from core.application.management.command_specs.testing import TESTING_COMMAND_SPECS

    specs = {spec.command_id: spec for spec in TESTING_COMMAND_SPECS}
    fields = {field.key: field for field in specs["testing.cutover-smoke"].fields}
    assert fields["username"].option_name == "--username"
    for key in ("password", "bearer_token"):
        assert fields[key].secret is True
        assert fields[key].allow_secret_file is True
        assert fields[key].allow_secret_stdin is True
        assert fields[key].option_name is None


@pytest.mark.parametrize(
    ("diagnostic", "expected_code"),
    [
        ("Authentication failed with HTTP 401", "authentication_failed"),
        ("HTTP 403 forbidden", "scope_forbidden"),
        ("connection error", "transport_unavailable"),
        ("no linked, enabled workflow", "workflow_precondition_failed"),
        ("job did not complete", "runtime_timeout"),
    ],
)
def test_cutover_wrapper_reports_typed_actionable_failures(
    diagnostic: str, expected_code: str
) -> None:
    result = _classify_cutover_failure(
        ToolRunResult(
            operation="testing.cutover-smoke",
            status="failed",
            exit_code=70,
            stderr=diagnostic,
        )
    )

    assert result.details["error_code"] == expected_code
    assert result.details["remediation"]


def test_cutover_bearer_session_is_verified_before_task_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _AuthenticatedApi()
    monkeypatch.setattr(cutover_smoke.requests, "Session", lambda: api)

    result = cutover_smoke.run_smoke(
        base_url="https://flow.test",
        verify_ssl=True,
        project_id="project-1",
        summary="authenticated smoke",
        timeout_seconds=30,
        poll_seconds=1,
        username="",
        password="",
        bearer_token="service-token",
    )

    assert result == 0
    assert [(method, url.rsplit("/api/", 1)[-1]) for method, url, _ in api.calls[:4]] == [
        ("GET", "auth/me"),
        ("GET", "projects/project-1/workflows"),
        ("GET", "projects/project-1/workflows/wf.current/contract"),
        ("POST", "workflow-runs"),
    ]
    assert api.headers == {"Authorization": "Bearer service-token"}


def test_cutover_submission_fills_required_text_inputs_with_the_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _AuthenticatedApi(
        workflow_input_properties={
            "topic": {"type": "string"},
            "count": {"type": "integer"},
            "notes": {"type": "string"},
        },
        workflow_input_required=["topic", "count"],
    )
    monkeypatch.setattr(cutover_smoke.requests, "Session", lambda: api)

    result = cutover_smoke.run_smoke(
        base_url="https://flow.test",
        verify_ssl=True,
        project_id="project-1",
        summary="authenticated smoke",
        timeout_seconds=30,
        poll_seconds=1,
        username="",
        password="",
        bearer_token="service-token",
    )

    assert result == 0
    submit = next(
        call for call in api.calls if call[:2] == ("POST", "https://flow.test/api/workflow-runs")
    )
    assert submit[2]["json"]["inputs"] == {"topic": "authenticated smoke"}
    assert "mode" not in submit[2]["json"]
    assert not {"task_id", "task_summary"} & submit[2]["json"].keys()


def test_cutover_omits_inputs_when_workflow_requires_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _AuthenticatedApi(workflow_input_properties={})
    monkeypatch.setattr(cutover_smoke.requests, "Session", lambda: api)

    result = cutover_smoke.run_smoke(
        base_url="https://flow.test",
        verify_ssl=True,
        project_id="project-1",
        summary="authenticated smoke",
        timeout_seconds=30,
        poll_seconds=1,
        bearer_token="service-token",
    )

    assert result == 0
    submit = next(
        call for call in api.calls if call[:2] == ("POST", "https://flow.test/api/workflow-runs")
    )
    assert "inputs" not in submit[2]["json"]


def test_cutover_job_poll_retries_transient_connection_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _AuthenticatedApi()
    original_get = api.get
    job_reads = 0

    def transient_get(url: str, **kwargs: Any) -> _Response:
        nonlocal job_reads
        if "/api/jobs/" in url and not url.endswith("/messages"):
            job_reads += 1
            if job_reads == 1:
                raise requests.ConnectionError("connection reset by peer")
        return original_get(url, **kwargs)

    api.get = transient_get  # type: ignore[method-assign]
    monkeypatch.setattr(cutover_smoke.requests, "Session", lambda: api)
    monkeypatch.setattr(cutover_smoke.time, "sleep", lambda _seconds: None)

    result = cutover_smoke.run_smoke(
        base_url="https://flow.test",
        verify_ssl=True,
        project_id="project-1",
        summary="authenticated smoke",
        timeout_seconds=30,
        poll_seconds=1,
        username="",
        password="",
        bearer_token="service-token",
    )

    assert result == 0
    assert job_reads >= 2


def test_cutover_local_login_is_verified_and_csrf_protects_task_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _AuthenticatedApi()
    monkeypatch.setattr(cutover_smoke.requests, "Session", lambda: api)

    result = cutover_smoke.run_smoke(
        base_url="https://flow.test",
        verify_ssl=True,
        project_id="project-1",
        summary="authenticated smoke",
        timeout_seconds=30,
        poll_seconds=1,
        username="admin@example.test",
        password="local-password",  # pragma: allowlist secret
        bearer_token="",
    )

    assert result == 0
    assert [(method, url.rsplit("/api/", 1)[-1]) for method, url, _ in api.calls[:5]] == [
        ("POST", "auth/login"),
        ("GET", "auth/me"),
        ("GET", "projects/project-1/workflows"),
        ("GET", "projects/project-1/workflows/wf.current/contract"),
        ("POST", "workflow-runs"),
    ]
    assert api.calls[4][2]["headers"] == {"x-csrf-token": "csrf-token"}


def test_cutover_rejects_obsolete_mode_instead_of_silently_ignoring_it() -> None:
    with pytest.raises(ValidationError, match="mode"):
        CutoverSmokeRequest(
            project_id="project-1",
            bearer_token="synthetic-test-token",
            mode="greenfield",
        )
