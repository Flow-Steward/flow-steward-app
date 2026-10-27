from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import requests

from core.application.tooling.testing import nightfire_regression


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "", payload: object = None) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _FakeSession:
    def __init__(self) -> None:
        self.cookies = {"fs_csrf": "csrf-token"}
        self.root_calls = 0
        self.login_calls = 0

    def request(self, method: str, url: str, **kwargs) -> _FakeResponse:
        if method == "GET" and url.endswith("/"):
            self.root_calls += 1
            if self.root_calls == 1:
                raise requests.ReadTimeout("warmup timeout")
            return _FakeResponse(302, "")
        if method == "POST" and url.endswith("/api/auth/login"):
            self.login_calls += 1
            if self.login_calls == 1:
                raise requests.ReadTimeout("auth timeout")
            return _FakeResponse(200, '{"status":"ok"}')
        if method == "GET" and url.endswith("/api/auth/me"):
            return _FakeResponse(200, '{"authenticated":true}')
        if method == "POST" and url.endswith("/api/auth/logout"):
            return _FakeResponse(200, '{"status":"logged_out"}')
        raise AssertionError(f"Unexpected request: {method} {url}")


def test_run_auth_smoke_retries_transient_request_failures(monkeypatch):
    fake_session = _FakeSession()
    monkeypatch.setattr(nightfire_regression.requests, "Session", lambda: fake_session)
    monkeypatch.setattr(nightfire_regression.time, "sleep", lambda *_args, **_kwargs: None)

    code, output = nightfire_regression._run_auth_smoke(
        {
            "APP_BASE_URL": "http://localhost:8099",
            "E2E_VERIFY_SSL": "0",
            "NIGHTFIRE_LANE": "ui",
            "E2E_UI_ROLE": "admin",
            "E2E_ADMIN_USERNAME": "e2e_admin@example.com",
            "E2E_ADMIN_PASSWORD": "e2e_admin_password_123!",
            "E2E_ADMIN_DISPLAY_NAME": "E2E Admin",
        }
    )

    assert code == 0
    assert fake_session.root_calls == 2
    assert fake_session.login_calls == 2
    assert "GET / -> 302" in output
    assert "POST /api/auth/login -> 200" in output
    assert "GET /api/auth/me -> 200" in output
    assert "POST /api/auth/logout -> 200" in output


def test_run_auth_smoke_selects_the_single_default_active_account(monkeypatch):
    class _MultiAccountSession(_FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.login_payloads: list[dict[str, str]] = []

        def request(self, method: str, url: str, **kwargs) -> _FakeResponse:
            if method == "GET" and url.endswith("/"):
                return _FakeResponse(200, "")
            if method == "POST" and url.endswith("/api/auth/login"):
                payload = kwargs["json"]
                self.login_payloads.append(payload)
                if "account_id" not in payload:
                    return _FakeResponse(
                        409,
                        '{"requires_account_selection":true}',
                        {
                            "requires_account_selection": True,
                            "account_options": [
                                {
                                    "account_id": "account-secondary",
                                    "is_active": True,
                                    "is_default": False,
                                },
                                {
                                    "account_id": "account-default",
                                    "is_active": True,
                                    "is_default": True,
                                },
                            ],
                        },
                    )
                return _FakeResponse(200, '{"status":"ok"}')
            if method == "GET" and url.endswith("/api/auth/me"):
                return _FakeResponse(200, '{"authenticated":true}')
            if method == "POST" and url.endswith("/api/auth/logout"):
                return _FakeResponse(200, '{"status":"logged_out"}')
            raise AssertionError(f"Unexpected request: {method} {url}")

    fake_session = _MultiAccountSession()
    monkeypatch.setattr(nightfire_regression.requests, "Session", lambda: fake_session)

    code, output = nightfire_regression._run_auth_smoke(
        {
            "APP_BASE_URL": "http://localhost:8099",
            "E2E_VERIFY_SSL": "0",
            "NIGHTFIRE_LANE": "backend",
            "NIGHTFIRE_AUTH_ROLE": "admin",
            "E2E_ADMIN_USERNAME": "e2e_admin@example.com",
            "E2E_ADMIN_PASSWORD": "e2e_admin_password_123!",
        }
    )

    assert code == 0
    assert fake_session.login_payloads == [
        {"email": "e2e_admin@example.com", "password": "e2e_admin_password_123!"},
        {
            "email": "e2e_admin@example.com",
            "password": "e2e_admin_password_123!",
            "account_id": "account-default",
        },
    ]
    assert "POST /api/auth/login (selected account) -> 200" in output


class _ProbeResult:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


def test_resolve_pytest_command_prefers_direct_pytest(monkeypatch):
    def fake_run(cmd: list[str], **kwargs) -> _ProbeResult:
        assert kwargs["timeout"] == 15.0
        if cmd == [nightfire_regression.sys.executable, "-m", "pytest", "--version"]:
            return _ProbeResult(0)
        raise AssertionError(f"Unexpected probe: {cmd}")

    monkeypatch.setattr(nightfire_regression.subprocess, "run", fake_run)

    cmd = nightfire_regression._resolve_pytest_command({"NIGHTFIRE_LANE": "ui"})

    assert cmd == [nightfire_regression.sys.executable, "-m", "pytest"]


def test_resolve_pytest_command_falls_back_to_test_runner(monkeypatch):
    def fake_run(cmd: list[str], **kwargs) -> _ProbeResult:
        assert kwargs["timeout"] in {10.0, 15.0, 30.0}
        if cmd == [nightfire_regression.sys.executable, "-m", "pytest", "--version"]:
            return _ProbeResult(1)
        if cmd == [
            "docker",
            "compose",
            "-p",
            "flow-steward-project",
            "exec",
            "-T",
            "fs-test-runner",
            "python",
            "-m",
            "pytest",
            "--version",
        ]:
            return _ProbeResult(0)
        raise AssertionError(f"Unexpected probe: {cmd}")

    monkeypatch.setattr(
        nightfire_regression, "_resolve_compose_project_name", lambda _env: "flow-steward-project"
    )
    monkeypatch.setattr(nightfire_regression.subprocess, "run", fake_run)

    cmd = nightfire_regression._resolve_pytest_command(
        {
            "APP_BASE_URL": "http://fs-app:8099",
            "COMPOSE_PROJECT_NAME": "flow-steward-project",
            "E2E_ADMIN_PASSWORD": "secret",
            "E2E_ADMIN_USERNAME": "e2e_admin@example.com",
            "NIGHTFIRE_AUTH_ROLE": "admin",
            "NIGHTFIRE_LANE": "ui",
            "TEST_PROJECT_UI_ID": "212",
        }
    )

    assert cmd[:6] == ["docker", "compose", "-p", "flow-steward-project", "exec", "-T"]
    assert "fs-test-runner" in cmd
    assert cmd[-3:] == ["python", "-m", "pytest"]


def test_typed_run_builds_testing_group_commands_for_fuzz_and_cutover(monkeypatch, tmp_path: Path):
    captured_stages: list[tuple[str, list[str], str | None]] = []

    monkeypatch.setattr(
        nightfire_regression, "_report_path", lambda _explicit: tmp_path / "nightfire.md"
    )
    monkeypatch.setattr(
        nightfire_regression,
        "apply_test_lane_defaults",
        lambda env, lane, role, include_dotenv: SimpleNamespace(
            lane=lane,
            role=role or "admin",
            project_id=env.get("TEST_PROJECT_UI_ID", "3"),
            project_name="test-project",
            username="e2e_admin@example.com",
            password="secret",
            display_name="E2E Admin",
        ),
    )
    monkeypatch.setattr(
        nightfire_regression,
        "resolve_base_url",
        lambda env, include_dotenv=False: "http://fs-app:8099",
    )
    monkeypatch.setattr(
        nightfire_regression, "_resolve_mcp_base_url", lambda env: "http://fs-mcp:8100"
    )
    monkeypatch.setattr(
        nightfire_regression,
        "_resolve_pytest_command",
        lambda env: [nightfire_regression.sys.executable, "-m", "pytest"],
    )
    monkeypatch.setattr(nightfire_regression, "_run_auth_smoke", lambda env: (0, "auth ok"))
    monkeypatch.setattr(
        nightfire_regression, "_run_authenticated_page_smoke", lambda env: (0, "page ok")
    )

    def fake_stage(
        label: str,
        cmd: list[str],
        env: dict[str, str],
        input_text: str | None = None,
    ) -> tuple[int, str]:
        assert "E2E_ADMIN_PASSWORD" not in env
        captured_stages.append((label, cmd, input_text))
        return 0, "ok"

    monkeypatch.setattr(nightfire_regression, "_stage", fake_stage)

    exit_code = nightfire_regression.run_nightfire_regression(
        lane="ui",
        project_id="3",
        base_url="http://fs-app:8099",
        verify_ssl=False,
        auth_role="admin",
        auth_username="",
        auth_password="",
        auth_display_name="",
        source_workflow_id="",
        with_cutover_smoke=True,
        with_full_ui=False,
        fail_fast=False,
        report_path=str(tmp_path / "nightfire.md"),
    )

    assert exit_code == 0

    stage_commands = {label: cmd for label, cmd, _input_text in captured_stages}
    stage_inputs = {label: input_text for label, _cmd, input_text in captured_stages}
    for label in (
        "Fuzz API contracts",
        "Fuzz UI routes and accessibility",
        "Fuzz MCP contracts",
        "Cutover smoke",
    ):
        assert "--confirm" in stage_commands[label], label
    assert "--confirm" not in stage_commands["Cutover preflight"]
    assert stage_commands["Fuzz API contracts"][:5] == [
        nightfire_regression.sys.executable,
        "-m",
        "core.entrypoints.cli",
        "testing",
        "fuzz-api",
    ]
    assert "--username" in stage_commands["Fuzz API contracts"]
    assert "e2e_admin@example.com" in stage_commands["Fuzz API contracts"]
    assert "--password-stdin" in stage_commands["Fuzz API contracts"]
    assert stage_inputs["Fuzz API contracts"] == "secret"
    assert stage_commands["Fuzz UI routes and accessibility"][:5] == [
        nightfire_regression.sys.executable,
        "-m",
        "core.entrypoints.cli",
        "testing",
        "fuzz-ui",
    ]
    assert "--username" in stage_commands["Fuzz UI routes and accessibility"]
    assert "e2e_admin@example.com" in stage_commands["Fuzz UI routes and accessibility"]
    assert "--password-stdin" in stage_commands["Fuzz UI routes and accessibility"]
    assert stage_inputs["Fuzz UI routes and accessibility"] == "secret"
    assert stage_commands["Fuzz MCP contracts"][:5] == [
        nightfire_regression.sys.executable,
        "-m",
        "core.entrypoints.cli",
        "testing",
        "fuzz-mcp",
    ]
    assert "--username" in stage_commands["Fuzz MCP contracts"]
    assert "e2e_admin@example.com" in stage_commands["Fuzz MCP contracts"]
    assert "--password-stdin" in stage_commands["Fuzz MCP contracts"]
    assert stage_inputs["Fuzz MCP contracts"] == "secret"
    assert stage_commands["Cutover preflight"][:5] == [
        nightfire_regression.sys.executable,
        "-m",
        "core.entrypoints.cli",
        "testing",
        "cutover-preflight",
    ]
    assert stage_commands["Cutover smoke"][:5] == [
        nightfire_regression.sys.executable,
        "-m",
        "core.entrypoints.cli",
        "testing",
        "cutover-smoke",
    ]
    assert stage_inputs["Cutover preflight"] is None
    assert stage_inputs["Cutover smoke"] is None


def test_backend_lane_does_not_resolve_or_run_playwright(monkeypatch, tmp_path: Path):
    stage_labels: list[str] = []
    monkeypatch.setattr(
        nightfire_regression,
        "_report_path",
        lambda _explicit: tmp_path / "nightfire-backend.md",
    )
    monkeypatch.setattr(
        nightfire_regression,
        "apply_test_lane_defaults",
        lambda env, lane, role, include_dotenv: SimpleNamespace(
            lane=lane,
            role=role or "admin",
            project_id=env.get("TEST_PROJECT_BACKEND_ID", "project-backend"),
            project_name="backend-project",
            username="e2e_admin@example.com",
            password="secret",
            display_name="E2E Admin",
        ),
    )
    monkeypatch.setattr(
        nightfire_regression,
        "resolve_base_url",
        lambda env, include_dotenv=False: "http://fs-app:8099",
    )
    monkeypatch.setattr(
        nightfire_regression,
        "_resolve_mcp_base_url",
        lambda env: "http://fs-mcp:8100",
    )
    monkeypatch.setattr(
        nightfire_regression,
        "_resolve_pytest_command",
        lambda _env: (_ for _ in ()).throw(AssertionError("backend lane resolved pytest")),
    )
    monkeypatch.setattr(nightfire_regression, "_run_auth_smoke", lambda env: (0, "auth ok"))
    monkeypatch.setattr(
        nightfire_regression,
        "_run_authenticated_page_smoke",
        lambda env: (0, "page ok"),
    )

    def fake_stage(
        label: str,
        _cmd: list[str],
        _env: dict[str, str],
        input_text: str | None = None,
    ) -> tuple[int, str]:
        stage_labels.append(label)
        return 0, "ok"

    monkeypatch.setattr(nightfire_regression, "_stage", fake_stage)

    exit_code = nightfire_regression.run_nightfire_regression(
        lane="backend",
        project_id="project-backend",
        base_url="http://fs-app:8099",
        verify_ssl=False,
        auth_role="admin",
        auth_username="e2e_admin@example.com",
        auth_password="secret",
        auth_display_name="E2E Admin",
        source_workflow_id="",
        with_cutover_smoke=False,
        with_full_ui=False,
        fail_fast=True,
        report_path=str(tmp_path / "nightfire-backend.md"),
    )

    assert exit_code == 0
    assert "Fuzz API contracts" in stage_labels
    assert all(not label.startswith("Playwright UI") for label in stage_labels)
