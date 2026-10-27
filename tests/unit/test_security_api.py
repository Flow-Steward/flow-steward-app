"""SEC-015/016: Tests for security API endpoints.

Run:
    pytest tests/unit/test_security_api.py -v
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Minimal stub for core.entrypoints.admin.deps to avoid import failures.
# Use module-level save/restore so we don't pollute the global for other tests.
import core.entrypoints.admin.deps as _deps
from core.application.management.errors import ServiceForbiddenError

_orig_require_auth = getattr(_deps, "_require_authenticated_user_fn", None)
_orig_api_error = getattr(_deps, "_api_error_response_fn", None)
_orig_get_logger_only_fn = getattr(_deps, "_get_logger_only_fn", None)

_deps._require_authenticated_user_fn = lambda r: {"id": 1, "username": "test"}
_deps._api_error_response_fn = lambda e, **kw: None
_deps._get_logger_only_fn = lambda: None


from fastapi import FastAPI  # noqa: E402

from core.entrypoints.api.security import router  # noqa: E402


@pytest.fixture(autouse=True, scope="module")
def _restore_deps():
    """Restore core.entrypoints.admin.deps globals after this module's tests complete."""
    yield
    _deps._require_authenticated_user_fn = _orig_require_auth
    _deps._api_error_response_fn = _orig_api_error
    _deps._get_logger_only_fn = _orig_get_logger_only_fn


class _MemoryProjectCursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0
        self._fetchone = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        params = params or ()
        self.rowcount = 0
        text = " ".join(str(sql).split())

        if "SELECT config->'security_policy' FROM projects WHERE id = %s" in text:
            project_id = int(params[0])
            row = self._conn.projects.get(project_id)
            payload = None
            if row:
                payload = (row.get("config") or {}).get("security_policy")
            self._fetchone = (payload,)
            return

        if "SELECT meta->'security_policy' FROM projects WHERE id = %s" in text:
            project_id = int(params[0])
            row = self._conn.projects.get(project_id)
            payload = None
            if row:
                payload = (row.get("meta") or {}).get("security_policy")
            self._fetchone = (payload,)
            return

        if "UPDATE projects" in text and "'{security_policy}'" in text:
            payload = json.loads(str(params[0] or "{}"))
            project_id = int(params[1])
            row = self._conn.projects.get(project_id)
            if not row:
                self.rowcount = 0
                return
            config = dict(row.get("config") or {})
            config["security_policy"] = payload
            row["config"] = config
            self.rowcount = 1
            return

        self._fetchone = None

    def fetchone(self):
        return self._fetchone


class _MemoryProjectConn:
    def __init__(self, projects: dict[int, dict]):
        self.projects = projects
        self.commit_calls = 0

    def cursor(self):
        return _MemoryProjectCursor(self)

    def commit(self):
        self.commit_calls += 1


class _LoggerWithConn:
    def __init__(self, conn):
        self.conn = conn


@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(router)

    @a.middleware("http")
    async def mock_auth(request, call_next):
        request.state.auth_user = {
            "id": 1,
            "username": "test",
            "roles": ["admin"],
            "account_id": "default",
        }
        request.state.auth_principal = dict(request.state.auth_user)
        return await call_next(request)

    return a


@pytest.fixture
def client(app):
    return TestClient(app)


class TestPolicyEndpoints:
    def test_get_global_policy(self, client):
        with patch(
            "core.infrastructure.security.policy_loader.load_global_policy",
            wraps=__import__(
                "core.infrastructure.security.policy_loader", fromlist=["load_global_policy"]
            ).load_global_policy,
        ) as load_policy:
            resp = client.get("/api/security/policy")
        assert resp.status_code == 200
        data = resp.json()
        assert data["level"] == "standard"
        assert "scanners" in data
        assert "surfaces" in data
        load_policy.assert_called_once_with("default")

    def test_get_global_policy_rejects_authenticated_viewer_before_loading_policy(self):
        app = FastAPI()
        app.include_router(router)

        @app.middleware("http")
        async def mock_viewer_auth(request, call_next):
            request.state.auth_principal = {
                "id": 2,
                "username": "viewer",
                "roles": ["viewer"],
                "account_id": "default",
            }
            return await call_next(request)

        with patch("core.infrastructure.security.policy_loader.load_global_policy") as load_policy:
            resp = TestClient(app).get("/api/security/policy")

        assert resp.status_code == 403
        assert resp.json() == {"error": "Admin role required"}
        load_policy.assert_not_called()

    def test_put_global_policy_rejects_unknown_fields_without_persisting(self, client):
        with patch("core.entrypoints.api.security.policy_service.persist_global_policy") as persist:
            resp = client.put(
                "/api/security/policy",
                json={"level": "strict", "internal_override": True},
            )

        assert resp.status_code == 400
        assert resp.json()["error"] == "Invalid security policy"
        assert resp.json()["correlation_id"]
        persist.assert_not_called()

    def test_put_global_policy_persists_canonical_payload(self, client):
        with patch(
            "core.entrypoints.api.security.policy_service.persist_global_policy",
            return_value=True,
        ) as persist:
            resp = client.put(
                "/api/security/policy",
                json={"enabled": "false", "level": "strict"},
            )

        assert resp.status_code == 200
        payload = persist.call_args.args[1]
        assert payload["enabled"] is False
        assert payload["level"] == "strict"
        assert set(payload) == {"enabled", "level", "scanners", "surfaces", "custom_rules"}
        assert persist.call_args.kwargs["account_id"] == "default"

    def test_put_global_policy_rejects_admin_without_account_scope(self, client):
        with (
            patch("core.entrypoints.api.security._request_account_id", return_value=None),
            patch("core.entrypoints.api.security.policy_service.persist_global_policy") as persist,
        ):
            resp = client.put("/api/security/policy", json={"level": "strict"})

        assert resp.status_code == 403
        assert resp.json()["error"] == "Account scope required"
        persist.assert_not_called()

    def test_put_global_policy_masks_internal_persistence_error(self, client):
        with patch(
            "core.entrypoints.api.security.policy_service.persist_global_policy",
            side_effect=RuntimeError("postgres://user:secret@db/internal_table"),
        ):
            resp = client.put("/api/security/policy", json={"level": "strict"})

        assert resp.status_code == 500
        assert resp.json()["error"] == "Failed to persist global policy"
        assert resp.json()["correlation_id"]
        assert "secret" not in resp.text

    def test_project_policy_loads_account_global_policy_for_project_owner(self, client):
        with (
            patch(
                "core.entrypoints.api.security._load_project_stub",
                return_value={"id": 1, "account_id": "account-b"},
            ),
            patch(
                "core.infrastructure.security.policy_loader.load_global_policy",
                return_value=__import__(
                    "core.infrastructure.security.policy", fromlist=["SecurityPolicy"]
                ).SecurityPolicy(),
            ) as load_policy,
            patch("core.entrypoints.api.security.require_project_access"),
        ):
            resp = client.get("/api/security/projects/1/policy")

        assert resp.status_code == 200
        load_policy.assert_called_once_with("account-b")

    def test_get_project_policy(self, client):
        with patch(
            "core.entrypoints.api.security._load_project_stub",
            return_value={"id": 1, "account_id": "default"},
        ):
            resp = client.get("/api/security/projects/1/policy")
        assert resp.status_code == 200
        data = resp.json()
        assert "level" in data

    def test_get_project_policy_with_sources(self, client):
        with (
            patch(
                "core.entrypoints.api.security._load_project_stub",
                return_value={"id": 1, "account_id": "default"},
            ),
            patch(
                "core.entrypoints.api.security._get_project_security_override",
                return_value={"scanners": {"pii": {"mode": "hard_redact"}}},
            ),
        ):
            resp = client.get("/api/security/projects/1/policy?include_sources=1")
        assert resp.status_code == 200
        data = resp.json()
        assert "effective_policy" in data
        assert "global_policy" in data
        assert "project_override" in data
        assert data["source_map"]["scanners.pii.mode"] == "project"

    def test_put_get_project_policy_cycle_uses_config_storage(self, client):
        conn = _MemoryProjectConn(
            {
                1: {"config": {}, "meta": {}},
            }
        )
        logger_stub = _LoggerWithConn(conn)
        with (
            patch("core.entrypoints.api.security.deps.get_logger_only", return_value=logger_stub),
            patch(
                "core.entrypoints.api.security._load_project_stub",
                return_value={"id": 1, "account_id": "default"},
            ),
        ):
            put_resp = client.put(
                "/api/security/projects/1/policy",
                json={
                    "level": "strict",
                    "surfaces": {"inbound": {"action": "skip"}},
                },
            )
            assert put_resp.status_code == 200

            get_resp = client.get("/api/security/projects/1/policy")
            assert get_resp.status_code == 200
            data = get_resp.json()
            assert data["level"] == "strict"
            assert data["surfaces"]["inbound"]["action"] == "skip"

        assert conn.projects[1]["config"]["security_policy"]["level"] == "strict"

    def test_get_project_policy_ignores_retired_meta_fallback(self, client):
        conn = _MemoryProjectConn(
            {
                1: {
                    "config": {},
                    "meta": {"security_policy": {"level": "minimal"}},
                },
            }
        )
        logger_stub = _LoggerWithConn(conn)
        with (
            patch("core.entrypoints.api.security.deps.get_logger_only", return_value=logger_stub),
            patch(
                "core.entrypoints.api.security._load_project_stub",
                return_value={"id": 1, "account_id": "default"},
            ),
        ):
            resp = client.get("/api/security/projects/1/policy")
            assert resp.status_code == 200
            data = resp.json()
            assert data["level"] == "standard"

        assert "security_policy" not in conn.projects[1]["config"]
        assert conn.commit_calls == 0

    def test_put_project_policy_accepts_ui_payload_with_pii_granules(self, client):
        conn = _MemoryProjectConn(
            {
                1: {"config": {}, "meta": {}},
            }
        )
        logger_stub = _LoggerWithConn(conn)
        payload = {
            "level": "standard",
            "scanners": {
                "secrets": {"enabled": True, "severity_threshold": "low", "allowlist": []},
                "injection": {
                    "enabled": True,
                    "severity_threshold": "low",
                    "allowlist": [],
                    "contexts": ["prompt", "display", "shell"],
                },
                "pii": {
                    "enabled": True,
                    "severity_threshold": "low",
                    "mode": "pseudonymize",
                    "allowlist": [],
                    "detectors": {
                        "person_name": {"enabled": False},
                        "email": {"enabled": True},
                        "phone": {"enabled": True},
                        "ssn": {"enabled": True},
                        "credit_card": {"enabled": True},
                        "ipv4": {"enabled": True},
                        "ipv6": {"enabled": True},
                    },
                },
            },
            "surfaces": {
                "inbound": {"action": "scan"},
                "outbound": {"action": "scan"},
                "tool": {"action": "scan"},
            },
            "custom_rules": [],
        }
        with (
            patch("core.entrypoints.api.security.deps.get_logger_only", return_value=logger_stub),
            patch(
                "core.entrypoints.api.security._load_project_stub",
                return_value={"id": 1, "account_id": "default"},
            ),
        ):
            put_resp = client.put("/api/security/projects/1/policy", json=payload)
            assert put_resp.status_code == 200
            get_resp = client.get("/api/security/projects/1/policy")
            assert get_resp.status_code == 200
            data = get_resp.json()

        assert data["scanners"]["pii"]["detectors"]["person_name"]["enabled"] is False

    def test_put_project_policy_rejects_invalid_payload(self, client):
        conn = _MemoryProjectConn(
            {
                1: {"config": {}, "meta": {}},
            }
        )
        logger_stub = _LoggerWithConn(conn)
        with (
            patch("core.entrypoints.api.security.deps.get_logger_only", return_value=logger_stub),
            patch(
                "core.entrypoints.api.security._load_project_stub",
                return_value={"id": 1, "account_id": "default"},
            ),
        ):
            resp = client.put(
                "/api/security/projects/1/policy",
                json={"scanners": {"pii": {"mode": "invalid_mode"}}},
            )
        assert resp.status_code == 400
        body = resp.json()
        assert "invalid project policy override" in str(body.get("error", "")).lower()
        assert "security_policy" not in conn.projects[1]["config"]

    def test_put_project_policy_rejects_unknown_fields_without_persisting(self, client):
        conn = _MemoryProjectConn({1: {"config": {}, "meta": {}}})
        logger_stub = _LoggerWithConn(conn)
        with (
            patch("core.entrypoints.api.security.deps.get_logger_only", return_value=logger_stub),
            patch(
                "core.entrypoints.api.security._load_project_stub",
                return_value={"id": 1, "account_id": "default"},
            ),
        ):
            resp = client.put(
                "/api/security/projects/1/policy",
                json={"level": "strict", "internal_override": True},
            )

        assert resp.status_code == 400
        assert resp.json()["error"] == "Invalid project policy override"
        assert resp.json()["correlation_id"]
        assert "security_policy" not in conn.projects[1]["config"]

    def test_put_project_policy_persists_normalized_override_only(self, client):
        conn = _MemoryProjectConn({1: {"config": {}, "meta": {}}})
        logger_stub = _LoggerWithConn(conn)
        with (
            patch("core.entrypoints.api.security.deps.get_logger_only", return_value=logger_stub),
            patch(
                "core.entrypoints.api.security._load_project_stub",
                return_value={"id": 1, "account_id": "default"},
            ),
        ):
            resp = client.put(
                "/api/security/projects/1/policy",
                json={"enabled": "false", "scanners": {"pii": {"enabled": "false"}}},
            )

        assert resp.status_code == 200
        stored = conn.projects[1]["config"]["security_policy"]
        assert stored == {"enabled": False, "scanners": {"pii": {"enabled": False}}}

    def test_put_project_policy_rejects_invalid_custom_regex_without_persisting(self, client):
        conn = _MemoryProjectConn({1: {"config": {}, "meta": {}}})
        logger_stub = _LoggerWithConn(conn)
        with (
            patch("core.entrypoints.api.security.deps.get_logger_only", return_value=logger_stub),
            patch(
                "core.entrypoints.api.security._load_project_stub",
                return_value={"id": 1, "account_id": "default"},
            ),
        ):
            resp = client.put(
                "/api/security/projects/1/policy",
                json={
                    "custom_rules": [
                        {
                            "rule_id": "audit.invalid.regex",
                            "scanner": "secrets",
                            "pattern": "[",
                        }
                    ]
                },
            )

        assert resp.status_code == 400
        assert "audit.invalid.regex" in str(resp.json().get("error", ""))
        assert "invalid pattern" in str(resp.json().get("error", "")).lower()
        assert "security_policy" not in conn.projects[1]["config"]

    def test_put_project_policy_returns_404_when_project_disappears_before_write(self, client):
        conn = _MemoryProjectConn({})
        logger_stub = _LoggerWithConn(conn)
        with (
            patch("core.entrypoints.api.security.deps.get_logger_only", return_value=logger_stub),
            patch(
                "core.entrypoints.api.security._load_project_stub",
                return_value={"id": 1, "account_id": "default"},
            ),
        ):
            resp = client.put(
                "/api/security/projects/1/policy",
                json={"level": "strict"},
            )

        assert resp.status_code == 404
        assert resp.json()["error"] == "Project not found"

    def test_add_custom_rule_returns_404_when_project_disappears_before_write(self, client):
        conn = _MemoryProjectConn({})
        logger_stub = _LoggerWithConn(conn)
        with (
            patch("core.entrypoints.api.security.deps.get_logger_only", return_value=logger_stub),
            patch(
                "core.entrypoints.api.security._load_project_stub",
                return_value={"id": 1, "account_id": "default"},
            ),
        ):
            resp = client.post(
                "/api/security/projects/1/rules",
                json={
                    "rule_id": "custom.secret",
                    "scanner": "secrets",
                    "description": "Reject obvious secrets",
                    "pattern": "AKIA",
                    "severity": "high",
                },
            )

        assert resp.status_code == 404
        assert resp.json()["error"] == "Project not found"

    def test_add_custom_rule_rejects_unknown_fields_without_persisting(self, client):
        conn = _MemoryProjectConn({1: {"config": {}, "meta": {}}})
        logger_stub = _LoggerWithConn(conn)
        with (
            patch("core.entrypoints.api.security.deps.get_logger_only", return_value=logger_stub),
            patch(
                "core.entrypoints.api.security._load_project_stub",
                return_value={"id": 1, "account_id": "default"},
            ),
        ):
            resp = client.post(
                "/api/security/projects/1/rules",
                json={
                    "rule_id": "custom.secret",
                    "scanner": "secrets",
                    "pattern": "AKIA",
                    "write_any_policy_key": True,
                },
            )

        assert resp.status_code == 400
        assert resp.json()["error"] == "Invalid custom rule"
        assert resp.json()["correlation_id"]
        assert "security_policy" not in conn.projects[1]["config"]


class TestObservabilityEndpoints:
    def test_get_posture(self, client):
        resp = client.get("/api/security/posture")
        assert resp.status_code == 200
        data = resp.json()
        assert "score" in data
        assert "level" in data

    def test_get_posture_requires_auth(self):
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        resp = client.get("/api/security/posture")
        assert resp.status_code == 401

    def test_get_posture_rejects_viewer_role(self):
        app = FastAPI()
        app.include_router(router)

        @app.middleware("http")
        async def mock_viewer_auth(request, call_next):
            request.state.auth_user = {
                "id": 2,
                "username": "viewer",
                "roles": ["viewer"],
                "account_id": "default",
            }
            request.state.auth_principal = dict(request.state.auth_user)
            return await call_next(request)

        resp = TestClient(app).get("/api/security/posture")
        assert resp.status_code == 403

    def test_get_posture_checks_project_scope(self, client):
        with (
            patch(
                "core.entrypoints.api.security._load_project_stub",
                return_value={"id": 1, "account_id": "default"},
            ),
            patch(
                "core.entrypoints.api.security.require_project_access",
                side_effect=ServiceForbiddenError("Access denied"),
            ),
        ):
            resp = client.get("/api/security/posture?project_id=1")
        assert resp.status_code == 403

    def test_get_posture_scopes_global_query_to_caller_account(self, client):
        """FS-AUDIT-007: global posture (no project_id) must be scoped to the
        caller's account, never returning the newest snapshot across accounts."""
        captured: dict = {}

        def _fake_payload(*, project_id=None, account_id=None):
            captured["project_id"] = project_id
            captured["account_id"] = account_id
            return {"score": 100, "level": "good"}

        with patch(
            "core.entrypoints.api.security.get_posture_payload",
            side_effect=_fake_payload,
        ):
            resp = client.get("/api/security/posture")

        assert resp.status_code == 200
        assert captured["project_id"] is None
        assert captured["account_id"] == "default"

    def test_get_posture_falls_back_to_default_account_when_unscoped(self):
        """FS-AUDIT-007 defense-in-depth: an admin principal without account_id
        must not reach the unfiltered global snapshot query — the route falls
        back to the default account rather than passing account_id=None."""
        from core.application.auth.config import DEFAULT_ACCOUNT_ID

        app = FastAPI()
        app.include_router(router)

        @app.middleware("http")
        async def mock_auth(request, call_next):
            request.state.auth_user = {
                "id": 9,
                "username": "admin",
                "roles": ["admin"],
            }
            request.state.auth_principal = dict(request.state.auth_user)
            return await call_next(request)

        captured: dict = {}

        def _fake_payload(*, project_id=None, account_id=None):
            captured["account_id"] = account_id
            return {"score": 1, "level": "x"}

        with patch(
            "core.entrypoints.api.security.get_posture_payload",
            side_effect=_fake_payload,
        ):
            resp = TestClient(app).get("/api/security/posture")

        assert resp.status_code == 200
        assert captured["account_id"] == DEFAULT_ACCOUNT_ID
        assert captured["account_id"]

    def test_list_events(self, client):
        resp = client.get("/api/security/events")
        assert resp.status_code == 200
        data = resp.json()
        assert "events" in data
        assert "page" in data

    def test_list_events_with_filters(self, client):
        resp = client.get(
            "/api/security/events?severity=critical&surface=inbound&page=1&per_page=10"
        )
        assert resp.status_code == 200

    def test_list_events_with_job_id_filter(self, client):
        sec_logger = MagicMock()
        sec_logger.list_events.return_value = []
        with patch(
            "core.infrastructure.security.event_logger.get_security_logger", return_value=sec_logger
        ):
            resp = client.get("/api/security/events?job_id=123&page=1&per_page=10")
        assert resp.status_code == 200
        sec_logger.list_events.assert_called_once()
        kwargs = sec_logger.list_events.call_args.kwargs
        assert kwargs["job_id"] == "123"

    def test_list_events_rejects_viewer_role(self):
        app = FastAPI()
        app.include_router(router)

        @app.middleware("http")
        async def mock_viewer_auth(request, call_next):
            request.state.auth_user = {
                "id": 2,
                "username": "viewer",
                "roles": ["viewer"],
                "account_id": "default",
            }
            request.state.auth_principal = dict(request.state.auth_user)
            return await call_next(request)

        resp = TestClient(app).get("/api/security/events")

        assert resp.status_code == 403

    def test_list_events_operator_requires_project_filter(self):
        app = FastAPI()
        app.include_router(router)

        @app.middleware("http")
        async def mock_operator_auth(request, call_next):
            request.state.auth_user = {
                "id": 3,
                "username": "operator",
                "roles": ["operator"],
                "account_id": "default",
            }
            request.state.auth_principal = dict(request.state.auth_user)
            return await call_next(request)

        resp = TestClient(app).get("/api/security/events")

        assert resp.status_code == 403

    def test_list_events_project_filter_checks_project_access(self, client):
        with (
            patch(
                "core.entrypoints.api.security._load_project_stub",
                return_value={"id": "project-b", "account_id": "default"},
            ),
            patch(
                "core.entrypoints.api.security.require_project_access",
                side_effect=ServiceForbiddenError("Access denied"),
            ),
        ):
            resp = client.get("/api/security/events?project_id=project-b")

        assert resp.status_code == 403

    def test_list_findings(self, client):
        resp = client.get("/api/security/findings")
        assert resp.status_code == 200
        data = resp.json()
        assert "findings" in data

    def test_pagination_bounds(self, client):
        resp = client.get("/api/security/events?page=0")
        assert resp.status_code == 422  # validation error

        resp = client.get("/api/security/events?per_page=200")
        assert resp.status_code == 422
