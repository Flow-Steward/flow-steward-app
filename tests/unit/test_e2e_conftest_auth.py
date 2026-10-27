from __future__ import annotations

import importlib
from unittest.mock import Mock

import pytest
import requests


def test_e2e_request_reauthenticates_and_retries_once_on_401(monkeypatch):
    e2e_conftest = importlib.import_module("tests.e2e.conftest")

    first_session = Mock()
    first_session.request.return_value = Mock(status_code=401)
    second_session = Mock()
    second_session.request.return_value = Mock(status_code=200)

    calls: list[tuple[str, bool]] = []

    def fake_authed_session():
        if not calls:
            calls.append(("first", True))
            return first_session, False
        calls.append(("second", True))
        return second_session, False

    monkeypatch.setattr(e2e_conftest, "_authed_session", fake_authed_session)
    monkeypatch.setattr(e2e_conftest, "_env_flag", lambda name, default=False: True)
    monkeypatch.setattr(e2e_conftest, "_AUTH_SESSION", object())

    response = e2e_conftest.e2e_request("GET", "http://example.test/api/jobs/1")

    assert response.status_code == 200
    assert first_session.request.call_count == 1
    assert second_session.request.call_count == 1
    assert calls == [("first", True), ("second", True)]


def test_e2e_request_does_not_retry_401_when_auth_enforcement_disabled(monkeypatch):
    e2e_conftest = importlib.import_module("tests.e2e.conftest")

    first_session = Mock()
    first_session.request.return_value = Mock(status_code=401)
    second_session = Mock()
    second_session.request.return_value = Mock(status_code=200)

    calls: list[str] = []

    def fake_authed_session():
        calls.append("authed")
        return first_session, False

    def fake_env_flag(name: str, default: bool = False) -> bool:
        if name == "E2E_ENFORCE_AUTH":
            return False
        return default

    monkeypatch.setattr(e2e_conftest, "_authed_session", fake_authed_session)
    monkeypatch.setattr(e2e_conftest, "_env_flag", fake_env_flag)
    monkeypatch.setattr(e2e_conftest, "_AUTH_SESSION", object())

    response = e2e_conftest.e2e_request("GET", "http://example.test/api/jobs/1")

    assert response.status_code == 401
    assert first_session.request.call_count == 1
    assert second_session.request.call_count == 0
    assert calls == ["authed"]


def test_e2e_request_retries_transient_connection_error_for_get(monkeypatch):
    e2e_conftest = importlib.import_module("tests.e2e.conftest")

    session = Mock()
    session.request.side_effect = [
        requests.exceptions.ConnectionError("Connection reset by peer"),
        Mock(status_code=200),
    ]

    monkeypatch.setattr(e2e_conftest, "_authed_session", lambda: (session, False))
    monkeypatch.setattr(e2e_conftest, "_env_flag", lambda name, default=False: True)
    monkeypatch.setattr(e2e_conftest.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("E2E_HTTP_RETRY_COUNT", "2")

    response = e2e_conftest.e2e_request("GET", "http://example.test/api/jobs/1")

    assert response.status_code == 200
    assert session.request.call_count == 2


def test_e2e_request_does_not_retry_connection_error_for_post(monkeypatch):
    e2e_conftest = importlib.import_module("tests.e2e.conftest")

    session = Mock()
    session.request.side_effect = requests.exceptions.ConnectionError("Connection reset by peer")

    monkeypatch.setattr(e2e_conftest, "_authed_session", lambda: (session, False))
    monkeypatch.setattr(e2e_conftest.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("E2E_HTTP_RETRY_COUNT", "2")

    with pytest.raises(requests.exceptions.ConnectionError):
        e2e_conftest.e2e_request("POST", "http://example.test/api/workflow-runs", json={"x": 1})

    assert session.request.call_count == 1


def test_e2e_request_retries_transient_503_for_get(monkeypatch):
    e2e_conftest = importlib.import_module("tests.e2e.conftest")

    session = Mock()
    session.request.side_effect = [
        Mock(status_code=503),
        Mock(status_code=200),
    ]

    monkeypatch.setattr(e2e_conftest, "_authed_session", lambda: (session, False))
    monkeypatch.setattr(e2e_conftest.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("E2E_HTTP_RETRY_COUNT", "2")

    response = e2e_conftest.e2e_request("GET", "http://example.test/api/jobs/1")

    assert response.status_code == 200
    assert session.request.call_count == 2


def test_e2e_request_does_not_retry_503_for_post(monkeypatch):
    e2e_conftest = importlib.import_module("tests.e2e.conftest")

    session = Mock()
    session.request.side_effect = [Mock(status_code=503)]

    monkeypatch.setattr(e2e_conftest, "_authed_session", lambda: (session, False))
    monkeypatch.setattr(e2e_conftest.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("E2E_HTTP_RETRY_COUNT", "2")

    response = e2e_conftest.e2e_request(
        "POST", "http://example.test/api/workflow-runs", json={"x": 1}
    )

    assert response.status_code == 503
    assert session.request.call_count == 1


def test_ensure_local_auth_session_retries_transient_login_connection_error(monkeypatch):
    e2e_conftest = importlib.import_module("tests.e2e.conftest")

    session = Mock()
    session.post.side_effect = [
        requests.exceptions.ConnectionError("Connection reset by peer"),
        Mock(status_code=200, text="ok"),
    ]

    monkeypatch.setattr(e2e_conftest.requests, "Session", lambda: session)
    monkeypatch.setattr(
        e2e_conftest,
        "_build_seed_credentials",
        lambda: {
            "username": "e2e_admin",
            "password": "secret",
            "display_name": "E2E Admin",
        },
    )
    monkeypatch.setattr(e2e_conftest, "_e2e_http_retry_count", lambda: 2)
    monkeypatch.setattr(e2e_conftest.time, "sleep", lambda *_args, **_kwargs: None)

    resolved = e2e_conftest._ensure_local_auth_session(
        base_url="http://example.test",
        verify_ssl=False,
    )

    assert resolved is session
    assert session.post.call_count == 2


def test_ensure_local_auth_session_selects_default_active_account(monkeypatch):
    e2e_conftest = importlib.import_module("tests.e2e.conftest")

    account_selection = Mock(status_code=409, text="account selection required")
    account_selection.json.return_value = {
        "requires_account_selection": True,
        "account_options": [
            {"account_id": "inactive", "is_active": False, "is_default": True},
            {"account_id": "selected-account", "is_active": True, "is_default": True},
            {"account_id": "other-account", "is_active": True, "is_default": False},
        ],
    }
    authenticated = Mock(status_code=200, text="ok")
    authenticated.json.return_value = {}
    session = Mock()
    session.post.side_effect = [account_selection, authenticated]

    monkeypatch.setattr(e2e_conftest.requests, "Session", lambda: session)
    monkeypatch.setattr(
        e2e_conftest,
        "_build_seed_credentials",
        lambda: {
            "username": "e2e_admin",
            "password": "secret",
            "display_name": "E2E Admin",
        },
    )
    monkeypatch.setattr(e2e_conftest, "_e2e_http_retry_count", lambda: 0)

    resolved = e2e_conftest._ensure_local_auth_session(
        base_url="http://example.test",
        verify_ssl=False,
    )

    assert resolved is session
    assert session.post.call_count == 2
    assert session.post.call_args_list[1].kwargs["json"] == {
        "email": "e2e_admin",
        "password": "secret",
        "account_id": "selected-account",
    }


def test_ensure_local_auth_session_retries_transient_bootstrap_connection_error(monkeypatch):
    e2e_conftest = importlib.import_module("tests.e2e.conftest")

    session = Mock()
    session.post.side_effect = [
        Mock(status_code=401, text="unauthorized"),
        requests.exceptions.ConnectionError("Connection reset by peer"),
        Mock(status_code=200, text="bootstrapped"),
        Mock(status_code=200, text="ok"),
    ]

    monkeypatch.setattr(e2e_conftest.requests, "Session", lambda: session)
    monkeypatch.setattr(
        e2e_conftest,
        "_build_seed_credentials",
        lambda: {
            "username": "e2e_admin",
            "password": "secret",
            "display_name": "E2E Admin",
        },
    )
    monkeypatch.setattr(e2e_conftest, "_bootstrap_headers", dict)
    monkeypatch.setattr(e2e_conftest, "_e2e_http_retry_count", lambda: 2)
    monkeypatch.setattr(e2e_conftest.time, "sleep", lambda *_args, **_kwargs: None)

    resolved = e2e_conftest._ensure_local_auth_session(
        base_url="http://example.test",
        verify_ssl=False,
    )

    assert resolved is session
    assert session.post.call_count == 4


def test_ensure_local_auth_session_uses_canonical_api_auth_paths_only(monkeypatch):
    e2e_conftest = importlib.import_module("tests.e2e.conftest")

    session = Mock()
    session.post.side_effect = [
        Mock(status_code=404, text="not found"),  # /api/auth/login
        Mock(status_code=404, text="not found"),  # /api/auth/bootstrap
        Mock(status_code=200, text="ok"),  # /api/auth/login after DB seed
    ]

    monkeypatch.setattr(e2e_conftest.requests, "Session", lambda: session)
    monkeypatch.setattr(
        e2e_conftest,
        "_build_seed_credentials",
        lambda: {
            "username": "e2e_admin",
            "password": "secret",
            "display_name": "E2E Admin",
        },
    )
    monkeypatch.setattr(e2e_conftest, "_bootstrap_headers", dict)
    monkeypatch.setattr(e2e_conftest, "_e2e_http_retry_count", lambda: 0)
    monkeypatch.setattr(e2e_conftest, "_env_flag", lambda name, default=False: True)
    monkeypatch.setattr(e2e_conftest, "_seed_user_via_db", lambda _creds: True)

    resolved = e2e_conftest._ensure_local_auth_session(
        base_url="http://example.test",
        verify_ssl=False,
    )

    assert resolved is session
    called_urls = [call.args[0] for call in session.post.call_args_list]
    assert called_urls == [
        "http://example.test/api/auth/login",
        "http://example.test/api/auth/bootstrap",
        "http://example.test/api/auth/login",
    ]


def test_ensure_local_auth_session_uses_db_seed_when_bootstrap_returns_404(monkeypatch):
    e2e_conftest = importlib.import_module("tests.e2e.conftest")

    session = Mock()
    session.post.side_effect = [
        Mock(status_code=401, text="unauthorized"),  # login pre-check
        Mock(status_code=404, text="not found"),  # /api/auth/bootstrap
        Mock(status_code=200, text="ok"),  # login after DB seed
    ]

    monkeypatch.setattr(e2e_conftest.requests, "Session", lambda: session)
    monkeypatch.setattr(
        e2e_conftest,
        "_build_seed_credentials",
        lambda: {
            "username": "e2e_admin",
            "password": "secret",
            "display_name": "E2E Admin",
        },
    )
    monkeypatch.setattr(e2e_conftest, "_bootstrap_headers", dict)
    monkeypatch.setattr(e2e_conftest, "_e2e_http_retry_count", lambda: 0)
    monkeypatch.setattr(e2e_conftest, "_env_flag", lambda name, default=False: True)
    seed_called = {"value": False}

    def _seed(_creds):
        seed_called["value"] = True
        return True

    monkeypatch.setattr(e2e_conftest, "_seed_user_via_db", _seed)

    resolved = e2e_conftest._ensure_local_auth_session(
        base_url="http://example.test",
        verify_ssl=False,
    )

    assert resolved is session
    assert seed_called["value"] is True
    assert session.post.call_count == 3


def test_seed_user_via_db_uses_resolved_e2e_dsn_when_env_missing(monkeypatch):
    import sys

    e2e_conftest = importlib.import_module("tests.e2e.conftest")

    recorded: dict[str, object] = {}

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            recorded.setdefault("queries", []).append((query, params))

        def fetchone(self):
            queries = recorded.get("queries") or []
            if len(queries) == 1:
                return None
            if len(queries) == 2:
                return (7,)
            return (1,)

    class _Conn:
        autocommit = False

        def cursor(self):
            return _Cursor()

        def commit(self):
            recorded["committed"] = True

        def close(self):
            recorded["closed"] = True

    class _Psycopg:
        @staticmethod
        def connect(dsn):
            recorded["dsn"] = dsn
            return _Conn()

    monkeypatch.delenv("E2E_AUTH_DSN", raising=False)
    monkeypatch.delenv("FS_DB_DSN", raising=False)
    monkeypatch.setattr(e2e_conftest, "_resolve_e2e_dsn", lambda: "dbname=test host=127.0.0.1")
    monkeypatch.setattr(e2e_conftest, "_resolve_seed_role_key", lambda _creds: "admin")
    monkeypatch.setattr(e2e_conftest, "_password_hash", lambda _password: "hash")
    monkeypatch.setitem(sys.modules, "psycopg", _Psycopg)

    result = e2e_conftest._seed_user_via_db(
        {
            "username": "e2e_admin@example.com",
            "password": "secret",
            "display_name": "E2E Admin",
        }
    )

    assert result is True
    assert recorded["dsn"] == "dbname=test host=127.0.0.1"
    assert recorded["committed"] is True
    assert recorded["closed"] is True
    executed_sql = "\n".join(str(query) for query, _params in recorded["queries"])
    assert "SELECT id" in executed_sql
    assert "WHERE username = %s" in executed_sql
    assert "INSERT INTO users" in executed_sql
    assert "ON CONFLICT (username)" not in executed_sql


def test_resolve_seed_role_key_prefers_explicit_backend_role(monkeypatch):
    e2e_conftest = importlib.import_module("tests.e2e.conftest")
    creds = {"username": "e2e_viewer", "password": "x", "display_name": "Viewer"}

    monkeypatch.setenv("E2E_BACKEND_ROLE", "operator")
    monkeypatch.setenv("E2E_ADMIN_USERNAME", "e2e_admin")
    monkeypatch.setenv("E2E_OPERATOR_USERNAME", "e2e_operator")
    monkeypatch.setenv("E2E_VIEWER_USERNAME", "e2e_viewer")

    assert e2e_conftest._resolve_seed_role_key(creds) == "operator"


def test_resolve_seed_role_key_infers_role_from_username(monkeypatch):
    e2e_conftest = importlib.import_module("tests.e2e.conftest")
    creds = {"username": "e2e_viewer", "password": "x", "display_name": "Viewer"}

    monkeypatch.delenv("E2E_BACKEND_ROLE", raising=False)
    monkeypatch.setenv("E2E_ADMIN_USERNAME", "e2e_admin")
    monkeypatch.setenv("E2E_OPERATOR_USERNAME", "e2e_operator")
    monkeypatch.setenv("E2E_VIEWER_USERNAME", "e2e_viewer")

    assert e2e_conftest._resolve_seed_role_key(creds) == "viewer"


def test_resolve_seed_role_key_defaults_to_admin(monkeypatch):
    e2e_conftest = importlib.import_module("tests.e2e.conftest")
    creds = {"username": "unknown_user", "password": "x", "display_name": "Unknown"}

    monkeypatch.setenv("E2E_BACKEND_ROLE", "invalid-role")
    monkeypatch.setenv("E2E_ADMIN_USERNAME", "e2e_admin")
    monkeypatch.setenv("E2E_OPERATOR_USERNAME", "e2e_operator")
    monkeypatch.setenv("E2E_VIEWER_USERNAME", "e2e_viewer")

    assert e2e_conftest._resolve_seed_role_key(creds) == "admin"


def test_sync_test_project_ids_preserves_explicit_env_backed_project_id(monkeypatch):
    e2e_conftest = importlib.import_module("tests.e2e.conftest")

    closed = {"value": False}

    class _Conn:
        def close(self):
            closed["value"] = True

    ensure_calls: list[str] = []
    monkeypatch.setattr(e2e_conftest, "_connect_e2e_db", lambda: _Conn())
    monkeypatch.setattr(e2e_conftest, "_project_exists", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(e2e_conftest, "_project_id_by_name", lambda *_args, **_kwargs: "db-name-id")
    monkeypatch.setattr(
        e2e_conftest,
        "_ensure_project_id_by_name",
        lambda project_name: ensure_calls.append(project_name) or "created-id",
    )
    materialize_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        e2e_conftest,
        "_materialize_configured_project_id",
        lambda *_args, **_kwargs: materialize_calls.append(("called", "called")) or True,
    )

    env = {
        "TEST_PROJECT_BACKEND_ID": "env-backed-id",
        "TEST_PROJECT_BACKEND_NAME": "test-project",
    }

    e2e_conftest._sync_test_project_ids(env)

    assert env["TEST_PROJECT_BACKEND_ID"] == "env-backed-id"
    assert ensure_calls == []
    assert materialize_calls == []
    assert closed["value"] is True


def test_sync_test_project_ids_resolves_existing_project_by_name_without_autocreate(monkeypatch):
    e2e_conftest = importlib.import_module("tests.e2e.conftest")

    closed = {"value": False}

    class _Conn:
        def close(self):
            closed["value"] = True

    ensure_calls: list[str] = []
    monkeypatch.setattr(e2e_conftest, "_connect_e2e_db", lambda: _Conn())
    monkeypatch.setattr(
        e2e_conftest, "_project_id_by_name", lambda *_args, **_kwargs: "resolved-existing-id"
    )
    monkeypatch.setattr(
        e2e_conftest,
        "_ensure_project_id_by_name",
        lambda project_name: ensure_calls.append(project_name) or "created-id",
    )

    env = {
        "TEST_PROJECT_BACKEND_NAME": "test-project",
    }

    e2e_conftest._sync_test_project_ids(env)

    assert env["TEST_PROJECT_BACKEND_ID"] == "resolved-existing-id"
    assert ensure_calls == []
    assert closed["value"] is True


def test_sync_test_project_ids_restores_or_creates_stale_explicit_project_id(monkeypatch):
    e2e_conftest = importlib.import_module("tests.e2e.conftest")

    closed = {"value": False}

    class _Conn:
        def close(self):
            closed["value"] = True

    monkeypatch.setattr(e2e_conftest, "_connect_e2e_db", lambda: _Conn())
    monkeypatch.setattr(e2e_conftest, "_project_exists", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(e2e_conftest, "_project_id_by_name", lambda *_args, **_kwargs: "")
    ensure_calls: list[str] = []
    monkeypatch.setattr(
        e2e_conftest,
        "_ensure_project_id_by_name",
        lambda project_name: ensure_calls.append(project_name) or "",
    )
    materialize_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        e2e_conftest,
        "_materialize_configured_project_id",
        lambda _conn, project_id, project_name: (
            materialize_calls.append((project_id, project_name)) or True
        ),
    )

    env = {
        "TEST_PROJECT_BACKEND_ID": "stale-id",
        "TEST_PROJECT_BACKEND_NAME": "test-project",
    }

    e2e_conftest._sync_test_project_ids(env)

    assert env["TEST_PROJECT_BACKEND_ID"] == "stale-id"
    assert materialize_calls == [("stale-id", "test-project")]
    assert ensure_calls == []
    assert closed["value"] is True
