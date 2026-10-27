from __future__ import annotations

from core.application.tooling.testing import runtime_env as runtime_env_mod
from core.application.tooling.testing.runtime_env import (
    apply_test_lane_defaults,
    resolve_base_url,
    resolve_test_env,
    resolve_ws_url,
)


def test_resolve_ui_lane_prefers_dedicated_ui_project_and_admin_credentials():
    env = {
        "TEST_PROJECT_UI_ID": "230",
        "TEST_PROJECT_UI_NAME": "Test Project UI",
        "E2E_ADMIN_USERNAME": "ui_admin",
        "E2E_ADMIN_PASSWORD": "secret",
        "E2E_ADMIN_DISPLAY_NAME": "UI Admin",
    }

    resolved = resolve_test_env(env, lane="ui")

    assert resolved.lane == "ui"
    assert resolved.role == "admin"
    assert resolved.project_id == "230"
    assert resolved.project_name == "Test Project UI"
    assert resolved.username == "ui_admin"
    assert resolved.password == "secret"
    assert resolved.display_name == "UI Admin"


def test_resolve_backend_lane_uses_role_specific_credentials():
    env = {
        "TEST_PROJECT_BACKEND_ID": "231",
        "TEST_PROJECT_BACKEND_NAME": "Test Project Backend",
        "E2E_BACKEND_ROLE": "operator",
        "E2E_OPERATOR_USERNAME": "api_operator",
        "E2E_OPERATOR_PASSWORD": "operator-secret",
        "E2E_OPERATOR_DISPLAY_NAME": "API Operator",
    }

    resolved = resolve_test_env(env, lane="backend")

    assert resolved.lane == "backend"
    assert resolved.role == "operator"
    assert resolved.project_id == "231"
    assert resolved.project_name == "Test Project Backend"
    assert resolved.username == "api_operator"
    assert resolved.password == "operator-secret"
    assert resolved.display_name == "API Operator"


def test_apply_test_lane_defaults_sets_legacy_aliases():
    env = {
        "TEST_PROJECT_BACKEND_ID": "231",
        "TEST_PROJECT_BACKEND_NAME": "Test Project Backend",
        "E2E_VIEWER_USERNAME": "readonly",
        "E2E_VIEWER_PASSWORD": "readonly-secret",
        "E2E_BACKEND_ROLE": "viewer",
    }

    resolved = apply_test_lane_defaults(env, lane="backend")

    assert resolved.role == "viewer"
    assert resolved.lane == "backend"
    assert resolved.project_id == "231"
    assert resolved.project_name == "Test Project Backend"
    assert "TEST_PROJECT_ID" not in env
    assert "TEST_PROJECT_NAME" not in env
    assert "E2E_AUTH_USERNAME" not in env
    assert "E2E_AUTH_PASSWORD" not in env


def test_apply_test_lane_defaults_respects_explicit_base_and_ws_urls():
    env = {
        "TEST_PROJECT_BACKEND_ID": "231",
        "TEST_PROJECT_BACKEND_NAME": "Test Project Backend",
        "APP_BASE_URL": "https://localhost:8099",
        "FS_SERVE_SSL": "0",
    }

    apply_test_lane_defaults(env, lane="backend")

    assert env["APP_BASE_URL"] == "https://localhost:8099"
    assert env["WS_URL"] == "wss://localhost:8099"


def test_resolve_backend_lane_requires_backend_specific_names():
    env = {
        "TEST_PROJECT_BACKEND_ID": "1",
        "TEST_PROJECT_BACKEND_NAME": "Backend Project",
        "E2E_OPERATOR_USERNAME": "backend_operator",
        "E2E_OPERATOR_PASSWORD": "backend-secret",
        "E2E_OPERATOR_DISPLAY_NAME": "Backend Operator",
        "E2E_BACKEND_ROLE": "operator",
    }

    resolved = resolve_test_env(env, lane="backend")

    assert resolved.lane == "backend"
    assert resolved.project_id == "1"
    assert resolved.project_name == "Backend Project"
    assert resolved.username == "backend_operator"
    assert resolved.password == "backend-secret"
    assert resolved.display_name == "Backend Operator"


def test_resolve_base_url_respects_explicit_https_even_when_ssl_disabled(monkeypatch):
    env = {
        "APP_BASE_URL": "https://localhost:8099",
        "TEST_RUNNER_BASE_URL": "http://fs-app:8099",
        "FS_SERVE_SSL": "0",
    }
    monkeypatch.setattr(runtime_env_mod, "_running_in_container", lambda _env=None: False)
    assert resolve_base_url(env) == "https://localhost:8099"


def test_resolve_base_url_prefers_test_runner_base_url_over_loopback_app_base_in_container(
    monkeypatch,
):
    env = {
        "APP_BASE_URL": "https://localhost:8099",
        "TEST_RUNNER_BASE_URL": "http://fs-app:8099",
        "FS_SERVE_SSL": "0",
    }
    monkeypatch.setattr(runtime_env_mod, "_running_in_container", lambda _env=None: True)
    assert resolve_base_url(env) == "http://fs-app:8099"


def test_resolve_base_url_prefers_test_runner_base_url_when_app_base_url_only_exists_in_dotenv(
    monkeypatch,
):
    env = {
        "TEST_RUNNER_BASE_URL": "http://fs-app:8099",
        "FS_SERVE_SSL": "0",
    }

    monkeypatch.setattr(runtime_env_mod, "_running_in_container", lambda _env=None: False)
    assert resolve_base_url(env) == "http://localhost:8099"


def test_resolve_base_url_keeps_https_when_ssl_enabled():
    env = {
        "APP_BASE_URL": "https://localhost:8099",
        "FS_SERVE_SSL": "1",
    }

    assert resolve_base_url(env) == "https://localhost:8099"


def test_resolve_ws_url_derives_plain_ws_from_plain_http_base():
    env = {
        "APP_BASE_URL": "https://localhost:8099",
        "FS_SERVE_SSL": "0",
    }

    assert resolve_ws_url(env) == "wss://localhost:8099"


def test_resolve_base_url_downgrades_default_localhost_https_when_ssl_disabled(monkeypatch):
    env = {"FS_SERVE_SSL": "0"}
    monkeypatch.setattr(runtime_env_mod, "_running_in_container", lambda _env=None: False)
    assert resolve_base_url(env) == "http://localhost:8099"


def test_resolve_ws_url_downgrades_default_localhost_wss_when_ssl_disabled(monkeypatch):
    env = {"FS_SERVE_SSL": "0"}
    monkeypatch.setattr(runtime_env_mod, "_running_in_container", lambda _env=None: False)
    assert resolve_ws_url(env) == "ws://localhost:8099"


def test_resolve_base_url_prefers_internal_compose_url_when_running_in_container_and_ssl_disabled(
    monkeypatch,
):
    env = {"FS_SERVE_SSL": "0"}
    monkeypatch.setattr(runtime_env_mod, "_running_in_container", lambda _env=None: True)
    assert resolve_base_url(env) == "http://fs-app:8099"


def test_apply_test_lane_defaults_sets_internal_compose_urls_when_running_in_container(monkeypatch):
    env = {
        "FS_SERVE_SSL": "0",
        "TEST_PROJECT_BACKEND_ID": "231",
        "TEST_PROJECT_BACKEND_NAME": "Test Project Backend",
    }
    monkeypatch.setattr(runtime_env_mod, "_running_in_container", lambda _env=None: True)
    apply_test_lane_defaults(env, lane="backend")

    assert env["APP_BASE_URL"] == "http://fs-app:8099"
    assert env["WS_URL"] == "ws://fs-app:8099"


def test_resolve_base_url_rewrites_dotenv_localhost_to_internal_compose_host_in_container(
    monkeypatch,
):
    monkeypatch.setattr(runtime_env_mod, "_running_in_container", lambda _env=None: True)
    monkeypatch.setattr(
        runtime_env_mod,
        "_read_dotenv",
        lambda path=None: {
            "APP_BASE_URL": "https://localhost:8099",
            "FS_SERVE_SSL": "0",
        },
    )
    assert resolve_base_url({}, include_dotenv=True) == "http://fs-app:8099"
