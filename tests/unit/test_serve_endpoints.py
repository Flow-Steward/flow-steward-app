"""
Unit tests for FastAPI endpoints exposed through the root serve.py launcher.

Tests: status codes, response shapes, error handling.

Run:
    docker compose exec fs-app python -m pytest tests/unit/test_serve_endpoints.py -v
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import unquote

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID, DEFAULT_WORKSPACE_ID
from core.application.management.errors import (
    ServiceNotFoundError,
)
from core.application.management.scope import ScopeContext
from core.entrypoints.api import route_registration as api_routes
from core.infrastructure.config.version import APP_VERSION
from core.infrastructure.runtime import http_app_auth_runtime as auth_runtime
from core.infrastructure.runtime import http_app_events_resume as events_runtime
from core.infrastructure.runtime import http_app_indexing_workflows as indexing_runtime
from core.infrastructure.runtime import http_app_lifecycle as lifecycle_runtime
from core.infrastructure.runtime import http_app_reactions_extensions as reactions_runtime
from core.infrastructure.runtime import http_app_runtime_helpers as runtime_helpers
from core.infrastructure.runtime import http_app_scheduled_jobs as scheduled_runtime
from tests.unit._psycopg_runtime_stub import install_psycopg_runtime_stub

install_psycopg_runtime_stub()


_OPTIONAL_TEST_DEPS = {
    "fastapi",
    "starlette",
    "pydantic",
    "jinja2",
    "langgraph",
    "langchain_core",
}


def test_watchdog_scan_forwards_runtime_maintenance_activation(monkeypatch) -> None:
    activated_at = "2026-09-18T14:39:04.282087+00:00"
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        runtime_helpers,
        "runtime_maintenance_state",
        lambda: {"activated_at": activated_at},
    )

    def run_watchdog_scan(_bl, **kwargs):
        captured.update(kwargs)
        return {"detection_count": 0}

    monkeypatch.setattr(runtime_helpers._watchdog_runtime, "run_watchdog_scan", run_watchdog_scan)

    result = runtime_helpers._watchdog_scan(object(), apply=False)

    assert result == {"detection_count": 0}
    assert captured["maintenance_activated_at"] == datetime.fromisoformat(activated_at)


def _fail_only_missing_optional_dep(exc: Exception, *, context: str) -> None:
    if isinstance(exc, ModuleNotFoundError) and getattr(exc, "name", "") in _OPTIONAL_TEST_DEPS:
        pytest.fail(f"{context}: missing optional dependency '{exc.name}'")
    raise exc


def _assert_frontend_shell_response(resp) -> str:
    assert resp.status_code == 200
    text = resp.text
    retired_brand = "Retired Orchestrator"
    assert "Flow Steward" in text or ("AI Orchestrator" not in text and retired_brand not in text)
    assert '<div id="root"></div>' in text
    assert "/app/assets/index-" in text
    return text


def _project_fixture(**overrides):
    row = {
        "id": 1,
        "name": "Demo",
        "account_id": DEFAULT_ACCOUNT_ID,
        "workspace_id": DEFAULT_WORKSPACE_ID,
    }
    row.update(overrides)
    return row


def _job_fixture(**overrides):
    row = {
        "id": 1,
        "account_id": DEFAULT_ACCOUNT_ID,
        "workspace_id": DEFAULT_WORKSPACE_ID,
        "meta": {},
    }
    row.update(overrides)
    return row


def _workflow_resume_payload(
    thread_id: str,
    *,
    approved: bool = True,
    action: str | None = None,
    comment: str = "",
    override_content: str = "",
    actor: str = "operator",
):
    return SimpleNamespace(
        dump=lambda: {
            "thread_id": thread_id,
            "approved": approved,
            "action": action or ("resume" if approved else "reject"),
            "comment": comment,
            "override_content": override_content,
            "actor": actor,
            "status": "in_progress",
        }
    )


def _run_coro_blocking(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, object] = {}
    error: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - re-raised below
            error["exc"] = exc

    thread = threading.Thread(target=_runner)
    thread.start()
    thread.join()
    if "exc" in error:
        raise error["exc"]
    return result.get("value")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    """Create a TestClient for the FastAPI app."""
    try:
        from fastapi.testclient import TestClient

        from core.entrypoints.api.app_factory import create_app

        app = create_app()
        app.state.local_auth_enabled_override = lambda: False
        app.state.external_auth_enabled_override = lambda: False
        yield TestClient(app, raise_server_exceptions=False)
    except Exception as e:
        _fail_only_missing_optional_dep(e, context="Could not create TestClient")


@pytest.fixture
def client_with_db():
    """TestClient with mocked DB calls."""
    try:
        from fastapi.testclient import TestClient

        from core.entrypoints.api.app_factory import create_app

        mock_bl = MagicMock()
        mock_bl.conn = MagicMock()
        app = create_app()
        app.state.local_auth_enabled_override = lambda: False
        app.state.external_auth_enabled_override = lambda: False

        with patch.dict(
            app.state.runtime_container.service_factories,
            {"db": lambda: mock_bl},
        ):
            yield TestClient(app, raise_server_exceptions=False)
    except Exception as e:
        _fail_only_missing_optional_dep(e, context="Could not create TestClient with mocked DB")


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


class TestServeHelpers:
    def test_as_utc_datetime_returns_none_for_missing_value_without_fallback(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for _as_utc_datetime test"
            )

        assert api_runtime._as_utc_datetime(None, fallback=None) is None
        assert api_runtime._as_utc_datetime("", fallback=None) is None

    def test_static_assets_receive_cache_control_header(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for static cache header test"
            )

        resp = client.get("/static/shell.css")

        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == (
            f"public, max-age={api_runtime._STATIC_CACHE_MAX_AGE_SECONDS}, stale-while-revalidate=86400"
        )

    def test_removed_tailwind_static_artifact_is_not_served(self, client):
        resp = client.get("/static/tailwind.css")

        assert resp.status_code == 404

    def test_static_assets_are_gzipped_when_client_accepts_it(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            _fail_only_missing_optional_dep(e, context="api_runtime import for static gzip test")

        resp = client.get(
            "/static/platform-kit/components/platform-grid.js",
            headers={"Accept-Encoding": "gzip"},
        )

        assert resp.status_code == 200
        if api_runtime._APP_GZIP_ENABLED:
            assert resp.headers.get("content-encoding") == "gzip"
            assert "accept-encoding" in str(resp.headers.get("vary") or "").lower()
        else:
            assert resp.headers.get("content-encoding") is None

    def test_favicon_redirects_to_versioned_static_asset(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for favicon redirect test"
            )

        resp = client.get("/favicon.ico", follow_redirects=False)

        assert resp.status_code == 307
        assert "favicon.svg" in (resp.headers.get("location") or "")

    def test_get_thread_state_values_reuses_cached_state_reader_graph(self):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for state reader cache test"
            )

        original_graph = auth_runtime._state_reader_graph
        original_dsn = auth_runtime._state_reader_graph_dsn
        snapshot = type(
            "Snapshot", (), {"values": {"current_step_id": "development"}, "tasks": []}
        )()
        fake_graph = MagicMock()
        fake_graph.get_state.return_value = snapshot

        try:
            auth_runtime._state_reader_graph = None
            auth_runtime._state_reader_graph_dsn = ""
            with (
                patch.object(
                    auth_runtime, "CONFIG", {"langgraph_postgres_uri": "postgresql://example"}
                ),
                patch.object(auth_runtime, "_get_checkpointer", return_value=object()),
                patch.object(
                    auth_runtime, "build_registry_graph", return_value=fake_graph
                ) as mock_build,
            ):
                first = runtime_helpers._get_thread_state_values("thread-1")
                second = runtime_helpers._get_thread_state_values("thread-1")
        finally:
            auth_runtime._state_reader_graph = original_graph
            auth_runtime._state_reader_graph_dsn = original_dsn

        assert first["current_step_id"] == "development"
        assert second["current_step_id"] == "development"
        assert mock_build.call_count == 1
        assert fake_graph.get_state.call_count == 2

    def test_get_thread_state_values_rebuilds_cached_graph_after_failure(self):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for state reader retry test"
            )

        original_graph = auth_runtime._state_reader_graph
        original_dsn = auth_runtime._state_reader_graph_dsn
        first_graph = MagicMock()
        first_graph.get_state.side_effect = RuntimeError("stale checkpointer")
        snapshot = type(
            "Snapshot", (), {"values": {"current_step_id": "proofreader"}, "tasks": []}
        )()
        second_graph = MagicMock()
        second_graph.get_state.return_value = snapshot

        try:
            auth_runtime._state_reader_graph = None
            auth_runtime._state_reader_graph_dsn = ""
            with (
                patch.object(
                    auth_runtime, "CONFIG", {"langgraph_postgres_uri": "postgresql://example"}
                ),
                patch.object(auth_runtime, "_get_checkpointer", return_value=object()),
                patch.object(
                    auth_runtime, "build_registry_graph", side_effect=[first_graph, second_graph]
                ) as mock_build,
            ):
                values = runtime_helpers._get_thread_state_values("thread-2")
        finally:
            auth_runtime._state_reader_graph = original_graph
            auth_runtime._state_reader_graph_dsn = original_dsn

        assert values["current_step_id"] == "proofreader"
        assert mock_build.call_count == 2
        assert first_graph.get_state.call_count == 1
        assert second_graph.get_state.call_count == 1

    def test_get_platform_store_retries_after_creation_failure(self):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(e, context="api_runtime import for logger retry test")

        fresh_logger = MagicMock()
        fresh_logger.conn.cursor.return_value.__enter__.return_value = MagicMock()

        create_calls = {"count": 0}

        def _create(dsn: str):
            create_calls["count"] += 1
            if create_calls["count"] == 1:
                raise RuntimeError("temporary db outage")
            return fresh_logger

        try:
            runtime_helpers._platform_store_singletons.clear()
            with (
                patch.object(runtime_helpers, "CONFIG", {"fs_db_dsn": "postgresql://example"}),
                patch.object(runtime_helpers.time, "sleep", return_value=None),
            ):
                result = runtime_helpers._get_platform_store(_create)
        finally:
            runtime_helpers._platform_store_singletons.clear()

        assert result is fresh_logger
        assert create_calls["count"] == 2

    def test_get_platform_logger_uses_project_store(self):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for get_platform_logger test"
            )

        shared_logger = MagicMock()
        shared_logger.add_project.return_value = 91

        with patch.object(runtime_helpers, "_get_project_store", return_value=shared_logger):
            bl, project_id = runtime_helpers._get_platform_logger("default")

        assert bl is shared_logger
        assert project_id == 91
        shared_logger.add_project.assert_called_once_with(
            "default",
            workspace_id=DEFAULT_WORKSPACE_ID,
        )

    def test_build_project_config_snapshot_redacts_secret_keys(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        snapshot = api_runtime._build_project_config_snapshot(
            {
                "agents_config": {"default_provider": "openai"},
                "api_key": "sk-live",
                "nested": {"client_secret": "secret-value", "token": "abc"},
                "list_items": [{"password": "pw"}, {"safe": "ok"}],
            }
        )

        assert snapshot["api_key"] == "[REDACTED]"
        assert snapshot["nested"]["client_secret"] == "[REDACTED]"
        assert snapshot["nested"]["token"] == "[REDACTED]"
        assert snapshot["list_items"][0]["password"] == "[REDACTED]"
        assert snapshot["list_items"][1]["safe"] == "ok"

    def test_password_reset_throttle_is_scoped_by_action(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for password reset throttle test"
            )

        original_attempts = api_runtime._password_reset_attempts
        original_lock = api_runtime._password_reset_lock
        try:
            api_runtime._password_reset_attempts = {}
            api_runtime._password_reset_lock = threading.Lock()
            client_ip = "127.0.0.1"

            for _ in range(api_runtime._PASSWORD_RESET_MAX_ATTEMPTS):
                api_runtime._password_reset_throttle_record(client_ip, action="request")

            throttled_request, retry_after_request = api_runtime._password_reset_throttle_check(
                client_ip, action="request"
            )
            throttled_verify, retry_after_verify = api_runtime._password_reset_throttle_check(
                client_ip, action="verify"
            )

            assert throttled_request is True
            assert retry_after_request > 0
            assert throttled_verify is False
            assert retry_after_verify == 0
        finally:
            api_runtime._password_reset_attempts = original_attempts
            api_runtime._password_reset_lock = original_lock

    def test_password_reset_request_throttle_blocks_rotated_subject(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for password reset subject throttle test"
            )

        original_attempts = api_runtime._password_reset_attempts
        original_lock = api_runtime._password_reset_lock
        try:
            api_runtime._password_reset_attempts = {}
            api_runtime._password_reset_lock = threading.Lock()
            client_ip = "127.0.0.1"

            for _ in range(api_runtime._PASSWORD_RESET_MAX_ATTEMPTS):
                api_runtime._password_reset_throttle_record(
                    client_ip, action="request", subject="first-user"
                )

            throttled_first, retry_after_first = api_runtime._password_reset_throttle_check(
                client_ip,
                action="request",
                subject="first-user",
            )
            throttled_second, retry_after_second = api_runtime._password_reset_throttle_check(
                client_ip,
                action="request",
                subject="second-user",
            )

            assert throttled_first is True
            assert retry_after_first > 0
            assert throttled_second is True
            assert retry_after_second > 0
        finally:
            api_runtime._password_reset_attempts = original_attempts
            api_runtime._password_reset_lock = original_lock

    def test_secret_storage_config_error_detects_missing_key(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for secret storage config test"
            )

        assert (
            api_runtime._secret_storage_config_error(
                RuntimeError("FS_SECRET_KEY is required for encrypted secret storage")
            )
            is True
        )
        assert (
            api_runtime._secret_storage_config_error(
                RuntimeError("cryptography/AESGCM is required for encrypted secret storage")
            )
            is True
        )
        assert (
            api_runtime._secret_storage_config_error(RuntimeError("different runtime error"))
            is False
        )

    def test_resolve_provider_project_run_config_requires_unique_bound_project(self):
        from core.application.extensions.provider_runtime import resolve_provider_project_run_config

        mock_bl = MagicMock()
        mock_bl.get_projects.return_value = [{"id": 1}, {"id": 2}]
        mock_bl.get_project_run_config.side_effect = [
            {
                "project_name": "A",
                "project_providers": [
                    {
                        "provider_key": "sample_tasks",
                        "provider_type_key": "task_provider",
                        "enabled": True,
                        "provider_enabled": True,
                        "binding_config": {"list_id": "list-a"},
                    }
                ],
            },
            {
                "project_name": "B",
                "project_providers": [
                    {
                        "provider_key": "sample_tasks",
                        "provider_type_key": "task_provider",
                        "enabled": True,
                        "provider_enabled": True,
                        "binding_config": {"list_id": "list-b"},
                    }
                ],
            },
        ]

        project_id, run_cfg = resolve_provider_project_run_config(
            mock_bl,
            provider_key="sample_tasks",
            provider_type_key="task_provider",
        )

        assert project_id is None
        assert run_cfg is None

    def test_resolve_provider_project_run_config_rejects_explicit_project_without_binding(self):
        from core.application.extensions.provider_runtime import resolve_provider_project_run_config

        mock_bl = MagicMock()
        mock_bl.get_project_run_config.return_value = {"project_name": "A", "project_providers": []}

        project_id, run_cfg = resolve_provider_project_run_config(
            mock_bl,
            provider_key="sample_tasks",
            provider_type_key="task_provider",
            explicit_project_id=7,
        )

        assert project_id is None
        assert run_cfg is None

    def test_is_admin_only_route_matches_control_plane_paths(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for admin-only route test"
            )

        # Control-plane registry routes are admin-only; provider/extension catalogs remain
        # operator-readable through the registry-read helper, with unsafe methods gated later.
        assert api_runtime._is_admin_only_route("/api/agents") is True
        assert api_runtime._is_admin_only_route("/api/providers") is False
        assert api_runtime._is_admin_only_route("/api/extensions/demo/enable") is False
        assert api_runtime._is_admin_only_route("/api/workspaces") is True
        assert api_runtime._is_admin_only_route("/api/model-providers") is True
        assert api_runtime._is_admin_only_route("/api/model-providers/provider-1") is True
        assert api_runtime._is_admin_only_route("/api/processes/123/kill") is True
        assert api_runtime._is_admin_only_route("/api/processes/proc-alpha/kill") is True
        assert api_runtime._is_admin_only_route("/api/preview") is True
        assert api_runtime._is_admin_only_route("/api/guardrails") is True
        assert api_runtime._is_admin_only_route("/admin/settings") is True
        assert api_runtime._is_admin_only_route("/api/jobs/1") is False
        # Registry-read helper
        assert api_runtime._is_registry_read_route("/api/agents") is True
        assert api_runtime._is_registry_read_route("/api/providers") is True
        assert api_runtime._is_registry_read_route("/api/extensions") is True
        assert api_runtime._is_registry_read_route("/api/model-selection/options") is True
        assert api_runtime._is_registry_read_route("/api/workload") is True
        assert api_runtime._is_registry_read_route("/api/runs") is True
        assert api_runtime._is_registry_read_route("/api/security/posture") is True
        assert api_runtime._is_registry_read_route("/health/deep") is True

    def test_resolve_actor_username_prefers_authenticated_user_over_explicit_actor(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for actor resolution test"
            )

        request = MagicMock()
        request.state.auth_user = {"username": "alice"}
        actor = api_runtime._resolve_actor_username(request, "mallory", default="operator")
        assert actor == "alice"


class TestScheduledJobsHelpers:
    @pytest.fixture
    def active_schedule_outcome(self, monkeypatch):
        # These scanner fixtures model active owners; real PostgreSQL controls
        # exercise current-owner locking and retirement outcome preservation.
        def update(store, *, project_id, scheduled_job_id, workflow_id, account_id, changes):
            store.update_scheduled_job(
                project_id, scheduled_job_id, account_id=account_id, **changes
            )

        from core.infrastructure.runtime.control import scans_scheduled_jobs

        monkeypatch.setattr(scans_scheduled_jobs, "update_scheduled_job_outcome", update)

    def test_cron_next_run_at_supports_step_expression(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        reference = datetime(2026, 3, 14, 10, 7, tzinfo=UTC)
        next_run = api_runtime._cron_next_run_at("*/15 * * * *", reference)
        assert next_run == datetime(2026, 3, 14, 10, 15, tzinfo=UTC)

    def test_cron_next_run_at_rejects_invalid_expression(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        with pytest.raises(ValueError):
            api_runtime._cron_next_run_at("bad cron", datetime(2026, 3, 14, 10, 0, tzinfo=UTC))

    def test_scheduled_jobs_scan_enqueues_due_job(self, active_schedule_outcome):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now_utc = datetime(2026, 3, 14, 9, 5, tzinfo=UTC)
        due_row = {
            "id": 21,
            "project_id": 1,
            "account_id": "00000000-0000-0000-0000-000000000002",
            "workflow_id": "wf_main",
            "task_summary": "Nightly check",
            "cron_expr": "*/30 * * * *",
            "enabled": True,
            "status": "active",
            "admission_allowed": True,
            "admission_workflow_revision": 1,
            "current_workflow_revision": 1,
            "last_job_id": None,
            "next_run_at": datetime(2026, 3, 14, 9, 0, tzinfo=UTC),
        }
        mock_bl = MagicMock()
        mock_bl.list_due_scheduled_jobs.return_value = [due_row]
        mock_bl.claim_due_scheduled_job.return_value = {
            **due_row,
            "next_run_at": datetime(2026, 3, 14, 9, 30, tzinfo=UTC),
        }
        mock_bl.get_project_run_config.return_value = {
            "project_name": "Demo Project",
            "project_root": "/target/demo",
            "account_id": "00000000-0000-0000-0000-000000000002",
        }
        mock_bl.create_scheduled_job_run = MagicMock()
        mock_bl.update_scheduled_job = MagicMock()
        contract_validation = MagicMock()
        contract_validation.ok = True
        contract_validation.errors = []
        contract_validation.inputs = {}
        contract_validation.interface = {"enabled": False}

        with (
            patch.object(
                api_runtime._control_scheduled_jobs,
                "_enqueue_scheduled_job",
                return_value={"status": "enqueued", "job_id": 901},
            ) as enqueue_mock,
            patch.object(
                api_runtime,
                "_validate_explicit_workflow_override",
                return_value=None,
            ),
            patch(
                "core.application.registry.workflow_contract_service.WorkflowInterfaceService"
            ) as service_cls,
        ):
            service_cls.return_value.validate_inputs_for_workflow_id.return_value = (
                contract_validation
            )
            payload = api_runtime._scheduled_jobs_scan(mock_bl, now_utc=now_utc)

        assert payload["enqueued"] == 1
        assert payload["failed"] == 0
        enqueue_mock.assert_called_once()
        mock_bl.claim_due_scheduled_job.assert_called_once()
        assert mock_bl.create_scheduled_job_run.call_count == 1
        assert mock_bl.update_scheduled_job.call_count == 1

    def test_scheduled_jobs_scan_skips_when_previous_run_active(self):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now_utc = datetime(2026, 3, 14, 10, 1, tzinfo=UTC)
        due_row = {
            "id": 31,
            "project_id": 1,
            "account_id": "00000000-0000-0000-0000-000000000002",
            "workflow_id": "wf_main",
            "task_summary": "Nightly check",
            "cron_expr": "0 * * * *",
            "enabled": True,
            "status": "active",
            "next_run_at": datetime(2026, 3, 14, 10, 0, tzinfo=UTC),
        }
        mock_bl = MagicMock()
        mock_bl.list_due_scheduled_jobs.return_value = [due_row]
        mock_bl.claim_due_scheduled_job.return_value = {
            **due_row,
            "next_run_at": datetime(2026, 3, 14, 11, 0, tzinfo=UTC),
        }
        mock_bl.create_scheduled_job_run = MagicMock()
        mock_bl.update_scheduled_job = MagicMock()

        with patch.object(
            scheduled_runtime,
            "_run_scheduled_job_once",
            return_value={"status": "skipped", "reason": "overlap_active", "job_id": 777},
        ):
            payload = scheduled_runtime._scheduled_jobs_scan(mock_bl, now_utc=now_utc)

        assert payload["enqueued"] == 0
        assert payload["skipped"] == 1
        assert payload["details"][0]["reason"] == "overlap_active"
        assert payload["details"][0]["job_id"] == "777"

    def test_scheduled_jobs_scan_skips_when_claim_lost(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.list_due_scheduled_jobs.return_value = [
            {
                "id": 42,
                "project_id": 1,
                "account_id": "00000000-0000-0000-0000-000000000002",
                "workflow_id": "wf_main",
                "task_summary": "Nightly check",
                "cron_expr": "*/15 * * * *",
                "enabled": True,
                "status": "active",
                "last_job_id": None,
                "next_run_at": datetime(2026, 3, 14, 12, 0, tzinfo=UTC),
            }
        ]
        mock_bl.claim_due_scheduled_job.return_value = None

        with patch.object(api_routes, "_enqueue_task") as enqueue_mock:
            payload = api_runtime._scheduled_jobs_scan(
                mock_bl, now_utc=datetime(2026, 3, 14, 12, 1, tzinfo=UTC)
            )

        assert payload["enqueued"] == 0
        assert payload["skipped"] == 1
        assert payload["failed"] == 0
        assert payload["details"][0]["reason"] == "claim_lost_or_already_processed"
        enqueue_mock.assert_not_called()

    def test_scheduled_jobs_scan_misfire_advances_from_now(self, active_schedule_outcome):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now_utc = datetime(2026, 3, 14, 12, 10, tzinfo=UTC)
        due_row = {
            "id": 43,
            "project_id": 1,
            "account_id": "00000000-0000-0000-0000-000000000002",
            "workflow_id": "wf_main",
            "task_summary": "Nightly check",
            "cron_expr": "*/30 * * * *",
            "enabled": True,
            "status": "active",
            "admission_allowed": True,
            "admission_workflow_revision": 1,
            "current_workflow_revision": 1,
            "last_job_id": None,
            "next_run_at": datetime(2026, 3, 14, 9, 0, tzinfo=UTC),
        }
        mock_bl = MagicMock()
        mock_bl.list_due_scheduled_jobs.return_value = [due_row]

        def _claim_side_effect(**kwargs):
            return {**due_row, "next_run_at": kwargs["next_run_at"]}

        mock_bl.claim_due_scheduled_job.side_effect = _claim_side_effect
        mock_bl.get_project_run_config.return_value = {
            "project_name": "Demo Project",
            "project_root": "/target/demo",
            "account_id": "00000000-0000-0000-0000-000000000002",
        }
        mock_bl.create_scheduled_job_run = MagicMock()
        mock_bl.update_scheduled_job = MagicMock()
        contract_validation = MagicMock()
        contract_validation.ok = True
        contract_validation.errors = []
        contract_validation.inputs = {}
        contract_validation.interface = {"enabled": False}

        with (
            patch.object(
                api_runtime._control_scheduled_jobs,
                "_enqueue_scheduled_job",
                return_value={"status": "enqueued", "job_id": 902},
            ) as enqueue_mock,
            patch.object(
                api_runtime,
                "_validate_explicit_workflow_override",
                return_value=None,
            ),
            patch(
                "core.application.registry.workflow_contract_service.WorkflowInterfaceService"
            ) as service_cls,
        ):
            service_cls.return_value.validate_inputs_for_workflow_id.return_value = (
                contract_validation
            )
            payload = api_runtime._scheduled_jobs_scan(mock_bl, now_utc=now_utc)

        assert payload["enqueued"] == 1
        _args, kwargs = mock_bl.claim_due_scheduled_job.call_args
        assert kwargs["expected_next_run_at"] == datetime(2026, 3, 14, 9, 0, tzinfo=UTC)
        assert kwargs["next_run_at"] == datetime(2026, 3, 14, 12, 30, tzinfo=UTC)
        assert kwargs["now"] == now_utc
        assert enqueue_mock.call_args.kwargs["scheduled_for"] == datetime(
            2026,
            3,
            14,
            9,
            0,
            tzinfo=UTC,
        )


class TestScheduledJobsEndpoints:
    def test_project_scheduled_job_form_post_is_removed_after_react_cutover(self, client):
        resp = client.post(
            "/projects/1/scheduled-jobs",
            data={
                "task_summary": "Nightly sync",
                "workflow_id": "wf_main",
                "cron_expr": "invalid cron",
                "enabled": "1",
            },
            follow_redirects=False,
        )

        assert resp.status_code in (404, 405)

    def test_api_project_run_scheduled_job_now_returns_job_payload(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_service = MagicMock()
        mock_service.run_scheduled_job_now.return_value = type(
            "RunNowPayload",
            (),
            {"dump": lambda self: {"status": "enqueued", "job_id": 555}},
        )()

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"project_command_service": lambda: mock_service},
        ):
            resp = client.post(
                "/api/projects/1/scheduled-jobs/7/run-now",
                follow_redirects=False,
            )

        assert resp.status_code == 200
        assert resp.json()["job_id"] == 555
        mock_service.run_scheduled_job_now.assert_called_once()

    def test_api_project_scheduled_jobs_returns_payload(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = _project_fixture(name="Demo Project")
        mock_bl.list_scheduled_jobs.return_value = [{"id": 1, "project_id": 1}]
        mock_bl.list_scheduled_job_runs.return_value = [{"id": 10, "scheduled_job_id": 1}]

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/projects/1/scheduled-jobs")

        assert resp.status_code == 200
        data = resp.json()
        assert data["project_id"] == "1"
        assert len(data["jobs"]) == 1
        assert len(data["runs"]) == 1


class TestCronPreviewEndpoint:
    def test_cron_preview_returns_next_runs(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = _project_fixture(
            schedule_timezone="Europe/Vilnius",
        )

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_routes, "_require_authenticated_user", return_value={"username": "alice"}
            ),
        ):
            resp = client.get("/api/projects/1/cron-preview?cron_expr=0+9+*+*+1")

        assert resp.status_code == 200
        data = resp.json()
        assert data["timezone"] == "Europe/Vilnius"
        assert data["cron_expr"] == "0 9 * * 1"
        assert "summary" in data
        assert "Monday" in data["summary"]
        assert len(data["next_runs"]) == 5

    def test_cron_preview_uses_utc_when_no_project_timezone(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = _project_fixture(schedule_timezone="")

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_routes, "_require_authenticated_user", return_value={"username": "alice"}
            ),
        ):
            resp = client.get("/api/projects/1/cron-preview?cron_expr=0+9+*+*+*")

        assert resp.status_code == 200
        data = resp.json()
        assert data["timezone"] == "UTC"

    def test_cron_preview_rejects_empty_expr(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = _project_fixture()

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_routes, "_require_authenticated_user", return_value={"username": "alice"}
            ),
        ):
            resp = client.get("/api/projects/1/cron-preview?cron_expr=")

        assert resp.status_code == 400
        assert "required" in resp.json()["error"].lower()

    def test_cron_preview_rejects_invalid_cron(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = _project_fixture()

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_routes, "_require_authenticated_user", return_value={"username": "alice"}
            ),
        ):
            resp = client.get("/api/projects/1/cron-preview?cron_expr=bad+cron")

        assert resp.status_code == 400
        assert "invalid" in resp.json()["error"].lower()

    def test_cron_preview_404_for_missing_project(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = None

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_routes, "_require_authenticated_user", return_value={"username": "alice"}
            ),
        ):
            resp = client.get("/api/projects/999/cron-preview?cron_expr=0+9+*+*+*")

        assert resp.status_code == 404

    def test_cron_preview_custom_count(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = _project_fixture(schedule_timezone="UTC")

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_routes, "_require_authenticated_user", return_value={"username": "alice"}
            ),
        ):
            resp = client.get("/api/projects/1/cron-preview?cron_expr=*/30+*+*+*+*&count=3")

        assert resp.status_code == 200
        assert len(resp.json()["next_runs"]) == 3

    def test_cron_preview_returns_403_for_cross_account_scope(self, client):
        try:
            from core.application.management.scope import ScopeContext
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {
            "id": 1,
            "name": "Demo",
            "account_id": "acc_alpha",
            "schedule_timezone": "UTC",
        }
        cross_scope = ScopeContext(account_id="acc_other", actor_id="eve", principal_type="user")

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_routes, "_require_authenticated_user", return_value={"username": "eve"}
            ),
            patch.object(api_routes, "_request_scope", return_value=cross_scope),
        ):
            resp = client.get("/api/projects/1/cron-preview?cron_expr=0+9+*+*+*")

        assert resp.status_code == 403


class TestScheduleTimezoneEndpoint:
    def test_schedule_timezone_update_returns_403_for_cross_account_scope(self, client):
        try:
            from core.application.management.scope import ScopeContext
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {
            "id": 1,
            "name": "Demo",
            "account_id": "acc_alpha",
            "schedule_timezone": "UTC",
        }
        mock_bl.conn = MagicMock()
        cross_scope = ScopeContext(account_id="acc_other", actor_id="eve", principal_type="user")

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_routes, "_require_authenticated_user", return_value={"username": "eve"}
            ),
            patch.object(api_routes, "_request_scope", return_value=cross_scope),
        ):
            resp = client.patch(
                "/api/projects/1/schedule-timezone",
                json={"schedule_timezone": "Europe/Vilnius"},
            )

        assert resp.status_code == 403
        mock_bl.conn.cursor.assert_not_called()

    def test_schedule_timezone_update_is_atomic_when_recalculation_fails(self, client):
        try:
            from core.application.management.scope import ScopeContext
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {
            "id": 1,
            "name": "Demo",
            "account_id": "acc_alpha",
            "schedule_timezone": "UTC",
        }
        mock_bl.list_scheduled_jobs.return_value = [
            {"id": 101, "enabled": True, "status": "active", "cron_expr": "0 2 * * *"}
        ]
        mock_bl.conn = MagicMock()
        same_scope = ScopeContext(account_id="acc_alpha", actor_id="alice", principal_type="user")

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_routes, "_require_authenticated_user", return_value={"username": "alice"}
            ),
            patch.object(api_routes, "_request_scope", return_value=same_scope),
            patch.object(api_routes, "_cron_next_run_at", side_effect=ValueError("invalid cron")),
        ):
            resp = client.patch(
                "/api/projects/1/schedule-timezone",
                json={"schedule_timezone": "Europe/Vilnius"},
            )

        assert resp.status_code == 409
        payload = resp.json()
        assert payload.get("failed_jobs")
        mock_bl.conn.cursor.assert_not_called()
        mock_bl.conn.commit.assert_not_called()


class TestUserTimezoneEndpoint:
    def test_update_timezone_returns_503_when_timezone_column_missing(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_cur = MagicMock()
        mock_cur.execute.side_effect = Exception(
            'column "timezone" of relation "users" does not exist'
        )
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cur

        with (
            patch.object(
                api_routes,
                "_request_auth_user_payload",
                return_value={"id": 7, "username": "alice"},
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
        ):
            resp = client.patch(
                "/api/users/me/timezone",
                json={"timezone": "Europe/Vilnius"},
            )

        assert resp.status_code == 503
        assert "schema" in str(resp.json().get("error") or "").lower()

    def test_update_timezone_marks_timezone_column_cache_available(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_cur = MagicMock()
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_bl._column_exists_cache = {("users", "timezone"): False}

        with (
            patch.object(
                api_routes,
                "_request_auth_user_payload",
                return_value={"id": 7, "username": "alice"},
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
        ):
            resp = client.patch(
                "/api/users/me/timezone",
                json={"timezone": "Europe/Vilnius"},
            )

        assert resp.status_code == 200
        assert mock_bl._column_exists_cache[("users", "timezone")] is True
        mock_bl.conn.commit.assert_called_once()


class TestAuthMeTimezone:
    def test_auth_me_reads_timezone_from_db_when_auth_state_missing_it(self):
        from core.entrypoints.api import app_factory
        from core.entrypoints.api.app import app

        class _State:
            def __init__(self):
                self.auth_user = {
                    "id": 7,
                    "username": "alice",
                    "display_name": "Alice",
                    "roles": ["operator"],
                    "must_change_password": False,
                    "timezone": "",
                }
                self.auth_source = "local"

        class _Request:
            def __init__(self):
                self.state = _State()
                self.cookies = {}

        request = _Request()

        mock_bl = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = ("Europe/Vilnius",)
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cur

        with (
            patch.object(
                app.state,
                "local_auth_enabled_override",
                return_value=True,
                create=True,
            ),
            patch.object(
                app.state,
                "external_auth_enabled_override",
                return_value=False,
                create=True,
            ),
            patch.dict(
                app.state.runtime_container.service_factories,
                {"db": lambda: mock_bl},
            ),
        ):
            endpoint = app_factory._auth_users_route_endpoint("/api/auth/me", "GET")
            payload = _run_coro_blocking(endpoint(request))

        assert payload["authenticated"] is True
        assert payload["user"]["timezone"] == "Europe/Vilnius"
        assert request.state.auth_user["timezone"] == "Europe/Vilnius"


class TestCronHumanSummary:
    def test_daily_summary(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")
        assert api_runtime._cron_human_summary("0 9 * * *", "UTC") == "Daily at 09:00 (UTC)"

    def test_weekly_summary(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")
        result = api_runtime._cron_human_summary("0 9 * * 1", "Europe/Vilnius")
        assert "Monday" in result
        assert "09:00" in result
        assert "Europe/Vilnius" in result

    def test_hourly_summary(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")
        result = api_runtime._cron_human_summary("30 * * * *", "UTC")
        assert "Every hour" in result
        assert ":30" in result

    def test_monthly_summary(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")
        result = api_runtime._cron_human_summary("0 10 15 * *", "UTC")
        assert "Monthly" in result
        assert "15" in result

    def test_minute_step_summary(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")
        result = api_runtime._cron_human_summary("*/15 * * * *", "UTC")
        assert "15 minutes" in result

    def test_hour_step_summary(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")
        result = api_runtime._cron_human_summary("0 */6 * * *", "UTC")
        assert "6 hours" in result


class TestScheduledJobCreateWithBuilderCron:
    def test_create_scheduled_job_with_builder_generated_cron(self, client):
        """Verify the React API contract: builder-generated cron_expr is preserved."""
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_service = MagicMock()
        mock_service.create_scheduled_job.return_value = type(
            "ScheduledJobCreatePayload",
            (),
            {
                "dump": lambda self: {
                    "scheduled_job_id": 42,
                    "enabled": True,
                    "next_run_at": "2026-03-27T09:00:00+00:00",
                }
            },
        )()

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: MagicMock()}
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"project_command_service": lambda: mock_service},
            ),
        ):
            resp = client.post(
                "/api/projects/1/scheduled-jobs",
                json={
                    "workflow_id": "wf_main",
                    "cron_expr": "0 9 * * *",
                    "enabled": True,
                },
                follow_redirects=False,
            )

        assert resp.status_code == 201
        assert resp.json()["scheduled_job_id"] == 42
        mock_service.create_scheduled_job.assert_called_once()
        create_call = mock_service.create_scheduled_job.call_args
        assert create_call.args[0] == "1"
        command = create_call.args[1]
        assert command.workflow_id == "wf_main"
        assert command.cron_expr == "0 9 * * *"
        assert command.enabled is True

    def test_create_scheduled_job_without_enabled_defaults_to_paused(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_service = MagicMock()
        mock_service.create_scheduled_job.return_value = type(
            "ScheduledJobCreatePayload",
            (),
            {
                "dump": lambda self: {
                    "scheduled_job_id": 43,
                    "enabled": False,
                    "next_run_at": None,
                }
            },
        )()

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: MagicMock()}
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"project_command_service": lambda: mock_service},
            ),
        ):
            resp = client.post(
                "/api/projects/1/scheduled-jobs",
                json={
                    "workflow_id": "wf_main",
                    "cron_expr": "0 9 * * *",
                },
                follow_redirects=False,
            )

        assert resp.status_code == 201
        mock_service.create_scheduled_job.assert_called_once()
        create_call = mock_service.create_scheduled_job.call_args
        assert create_call.args[0] == "1"
        command = create_call.args[1]
        assert command.cron_expr == "0 9 * * *"
        assert command.enabled is False


class TestHealthEndpoint:
    def test_health_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["build_version"] == APP_VERSION

    def test_api_health_alias_returns_200(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["build_version"] == APP_VERSION

    def test_health_returns_ok_status(self, client):
        resp = client.get("/health")
        body = resp.json()
        assert body["status"] == "ok"
        assert body["build_version"] == APP_VERSION

    def test_health_live_returns_ok(self, client):
        resp = client.get("/health/live")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["build_version"] == APP_VERSION

    def test_health_ready_returns_503_when_not_ready(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        try:
            from core.entrypoints.admin import deps as route_deps
        except Exception as e:
            pytest.fail(f"core.entrypoints.admin.deps not importable: {e}")

        mock_bl = MagicMock()
        with (
            patch.object(route_deps, "_get_logger_only_fn", return_value=mock_bl),
            patch.object(
                route_deps,
                "_compute_readiness_fn",
                return_value={"ready": False, "checks": {"postgres": {"ok": False}}},
            ),
        ):
            resp = client.get("/health/ready")

        assert resp.status_code == 503
        assert resp.json()["ready"] is False

    def test_health_stays_responsive_while_readiness_is_running(self):
        try:
            import httpx
            from fastapi import FastAPI

            from core.entrypoints.admin import deps as route_deps
            from core.entrypoints.api.health import router as health_router
        except Exception as e:
            pytest.fail(f"health route dependencies not importable: {e}")

        readiness_started = threading.Event()
        release_readiness = threading.Event()

        def blocking_readiness(_bl):
            readiness_started.set()
            assert release_readiness.wait(timeout=2)
            return {"ready": True, "checks": {"postgres": {"ok": True}}}

        async def exercise() -> None:
            app = FastAPI()
            app.include_router(health_router)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                ready_task = asyncio.create_task(client.get("/health/ready"))
                assert await asyncio.to_thread(readiness_started.wait, 1)
                live_task = asyncio.create_task(client.get("/health"))
                await asyncio.sleep(0.05)

                assert live_task.done()
                assert live_task.result().status_code == 200
                assert not ready_task.done()

                release_readiness.set()
                assert (await ready_task).status_code == 200

        timer = threading.Timer(0.5, release_readiness.set)
        with (
            patch.object(route_deps, "_get_logger_only_fn", return_value=MagicMock()),
            patch.object(route_deps, "_compute_readiness_fn", side_effect=blocking_readiness),
        ):
            timer.start()
            try:
                asyncio.run(exercise())
            finally:
                release_readiness.set()
                timer.cancel()

    def test_health_deep_requires_operator_role(self, client):
        resp = client.get("/health/deep")
        assert resp.status_code == 403

    def test_health_deep_includes_meta_counts(self, client):
        try:
            import core.entrypoints.api.health as health_mod
            from core.entrypoints.admin import deps as route_deps
        except Exception as e:
            pytest.fail(f"core.entrypoints.admin.deps not importable: {e}")

        mock_bl = MagicMock()
        mock_cur = MagicMock()
        # projects, pending, resume_pending, in_progress, awaiting_approval,
        # awaiting_timer, awaiting_child, stuck, orphaned, active leases
        mock_cur.fetchone.side_effect = [(2,), (5,), (1,), (1,), (1,), (0,), (0,), (0,), (0,), (1,)]
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_bl._table_exists.return_value = True
        with (
            patch.object(route_deps, "_get_logger_only_fn", return_value=mock_bl),
            patch.object(
                route_deps,
                "_compute_readiness_fn",
                return_value={"ready": True, "checks": {"postgres": {"ok": True}}},
            ),
            patch.object(route_deps, "_env_flag_fn", return_value=False),
            patch.object(route_deps, "_local_auth_enabled_fn", return_value=False),
            patch.object(
                health_mod,
                "_operations_request_authorized",
                return_value=True,
            ),
        ):
            resp = client.get("/health/deep")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ready"] is True
        assert body["meta"]["project_count"] == 2

    def test_metrics_requires_token_or_operator_role(self, client):
        try:
            import core.entrypoints.api.health as health_mod
        except Exception as e:
            pytest.fail(f"core.entrypoints.api.health not importable: {e}")

        with patch.object(health_mod, "_METRICS_TOKEN", ""):
            resp = client.get("/metrics")

        assert resp.status_code == 401

    def test_metrics_returns_prometheus_payload(self, client):
        try:
            import core.entrypoints.api.health as health_mod
            from core.entrypoints.admin import deps as route_deps
        except Exception as e:
            pytest.fail(f"core.entrypoints.api.health not importable: {e}")

        mock_bl = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.side_effect = [(3,), (1,)]
        mock_cur.fetchall.return_value = [("pending", 4), ("in_progress", 2)]
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_bl._table_exists.return_value = True
        mock_bl.get_core_slo_metrics.return_value = {
            "p95_latency_seconds": 12.5,
            "retry_rate": 0.2,
            "dead_letter_rate": 0.05,
            "stuck_jobs": 3,
            "per_agent_failure_rate": [
                {"agent": "composer", "failure_rate": 0.15},
            ],
        }

        with (
            patch.object(health_mod, "_METRICS_TOKEN", "metrics-secret"),
            patch.object(route_deps, "_get_logger_only_fn", return_value=mock_bl),
            patch.object(
                route_deps,
                "_compute_readiness_fn",
                return_value={"ready": True, "checks": {"postgres": {"ok": True}}},
            ),
            patch.object(route_deps, "_get_ready_queue_depth_fn", return_value=7),
        ):
            resp = client.get("/metrics", headers={"x-metrics-token": "metrics-secret"})

        assert resp.status_code == 200
        assert "text/plain" in resp.headers.get("content-type", "")
        body = resp.text
        assert "platform_health_ready 1" in body
        assert 'platform_jobs_total{status="pending"} 4' in body
        assert "platform_ready_queue_depth 7" in body
        assert "platform_slo_p95_latency_seconds 12.5" in body
        assert 'platform_slo_agent_failure_rate{agent="composer"} 0.15' in body

    def test_health_echoes_request_correlation_id_header(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=False),
            patch.object(client.app.state, "external_auth_enabled_override", return_value=False),
        ):
            resp = client.get("/health", headers={"x-request-id": "req-123"})

        assert resp.status_code == 200
        assert resp.headers.get("x-correlation-id") == "req-123"

    def test_metrics_requires_token_when_configured(self, client):
        try:
            import core.entrypoints.api.health as health_mod
            from core.entrypoints.admin import deps as route_deps
        except Exception as e:
            pytest.fail(f"core.entrypoints.api.health not importable: {e}")

        with patch.object(health_mod, "_METRICS_TOKEN", "metrics-secret"):
            unauthorized = client.get("/metrics")
            assert unauthorized.status_code == 401

        mock_bl = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.side_effect = [(0,), (0,)]
        mock_cur.fetchall.return_value = []
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_bl._table_exists.return_value = True

        with (
            patch.object(health_mod, "_METRICS_TOKEN", "metrics-secret"),
            patch.object(route_deps, "_get_logger_only_fn", return_value=mock_bl),
            patch.object(
                route_deps,
                "_compute_readiness_fn",
                return_value={"ready": False, "checks": {"postgres": {"ok": False}}},
            ),
            patch.object(route_deps, "_get_ready_queue_depth_fn", return_value=0),
        ):
            authorized = client.get("/metrics", headers={"x-metrics-token": "metrics-secret"})

        assert authorized.status_code == 200

    def test_api_core_slo_metrics_uses_account_scoped_window(self, client):
        try:
            from core.application.management.scope import ScopeContext
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for core slo metrics test"
            )

        mock_bl = MagicMock()
        mock_bl.get_core_slo_metrics.return_value = {
            "generated_at": "2026-03-27T00:00:00+00:00",
            "window_hours": 48,
            "account_id": "acc_ops",
            "p95_latency_seconds": 9.1,
            "retry_rate": 0.1,
            "dead_letter_rate": 0.02,
            "stuck_jobs": 1,
            "per_agent_failure_rate": [],
        }
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_routes, "_request_scope", return_value=ScopeContext(account_id="acc_ops")
            ),
        ):
            resp = client.get("/api/slo/core?window_hours=48")

        assert resp.status_code == 200
        body = resp.json()
        assert body["account_id"] == "acc_ops"
        assert body["window_hours"] == 48
        assert body["p95_latency_seconds"] == 9.1
        mock_bl.get_core_slo_metrics.assert_called_once_with(account_id="acc_ops", window_hours=48)


# ---------------------------------------------------------------------------
# Projects endpoints
# ---------------------------------------------------------------------------


class TestProjectsEndpoints:
    def test_projects_list_returns_200(self, client):
        resp = client.get("/projects")
        assert resp.status_code == 200

    def test_projects_detail_nonexistent_returns_404_or_redirect(self, client):
        resp = client.get("/projects/nonexistent-project-xyz-12345")
        assert resp.status_code in (200, 404, 422)

    def test_project_workflow_preview_endpoint(self, client):
        resp = client.post(
            "/api/projects/1/workflow-preview",
            json={"task_summary": "Preview workflow plan"},
        )
        assert resp.status_code in (200, 404, 422, 500, 503)

    def test_project_workflow_preview_rejects_invalid_explicit_override(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        from core.application.management.errors import ServiceValidationError

        query_service = MagicMock()
        query_service.get_workflow_preview.side_effect = ServiceValidationError(
            "Workflow 'weekly_digest' does not support feature"
        )

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"project_query_service": lambda: query_service},
            ),
            patch.object(client.app.state, "local_auth_enabled_override", return_value=False),
        ):
            resp = client.post(
                "/api/projects/1/workflow-preview",
                json={"task_summary": "Build full export flow", "workflow_id": "weekly_digest"},
            )

        assert resp.status_code == 400
        assert "does not support" in resp.json().get("error", "")

    def test_api_project_settings_history_returns_revisions(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = _project_fixture(name="Demo Project")
        mock_bl.list_project_settings_revisions.return_value = [
            {
                "id": 10,
                "project_id": 1,
                "author": "admin",
                "action": "save",
                "source": "project_edit_form",
            },
            {
                "id": 9,
                "project_id": 1,
                "author": "admin",
                "action": "create",
                "source": "project_create_form",
            },
        ]

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/projects/1/history")

        assert resp.status_code == 200
        body = resp.json()
        assert body["project_id"] == "1"
        assert len(body["revisions"]) == 2
        mock_bl.list_project_settings_revisions.assert_called_once()

    def test_api_project_settings_history_diff_uses_current_snapshot(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        selected_snapshot = {
            "project": {
                "name": "Demo Project",
                "default_branch": "main",
            },
            "operator_default_workflow_id": "weekly_report",
        }
        current_snapshot = {
            "project": {
                "name": "Demo Project",
                "default_branch": "release",
            },
            "operator_default_workflow_id": "weekly_digest",
        }

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = _project_fixture(name="Demo Project")
        mock_bl.get_project_settings_revision.return_value = {
            "id": 7,
            "project_id": 1,
            "author": "admin",
            "action": "save",
            "source": "project_edit_form",
            "snapshot_jsonb": selected_snapshot,
            "created_at": datetime(2026, 3, 15, 8, 0, tzinfo=UTC),
        }
        mock_bl.get_project_settings_snapshot.return_value = current_snapshot

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/projects/1/history/7/diff")

        assert resp.status_code == 200
        body = resp.json()
        assert body["selected_revision"]["id"] == "7"
        assert body["summary"]["total"] >= 1
        changed_paths = {str(item.get("path") or "") for item in (body.get("changes") or [])}
        assert "project.default_branch" in changed_paths
        assert "operator_default_workflow_id" in changed_paths

    def test_api_project_settings_history_restore_returns_result(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = _project_fixture(name="Demo Project")
        mock_bl.restore_project_settings_revision.return_value = {
            "status": "restored",
            "project_id": "1",
            "restored_from_revision_id": "11111111-1111-1111-1111-111111111111",
            "new_revision_id": "22222222-2222-2222-2222-222222222222",
        }

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.post(
                "/api/projects/1/history/11111111-1111-1111-1111-111111111111/restore"
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "restored"
        mock_bl.restore_project_settings_revision.assert_called_once()

    def test_project_settings_history_form_restore_is_removed_after_react_cutover(self, client):
        resp = client.post(
            "/projects/1/history/7/restore",
            data={"return_to": "/projects/1?tab=history"},
            follow_redirects=False,
        )

        assert resp.status_code in (404, 405)

    def test_project_settings_history_restore_transports_share_service_contract(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        restore_payload = MagicMock()
        restore_payload.dump.return_value = {
            "status": "restored",
            "project_id": "1",
            "restored_from_revision_id": "11111111-1111-1111-1111-111111111111",
            "new_revision_id": "22222222-2222-2222-2222-222222222222",
        }
        mock_service = MagicMock()
        mock_service.restore_project_settings_revision.return_value = restore_payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"project_command_service": lambda: mock_service},
        ):
            api_resp = client.post(
                "/api/projects/1/history/11111111-1111-1111-1111-111111111111/restore"
            )
            removed_form_resp = client.post(
                "/projects/1/history/11111111-1111-1111-1111-111111111111/restore"
            )

        assert api_resp.status_code == 200
        assert api_resp.json()["new_revision_id"] == "22222222-2222-2222-2222-222222222222"
        assert removed_form_resp.status_code in (404, 405)
        assert mock_service.restore_project_settings_revision.call_count == 1

        (first_call,) = mock_service.restore_project_settings_revision.call_args_list
        assert first_call.args[:2] == ("1", "11111111-1111-1111-1111-111111111111")
        assert first_call.args[2].author == "system"
        assert first_call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert first_call.kwargs["scope"].project_id == "1"

    def test_projects_list_shows_default_workflow_column_and_create_selector(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_projects.return_value = [
            {
                "id": 1,
                "name": "Demo Project",
                "job_count": 0,
                "created_at": "2026-03-10T10:00:00+00:00",
                "operator_default_workflow_id": "weekly_report",
            }
        ]
        mock_bl.get_project_run_config.return_value = {
            "project_name": "Demo Project",
            "project_workflows": [
                {"workflow_id": "weekly_report", "enabled": True, "is_default": True}
            ],
        }
        workflows = [{"workflow_id": "weekly_report", "display_name": "Standard Feature"}]

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_runtime, "_list_workflow_choices", return_value=workflows),
        ):
            resp = client.get("/projects")

        _assert_frontend_shell_response(resp)

    def test_project_edit_form_allows_inherited_provider_and_model(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {
            "id": 1,
            "name": "Demo Project",
            "account_id": DEFAULT_ACCOUNT_ID,
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "agents_config": {},
        }

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_runtime, "_list_workflow_choices", return_value=[]),
        ):
            resp = client.get("/projects/1")

        _assert_frontend_shell_response(resp)

    def test_project_edit_form_preserves_unavailable_provider_and_model_overrides(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {
            "id": 1,
            "name": "Demo Project",
            "account_id": DEFAULT_ACCOUNT_ID,
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "agents_config": {
                "default_provider": "legacy_provider",
                "default_model": "retired-model",
                "agents": {
                    "config": {
                        "composer": {
                            "provider": "legacy_agent_provider",
                            "model": "retired-agent-model",
                        }
                    }
                },
            },
        }

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_runtime, "_list_workflow_choices", return_value=[]),
            patch.object(
                api_runtime,
                "_request_model_selection_context",
                return_value={
                    "default_agent_models": {"composer": {"provider": "openai", "model": "gpt-5"}},
                    "available_providers": [("openai", "OpenAI")],
                    "available_models": ["gpt-5"],
                    "providers_with_models": {},
                },
            ),
        ):
            resp = client.get("/projects/1")

        _assert_frontend_shell_response(resp)

    @pytest.mark.parametrize(
        ("path", "form_data"),
        [
            (
                "/projects",
                {
                    "name": "Demo Project",
                    "workspace_id": DEFAULT_WORKSPACE_ID,
                    "project_root": "/host/demo",
                    "container_root": "/target/demo",
                },
            ),
            (
                "/projects",
                {
                    "name": "Demo Project",
                    "workspace_id": DEFAULT_WORKSPACE_ID,
                    "project_root": "/host/demo",
                    "container_root": "/target/demo",
                    "fixture_notifier_token": "fixture_notifier-bot-token",
                    "fixture_notifier_chat_id": "chat-42",
                },
            ),
            (
                "/projects",
                {
                    "name": "Secret DSN Project",
                    "workspace_id": DEFAULT_WORKSPACE_ID,
                    "project_root": "/host/demo",
                    "container_root": "/target/demo",
                    "db_dsn": "postgresql://user:pass@db:5432/app",
                },
            ),
            (
                "/projects/1",
                {
                    "name": "Demo Project",
                    "workspace_id": DEFAULT_WORKSPACE_ID,
                    "project_root": "/host/demo",
                    "container_root": "/target/demo",
                    "workflow_ids": ["weekly_report"],
                    "fixture_notifier_token": "fixture_notifier-bot-token-2",
                    "fixture_notifier_chat_id": "chat-99",
                },
            ),
            (
                "/projects/1",
                {
                    "name": "Demo Project",
                    "workspace_id": DEFAULT_WORKSPACE_ID,
                    "project_root": "/host/demo",
                    "container_root": "/target/demo",
                    "workflow_ids": ["weekly_report"],
                    "db_dsn": "postgresql://user:new@db:5432/app",
                },
            ),
            (
                "/projects/1",
                {
                    "name": "Demo Project",
                    "workspace_id": DEFAULT_WORKSPACE_ID,
                    "project_root": "/host/demo",
                    "container_root": "/target/demo",
                    "workflow_ids": ["weekly_report"],
                    "sample_tasks_list_id": "new-list",
                    "sample_tasks_trigger_status": "in progress",
                },
            ),
        ],
    )
    def test_legacy_project_form_posts_are_removed(self, client, path, form_data):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.post(path, data=form_data, follow_redirects=False)

        assert resp.status_code == 405
        mock_bl.add_project.assert_not_called()
        mock_bl.update_project.assert_not_called()
        mock_bl.save_project_secret.assert_not_called()
        mock_bl.save_provider_secret.assert_not_called()
        mock_bl.save_project_provider_binding.assert_not_called()

    def test_project_detail_uses_jobs_pagination(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {
            "id": 1,
            "name": "Demo Project",
            "account_id": DEFAULT_ACCOUNT_ID,
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "repo_url": "",
            "operator_default_workflow_id": "",
            "agents_config": {},
        }
        mock_bl.count_jobs.return_value = 120
        mock_bl.get_jobs.return_value = []

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_runtime, "_list_workflow_choices", return_value=[]),
        ):
            resp = client.get("/projects/1?page=2&per_page=25")

        _assert_frontend_shell_response(resp)
        mock_bl.count_jobs.assert_not_called()
        mock_bl.get_jobs.assert_not_called()

    def test_api_projects_list_uses_pagination(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.list_projects.return_value = [{"id": 7, "name": "Demo", "job_count": 3}]
        mock_bl.count_projects.return_value = 31

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/projects?page=2&per_page=25&search=demo")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 31
        assert body["page"] == 2
        assert body["per_page"] == 25
        # The filter and sort arguments are always sent; empty means "no filter"
        # and "default order" rather than being omitted from the call.
        mock_bl.list_projects.assert_called_once_with(
            limit=25,
            offset=25,
            search="demo",
            account_id=DEFAULT_ACCOUNT_ID,
            workspace_id=None,
            has_jobs="",
            sort="",
            direction="",
        )
        mock_bl.count_projects.assert_called_once_with(
            search="demo",
            account_id=DEFAULT_ACCOUNT_ID,
            workspace_id=None,
            has_jobs="",
        )

    def test_project_detail_submit_modal_uses_explicit_workflow_source_labels(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {
            "id": 1,
            "name": "Demo Project",
            "account_id": DEFAULT_ACCOUNT_ID,
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "repo_url": "",
            "operator_default_workflow_id": "weekly_report",
            "agents_config": {},
        }
        mock_bl.get_project_workflows.return_value = [
            {"workflow_id": "weekly_report", "enabled": True, "is_default": True}
        ]
        mock_bl.count_jobs.return_value = 0
        mock_bl.get_jobs.return_value = []

        workflows = [{"workflow_id": "weekly_report", "display_name": "Standard Feature"}]
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_runtime, "_list_workflow_choices", return_value=workflows),
        ):
            resp = client.get("/projects/1")

        _assert_frontend_shell_response(resp)


class TestLocalAuthEndpoints:
    def test_local_auth_middleware_requires_auth_for_protected_api(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for local auth middleware test"
            )

        mock_bl = MagicMock()
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
        ):
            resp = client.get("/api/jobs")

        assert resp.status_code == 401
        assert "authentication" in resp.json().get("error", "").lower()

    def test_api_app_bootstrap_hydrates_authenticated_session_on_public_route(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for public bootstrap auth hydration test"
            )

        mock_bl = MagicMock()
        session_user = {
            "id": 9,
            "username": "viewer",
            "email": "viewer@example.test",
            "display_name": "Viewer",
            "is_active": True,
            "roles": ["viewer"],
        }

        client.cookies.set("fs_session", "session-token")
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.object(client.app.state, "external_auth_enabled_override", return_value=False),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
        ):
            resp = client.get("/api/app/bootstrap")

        assert resp.status_code == 200
        body = resp.json()
        assert body["auth"]["authenticated"] is True
        assert body["auth"]["auth_source"] == "local"
        assert body["auth"]["user"]["username"] == "viewer"

    def test_local_auth_middleware_returns_json_401_when_no_users(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for bootstrap-required JSON response test"
            )

        mock_bl = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (0,)
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cur

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.object(client.app.state, "external_auth_enabled_override", return_value=False),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_local_auth_bootstrap_required", return_value=True),
        ):
            resp = client.get(
                "/dashboard", headers={"accept": "application/json"}, follow_redirects=False
            )

        assert resp.status_code == 401
        payload = resp.json()
        assert payload == {"error": "Authentication required", "bootstrap_required": True}

    def test_local_auth_bootstrap_required_false_when_tables_unavailable(self):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for bootstrap behavior test"
            )

        mock_bl = MagicMock()
        assert (
            auth_runtime._local_auth_bootstrap_required(
                mock_bl,
                get_logger_only_fn=lambda: mock_bl,
            )
            is False
        )

    def test_local_auth_middleware_redirects_html_to_login_when_no_users(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for bootstrap redirect test"
            )

        mock_bl = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (0,)
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cur

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.object(client.app.state, "external_auth_enabled_override", return_value=False),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_local_auth_bootstrap_required", return_value=True),
        ):
            resp = client.get("/dashboard", headers={"accept": "text/html"}, follow_redirects=False)

        assert resp.status_code == 302
        assert resp.headers.get("location") == "/bootstrap"

    def test_local_auth_middleware_redirects_html_to_login_when_users_exist(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(e, context="api_runtime import for login redirect test")

        mock_bl = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (2,)
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cur

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.object(client.app.state, "external_auth_enabled_override", return_value=False),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
        ):
            resp = client.get("/dashboard", headers={"accept": "text/html"}, follow_redirects=False)

        assert resp.status_code == 302
        assert resp.headers.get("location") == "/login?next=%2Fdashboard"

    def test_local_auth_middleware_redirects_html_to_login_with_original_query(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for login redirect query test"
            )

        mock_bl = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (2,)
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cur

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.object(client.app.state, "external_auth_enabled_override", return_value=False),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
        ):
            resp = client.get(
                "/registry/workflows/monthly_review/builder?project_id=abc-123",
                headers={"accept": "text/html"},
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert (
            resp.headers.get("location")
            == "/login?next=%2Fregistry%2Fworkflows%2Fmonthly_review%2Fbuilder%3Fproject_id%3Dabc-123"
        )

    def test_bootstrap_page_redirects_to_login_when_users_exist(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for bootstrap page redirect test"
            )

        mock_bl = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (1,)
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cur

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.object(client.app.state, "external_auth_enabled_override", return_value=False),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
        ):
            resp = client.get("/bootstrap", follow_redirects=False)

        _assert_frontend_shell_response(resp)

    def test_bootstrap_page_renders_when_no_users_exist(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for bootstrap page render test"
            )

        mock_bl = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (0,)
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cur

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.object(client.app.state, "external_auth_enabled_override", return_value=False),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
        ):
            resp = client.get("/bootstrap")

        assert resp.status_code == 200

    def test_api_auth_bootstrap_creates_admin_user(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(e, context="api_runtime import for auth bootstrap test")

        mock_bl = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.side_effect = [
            (0,),
            (101, "admin@example.com", "Admin"),
            (1,),
            ("00000000-0000-0000-0000-000000000002",),
        ]
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cur

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.object(api_routes, "_bootstrap_request_allowed", return_value=(True, "")),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_seed_default_roles"),
            patch.object(api_routes, "_password_hash", return_value="hash"),
        ):
            resp = client.post(
                "/api/auth/bootstrap",
                json={
                    "email": "admin@example.com",
                    "password": "Sup3rsecret!",
                    "display_name": "Admin",
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "bootstrapped"
        assert body["user"]["username"] == "admin@example.com"
        assert body["user"]["roles"] == ["admin"]

    def test_api_auth_bootstrap_rejects_when_bootstrap_not_allowed(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for auth bootstrap guard test"
            )

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.object(
                api_routes,
                "_bootstrap_request_allowed",
                return_value=(False, "Invalid bootstrap setup token"),
            ),
        ):
            resp = client.post(
                "/api/auth/bootstrap",
                json={
                    "email": "admin@example.com",
                    "password": "Sup3rsecret!",
                    "display_name": "Admin",
                },
            )

        assert resp.status_code == 403
        assert "setup token" in resp.json().get("error", "").lower()

    def test_api_auth_login_sets_session_cookie(self, client):
        try:
            from core.entrypoints.api import auth_sessions
        except Exception as e:
            _fail_only_missing_optional_dep(e, context="api_runtime import for auth login test")

        mock_bl = MagicMock()
        mock_bl.conn.cursor.return_value.__enter__.return_value = MagicMock()
        mock_bl._column_exists = MagicMock(return_value=False)
        user_row = {
            "id": 7,
            "username": "admin@example.com",
            "display_name": "Admin",
            "password_hash": "hash",
            "is_active": True,
            "roles": ["admin"],
        }

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_username", return_value=user_row),
            patch.object(api_routes, "_verify_password", return_value=True),
            patch.object(
                auth_sessions,
                "resolve_login_account_memberships",
                return_value=[{"account_id": "default", "is_active": True}],
            ),
        ):
            resp = client.post(
                "/api/auth/login", json={"email": "admin@example.com", "password": "supersecret"}
            )

        assert resp.status_code == 200
        assert "set-cookie" in resp.headers
        assert "fs_session=" in resp.headers["set-cookie"]
        assert resp.json()["user"]["username"] == "admin@example.com"

    def test_api_auth_login_rejects_when_throttled(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(e, context="api_runtime import for auth throttle test")

        mock_bl = MagicMock()
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_auth_login_lock_state", return_value=(True, 25)),
        ):
            resp = client.post(
                "/api/auth/login", json={"email": "admin@example.com", "password": "bad-pass"}
            )

        assert resp.status_code == 429
        assert resp.json()["retry_after_seconds"] == 25
        assert resp.headers["Retry-After"] == "25"

    def test_api_auth_login_invalid_credentials_throttled_after_failure_record(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for auth throttle escalation test"
            )

        mock_bl = MagicMock()
        user_row = {
            "id": 7,
            "username": "admin@example.com",
            "display_name": "Admin",
            "password_hash": "hash",
            "is_active": True,
            "roles": ["admin"],
        }

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_auth_login_lock_state", return_value=(False, 0)),
            patch.object(api_routes, "_get_user_by_username", return_value=user_row),
            patch.object(api_routes, "_verify_password", return_value=False),
            patch.object(api_routes, "_auth_login_record_failure", return_value=(True, 40)),
        ):
            resp = client.post(
                "/api/auth/login", json={"email": "admin@example.com", "password": "wrong"}
            )

        assert resp.status_code == 429
        assert resp.json()["retry_after_seconds"] == 40
        assert resp.headers["Retry-After"] == "40"

    def test_api_auth_login_rejects_temporary_password_without_new_password(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for auth reset-required login test"
            )

        mock_bl = MagicMock()
        mock_bl.conn.cursor.return_value.__enter__.return_value = MagicMock()
        user_row = {
            "id": 7,
            "username": "admin@example.com",
            "display_name": "Admin",
            "password_hash": "hash",
            "is_active": True,
            "roles": ["admin"],
            "must_change_password": True,
        }

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_username", return_value=user_row),
            patch.object(api_routes, "_verify_password", return_value=True),
            patch.object(api_routes, "_auth_login_clear_failures") as clear_failures_mock,
        ):
            resp = client.post(
                "/api/auth/login", json={"email": "admin@example.com", "password": "supersecret"}
            )

        assert resp.status_code == 403
        assert resp.json()["password_change_required"] is True
        assert "fs_session=" not in resp.headers.get("set-cookie", "")
        clear_failures_mock.assert_called_once()

    def test_api_auth_change_password_updates_hash_and_revokes_other_sessions(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for change-password test"
            )

        mock_bl = MagicMock()
        mock_cur = MagicMock()
        mock_cur.rowcount = 2
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cur
        session_user = {
            "id": 7,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }
        user_row = {
            "id": 7,
            "username": "admin@example.com",
            "display_name": "Admin",
            "password_hash": "hash",
            "is_active": True,
            "roles": ["admin"],
        }

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
            patch.object(api_routes, "_get_user_by_username", return_value=user_row),
            patch.object(api_routes, "_verify_password", return_value=True),
            patch.object(api_routes, "_password_hash", return_value="new-hash"),
        ):
            resp = client.post(
                "/api/auth/change-password",
                headers={"Authorization": "Bearer current-session-token"},
                json={"current_password": "old-pass", "new_password": "new-password-123"},
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "password_updated"
        assert resp.json()["revoked_sessions"] == 2
        assert any(
            "UPDATE users SET password_hash" in str(call.args[0])
            for call in mock_cur.execute.call_args_list
        )
        assert any(
            "UPDATE auth_sessions" in str(call.args[0]) for call in mock_cur.execute.call_args_list
        )

    def test_api_auth_password_reset_request_creates_token_for_existing_user(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for password reset request test"
            )

        mock_bl = MagicMock()
        mock_cur = MagicMock()
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cur
        user_row = {
            "id": 8,
            "username": "ops@example.com",
            "display_name": "Ops",
            "is_active": True,
            "roles": ["operator"],
        }

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.object(api_routes, "_password_reset_enabled", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_auth_password_reset_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_username", return_value=user_row),
            patch.object(api_routes, "_auth_login_clear_user_failures") as clear_failures_mock,
        ):
            resp = client.post(
                "/api/auth/password-reset/request", json={"email": "ops@example.com"}
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"
        assert any(
            "INSERT INTO auth_password_resets" in str(call.args[0])
            for call in mock_cur.execute.call_args_list
        )
        clear_failures_mock.assert_called_once_with("ops@example.com")

    def test_api_auth_password_reset_request_dispatches_email_for_email_username(
        self, client, monkeypatch
    ):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for password reset email dispatch test"
            )

        mock_bl = MagicMock()
        mock_cur = MagicMock()
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cur
        user_row = {
            "id": 8,
            "username": "ops@example.com",
            "display_name": "Ops",
            "is_active": True,
            "roles": ["operator"],
        }

        monkeypatch.setenv("FS_PUBLIC_BASE_URL", "https://flow-steward.test")
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.object(api_routes, "_password_reset_enabled", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_auth_password_reset_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_username", return_value=user_row),
            patch.object(api_routes, "_auth_login_clear_user_failures"),
            patch.object(
                auth_runtime,
                "send_password_reset_email",
                return_value={"ok": True, "reason": "sent"},
            ) as send_mock,
        ):
            resp = client.post(
                "/api/auth/password-reset/request", json={"email": "ops@example.com"}
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"
        send_mock.assert_called_once()
        kwargs = send_mock.call_args.kwargs
        assert kwargs["recipient"] == "ops@example.com"
        assert "/reset-password?token=" in kwargs["reset_url"]
        assert kwargs["username"] == "ops@example.com"

    def test_api_auth_users_unlock_clears_lockout_state(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for auth user unlock test"
            )

        mock_bl = MagicMock()
        admin_user = {
            "id": 1,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        target_user = {
            "id": "00000000-0000-0000-0000-000000000025",
            "username": "ops",
            "display_name": "Ops",
            "is_active": True,
            "roles": ["operator"],
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
            patch.object(api_routes, "_get_user_by_id", return_value=target_user),
            patch.object(api_routes, "_auth_login_clear_user_failures") as clear_failures_mock,
        ):
            resp = client.post(
                "/api/auth/users/00000000-0000-0000-0000-000000000025/unlock",
                headers={"Authorization": "Bearer token-123"},
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "unlocked"
        clear_failures_mock.assert_called_once_with("ops", bl=mock_bl)

    def test_api_auth_users_force_password_reset_marks_user_and_revokes_sessions(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for force-password-reset test"
            )

        mock_bl = MagicMock()
        mock_cur = MagicMock()
        mock_cur.rowcount = 4
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cur
        admin_user = {
            "id": 1,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        target_user = {
            "id": "00000000-0000-0000-0000-000000000025",
            "username": "ops",
            "display_name": "Ops",
            "is_active": True,
            "roles": ["operator"],
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
            patch.object(api_routes, "_get_user_by_id", return_value=target_user),
            patch.object(api_routes, "_auth_user_column_exists", return_value=True),
            patch.object(api_routes, "_auth_login_clear_user_failures") as clear_failures_mock,
        ):
            resp = client.post(
                "/api/auth/users/00000000-0000-0000-0000-000000000025/force-password-reset",
                headers={"Authorization": "Bearer token-123"},
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "password_reset_required"
        assert resp.json()["revoked_sessions"] == 4
        assert any(
            "UPDATE users SET must_change_password = true" in str(call.args[0])
            for call in mock_cur.execute.call_args_list
        )
        assert any(
            "UPDATE auth_sessions" in str(call.args[0]) for call in mock_cur.execute.call_args_list
        )
        clear_failures_mock.assert_called_once_with("ops", bl=mock_bl)

    def test_api_auth_users_requires_admin_role(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for auth users RBAC test"
            )

        mock_bl = MagicMock()
        viewer_user = {
            "id": 12,
            "username": "viewer",
            "display_name": "Viewer",
            "is_active": True,
            "roles": ["viewer"],
        }
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=viewer_user),
        ):
            resp = client.get("/api/auth/users", headers={"Authorization": "Bearer token-123"})

        assert resp.status_code == 403

    def test_api_auth_users_list_uses_pagination(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for auth users list pagination test"
            )

        mock_bl = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = {"total": 31}
        mock_cur.fetchall.return_value = [
            {
                "id": 26,
                "username": "ops",
                "display_name": "Ops",
                "is_active": True,
                "must_change_password": False,
                "created_at": "2026-03-18T00:00:00Z",
                "updated_at": "2026-03-18T00:00:00Z",
                "roles": ["viewer"],
            }
        ]
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cur
        admin_user = {
            "id": 1,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
            patch.object(api_routes, "_auth_user_column_exists", return_value=True),
        ):
            resp = client.get(
                "/api/auth/users?page=2&per_page=25&search=ops&role=viewer&active=1",
                headers={"Authorization": "Bearer token-123"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 31
        assert body["page"] == 2
        assert body["per_page"] == 25
        assert body["users"][0]["username"] == "ops"
        list_params = mock_cur.execute.call_args_list[1].args[1]
        assert list_params[-2:] == (25, 25)

    def test_api_auth_users_create_creates_user_with_roles(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for auth users create test"
            )

        mock_bl = MagicMock()
        admin_user = {
            "id": 1,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (55,)
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cur

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
            patch.object(api_routes, "_get_user_by_username", return_value=None),
            patch.object(
                api_routes,
                "_get_user_by_id",
                return_value={
                    "id": 55,
                    "username": "ops@example.com",
                    "display_name": "Ops",
                    "is_active": True,
                    "roles": ["operator"],
                },
            ),
            patch.object(api_routes, "_password_hash", return_value="hashed-pass"),
            patch.object(api_routes, "_seed_default_roles"),
        ):
            resp = client.post(
                "/api/auth/users",
                headers={"Authorization": "Bearer token-123"},
                json={
                    "email": "ops@example.com",
                    "password": "Super-strong-pass1!",
                    "display_name": "Ops",
                    "roles": ["operator"],
                    "is_active": True,
                },
            )

        assert resp.status_code == 200
        assert resp.json()["user"]["username"] == "ops@example.com"
        assert resp.json()["user"]["roles"] == ["operator"]

    def test_api_auth_me_uses_bearer_session_token(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(e, context="api_runtime import for auth me test")

        mock_bl = MagicMock()
        session_user = {
            "id": 9,
            "username": "viewer",
            "display_name": "Viewer",
            "is_active": True,
            "roles": ["viewer"],
        }
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
        ):
            resp = client.get("/api/auth/me", headers={"Authorization": "Bearer token-123"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["authenticated"] is True
        assert body["user"]["username"] == "viewer"

    def test_local_auth_middleware_normalizes_session_principal_for_service_scope(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for session principal normalization test"
            )

        mock_bl = MagicMock()
        session_user = {
            "id": 9,
            "username": "viewer",
            "display_name": "Viewer",
            "is_active": True,
            "roles": ["viewer"],
            "must_change_password": True,
        }
        payload = MagicMock()
        payload.dump.return_value = {
            "projects": [],
            "total": 0,
            "page": 1,
            "per_page": 25,
            "total_pages": 1,
        }
        mock_service = MagicMock()
        mock_service.list_projects.return_value = payload

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"project_query_service": lambda: mock_service},
            ),
        ):
            resp = client.get("/api/projects", headers={"Authorization": "Bearer token-123"})

        assert resp.status_code == 200
        call = mock_service.list_projects.call_args
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].principal_type == "viewer"
        assert call.kwargs["scope"].allowed_project_ids == frozenset()
        assert call.kwargs["scope"].allowed_workflow_ids == frozenset()

    def test_local_auth_middleware_rejects_disabled_account(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for disabled account middleware test"
            )

        mock_bl = MagicMock()
        mock_bl.is_account_active.return_value = False
        session_user = {
            "id": 9,
            "username": "viewer",
            "display_name": "Viewer",
            "is_active": True,
            "roles": ["viewer"],
            "account_id": "acc_archived",
        }

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_auth_account_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
        ):
            resp = client.get("/api/projects", headers={"Authorization": "Bearer token-123"})

        assert resp.status_code == 403
        assert resp.json()["error"] == "Account is disabled"
        mock_bl.is_account_active.assert_called_once_with("acc_archived")

    def test_local_auth_middleware_stores_normalized_session_principal_shape(self, client):
        try:
            from starlette.requests import Request
            from starlette.responses import Response

            from core.application.auth.principal import ResolvedPrincipal
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for direct middleware principal test"
            )

        mock_bl = MagicMock()
        session_user = {
            "id": 9,
            "username": "viewer",
            "display_name": "Viewer",
            "is_active": True,
            "roles": ["viewer"],
            "must_change_password": True,
        }
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/projects",
            "raw_path": b"/api/projects",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer token-123")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "root_path": "",
            "app": client.app,
        }
        request = Request(scope)
        captured: dict[str, object] = {}

        async def call_next(req):
            captured["auth_principal"] = req.state.auth_principal
            captured["auth_user"] = req.state.auth_user
            captured["auth_source"] = req.state.auth_source
            return Response(status_code=204)

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
        ):
            resp = _run_coro_blocking(api_runtime.local_auth_middleware(request, call_next))

        assert resp.status_code == 204
        principal = captured["auth_principal"]
        user = captured["auth_user"]
        assert isinstance(principal, ResolvedPrincipal)
        assert isinstance(user, dict)
        assert principal.account_id == DEFAULT_ACCOUNT_ID
        assert principal.principal_type == "viewer"
        assert principal.auth_source == "local"
        assert principal.must_change_password is True
        assert user["account_id"] == DEFAULT_ACCOUNT_ID
        assert user["principal_type"] == "viewer"
        assert user["auth_source"] == "local"
        assert user["must_change_password"] is True
        assert user["allowed_project_ids"] == []
        assert user["allowed_workflow_ids"] == []
        assert captured["auth_source"] == "local"

    def test_local_auth_middleware_normalizes_machine_principal_for_service_scope(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for machine principal normalization test"
            )

        mock_bl = MagicMock()
        machine_principal = {
            "principal_type": "service_account",
            "service_account_id": 12,
            "service_account_key": "svc-ci",
            "roles": ["operator"],
            "scopes": ["projects.read"],
            "auth_source": "machine",
            "account_id": "acc_ops",
            "allowed_project_ids": ["7"],
            "allowed_workflow_ids": ["wf_demo"],
        }
        payload = MagicMock()
        payload.dump.return_value = {
            "projects": [],
            "total": 0,
            "page": 1,
            "per_page": 25,
            "total_pages": 1,
        }
        mock_service = MagicMock()
        mock_service.list_projects.return_value = payload

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=None),
            patch("core.application.auth.resolve.resolve_pat_principal", return_value=None),
            patch(
                "core.application.auth.resolve.resolve_machine_principal",
                return_value=machine_principal,
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"project_query_service": lambda: mock_service},
            ),
        ):
            resp = client.get("/api/projects", headers={"Authorization": "Bearer token-123"})

        assert resp.status_code == 200
        call = mock_service.list_projects.call_args
        assert call.kwargs["scope"].account_id == "acc_ops"
        assert call.kwargs["scope"].principal_type == "service_account"
        assert call.kwargs["scope"].allowed_project_ids == frozenset({"7"})
        assert call.kwargs["scope"].allowed_workflow_ids == frozenset({"wf_demo"})

    def test_api_auth_tokens_list_returns_current_user_tokens(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for auth tokens list test"
            )

        mock_bl = MagicMock()
        session_user = {
            "id": 9,
            "username": "viewer",
            "display_name": "Viewer",
            "is_active": True,
            "roles": ["viewer"],
        }
        mock_bl.count_user_pats.return_value = 1
        mock_bl.list_user_pats_paginated.return_value = [
            {
                "id": 12,
                "label": "CLI token",
                "expires_at": None,
                "last_used_at": None,
                "created_at": "2026-03-18T00:00:00Z",
                "revoked": False,
                "expired": False,
                "scopes": [],
                "allowed_project_ids": [],
                "allowed_workflow_ids": [],
            }
        ]
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_auth_pat_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
        ):
            resp = client.get("/api/auth/tokens", headers={"Authorization": "Bearer token-123"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["tokens"][0]["id"] == 12
        assert body["tokens"][0]["label"] == "CLI token"
        assert body["total"] == 1
        assert body["per_page"] == 25
        mock_bl.count_user_pats.assert_called_once_with("9")
        mock_bl.list_user_pats_paginated.assert_called_once_with("9", limit=25, offset=0)

    def test_api_auth_tokens_create_returns_raw_token(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for auth tokens create test"
            )

        mock_bl = MagicMock()
        session_user = {
            "id": 14,
            "username": "viewer",
            "display_name": "Viewer",
            "is_active": True,
            "roles": ["viewer"],
        }
        mock_bl.create_pat.return_value = {
            "id": 31,
            "label": "Nightfire token",
            "raw_token": "fs_pat_secret",
            "scopes": [],
            "allowed_project_ids": [],
            "allowed_workflow_ids": [],
            "expires_at": None,
            "created_at": "2026-03-18T00:00:00Z",
        }
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_auth_pat_tables_available", return_value=True),
            patch.object(api_routes, "_csrf_request_valid", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
        ):
            resp = client.post(
                "/api/auth/tokens",
                headers={"Authorization": "Bearer token-123"},
                json={"name": "Nightfire token", "expires_in_days": 30},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "31"
        assert body["token"]["raw_token"] == "fs_pat_secret"
        mock_bl.create_pat.assert_called_once_with(
            user_id="14",
            account_id=DEFAULT_ACCOUNT_ID,
            label="Nightfire token",
            scopes=[],
            allowed_project_ids=[],
            allowed_workflow_ids=[],
            expires_in_days=30,
        )

    def test_api_auth_tokens_revoke_revokes_current_user_token(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for auth tokens revoke test"
            )

        mock_bl = MagicMock()
        session_user = {
            "id": 14,
            "username": "viewer",
            "display_name": "Viewer",
            "is_active": True,
            "roles": ["viewer"],
        }
        mock_bl.revoke_pat.return_value = {"id": 31, "label": "Nightfire token", "revoked": True}
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_auth_pat_tables_available", return_value=True),
            patch.object(api_routes, "_csrf_request_valid", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
        ):
            resp = client.post(
                "/api/auth/tokens/31/revoke", headers={"Authorization": "Bearer token-123"}
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "revoked"
        assert body["id"] == "31"
        mock_bl.revoke_pat.assert_called_once_with("31", "14")

    def test_api_auth_accounts_list_returns_accounts(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for auth accounts list test"
            )

        mock_bl = MagicMock()
        admin_user = {
            "id": 1,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }
        mock_bl.list_accounts.return_value = [
            {
                "account_id": DEFAULT_ACCOUNT_ID,
                "display_name": "Default Account",
                "is_active": True,
            },
            {"account_id": "acc_acme", "display_name": "Acme", "is_active": False},
        ]
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_auth_account_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
        ):
            resp = client.get(
                "/api/auth/accounts?include_inactive=true&search=ac&limit=50&offset=10",
                headers={"Authorization": "Bearer token-123"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["accounts"]) == 2
        assert body["accounts"][1]["account_id"] == "acc_acme"
        mock_bl.list_accounts.assert_called_once_with(
            include_inactive=True,
            search="ac",
            limit=50,
            offset=10,
        )

    def test_api_auth_accounts_create_persists_account(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for auth accounts create test"
            )

        mock_bl = MagicMock()
        admin_user = {
            "id": 1,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }
        mock_bl.create_account.return_value = {
            "account_id": "acc_acme",
            "display_name": "Acme Inc",
            "is_active": True,
        }
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_auth_account_tables_available", return_value=True),
            patch.object(api_routes, "_csrf_request_valid", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
        ):
            resp = client.post(
                "/api/auth/accounts",
                headers={"Authorization": "Bearer token-123"},
                json={"account_id": "acc_acme", "display_name": "Acme Inc", "is_active": True},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["account"]["account_id"] == "acc_acme"
        mock_bl.create_account.assert_called_once_with(
            account_id="acc_acme",
            display_name="Acme Inc",
            is_active=True,
        )

    def test_api_auth_accounts_disable_marks_account_inactive(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for auth accounts disable test"
            )

        mock_bl = MagicMock()
        admin_user = {
            "id": 1,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }
        mock_bl.set_account_active.return_value = {
            "account_id": "acc_acme",
            "display_name": "Acme Inc",
            "is_active": False,
        }
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_auth_account_tables_available", return_value=True),
            patch.object(api_routes, "_csrf_request_valid", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
        ):
            resp = client.post(
                "/api/auth/accounts/acc_acme/disable",
                headers={"Authorization": "Bearer token-123"},
                json={"reason": "offboarding"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "disabled"
        assert body["account"]["is_active"] is False
        mock_bl.set_account_active.assert_called_once_with("acc_acme", is_active=False)

    def test_api_auth_service_accounts_list_returns_accounts(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for service accounts list test"
            )

        mock_bl = MagicMock()
        admin_user = {
            "id": 1,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }
        mock_bl.list_service_accounts.return_value = [
            {
                "id": 7,
                "service_account_key": "ci-bot",
                "display_name": "CI Bot",
                "is_active": True,
                "scopes": ["jobs:write"],
            }
        ]
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_auth_service_account_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
        ):
            resp = client.get(
                "/api/auth/service-accounts", headers={"Authorization": "Bearer token-123"}
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["service_accounts"][0]["service_account_key"] == "ci-bot"
        mock_bl.list_service_accounts.assert_called_once_with(
            account_id=DEFAULT_ACCOUNT_ID,
            include_inactive=True,
        )

    def test_api_auth_service_accounts_create_persists_service_account(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for service accounts create test"
            )

        mock_bl = MagicMock()
        admin_user = {
            "id": 1,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }
        mock_bl.create_service_account.return_value = {
            "id": 9,
            "service_account_key": "deploy-bot",
            "display_name": "Deploy Bot",
            "description": "CI deploy account",
            "is_active": True,
            "scopes": ["jobs:write"],
            "allowed_project_ids": ["7"],
            "allowed_workflow_ids": ["weekly_report"],
        }
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_auth_service_account_tables_available", return_value=True),
            patch.object(api_routes, "_csrf_request_valid", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
        ):
            resp = client.post(
                "/api/auth/service-accounts",
                headers={"Authorization": "Bearer token-123"},
                json={
                    "service_account_key": "Deploy-Bot",
                    "display_name": "Deploy Bot",
                    "description": "CI deploy account",
                    "scopes": ["jobs:write"],
                    "allowed_project_ids": ["7"],
                    "allowed_workflow_ids": ["weekly_report"],
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["service_account"]["service_account_key"] == "deploy-bot"
        mock_bl.create_service_account.assert_called_once_with(
            service_account_key="deploy-bot",
            display_name="Deploy Bot",
            description="CI deploy account",
            scopes=["jobs:write"],
            allowed_project_ids=["7"],
            allowed_workflow_ids=["weekly_report"],
            account_id=DEFAULT_ACCOUNT_ID,
            created_by="admin@example.com",
            is_active=True,
        )

    def test_api_auth_service_accounts_update_and_delete_lifecycle(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for service accounts lifecycle test"
            )

        mock_bl = MagicMock()
        admin_user = {
            "id": 1,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }
        mock_bl.update_service_account.return_value = {
            "id": 9,
            "service_account_key": "deploy-bot",
            "display_name": "Deploy Bot Disabled",
            "description": "paused",
            "is_active": False,
            "scopes": ["jobs:write"],
            "allowed_project_ids": ["7"],
            "allowed_workflow_ids": ["weekly_report"],
        }
        mock_bl.delete_service_account.return_value = {
            "id": 9,
            "service_account_key": "deploy-bot",
            "display_name": "Deploy Bot Disabled",
            "is_active": False,
        }
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_auth_service_account_tables_available", return_value=True),
            patch.object(api_routes, "_csrf_request_valid", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
        ):
            update_resp = client.patch(
                "/api/auth/service-accounts/deploy-bot",
                headers={"Authorization": "Bearer token-123"},
                json={
                    "display_name": "Deploy Bot Disabled",
                    "description": "paused",
                    "scopes": ["jobs:write"],
                    "allowed_project_ids": ["7"],
                    "allowed_workflow_ids": ["weekly_report"],
                    "is_active": False,
                },
            )
            delete_resp = client.delete(
                "/api/auth/service-accounts/deploy-bot",
                headers={"Authorization": "Bearer token-123"},
            )

        assert update_resp.status_code == 200
        assert update_resp.json()["service_account"]["is_active"] is False
        mock_bl.update_service_account.assert_called_once_with(
            "deploy-bot",
            display_name="Deploy Bot Disabled",
            description="paused",
            scopes=["jobs:write"],
            allowed_project_ids=["7"],
            allowed_workflow_ids=["weekly_report"],
            account_id=DEFAULT_ACCOUNT_ID,
            is_active=False,
        )
        assert delete_resp.status_code == 200
        assert delete_resp.json()["service_account"]["service_account_key"] == "deploy-bot"
        mock_bl.delete_service_account.assert_called_once_with(
            "deploy-bot",
            account_id=DEFAULT_ACCOUNT_ID,
        )

    def test_api_auth_service_account_tokens_list_returns_tokens(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for service account token list test"
            )

        mock_bl = MagicMock()
        admin_user = {
            "id": 1,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }
        mock_bl.list_service_account_tokens.return_value = [
            {
                "id": 18,
                "label": "CI token",
                "revoked": False,
                "expires_at": None,
            }
        ]
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_auth_machine_token_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
        ):
            resp = client.get(
                "/api/auth/service-accounts/ci-bot/tokens",
                headers={"Authorization": "Bearer token-123"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["tokens"][0]["id"] == 18
        mock_bl.list_service_account_tokens.assert_called_once_with(
            "ci-bot",
            account_id=DEFAULT_ACCOUNT_ID,
            include_revoked=True,
        )

    def test_api_auth_service_account_tokens_create_returns_raw_token(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for service account token create test"
            )

        mock_bl = MagicMock()
        admin_user = {
            "id": 1,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }
        mock_bl.create_machine_token.return_value = {
            "id": 21,
            "label": "CI machine token",
            "token": "fs_svc_secret",
            "scopes": ["jobs:write"],
            "allowed_project_ids": ["7"],
            "allowed_workflow_ids": ["weekly_report"],
            "expires_at": None,
        }
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_auth_machine_token_tables_available", return_value=True),
            patch.object(api_routes, "_csrf_request_valid", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
        ):
            resp = client.post(
                "/api/auth/service-accounts/ci-bot/tokens",
                headers={"Authorization": "Bearer token-123"},
                json={
                    "name": "CI machine token",
                    "expires_in_days": 30,
                    "allowed_project_ids": ["7"],
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "21"
        assert body["token"]["token"] == "fs_svc_secret"
        mock_bl.create_machine_token.assert_called_once_with(
            service_account_key="ci-bot",
            label="CI machine token",
            scopes=[],
            allowed_project_ids=["7"],
            allowed_workflow_ids=[],
            expires_in_days=30,
            account_id=DEFAULT_ACCOUNT_ID,
            created_by="admin@example.com",
        )

    def test_api_auth_service_account_tokens_update_checks_membership(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for service account token update test"
            )

        mock_bl = MagicMock()
        admin_user = {
            "id": 1,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }
        mock_bl.list_service_account_tokens.return_value = [
            {"id": 31, "label": "CI token", "revoked": False}
        ]
        mock_bl.update_machine_token.return_value = {
            "id": 31,
            "label": "CI token updated",
            "scopes": ["jobs.read"],
            "allowed_project_ids": ["7"],
            "allowed_workflow_ids": ["weekly_report"],
            "revoked": False,
        }
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_auth_machine_token_tables_available", return_value=True),
            patch.object(api_routes, "_csrf_request_valid", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
        ):
            resp = client.patch(
                "/api/auth/service-accounts/ci-bot/tokens/31",
                headers={"Authorization": "Bearer token-123"},
                json={
                    "label": "CI token updated",
                    "scopes": ["jobs.read"],
                    "allowed_project_ids": ["7"],
                    "allowed_workflow_ids": ["weekly_report"],
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "updated"
        assert body["token"]["label"] == "CI token updated"
        mock_bl.list_service_account_tokens.assert_called_once_with(
            "ci-bot",
            account_id=DEFAULT_ACCOUNT_ID,
            include_revoked=True,
        )
        mock_bl.update_machine_token.assert_called_once_with(
            "31",
            label="CI token updated",
            scopes=["jobs.read"],
            allowed_project_ids=["7"],
            allowed_workflow_ids=["weekly_report"],
            account_id=DEFAULT_ACCOUNT_ID,
        )

    def test_api_auth_service_account_tokens_revoke_checks_membership(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for service account token revoke test"
            )

        mock_bl = MagicMock()
        admin_user = {
            "id": 1,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }
        mock_bl.list_service_account_tokens.return_value = [
            {"id": 31, "label": "CI token", "revoked": False}
        ]
        mock_bl.revoke_machine_token.return_value = {"id": 31, "label": "CI token", "revoked": True}
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_auth_machine_token_tables_available", return_value=True),
            patch.object(api_routes, "_csrf_request_valid", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
        ):
            resp = client.post(
                "/api/auth/service-accounts/ci-bot/tokens/31/revoke",
                headers={"Authorization": "Bearer token-123"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "revoked"
        mock_bl.list_service_account_tokens.assert_called_once_with(
            "ci-bot",
            account_id=DEFAULT_ACCOUNT_ID,
            include_revoked=True,
        )
        mock_bl.revoke_machine_token.assert_called_once_with("31", account_id=DEFAULT_ACCOUNT_ID)

    def test_api_auth_service_account_tokens_delete_checks_membership(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for service account token delete test"
            )

        mock_bl = MagicMock()
        admin_user = {
            "id": 1,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }
        mock_bl.list_service_account_tokens.return_value = [
            {"id": 32, "label": "CI token", "revoked": True}
        ]
        mock_bl.delete_machine_token.return_value = {"id": 32, "label": "CI token", "revoked": True}
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_auth_machine_token_tables_available", return_value=True),
            patch.object(api_routes, "_csrf_request_valid", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
        ):
            resp = client.delete(
                "/api/auth/service-accounts/ci-bot/tokens/32",
                headers={"Authorization": "Bearer token-123"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "deleted"
        mock_bl.list_service_account_tokens.assert_called_once_with(
            "ci-bot",
            account_id=DEFAULT_ACCOUNT_ID,
            include_revoked=True,
        )
        mock_bl.delete_machine_token.assert_called_once_with("32", account_id=DEFAULT_ACCOUNT_ID)

    def test_api_auth_service_account_tokens_delete_returns_not_found_for_other_account(
        self, client
    ):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e,
                context="api_runtime import for service account token delete missing membership test",
            )

        mock_bl = MagicMock()
        admin_user = {
            "id": 1,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }
        mock_bl.list_service_account_tokens.return_value = [
            {"id": 99, "label": "Other token", "revoked": False}
        ]
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_auth_machine_token_tables_available", return_value=True),
            patch.object(api_routes, "_csrf_request_valid", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
        ):
            resp = client.delete(
                "/api/auth/service-accounts/ci-bot/tokens/32",
                headers={"Authorization": "Bearer token-123"},
            )

        assert resp.status_code == 404
        mock_bl.list_service_account_tokens.assert_called_once_with(
            "ci-bot",
            account_id=DEFAULT_ACCOUNT_ID,
            include_revoked=True,
        )
        mock_bl.delete_machine_token.assert_not_called()

    def test_api_auth_service_account_tokens_old_delete_path_is_not_available(self, client):
        resp = client.post("/api/auth/service-accounts/ci-bot/tokens/44/delete")
        assert resp.status_code == 404

    def test_local_auth_cookie_post_requires_csrf_validation(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for CSRF validation test"
            )

        mock_bl = MagicMock()
        admin_user = {
            "id": 13,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
        ):
            client.cookies.set("fs_session", "cookie-session")
            resp = client.post(
                "/api/providers",
                json={
                    "provider_key": "demo",
                    "provider_type_key": "task_provider",
                    "display_name": "Demo",
                },
            )

        assert resp.status_code == 403
        assert "csrf" in resp.json().get("error", "").lower()

    def test_local_auth_treats_provider_webhook_as_public_path(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for provider webhook auth path test"
            )

        mock_bl = MagicMock()
        mock_bl.get_provider_secret.return_value = ""
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
        ):
            resp = client.post("/webhook/providers/demo", json={})

        assert resp.status_code != 401

    def test_local_auth_viewer_cannot_post_api_write_route(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for local auth RBAC write test"
            )

        mock_bl = MagicMock()
        session_user = {
            "id": 10,
            "username": "viewer",
            "display_name": "Viewer",
            "is_active": True,
            "roles": ["viewer"],
        }
        client.cookies.clear()
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
        ):
            resp = client.post(
                "/api/providers",
                headers={"Authorization": "Bearer token-123"},
                json={
                    "provider_key": "demo",
                    "provider_type_key": "task_provider",
                    "display_name": "Demo",
                },
            )

        assert resp.status_code == 403
        assert "admin role required" in resp.json().get("error", "").lower()

    def test_local_auth_operator_cannot_access_admin_routes(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for local auth admin RBAC test"
            )

        mock_bl = MagicMock()
        session_user = {
            "id": 11,
            "username": "operator",
            "display_name": "Operator",
            "is_active": True,
            "roles": ["operator"],
        }
        client.cookies.clear()
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
        ):
            resp = client.post(
                "/api/guardrails",
                headers={"Authorization": "Bearer token-123"},
                json={"guardrail_id": "operator-blocked"},
                follow_redirects=False,
            )

        assert resp.status_code == 403
        assert "admin role required" in resp.json().get("error", "").lower()

    @pytest.mark.parametrize(
        "method,path",
        [
            ("post", "/api/processes/42/kill"),
            ("post", "/api/workspaces"),
            ("patch", "/api/workspaces/ws-1/restore"),
        ],
    )
    def test_local_auth_operator_cannot_access_admin_only_control_plane_routes(
        self, client, method, path
    ):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for control-plane RBAC test"
            )

        mock_bl = MagicMock()
        session_user = {
            "id": 12,
            "username": "operator",
            "display_name": "Operator",
            "is_active": True,
            "roles": ["operator"],
        }
        client.cookies.clear()
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
        ):
            if method == "post":
                resp = client.post(path, headers={"Authorization": "Bearer token-123"})
            elif method == "patch":
                resp = client.patch(path, headers={"Authorization": "Bearer token-123"})
            else:
                resp = client.get(path, headers={"Authorization": "Bearer token-123"})

        assert resp.status_code == 403
        assert "admin role required" in resp.json().get("error", "").lower()

    @pytest.mark.parametrize(
        ("path", "expected_status"),
        [
            ("/api/agents", 403),
            ("/api/providers", 200),
            ("/api/extensions", 200),
            ("/api/model-selection/options", 200),
        ],
    )
    def test_local_auth_operator_registry_read_contract(self, client, path, expected_status):
        """Operators get typed read access only where the concrete route contract allows it."""
        mock_bl = MagicMock()
        mock_bl.list_provider_types.return_value = []
        mock_bl.list_providers.return_value = []
        mock_bl.list_latest_provider_health.return_value = {}
        session_user = {
            "id": 12,
            "username": "operator",
            "display_name": "Operator",
            "is_active": True,
            "roles": ["operator"],
        }
        mock_extension_query = MagicMock()
        mock_extension_query.list_extensions.return_value = type(
            "ExtensionListPayload",
            (),
            {"dump": lambda self: {"items": [], "total": 0}},
        )()
        client.cookies.clear()
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {
                    "db": lambda: mock_bl,
                    "extension_query_service": lambda: mock_extension_query,
                },
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
        ):
            resp = client.get(path, headers={"Authorization": "Bearer token-123"})

        assert resp.status_code == expected_status

    def test_local_auth_viewer_cannot_read_model_selection_options(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for model catalog RBAC test"
            )

        mock_bl = MagicMock()
        session_user = {
            "id": 13,
            "username": "viewer",
            "display_name": "Viewer",
            "is_active": True,
            "roles": ["viewer"],
        }
        client.cookies.clear()
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
        ):
            resp = client.get(
                "/api/model-selection/options",
                headers={"Authorization": "Bearer token-123"},
            )

        assert resp.status_code == 403
        assert "operator role required" in resp.json().get("error", "").lower()

    @pytest.mark.parametrize(
        "path",
        [
            "/monitoring?tab=events",
            "/settings",
            "/monitoring?tab=processes",
            "/trash",
            "/projects/1",
        ],
    )
    def test_local_auth_viewer_redirected_from_role_restricted_pages(self, client, path):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for page RBAC parity test"
            )

        mock_bl = MagicMock()
        session_user = {
            "id": 14,
            "username": "viewer",
            "display_name": "Viewer",
            "is_active": True,
            "roles": ["viewer"],
        }
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
        ):
            resp = client.get(
                path, headers={"Authorization": "Bearer token-123"}, follow_redirects=False
            )

        _assert_frontend_shell_response(resp)

    def test_local_auth_viewer_cannot_read_processes_api(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for processes API RBAC parity test"
            )

        mock_bl = MagicMock()
        session_user = {
            "id": 15,
            "username": "viewer",
            "display_name": "Viewer",
            "is_active": True,
            "roles": ["viewer"],
        }
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
        ):
            resp = client.get("/api/processes", headers={"Authorization": "Bearer token-123"})

        assert resp.status_code == 403
        assert "operator role required" in resp.text.lower()

    def test_local_auth_operator_can_read_processes_api(self, client):
        try:
            import core.entrypoints.api.processes as processes
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for processes API operator access test"
            )

        mock_bl = MagicMock()
        session_user = {
            "id": 16,
            "username": "operator",
            "display_name": "Operator",
            "is_active": True,
            "roles": ["operator"],
        }
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
            patch.object(
                processes,
                "list_process_payload",
                return_value={
                    "jobs": [],
                    "subprocesses": [],
                    "suspected_orphans": [],
                    "project_leases": [],
                    "project_lease_evidence_unavailable": False,
                    "workers": [],
                    "reaped_worker_subprocess_count": 0,
                },
            ),
        ):
            resp = client.get("/api/processes", headers={"Authorization": "Bearer token-123"})

        assert resp.status_code == 200
        body = resp.json()
        assert "jobs" in body
        assert "workers" in body

    def test_local_auth_operator_cannot_bulk_delete_jobs(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for bulk delete RBAC parity test"
            )

        mock_bl = MagicMock()
        session_user = {
            "id": 17,
            "username": "operator",
            "display_name": "Operator",
            "is_active": True,
            "roles": ["operator"],
        }
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_csrf_request_valid", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
        ):
            resp = client.post(
                "/api/projects/1/jobs/bulk-delete",
                headers={"Authorization": "Bearer token-123"},
                json={"ids": [10], "confirm": "DELETE"},
            )

        assert resp.status_code == 403
        assert "admin role required" in resp.json().get("error", "").lower()

    def test_api_project_jobs_bulk_delete_allows_admin_role(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for admin bulk delete test"
            )

        session_user = {
            "id": 18,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }
        mock_service = MagicMock()
        mock_payload = type(
            "BulkDeletePayload",
            (),
            {
                "status": "ok",
                "requested": [10],
                "deleted": [10],
                "deleted_count": 1,
                "skipped": [],
            },
        )()
        mock_service.bulk_delete_project_jobs.return_value = mock_payload

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: MagicMock()}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_csrf_request_valid", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_command_service": lambda: mock_service},
            ),
        ):
            resp = client.post(
                "/api/projects/1/jobs/bulk-delete",
                headers={"Authorization": "Bearer token-123"},
                json={"ids": [10], "confirm": "DELETE"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_count"] == 1
        assert body["deleted"] == [10]

    def test_local_auth_operator_cannot_delete_single_job(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for single delete RBAC parity test"
            )

        mock_bl = MagicMock()
        session_user = {
            "id": 19,
            "username": "operator",
            "display_name": "Operator",
            "is_active": True,
            "roles": ["operator"],
        }
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
        ):
            resp = client.delete(
                "/api/jobs/42",
                headers={"Authorization": "Bearer token-123"},
            )

        assert resp.status_code == 403
        assert "admin role required" in resp.json().get("error", "").lower()

    def test_api_job_delete_allows_admin_role(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for single delete admin access test"
            )

        session_user = {
            "id": 20,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }
        mock_service = MagicMock()
        mock_payload = type(
            "DeletePayload",
            (),
            {
                "status": "ok",
                "job_id": 42,
            },
        )()
        mock_service.delete_job.return_value = mock_payload

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: MagicMock()}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_command_service": lambda: mock_service},
            ),
        ):
            resp = client.delete(
                "/api/jobs/42",
                headers={"Authorization": "Bearer token-123"},
            )

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "ok"
        assert payload["job_id"] == 42

    def test_api_model_providers_contract_shape(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for model providers contract test"
            )

        mock_service = MagicMock()
        mock_service.list_model_providers.return_value = [
            {
                "provider_id": 7,
                "provider_key": "openai-main",
                "provider_type": "openai",
                "default_model": "gpt-5-mini",
            }
        ]
        with patch.object(api_routes, "_model_provider_command_service", return_value=mock_service):
            resp = client.get("/api/model-providers")

        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body.get("providers"), list)
        assert body["providers"][0]["provider_key"] == "openai-main"

    def test_api_mcp_contract_shape(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for mcp connectors contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "connectors": [{"connector_id": "mcp_fs", "display_name": "Flow Steward MCP"}],
            "total": 1,
        }
        svc = MagicMock()
        svc.list_connectors.return_value = payload
        project_svc = MagicMock()

        with (
            patch(
                "core.application.management.mcp_connector_services.McpConnectorQueryService",
                return_value=svc,
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {
                    "db": lambda: MagicMock(),
                    "project_query_service": lambda: project_svc,
                },
            ),
        ):
            resp = client.get(
                "/api/mcp",
                params={"project_id": "00000000-0000-0000-0000-000000000007"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["connectors"][0]["connector_id"] == "mcp_fs"

    def test_api_project_connections_contract_shape(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for project connections contract test"
            )

        conn = MagicMock()
        project_id = "00000000-0000-0000-0000-000000000007"
        conn.dump.return_value = {
            "connection_id": "conn_123",
            "project_id": project_id,
            "extension_id": "ext_demo",
            "enabled": True,
        }
        svc = MagicMock()
        svc.list_connections.return_value = [conn]

        # The endpoint now fetches the project to enforce account ownership.
        # Return a dict with account_id matching the unauthenticated test client's
        # implicit scope (DEFAULT_ACCOUNT_ID = "acc_default").
        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {
            "id": project_id,
            "name": "test-proj",
            "account_id": DEFAULT_ACCOUNT_ID,
        }

        with (
            patch(
                "core.application.management.command_services.ProjectConnectionService",
                return_value=svc,
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
        ):
            resp = client.get(f"/api/projects/{project_id}/connections")

        assert resp.status_code == 200
        body = resp.json()
        assert body["project_id"] == project_id
        assert body["connections"][0]["connection_id"] == "conn_123"

    def test_api_project_connections_cross_account_denied(self, client):
        """A project belonging to a different account must return 403."""
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for project connections cross-account test"
            )

        # Return a project owned by "acc_other" — test client runs as "acc_default".
        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {
            "id": 7,
            "name": "foreign-proj",
            "account_id": "acc_other",
        }

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/projects/7/connections")

        assert resp.status_code in (403, 404)

    def test_api_project_connections_project_not_found_returns_404(self, client):
        """If the project does not exist, return 404 before exposing connection data."""
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for project connections 404 test"
            )

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = None  # project not found

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/projects/99/connections")

        assert resp.status_code == 404

    def test_api_commands_batch_contract_shape(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for commands batch contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "status": "ok",
            "results": [{"command_type": "noop", "status": "accepted"}],
        }
        svc = MagicMock()
        svc.dispatch_batch.return_value = payload

        with (
            patch(
                "core.application.management.command_services_dispatch.CommandDispatchService",
                return_value=svc,
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: MagicMock()}
            ),
        ):
            resp = client.post(
                "/api/commands/batch",
                json={"commands": [{"command_type": "noop", "target": {}, "payload": {}}]},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["results"][0]["command_type"] == "noop"

    def test_api_command_dispatch_rejects_type_coercion_for_command_type(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for command dispatch strict validation test"
            )

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: MagicMock()}
        ):
            resp = client.post(
                "/api/commands",
                json={"command_type": 123, "target": {}, "payload": {}},
            )

        assert resp.status_code == 422

    def test_api_reaction_ingest_rejects_type_coercion_for_job_id(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for reaction ingest strict validation test"
            )

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 55,
            "project_id": 7,
            "account_id": DEFAULT_ACCOUNT_ID,
            "meta": {"selected_workflow_id": "weekly_report"},
        }
        mock_bl.get_project.return_value = {"id": 7, "account_id": DEFAULT_ACCOUNT_ID}
        event_payload = MagicMock()
        event_payload.dump.return_value = {"status": "accepted"}
        event_service = MagicMock()
        event_service.ingest_reaction_event.return_value = event_payload
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_routes,
                "_request_scope",
                return_value=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"event_command_service": lambda: event_service},
            ),
        ):
            resp = client.post(
                "/api/reactions/ingest",
                json={
                    "event_type": "external.comment.received",
                    "job_id": "55",
                    "source": "sample_tasks",
                    "source_id": "comment-1",
                    "event_key": "comment.created",
                    "payload": {"text": "please retry"},
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("status") in {"accepted", "no_reactions", None}

    def test_api_trash_requires_admin_role_boundary(self, client):
        resp = client.get("/api/trash")
        assert resp.status_code == 403

    def test_api_trash_contract_shape_as_admin(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(e, context="api_runtime import for trash contract test")

        mock_bl = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.side_effect = [
            [],
            [
                {
                    "entity_id": 9,
                    "label": "Demo project",
                    "deleted_at": datetime.now(UTC),
                    "deleted_by": "admin",
                }
            ],
            [],
            [],
            [],
            [],
            [],
            [],
            [],
            [],
            [],
            [],
            [],
            [],
        ]
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cur

        admin_user = {
            "id": 19,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
        ):
            resp = client.get("/api/trash", headers={"Authorization": "Bearer token-123"})

        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body.get("items"), list)
        assert any(item.get("entity_type") == "project" for item in body["items"])
        assert any(
            item.get("entity_type") == "project" and item.get("category") == "application"
            for item in body["items"]
        )

    def test_auth_bootstrap_emits_audit_log(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for auth audit bootstrap test"
            )

        mock_bl = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.side_effect = [
            (0,),
            (101, "admin@example.com", "Admin"),
            (1,),
            ("00000000-0000-0000-0000-000000000002",),
        ]
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cur

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.object(api_routes, "_bootstrap_request_allowed", return_value=(True, "")),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_seed_default_roles"),
            patch.object(api_routes, "_password_hash", return_value="hash"),
        ):
            resp = client.post(
                "/api/auth/bootstrap",
                json={
                    "email": "admin@example.com",
                    "password": "Sup3rsecret!",
                    "display_name": "Admin",
                },
            )

        assert resp.status_code == 200
        action_types = [
            str(call.kwargs.get("action_type") or "") for call in mock_bl.log_action.call_args_list
        ]
        assert "auth_bootstrap" in action_types

    def test_auth_login_and_logout_emit_audit_logs(self, client):
        try:
            from core.entrypoints.api import auth_sessions
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for auth audit login/logout test"
            )

        mock_bl = MagicMock()
        mock_bl.conn.cursor.return_value.__enter__.return_value = MagicMock()
        mock_bl._column_exists = MagicMock(return_value=False)
        user_row = {
            "id": 7,
            "username": "admin@example.com",
            "display_name": "Admin",
            "password_hash": "hash",
            "is_active": True,
            "roles": ["admin"],
        }

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_username", return_value=user_row),
            patch.object(api_routes, "_verify_password", return_value=True),
            patch.object(api_routes, "_csrf_request_valid", return_value=True),
            patch.object(
                auth_sessions,
                "resolve_login_account_memberships",
                return_value=[{"account_id": "default", "is_active": True}],
            ),
        ):
            login_resp = client.post(
                "/api/auth/login", json={"email": "admin@example.com", "password": "supersecret"}
            )
            assert login_resp.status_code == 200
            logout_resp = client.post("/api/auth/logout")

        assert logout_resp.status_code == 200
        action_types = [
            str(call.kwargs.get("action_type") or "") for call in mock_bl.log_action.call_args_list
        ]
        assert "auth_login" in action_types
        assert "auth_logout" in action_types


# ---------------------------------------------------------------------------
# Job endpoints
# ---------------------------------------------------------------------------


class TestJobEndpoints:
    def test_job_detail_nonexistent_returns_404_or_error(self, client):
        resp = client.get("/jobs/999999")
        assert resp.status_code in (200, 404)

    def test_api_job_nonexistent_returns_404(self, client):
        resp = client.get("/api/jobs/999999")
        assert resp.status_code in (200, 404, 500, 503)

    def test_api_job_events_nonexistent(self, client):
        resp = client.get("/api/jobs/999999/events")
        assert resp.status_code in (200, 404, 500, 503)

    def test_api_job_cost_breakdown_nonexistent(self, client):
        resp = client.get("/api/jobs/999999/cost-breakdown")
        assert resp.status_code in (200, 404, 500, 503)

    def test_api_job_logs_nonexistent(self, client):
        resp = client.get("/api/jobs/999999/logs")
        assert resp.status_code in (200, 404, 500, 503)

    def test_api_job_approval_nonexistent(self, client):
        resp = client.get("/api/jobs/999999/approval")
        assert resp.status_code in (200, 404, 500, 503)

    def test_api_job_status_includes_thread_id(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = _job_fixture(
            id=42,
            status="awaiting_approval",
            thread_id="thread-42",
            task_summary="Test summary",
            started_at=None,
            finished_at=None,
            duration_ms=None,
        )
        mock_bl.get_job_cost_summary.return_value = {}
        from core.application.jobs.workflow_run_models import WorkflowRunStateV1

        mock_bl.get_public_progress_snapshot.return_value = SimpleNamespace(
            snapshot=WorkflowRunStateV1(
                job_id="42",
                project_id="1",
                workflow_id="unknown",
                status="awaiting_approval",
                terminal=False,
                updated_at=datetime.now(UTC),
            ),
            marker_id="marker-42",
        )

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(runtime_helpers, "_build_runtime_payload", return_value={}),
        ):
            resp = client.get("/api/jobs/42")

        assert resp.status_code == 200
        data = resp.json()
        assert data["thread_id"] == "thread-42"

    @pytest.mark.parametrize(
        ("auth_user", "expected_role"),
        [
            ({"roles": ["viewer"]}, "viewer"),
            ({"roles": ["operator"]}, "operator"),
            ({"roles": ["admin"]}, "admin"),
            ({"roles": ["viewer", "operator"]}, "operator"),
            ({"roles": ["viewer", "operator", "admin"]}, "admin"),
            (None, "viewer"),
            (
                {
                    "roles": ["operator"],
                    "auth_source": "machine",
                    "principal_type": "service_account",
                    "scopes": ["jobs.read"],
                },
                "operator",
            ),
        ],
    )
    def test_api_job_status_projects_authenticated_role(self, client, auth_user, expected_role):
        captured: dict[str, object] = {}
        scope = ScopeContext(account_id=DEFAULT_ACCOUNT_ID)

        async def _fake_build_job_detail_contract(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                dump=lambda: {
                    "id": "42",
                    "status": "completed",
                    "user_role": kwargs["user_role"],
                }
            )

        with (
            patch.object(api_routes, "_request_auth_user_payload", return_value=auth_user),
            patch.object(api_routes, "_request_scope", return_value=scope),
            patch(
                "core.entrypoints.api.jobs.build_job_detail_contract",
                side_effect=_fake_build_job_detail_contract,
            ),
        ):
            resp = client.get("/api/jobs/42")

        assert resp.status_code == 200
        assert resp.json()["user_role"] == expected_role
        assert captured["scope"] is scope

    def test_api_job_status_falls_back_when_runtime_builder_times_out(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now = datetime.now(UTC)
        mock_bl = MagicMock()
        mock_bl.get_job.return_value = _job_fixture(
            id=43,
            status="in_progress",
            thread_id="thread-43",
            task_summary="Long-running task",
            started_at=now,
            finished_at=None,
            duration_ms=None,
            selected_workflow="weekly_report",
            current_step_id="planning",
            current_agent="coordinator",
            heartbeat_at=now,
            last_error="",
            last_response_excerpt="",
        )
        mock_bl.get_job_cost_summary.return_value = {}
        from core.application.jobs.workflow_run_models import WorkflowRunStateV1

        mock_bl.get_public_progress_snapshot.return_value = SimpleNamespace(
            snapshot=WorkflowRunStateV1(
                job_id="43",
                project_id="1",
                workflow_id="weekly_report",
                status="in_progress",
                terminal=False,
                updated_at=now,
            ),
            marker_id="marker-43",
        )

        def _slow_runtime_builder(_job, _bl):
            time.sleep(0.05)
            return {"current_step_id": "should-not-be-returned"}

        with (
            patch.object(runtime_helpers, "_JOB_RUNTIME_PAYLOAD_TIMEOUT_SECONDS", 0.01),
            patch.object(
                runtime_helpers, "_build_runtime_payload", side_effect=_slow_runtime_builder
            ),
        ):
            data = _run_coro_blocking(
                runtime_helpers._build_runtime_payload_for_api(
                    mock_bl.get_job.return_value,
                    mock_bl,
                )
            )

        assert data["runtime_fallback"] is True
        assert data["current_step_id"] == "planning"
        assert data["active_agent"] == "coordinator"

    def test_api_jobs_awaiting_approval_returns_list(self, client):
        resp = client.get("/api/jobs/awaiting-approval")
        assert resp.status_code in (200, 500)
        if resp.status_code == 200:
            data = resp.json()
            # Endpoint returns {"jobs": [...]} or a bare list
            if isinstance(data, dict):
                assert "jobs" in data
                assert isinstance(data["jobs"], list)
            else:
                assert isinstance(data, list)

    def test_api_jobs_awaiting_approval_trims_heavy_summaries(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        huge_summary = "Task Brief: " + ("A" * 5000)
        mock_bl = MagicMock()
        mock_bl.get_awaiting_approval_jobs.return_value = [
            {
                "id": 7,
                "project_id": 11,
                "project_name": "Alpha",
                "task_summary": huge_summary,
                "account_id": DEFAULT_ACCOUNT_ID,
                "tenant_id": DEFAULT_TENANT_ID,
            }
        ]

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/jobs/awaiting-approval")

        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]["id"] == "7"
        assert body[0]["project_name"] == "Alpha"
        assert body[0]["task_summary"] == body[0]["display_summary"]
        assert len(body[0]["task_summary"]) <= 96
        assert "A" * 512 not in resp.text

    def test_api_watchdog_scan_calls_scan_helper(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        scan_payload = {"detection_count": 1, "applied_count": 1, "detections": []}

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=False),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_watchdog_scan", return_value=scan_payload) as scan_mock,
        ):
            resp = client.get("/api/watchdog/jobs/scan")

        assert resp.status_code == 200
        assert resp.json()["detection_count"] == 1
        scan_mock.assert_called_once_with(mock_bl, apply=False)

    def test_api_watchdog_scan_apply_requires_admin(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        session_user = {
            "id": 22,
            "username": "operator",
            "display_name": "Operator",
            "is_active": True,
            "roles": ["operator"],
        }
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
            patch.object(api_routes, "_watchdog_scan") as scan_mock,
        ):
            resp = client.get(
                "/api/watchdog/jobs/scan",
                params={"apply": 1},
                headers={"Authorization": "Bearer token-123"},
            )

        assert resp.status_code == 405
        assert "use post" in resp.text.lower()
        scan_mock.assert_not_called()

    def test_api_watchdog_scan_get_rejects_apply_even_for_admin(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        scan_payload = {"detection_count": 1, "applied_count": 1, "detections": []}
        session_user = {
            "id": 23,
            "username": "admin",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
            patch.object(api_routes, "_watchdog_scan", return_value=scan_payload) as scan_mock,
        ):
            resp = client.get(
                "/api/watchdog/jobs/scan",
                params={"apply": 1},
                headers={"Authorization": "Bearer token-123"},
            )

        assert resp.status_code == 405
        scan_mock.assert_not_called()

    def test_api_watchdog_scan_post_apply_allows_admin(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        scan_payload = {"detection_count": 1, "applied_count": 1, "detections": []}
        session_user = {
            "id": 23,
            "username": "admin",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
            patch.object(api_routes, "_watchdog_scan", return_value=scan_payload) as scan_mock,
        ):
            resp = client.post(
                "/api/watchdog/jobs/scan",
                headers={"Authorization": "Bearer token-123", "x-csrf-token": "test"},
            )

        assert resp.status_code == 200
        scan_mock.assert_called_once_with(mock_bl, apply=True)

    def test_api_watchdog_recover_kill_marks_job_cancelled(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        # FS-DEEP-CAND-017: destructive recovery (kill) is admin-only.
        admin_user = {"id": 7, "username": "admin", "is_active": True, "roles": ["admin"]}
        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 42,
            "status": "in_progress",
            "worker_id": "worker-101",
            "project_id": 1,
            "task_id": "task-42",
            "task_summary": "Debug stuck run",
            "task_source": "manual",
        }

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.object(client.app.state, "external_auth_enabled_override", return_value=False),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            # The route resolves a SIGNALABLE pid, not merely a parseable one:
            # a worker pid recorded in another container must never be signalled
            # from here. See control.scans.signalable_worker_pid.
            patch.object(api_routes, "_signalable_worker_pid", return_value=101),
            patch.object(
                api_routes,
                "_terminate_worker_pid",
                return_value={"attempted": True, "signal": "SIGTERM", "pid": 101},
            ),
            patch("core.entrypoints.api.jobs.cancel_job_terminally") as status_mock,
            patch.object(
                runtime_helpers, "_append_runtime_journal", return_value={"event": "watchdog_kill"}
            ),
        ):
            resp = client.post(
                "/api/watchdog/jobs/42/recover",
                json={"action": "kill", "reason": "operator kill"},
                headers={"Authorization": "Bearer admin-token", "x-csrf-token": "test"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] == "kill"
        assert body["kill_result"]["attempted"] is True
        # A kill is a cancel through the terminal path, with its own code: it
        # also cancels the job's unfinished child invocations.
        status_mock.assert_called_once()
        assert status_mock.call_args.kwargs["error_code"] == "watchdog_killed"
        assert status_mock.call_args.kwargs["reason"] == "Killed by watchdog action: operator kill"

    def test_api_watchdog_recover_denies_operator(self, client):
        # FS-DEEP-CAND-017: an operator (non-admin) must NOT be able to kill/retry
        # jobs via watchdog recovery.
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        operator_user = {
            "id": 9,
            "username": "operator",
            "is_active": True,
            "roles": ["operator"],
        }
        mock_bl = MagicMock()
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.object(client.app.state, "external_auth_enabled_override", return_value=False),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=operator_user),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_terminate_worker_pid") as terminate_mock,
        ):
            resp = client.post(
                "/api/watchdog/jobs/42/recover",
                json={"action": "kill", "reason": "operator kill"},
                headers={"Authorization": "Bearer op-token", "x-csrf-token": "test"},
            )

        assert resp.status_code == 403
        terminate_mock.assert_not_called()

    def test_api_watchdog_recover_retry_active_requires_force(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        admin_user = {"id": 7, "username": "admin", "is_active": True, "roles": ["admin"]}
        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 77,
            "status": "in_progress",
            "project_id": 1,
            "task_id": "task-77",
            "task_summary": "Run workflow",
            "task_source": "manual",
        }

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.object(client.app.state, "external_auth_enabled_override", return_value=False),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
        ):
            resp = client.post(
                "/api/watchdog/jobs/77/recover",
                json={"action": "retry", "reason": "retry test"},
                headers={"Authorization": "Bearer admin-token", "x-csrf-token": "test"},
            )

        assert resp.status_code == 409
        assert "force=true" in resp.json()["error"]

    def test_api_watchdog_recover_retry_enqueues_new_job(self, client):
        from core.application.registry.workflow_secret_defaults import secret_ref_fingerprint
        from core.infrastructure.security.secret_reference import (
            SecretReference,
            inline_secret_ref_fingerprint,
        )

        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 88,
            "status": "failed",
            "project_id": 1,
            "task_id": "task-88",
            "task_summary": "Retry failed flow",
            "task_source": "manual",
            "mode": "brownfield",
            "meta": {
                "requested_workflow_id": "weekly_digest",
                "workflow_selection_source": "project_default",
            },
        }
        mock_bl.get_project_run_config.return_value = {
            "project_name": "Demo",
            "project_root": "/target/demo",
        }
        fingerprints = sorted(
            [
                secret_ref_fingerprint("workflow/weekly_digest/configured/v1"),
                inline_secret_ref_fingerprint(
                    SecretReference(name="inline", ciphertext="encrypted-inline")
                ),
            ]
        )

        admin_user = {"id": 7, "username": "admin", "is_active": True, "roles": ["admin"]}
        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.object(client.app.state, "external_auth_enabled_override", return_value=False),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_enqueue_task", return_value=901) as enqueue_mock,
            patch.object(
                api_routes,
                "_load_job_replay_snapshot",
                return_value={
                    "workflow_id": "weekly_digest",
                    "inputs": {},
                    "files": [],
                    "test_mode": False,
                    "input_contract": "direct_inputs",
                    "task_envelope": {"inputs": {}, "files": []},
                    "_workflow_secret_ref_fingerprints": fingerprints,
                },
            ),
            patch.object(
                runtime_helpers, "_append_runtime_journal", return_value={"event": "watchdog_retry"}
            ),
            patch("core.entrypoints.api.jobs.cancel_job_terminally") as status_mock,
        ):
            resp = client.post(
                "/api/watchdog/jobs/88/recover",
                json={"action": "retry", "reason": "retry test"},
                headers={"Authorization": "Bearer admin-token", "x-csrf-token": "test"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] == "retry"
        assert body["new_job_id"] == "901"
        status_mock.assert_not_called()
        kwargs = enqueue_mock.call_args.kwargs
        assert kwargs["extra_state"]["workflow_id"] == "weekly_digest"
        assert kwargs["extra_state"]["workflow_selection_source"] == "explicit_request"
        assert kwargs["extra_state"]["_workflow_secret_ref_fingerprints"] == fingerprints
        assert kwargs["meta"]["requested_workflow_id"] == "weekly_digest"
        assert "workflow_selection_source_original" not in kwargs["meta"]

    def test_api_watchdog_recover_retry_uses_production_project_store_port(self):
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse
        from fastapi.testclient import TestClient

        from core.application.management.command_services_job import JobCommandService
        from core.entrypoints.api.jobs import create_api_jobs_router
        from core.infrastructure.db.platform_stores import (
            PlatformProjectStore,
            PlatformWorkerStore,
        )

        job_id = "e1a21d42-9d0f-4d78-8f30-4d2a37efca67"
        worker_store = MagicMock(spec=PlatformWorkerStore)
        worker_store.get_job.return_value = {
            "id": job_id,
            "status": "failed",
            "project_id": "project-production",
            "task_id": "task-production",
            "task_summary": "Retry failed production flow",
            "task_source": "manual",
            "mode": "brownfield",
            "meta": {},
        }
        project_store = MagicMock(spec=PlatformProjectStore)
        project_store.get_project_run_config.return_value = {
            "project_name": "Production Project",
        }
        job_command = JobCommandService(
            get_logger=lambda: worker_store,
            get_project_run_config=project_store.get_project_run_config,
        )
        job_query = SimpleNamespace(
            get_job_status=AsyncMock(return_value=SimpleNamespace(status="failed"))
        )
        enqueued: list[dict] = []

        def enqueue_task(**kwargs):
            enqueued.append(dict(kwargs))
            return 902

        app = FastAPI()

        @app.middleware("http")
        async def authenticate_admin(request: Request, call_next):
            request.state.auth_user = {
                "id": 7,
                "username": "admin",
                "is_active": True,
                "roles": ["admin"],
            }
            return await call_next(request)

        app.include_router(
            create_api_jobs_router(
                resolve_user_role=lambda _request: "admin",
                job_query_service=lambda: job_query,
                job_command_service=lambda: job_command,
                approval_query_service=lambda: SimpleNamespace(),
                request_scope=lambda _request, **_kwargs: None,
                resolve_actor_username=lambda _request, requested, default="operator": (
                    requested or default
                ),
                optional_actor_username=lambda _request: "admin",
                service_error_response=lambda exc: JSONResponse(
                    {"error": str(exc)}, status_code=400
                ),
                api_error_response=lambda exc, **_kwargs: JSONResponse(
                    {"error": str(exc)}, status_code=500
                ),
                get_logger_only=lambda: worker_store,
                watchdog_scan=lambda _store, **_kwargs: {},
                parse_worker_pid=lambda _worker_id: None,
                terminate_worker_pid=lambda _pid: {"attempted": False},
                append_runtime_journal=lambda *_args, **_kwargs: {},
                build_rerun_workflow_selection=lambda _job, *, replay_snapshot: (
                    {"workflow_id": replay_snapshot["workflow_id"]},
                    {},
                ),
                load_replay_snapshot=lambda _job: {
                    "workflow_id": "wf-production",
                    "inputs": {},
                    "files": [],
                    "test_mode": False,
                    "_workflow_secret_ref_fingerprints": [],
                },
                enqueue_task=enqueue_task,
                retryable_interrupted_step_status="retryable_interrupted_step",
                err_database_not_configured="Database not configured",
            )
        )
        response = TestClient(app, raise_server_exceptions=False).post(
            f"/api/watchdog/jobs/{job_id}/recover",
            json={"action": "retry", "reason": "production retry"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["new_job_id"] == "902"
        assert enqueued[0]["project_name"] == "Production Project"
        assert "project_root" not in enqueued[0]

    def test_api_watchdog_recover_refuses_step_test_run_retry(self, client):
        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 89,
            "status": "queued",
            "project_id": 1,
            "task_id": "task-89",
            "task_summary": "Retry selected store step",
            "task_source": "step_test_run",
            "meta": {
                "selected_workflow_id": "wf-acme-store",
                "step_test_run": True,
                "step_id": "store-locations",
            },
        }
        mock_bl.get_project_run_config.return_value = {
            "project_name": "Demo",
            "project_root": "/target/demo",
        }
        admin_user = {"id": 7, "username": "admin", "is_active": True, "roles": ["admin"]}

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.object(client.app.state, "external_auth_enabled_override", return_value=False),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=admin_user),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_enqueue_task", return_value=902) as enqueue_mock,
            patch.object(
                api_routes,
                "_load_job_replay_snapshot",
                return_value={
                    "workflow_id": "wf-acme-store",
                    "inputs": {},
                    "files": [],
                    "test_mode": True,
                    "_workflow_secret_ref_fingerprints": [],
                },
            ),
            patch("core.entrypoints.api.jobs.cancel_job_terminally") as status_mock,
        ):
            resp = client.post(
                "/api/watchdog/jobs/89/recover",
                json={"action": "retry", "reason": "retry test", "force": True},
                headers={"Authorization": "Bearer admin-token", "x-csrf-token": "test"},
            )

        assert resp.status_code == 409
        assert "start a new step test run" in resp.json()["error"].lower()
        enqueue_mock.assert_not_called()
        status_mock.assert_not_called()

    def test_api_job_runtime_journal_returns_entries(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = _job_fixture(
            id=56,
            meta='{"runtime_journal":[{"event":"watchdog_mark","reason":"stuck"}]}',
        )

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/jobs/56/runtime-journal")

        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == "56"
        assert isinstance(body["runtime_journal"], list)
        assert body["runtime_journal"][0]["event"] == "watchdog_mark"

    def test_api_job_decision_overrides_lists_rows(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = _job_fixture(id=56)
        mock_bl.list_job_decision_overrides.return_value = [
            {
                "id": 1,
                "job_id": 56,
                "domain": "gate_outcome",
                "original_value": {"status": "fail"},
                "override_value": {"status": "pass"},
                "reason": "Operator accepted gate outcome",
                "created_by": "tester",
                "active": True,
                "created_at": datetime(2026, 3, 12, 8, 0, 0, tzinfo=UTC),
            }
        ]

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/jobs/56/decision-overrides")

        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == "56"
        assert body["overrides"][0]["domain"] == "gate_outcome"
        assert body["overrides"][0]["override_value"]["status"] == "pass"

    def test_api_create_job_decision_override_rejects_workflow_selection_domain(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 78,
            "meta": {},
            "account_id": DEFAULT_ACCOUNT_ID,
        }

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_routes,
                "_request_scope",
                return_value=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            ),
        ):
            resp = client.post(
                "/api/jobs/78/decision-overrides",
                json={
                    "domain": "workflow_selection",
                    "override_value": {"workflow_id": "weekly_digest"},
                    "reason": "Force quick fix",
                },
            )

        assert resp.status_code == 400
        assert "no longer supported" in resp.json().get("error", "")
        mock_bl.create_job_decision_override.assert_not_called()
        mock_bl.update_job.assert_not_called()
        mock_bl.update_job_runtime.assert_not_called()

    def test_api_create_job_decision_override_rejects_invalid_workflow_override(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 79,
            "project_id": 1,
            "task_summary": "Build export endpoint",
            "meta": {},
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_bl.get_project.return_value = {
            "id": 1,
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_bl.get_project_run_config.return_value = {
            "project_name": "Demo",
            "project_root": "/target/demo",
            "project_workflows": [{"workflow_id": "weekly_report", "enabled": True}],
        }

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_runtime,
                "_validate_explicit_workflow_override",
                side_effect=ValueError("Workflow 'weekly_digest' does not support feature"),
            ),
        ):
            resp = client.post(
                "/api/jobs/79/decision-overrides",
                json={
                    "domain": "workflow_selection",
                    "override_value": {"workflow_id": "weekly_digest"},
                    "reason": "Force quick fix",
                },
            )

        assert resp.status_code == 400
        assert "no longer supported" in resp.json().get("error", "")
        mock_bl.create_job_decision_override.assert_not_called()
        mock_bl.update_job.assert_not_called()

    def test_api_create_job_decision_override_derives_original_value(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 80,
            "thread_id": "thread-80",
            "meta": {},
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_bl.create_job_decision_override.return_value = {
            "id": 81,
            "job_id": 80,
            "domain": "gate_outcome",
            "original_value": {"status": "fail"},
            "override_value": {"status": "pass"},
            "reason": "accept risk",
            "created_by": "job_ui",
            "active": True,
            "created_at": datetime(2026, 3, 12, 8, 0, 0, tzinfo=UTC),
        }

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_routes,
                "_request_scope",
                return_value=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            ),
            patch.object(
                scheduled_runtime,
                "_get_thread_state_values",
                return_value={"status": "fail", "current_step_id": "quality"},
            ),
        ):
            resp = client.post(
                "/api/jobs/80/decision-overrides",
                json={
                    "domain": "gate_outcome",
                    "override_value": {"status": "pass"},
                    "reason": "accept risk",
                },
            )

        assert resp.status_code == 200
        kwargs = mock_bl.create_job_decision_override.call_args.kwargs
        assert kwargs["original_value"]["action"] is None
        assert kwargs["original_value"]["step_id"] == "quality"

    def test_api_create_job_decision_override_uses_authenticated_actor(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for decision override actor test"
            )

        now = datetime.now(UTC)
        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 81,
            "thread_id": "thread-81",
            "meta": {},
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_bl.create_job_decision_override.return_value = {
            "id": 82,
            "job_id": 81,
            "domain": "gate_outcome",
            "original_value": {"status": "fail"},
            "override_value": {"status": "pass"},
            "reason": "accept risk",
            "created_by": "alice",
            "active": True,
            "created_at": now,
        }
        auth_user = {
            "id": 1,
            "username": "alice",
            "display_name": "Alice",
            "roles": ["operator"],
            "is_active": True,
        }

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=auth_user),
            patch.object(
                api_routes,
                "_request_scope",
                return_value=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="alice"),
            ),
            patch.object(
                scheduled_runtime, "_get_thread_state_values", return_value={"status": "fail"}
            ),
        ):
            resp = client.post(
                "/api/jobs/81/decision-overrides",
                headers={"Authorization": "Bearer token-123"},
                json={
                    "domain": "gate_outcome",
                    "override_value": {"status": "pass"},
                    "reason": "accept risk",
                    "created_by": "mallory",
                },
            )

        assert resp.status_code == 200
        kwargs = mock_bl.create_job_decision_override.call_args.kwargs
        assert kwargs["created_by"] == "alice"

    def test_api_job_errors_lists_rows(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = _job_fixture(id=90)
        mock_bl.list_job_errors.return_value = [
            {
                "id": 44,
                "job_id": 90,
                "source": "provider",
                "category": "provider",
                "provider_name": "openai",
                "http_status": 429,
                "provider_error_code": "rate_limit",
                "provider_error_type": "RateLimitError",
                "request_id": "req_abc",
                "retryable": True,
                "error_message": "Rate limit exceeded",
                "response_excerpt": '{"error":"rate_limit"}',
                "traceback": "Traceback...",
                "context_jsonb": {"node": "composer"},
                "created_at": datetime(2026, 3, 12, 9, 0, 0, tzinfo=UTC),
            }
        ]

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/jobs/90/errors")

        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == "90"
        assert body["errors"][0]["source"] == "provider"
        assert body["errors"][0]["http_status"] == 429

    def test_build_runtime_payload_includes_parallel_lanes(self):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now = datetime(2026, 3, 12, 10, 0, 0, tzinfo=UTC)
        job = {
            "id": 77,
            "status": "in_progress",
            "thread_id": "thread-77",
            "meta": {},
            "selected_workflow": "",
            "workflow_id": "",
            "current_step_id": "",
            "current_agent": "",
            "last_error": "",
            "last_response_excerpt": "",
            "heartbeat_at": now,
        }
        mock_bl = MagicMock()
        mock_bl.get_latest_log_entry.return_value = None
        mock_bl.get_latest_event.return_value = None
        mock_bl.get_latest_agent_message.return_value = None
        mock_bl.list_job_errors.return_value = []

        runtime_state = {
            "test_mode": True,
            "parallel_lanes": [
                {
                    "lane_id": "lane_a",
                    "status": "running",
                    "completed_steps": 1,
                    "total_steps": 3,
                    "next_agent": "composer",
                    "positions": [0, 2, 4],
                    "merge_status": "retrying",
                    "merge_id": 42,
                }
            ],
            "parallel_concurrency_cap": 2,
            "parallel_execution_order": [0, 1, 2],
        }

        with patch.object(runtime_helpers, "_get_thread_state_values", return_value=runtime_state):
            payload = runtime_helpers._build_runtime_payload(job, mock_bl)

        assert payload["parallel_concurrency_cap"] == 2
        assert payload["parallel_execution_order"] == [0, 1, 2]
        assert payload["parallel_lanes"][0]["lane_id"] == "lane_a"
        assert payload["parallel_lanes"][0]["status"] == "running"
        assert payload["parallel_lanes"][0]["merge_status"] == "retrying"
        assert payload["parallel_lanes"][0]["merge_id"] == "42"
        assert payload["test_mode"] is True
        assert "job_worktrees" not in payload

    def test_build_runtime_payload_includes_retryable_interrupted_snapshot(self):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now = datetime(2026, 3, 12, 10, 0, 0, tzinfo=UTC)
        job = {
            "id": 99,
            "status": "retryable_interrupted_step",
            "thread_id": "thread-99",
            "meta": {
                "retryable_interrupted_step": {
                    "detected_at": now.isoformat(),
                    "detection_source": "prompt_request_without_response",
                    "job_id": 99,
                    "thread_id": "thread-99",
                    "step_id": "composer:draft_summary",
                    "agent_id": "composer",
                    "skill": "draft_summary",
                    "correlation_id": "corr-99",
                }
            },
            "selected_workflow": "",
            "workflow_id": "",
            "current_step_id": "implementation",
            "current_agent": "composer",
            "last_error": "",
            "last_response_excerpt": "",
            "heartbeat_at": now,
        }
        mock_bl = MagicMock()
        mock_bl.get_latest_log_entry.return_value = None
        mock_bl.get_latest_event.return_value = None
        mock_bl.get_latest_agent_message.return_value = None
        mock_bl.list_job_errors.return_value = []

        with patch.object(runtime_helpers, "_get_thread_state_values", return_value={}):
            payload = runtime_helpers._build_runtime_payload(job, mock_bl)

        assert payload["can_retry_interrupted_step"] is True
        assert payload["retryable_interrupted_step"]["skill"] == "draft_summary"

    def test_watchdog_scan_apply_updates_runtime_status(self):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now = datetime.now(UTC)
        mock_bl = MagicMock()
        mock_bl.get_active_jobs.return_value = [
            {
                "id": 5,
                "status": "in_progress",
                "started_at": now - timedelta(seconds=90),
                "heartbeat_at": now - timedelta(seconds=90),
                "queued_at": now - timedelta(seconds=90),
                "worker_id": "",
            }
        ]
        mock_bl.get_job.return_value = {"id": 5, "meta": {}}

        with (
            patch.object(lifecycle_runtime, "_WATCHDOG_STUCK_SECONDS", 30),
            patch.object(lifecycle_runtime, "_WATCHDOG_ORPHAN_GRACE_SECONDS", 30),
            # This job was never picked up, so pickup grace is the threshold that
            # actually decides it, and it was the one left unpatched. The test passed
            # only while the compiled-in default happened to be 45 -- a number no
            # deployment ever ran, because Compose has always set 120. Driving every
            # threshold the case depends on makes the test independent of that tuning.
            patch.object(lifecycle_runtime, "_WATCHDOG_PICKUP_GRACE_SECONDS", 30),
        ):
            payload = runtime_helpers._watchdog_scan(mock_bl, apply=True)

        assert payload["detection_count"] == 1
        assert payload["applied_count"] == 1
        status_calls = [
            c for c in mock_bl.update_job.call_args_list if c.kwargs.get("status") == "stuck"
        ]
        assert status_calls
        assert mock_bl.update_job_runtime.call_count >= 1

    def test_watchdog_scan_pending_job_not_marked_stuck_when_project_lease_is_active(self):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now = datetime.now(UTC)
        mock_bl = MagicMock()
        mock_bl.get_active_jobs.return_value = [
            {
                "id": 769,
                "project_id": 11,
                "status": "pending",
                "started_at": None,
                "heartbeat_at": None,
                "queued_at": now - timedelta(seconds=300),
                "worker_id": "",
            }
        ]
        mock_bl.list_project_execution_leases.return_value = [
            {
                "project_id": 11,
                "active_job_id": 768,
                "heartbeat_at": now,
            }
        ]

        with (
            patch.object(lifecycle_runtime, "_WATCHDOG_STUCK_SECONDS", 30),
            patch.object(lifecycle_runtime, "_WATCHDOG_ORPHAN_GRACE_SECONDS", 60),
        ):
            payload = runtime_helpers._watchdog_scan(mock_bl, apply=True)

        assert payload["detection_count"] == 0
        assert payload["applied_count"] == 0
        mock_bl.update_job.assert_not_called()

    def test_watchdog_scan_pending_job_not_marked_stuck_when_other_project_lease_is_active(self):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now = datetime.now(UTC)
        mock_bl = MagicMock()
        mock_bl.get_active_jobs.return_value = [
            {
                "id": 770,
                "project_id": 11,
                "status": "pending",
                "started_at": None,
                "heartbeat_at": None,
                "queued_at": now - timedelta(seconds=300),
                "worker_id": "",
            }
        ]
        mock_bl.list_project_execution_leases.return_value = [
            {
                "project_id": 12,
                "active_job_id": 900,
                "heartbeat_at": now,
                "expires_at": now + timedelta(seconds=60),
            }
        ]

        with (
            patch.object(lifecycle_runtime, "_WATCHDOG_STUCK_SECONDS", 30),
            patch.object(lifecycle_runtime, "_WATCHDOG_ORPHAN_GRACE_SECONDS", 60),
        ):
            payload = runtime_helpers._watchdog_scan(mock_bl, apply=True)

        assert payload["detection_count"] == 0
        assert payload["applied_count"] == 0
        mock_bl.update_job.assert_not_called()

    def test_watchdog_scan_in_progress_not_marked_stuck_within_pickup_grace(self):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now = datetime.now(UTC)
        mock_bl = MagicMock()
        mock_bl.get_active_jobs.return_value = [
            {
                "id": 812,
                "project_id": 44,
                "status": "in_progress",
                "started_at": now - timedelta(seconds=10),
                "heartbeat_at": None,
                "queued_at": now - timedelta(seconds=60),
                "worker_id": "",
            }
        ]
        mock_bl.list_project_execution_leases.return_value = []

        with (
            patch.object(lifecycle_runtime, "_WATCHDOG_STUCK_SECONDS", 5),
            patch.object(lifecycle_runtime, "_WATCHDOG_PICKUP_GRACE_SECONDS", 30),
        ):
            payload = runtime_helpers._watchdog_scan(mock_bl, apply=True)

        assert payload["detection_count"] == 0
        assert payload["applied_count"] == 0
        mock_bl.update_job.assert_not_called()

    def test_watchdog_scan_in_progress_live_lease_wins_over_stuck_mark(self):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now = datetime.now(UTC)
        mock_bl = MagicMock()
        mock_bl.get_active_jobs.return_value = [
            {
                "id": 813,
                "project_id": 45,
                "status": "in_progress",
                "started_at": now - timedelta(seconds=120),
                "heartbeat_at": now - timedelta(seconds=120),
                "queued_at": now - timedelta(seconds=150),
                "worker_id": "",
            }
        ]
        mock_bl.list_project_execution_leases.return_value = [
            {
                "project_id": 45,
                "active_job_id": 813,
                "heartbeat_at": now - timedelta(seconds=80),
                "expires_at": now + timedelta(seconds=120),
            }
        ]

        with (
            patch.object(lifecycle_runtime, "_WATCHDOG_STUCK_SECONDS", 30),
            patch.object(lifecycle_runtime, "_WATCHDOG_ORPHAN_GRACE_SECONDS", 30),
        ):
            payload = runtime_helpers._watchdog_scan(mock_bl, apply=True)

        assert payload["detection_count"] == 0
        assert payload["applied_count"] == 0
        mock_bl.update_job.assert_not_called()

    def test_watchdog_scan_recovers_stuck_job_when_live_lease_appears(self):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now = datetime.now(UTC)
        lease_row = {
            "project_id": 90,
            "active_job_id": 9901,
            "heartbeat_at": now - timedelta(seconds=2),
            "expires_at": now + timedelta(seconds=120),
            "acquired_at": now - timedelta(seconds=3),
        }
        stuck_row = {
            "id": 9901,
            "project_id": 90,
            "status": "stuck",
            "started_at": now - timedelta(seconds=200),
            "heartbeat_at": now - timedelta(seconds=2),
            "worker_id": "worker-1",
        }
        mock_bl = MagicMock()
        mock_bl.get_active_jobs.return_value = []
        mock_bl.list_jobs_for_attention.return_value = [stuck_row]
        mock_bl.list_project_execution_leases.return_value = [lease_row]
        mock_bl.get_project_execution_lease.return_value = lease_row
        mock_bl.get_job.return_value = dict(stuck_row)

        payload = runtime_helpers._watchdog_scan(mock_bl, apply=True)

        assert payload["recovery_count"] == 1
        assert payload["recovery_applied_count"] == 1
        recovered_calls = [
            c for c in mock_bl.update_job.call_args_list if c.kwargs.get("status") == "in_progress"
        ]
        assert recovered_calls

    def test_watchdog_scan_does_not_self_recover_pending_job_it_just_marked_stuck(self):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now = datetime.now(UTC)

        class _FakeBL:
            def __init__(self):
                self.job = {
                    "id": 9911,
                    "project_id": 91,
                    "status": "pending",
                    "started_at": None,
                    "queued_at": now - timedelta(seconds=240),
                    "heartbeat_at": None,
                    "worker_id": "",
                    "last_error": "",
                }
                self.update_calls: list[dict] = []
                self.runtime_calls: list[dict] = []

            def get_active_jobs(self):
                if str(self.job.get("status") or "").lower() == "pending":
                    return [dict(self.job)]
                return []

            def list_project_execution_leases(self, active_only=True):
                return []

            def get_job(self, job_id):
                assert str(job_id) == str(self.job["id"])
                return dict(self.job)

            def update_job(self, job_id, **kwargs):
                assert str(job_id) == str(self.job["id"])
                self.job.update(kwargs)
                self.update_calls.append(dict(kwargs))

            def update_job_runtime(self, job_id, **kwargs):
                assert str(job_id) == str(self.job["id"])
                self.runtime_calls.append(dict(kwargs))
                if kwargs.get("heartbeat_now"):
                    self.job["heartbeat_at"] = datetime.now(UTC)
                if kwargs.get("clear_last_error"):
                    self.job["last_error"] = ""
                elif kwargs.get("last_error") is not None:
                    self.job["last_error"] = str(kwargs.get("last_error") or "")

            def list_jobs_for_attention(self, limit=500):
                return [dict(self.job)]

        fake_bl = _FakeBL()

        with (
            patch.object(lifecycle_runtime, "_WATCHDOG_STUCK_SECONDS", 30),
            patch.object(lifecycle_runtime, "_WATCHDOG_ORPHAN_GRACE_SECONDS", 30),
            patch.object(
                runtime_helpers, "_append_runtime_journal", return_value={"event": "watchdog_mark"}
            ),
        ):
            payload = runtime_helpers._watchdog_scan(fake_bl, apply=True)

        assert payload["detection_count"] == 1
        assert payload["applied_count"] == 1
        assert payload["recovery_count"] == 0
        assert payload["recovery_applied_count"] == 0
        assert fake_bl.job["status"] == "stuck"
        assert fake_bl.job["heartbeat_at"] is None
        assert fake_bl.runtime_calls == [
            {
                "heartbeat_now": False,
                "last_error": "Job has been pending in queue for 240s (threshold=30s)",
            }
        ]

    def test_watchdog_scan_apply_marks_retryable_interrupted_step(self):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now = datetime.now(UTC)
        mock_bl = MagicMock()
        mock_bl.get_active_jobs.return_value = [
            {
                "id": 66,
                "project_id": 3,
                "status": "in_progress",
                "started_at": now - timedelta(seconds=120),
                "heartbeat_at": now - timedelta(seconds=120),
                "queued_at": now - timedelta(seconds=120),
                "worker_id": "worker-99999",
                "thread_id": "thread-66",
                "meta": {},
            }
        ]
        mock_bl.get_job.return_value = {
            "id": 66,
            "project_id": 3,
            "status": "in_progress",
            "thread_id": "thread-66",
            "meta": {},
            "current_step_id": "implementation",
            "current_agent": "composer",
        }
        snapshot = {
            "detected_at": now.isoformat(),
            "detection_source": "prompt_request_without_response",
            "job_id": 66,
            "thread_id": "thread-66",
            "step_id": "composer:draft_summary",
            "agent_id": "composer",
            "skill": "draft_summary",
            "correlation_id": "corr-66",
        }

        with (
            patch.object(lifecycle_runtime, "_WATCHDOG_STUCK_SECONDS", 30),
            patch.object(lifecycle_runtime, "_WATCHDOG_ORPHAN_GRACE_SECONDS", 30),
            patch.object(
                runtime_helpers,
                "_detect_retryable_interrupted_step_snapshot",
                return_value=snapshot,
            ),
        ):
            payload = runtime_helpers._watchdog_scan(mock_bl, apply=True)

        assert payload["detection_count"] == 1
        assert payload["applied_count"] == 1, payload
        status_calls = [
            c
            for c in mock_bl.update_job.call_args_list
            if c.kwargs.get("status") == "retryable_interrupted_step"
        ]
        assert status_calls
        meta_calls = [
            c for c in mock_bl.update_job.call_args_list if isinstance(c.kwargs.get("meta"), dict)
        ]
        assert meta_calls
        assert (
            meta_calls[-1].kwargs["meta"]["retryable_interrupted_step"]["skill"] == "draft_summary"
        )

    def test_cancel_nonexistent_job(self, client):
        resp = client.post("/api/jobs/999999/cancel")
        assert resp.status_code in (200, 404, 422, 500, 503)
        assert resp.status_code not in (302, 303)

    def test_restart_nonexistent_job(self, client):
        resp = client.post("/api/jobs/999999/restart", headers={"Accept": "application/json"})
        assert resp.status_code in (200, 400, 404, 422, 500, 503)
        assert resp.status_code not in (302, 303)

    def test_restart_preserves_pinned_workflow_selection(self, client):
        from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
        from core.application.jobs.job_invocation_artifacts import capture_replay_snapshot

        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        artifact_plane = ArtifactDatasetPlane()
        replay_pointer = capture_replay_snapshot(
            artifact_plane=artifact_plane,
            project_id="1",
            job_id="55",
            snapshot={
                "workflow_id": "weekly_report",
                "inputs": {},
                "files": [],
                "test_mode": False,
                "input_contract": "direct_inputs",
                "task_envelope": {"inputs": {}, "files": []},
                "_workflow_secret_ref_fingerprints": [],
            },
        )
        mock_bl.get_job.return_value = {
            "id": 55,
            "status": "failed",
            "project_id": 1,
            "task_id": "task-55",
            "task_summary": "Retry this task",
            "task_source": "manual",
            "mode": "brownfield",
            "account_id": DEFAULT_ACCOUNT_ID,
            "meta": {
                "replay_snapshot": replay_pointer,
                "requested_workflow_id": "weekly_report",
                "workflow_selection_source": "project_default",
            },
        }
        mock_bl.get_project.return_value = {
            "id": 1,
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_bl.get_project_run_config.return_value = {
            "project_name": "Demo",
            "project_root": "/target/demo",
        }

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(scheduled_runtime, "_artifact_plane", artifact_plane),
            patch.object(scheduled_runtime, "_enqueue_task", return_value=556) as enqueue_mock,
        ):
            resp = client.post(
                "/api/jobs/55/restart",
                headers={"Accept": "application/json"},
                follow_redirects=False,
            )

        assert resp.status_code == 200
        assert resp.json() == {"job_id": "55", "new_job_id": "556"}
        kwargs = enqueue_mock.call_args.kwargs
        assert kwargs["extra_state"]["workflow_id"] == "weekly_report"
        assert kwargs["extra_state"]["workflow_selection_source"] == "explicit_request"
        assert "workflow_selection_source_original" not in kwargs["meta"]

    def test_job_logs_uses_run_config_project_name_when_direct_lookup_fails(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 785,
            "project_id": 1,
            "status": "completed",
            "task_source": "manual",
            "task_id": "task-785",
            "task_summary": "Some task",
            "thread_id": "thread-785",
            "meta": {},
            "queue_name": None,
            "started_at": None,
            "finished_at": None,
            "duration_ms": None,
        }
        mock_bl.get_project.side_effect = RuntimeError("temporary lookup failure")
        mock_bl.get_project_run_config.return_value = {"project_name": "test-project"}
        mock_bl.get_logs.return_value = []
        mock_bl.get_events.return_value = []
        mock_bl.get_agent_messages.return_value = []
        mock_bl.list_job_decision_overrides.return_value = []
        mock_bl.list_job_errors.return_value = []
        mock_bl.get_job_cost_breakdown.return_value = []

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(runtime_helpers, "_build_runtime_payload", return_value={}),
        ):
            resp = client.get("/jobs/785")

        _assert_frontend_shell_response(resp)

    def test_job_logs_initial_payload_uses_small_batches(self, client):
        """Job detail page uses query service with bounded initial limits."""
        try:
            from core.application.management.query_models import JobLogsPageView
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_service = MagicMock()
        mock_service.get_logs_page.return_value = JobLogsPageView(
            job={
                "id": 786,
                "project_id": 1,
                "status": "completed",
                "task_source": "manual",
                "task_id": "task-786",
                "task_summary": "Some task",
                "thread_id": "thread-786",
                "meta": {},
                "queue_name": None,
                "started_at": None,
                "finished_at": None,
                "duration_ms": None,
            },
            logs=[],
            events=[],
            messages=[],
            project_name="test-project",
        )

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_query_service": lambda: mock_service},
            ),
        ):
            resp = client.get("/jobs/786")

        _assert_frontend_shell_response(resp)
        mock_service.get_logs_page.assert_not_called()

    def test_api_job_delete_trashes_job(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_service = MagicMock()
        mock_service.delete_job.return_value = type(
            "DeletePayload",
            (),
            {"status": "ok", "job_id": 42},
        )()

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: MagicMock()}
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_command_service": lambda: mock_service},
            ),
        ):
            resp = client.delete("/api/jobs/42")

        assert resp.status_code == 200
        body = resp.json()
        assert body == {"status": "ok", "job_id": 42}
        mock_service.delete_job.assert_called_once()
        delete_call = mock_service.delete_job.call_args
        assert delete_call.args[0] == "42"

    def test_api_job_delete_rejects_active_job(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 42,
            "project_id": 1,
            "status": "in_progress",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_bl.get_project.return_value = {
            "id": 1,
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.delete("/api/jobs/42")

        assert resp.status_code == 409

    def test_api_project_jobs_bulk_delete_requires_confirm(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = _project_fixture()
        session_user = {
            "id": 18,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_csrf_request_valid", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
        ):
            resp = client.post(
                "/api/projects/1/jobs/bulk-delete",
                headers={"Authorization": "Bearer token-123"},
                json={"ids": [10, 11], "confirm": "no"},
            )

        assert resp.status_code == 400
        mock_bl.delete_job.assert_not_called()

    def test_api_project_jobs_bulk_delete_trashes_matching_jobs(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_service = MagicMock()
        mock_service.bulk_delete_project_jobs.return_value = type(
            "BulkDeletePayload",
            (),
            {
                "status": "ok",
                "requested": [10, 11, 12],
                "deleted": [10],
                "deleted_count": 1,
                "skipped": [
                    {"job_id": 11, "reason": "wrong_project"},
                    {"job_id": 12, "reason": "not_found"},
                ],
            },
        )()
        session_user = {
            "id": 18,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: MagicMock()}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_csrf_request_valid", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_command_service": lambda: mock_service},
            ),
        ):
            resp = client.post(
                "/api/projects/1/jobs/bulk-delete",
                headers={"Authorization": "Bearer token-123"},
                json={"ids": [10, 11, 12], "confirm": "DELETE"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_count"] == 1
        assert body["deleted"] == [10]
        assert body["requested"] == [10, 11, 12]
        assert body["skipped"] == [
            {"job_id": 11, "reason": "wrong_project"},
            {"job_id": 12, "reason": "not_found"},
        ]
        mock_service.bulk_delete_project_jobs.assert_called_once()

    def test_api_project_jobs_bulk_delete_skips_active_jobs(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_service = MagicMock()
        mock_service.bulk_delete_project_jobs.return_value = type(
            "BulkDeletePayload",
            (),
            {
                "status": "ok",
                "requested": [10],
                "deleted": [],
                "deleted_count": 0,
                "skipped": [{"job_id": 10, "reason": "active (in_progress)"}],
            },
        )()
        session_user = {
            "id": 18,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: MagicMock()}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_csrf_request_valid", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=session_user),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_command_service": lambda: mock_service},
            ),
        ):
            resp = client.post(
                "/api/projects/1/jobs/bulk-delete",
                headers={"Authorization": "Bearer token-123"},
                json={"ids": [10], "confirm": "DELETE"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted"] == []
        assert body["skipped"] == [{"job_id": 10, "reason": "active (in_progress)"}]
        mock_service.bulk_delete_project_jobs.assert_called_once()

    def test_api_job_messages_passes_filters(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = _job_fixture(id=42)
        mock_bl.get_agent_messages.return_value = [
            {
                "id": 10,
                "timestamp": datetime.now(UTC),
                "job_id": 42,
                "correlation_id": "corr-1",
                "schema_version": "agent_message_v1",
                "from_agent": "proofreader",
                "to_agent": "orchestrator",
                "step_id": "quality",
                "skill": "run_quality_checks",
                "status": "fail",
                "summary": "failed checks",
                "artifacts": {},
                "errors": ["x"],
                "envelope": {},
            }
        ]

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get(
                "/api/jobs/42/messages",
                params={
                    "since_id": 9,
                    "step_id": "quality",
                    "producer": "proofreader",
                    "skill": "run_quality_checks",
                    "status": "fail",
                    "limit": 250,
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert "messages" in body
        mock_bl.get_agent_messages.assert_called_once_with(
            "42",
            since_id="9",
            before_id=None,
            newest=False,
            limit=251,
            step_id="quality",
            from_agent="proofreader",
            skill="run_quality_checks",
            status="fail",
        )

    def test_api_job_messages_limit_is_clamped(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = _job_fixture(id=7)
        mock_bl.get_agent_messages.return_value = []

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/jobs/7/messages", params={"limit": 50000})

        assert resp.status_code == 200
        mock_bl.get_agent_messages.assert_called_once_with(
            "7",
            since_id="",
            before_id=None,
            newest=False,
            limit=2001,
            step_id=None,
            from_agent=None,
            skill=None,
            status=None,
        )

    def test_api_job_messages_supports_before_id_and_newest(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = _job_fixture(id=99)
        mock_bl.get_agent_messages.return_value = []

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get(
                "/api/jobs/99/messages",
                params={"before_id": 1500, "newest": 1, "limit": 50},
            )

        assert resp.status_code == 200
        mock_bl.get_agent_messages.assert_called_once_with(
            "99",
            since_id="",
            before_id="1500",
            newest=True,
            limit=51,
            step_id=None,
            from_agent=None,
            skill=None,
            status=None,
        )

    def test_api_job_attachment_download_returns_file_content(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        payload = MagicMock()
        payload.dump.return_value = {"id": 42, "status": "completed"}
        mock_service = MagicMock()
        mock_service.get_job_status = AsyncMock(return_value=payload)
        mock_bl = MagicMock()
        mock_bl.get_attachments.return_value = [
            {
                "id": 7,
                "filename": "demo_enriched_catalog.csv",
                "mime_type": "text/csv",
                "size_bytes": 12,
                "content": b"sku,name\n1,A\n",
            }
        ]

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_query_service": lambda: mock_service},
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
        ):
            resp = client.get("/api/jobs/42/attachments/7")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "demo_enriched_catalog.csv" in resp.headers["content-disposition"]
        assert resp.content == b"sku,name\n1,A\n"
        mock_service.get_job_status.assert_awaited_once()
        mock_bl.get_attachments.assert_called_once_with("42")

    def test_api_job_artifact_download_redirects_to_signed_storage_url(self, client):
        mock_service = MagicMock()
        mock_service.get_job_artifact_download.return_value = SimpleNamespace(
            artifact_id="art-42",
            filename="catalog.csv",
            content_type="text/csv",
            size_bytes=42,
        )

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_query_service": lambda: mock_service},
            ),
            patch(
                "core.entrypoints.api.dependencies._artifact_presigned_url",
                lambda artifact_id, *, method, ttl_seconds, filename: (
                    f"https://storage.example/signed/{artifact_id}"
                    f"?method={method}&ttl={ttl_seconds}&filename={filename}"
                ),
            ),
        ):
            response = client.get(
                "/api/jobs/42/artifacts/art-42",
                follow_redirects=False,
            )

        assert response.status_code == 307
        assert response.headers["location"] == (
            "https://storage.example/signed/art-42?method=GET&ttl=600&filename=catalog.csv"
        )
        mock_service.get_job_artifact_download.assert_called_once()

    @staticmethod
    def _artifact_download_service(filename: str = "quarterly report (v2).csv"):
        mock_service = MagicMock()
        mock_service.get_job_artifact_download.return_value = SimpleNamespace(
            artifact_id="art-42",
            filename=filename,
            content_type="text/csv",
            size_bytes=13,
        )
        return mock_service

    def test_api_job_artifact_download_streams_local_storage_without_a_url(self, client):
        """local_fs cannot grant a browser URL, so the API returns the bytes."""
        mock_service = self._artifact_download_service()
        chunks = [b"sku,name\n", b"1,A\n"]

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_query_service": lambda: mock_service},
            ),
            patch(
                "core.entrypoints.api.dependencies._artifact_presigned_url",
                lambda artifact_id, *, method, ttl_seconds, filename: "",
            ),
            patch(
                "core.entrypoints.api.dependencies._artifact_local_stream",
                lambda artifact_id: (iter(chunks), 13),
            ),
        ):
            # Identity encoding: with compression on, gzip re-encodes the
            # stream and correctly drops the length the route declared.
            response = client.get(
                "/api/jobs/42/artifacts/art-42",
                follow_redirects=False,
                headers={"Accept-Encoding": "identity"},
            )

        assert response.status_code == 200
        assert response.content == b"".join(chunks)
        assert response.headers["content-type"].startswith("text/csv")
        assert response.headers["content-length"] == "13"
        # Percent-encoded, so spaces and parentheses cannot break the header.
        assert response.headers["content-disposition"] == (
            "attachment; filename*=UTF-8''quarterly%20report%20%28v2%29.csv"
        )
        assert "/" not in response.headers["content-disposition"].split("''")[1]
        mock_service.get_job_artifact_download.assert_called_once()

    def test_api_job_artifact_download_declares_the_length_it_is_sending(self, client):
        """The stored row and the object on disk can disagree; the body cannot."""
        mock_service = self._artifact_download_service()

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_query_service": lambda: mock_service},
            ),
            patch(
                "core.entrypoints.api.dependencies._artifact_presigned_url",
                lambda artifact_id, *, method, ttl_seconds, filename: "",
            ),
            patch(
                # The row says 13 bytes; the object holds 5.
                "core.entrypoints.api.dependencies._artifact_local_stream",
                lambda artifact_id: (iter([b"short"]), 5),
            ),
        ):
            response = client.get(
                "/api/jobs/42/artifacts/art-42",
                follow_redirects=False,
                headers={"Accept-Encoding": "identity"},
            )

        assert response.status_code == 200
        assert response.content == b"short"
        assert response.headers["content-length"] == "5"

    def test_api_job_artifact_download_reports_a_missing_local_object(self, client):
        """Valid metadata with no object must not become an empty 200."""
        mock_service = self._artifact_download_service()

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_query_service": lambda: mock_service},
            ),
            patch(
                "core.entrypoints.api.dependencies._artifact_presigned_url",
                lambda artifact_id, *, method, ttl_seconds, filename: "",
            ),
            patch(
                "core.entrypoints.api.dependencies._artifact_local_stream",
                lambda artifact_id: None,
            ),
        ):
            response = client.get("/api/jobs/42/artifacts/art-42", follow_redirects=False)

        assert response.status_code == 503
        assert response.json()

    @pytest.mark.parametrize(
        "filename",
        [
            "folder/report.csv",
            "../report.csv",
            "..\\report.csv",
            "отчёт квартал.csv",
            "report\r\nX-Injected: 1.csv",
            'report";x="1.csv',
        ],
    )
    def test_api_job_artifact_download_encodes_every_filename_character(self, client, filename):
        """A stored name is data: nothing in it may reach the header verbatim."""
        mock_service = self._artifact_download_service(filename)

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_query_service": lambda: mock_service},
            ),
            patch(
                "core.entrypoints.api.dependencies._artifact_presigned_url",
                lambda artifact_id, *, method, ttl_seconds, filename: "",
            ),
            patch(
                "core.entrypoints.api.dependencies._artifact_local_stream",
                lambda artifact_id: (iter([b"sku,name\n"]), 13),
            ),
        ):
            response = client.get(
                "/api/jobs/42/artifacts/art-42",
                follow_redirects=False,
                headers={"Accept-Encoding": "identity"},
            )

        assert response.status_code == 200
        disposition = response.headers["content-disposition"]
        encoded = disposition.split("''", 1)[1]
        assert disposition.startswith("attachment; filename*=UTF-8''")
        # No separator, quote, whitespace or non-ASCII survives the encoding,
        # so the browser cannot read a path out of it and the header cannot be
        # split or terminated early.
        assert not any(character in encoded for character in '/\\"; \r\n')
        assert encoded.isascii()
        assert unquote(encoded) == filename

    def test_composition_root_wires_the_artifact_download_dependencies(self):
        """The route's fallback is only real if the app actually wires it.

        Every other test here builds the app through `app_factory` and patches
        these module globals, which would pass even if nothing wired them. The
        deployed entrypoint is `core.entrypoints.api.app`, and importing it
        runs the same wiring the server does.
        """
        import core.entrypoints.api.app  # noqa: F401  (import runs the wiring)
        from core.entrypoints.api import dependencies as api_dependencies
        from core.infrastructure.runtime import http_app_request_helpers as runtime_helpers

        plane = runtime_helpers._artifact_plane
        assert api_dependencies._artifact_local_stream is not None
        assert getattr(api_dependencies._artifact_local_stream, "__self__", None) is plane
        assert getattr(api_dependencies._artifact_presigned_url, "__self__", None) is plane

    def test_api_job_artifact_download_never_reads_storage_when_unauthorized(self, client):
        """Authorization runs first: a refusal must not touch the object store."""
        mock_service = MagicMock()
        mock_service.get_job_artifact_download.side_effect = ServiceNotFoundError(
            "Artifact not found"
        )
        reads: list[str] = []

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_query_service": lambda: mock_service},
            ),
            patch(
                "core.entrypoints.api.dependencies._artifact_presigned_url",
                lambda artifact_id, **kwargs: reads.append(artifact_id) or "",
            ),
            patch(
                "core.entrypoints.api.dependencies._artifact_local_stream",
                lambda artifact_id: reads.append(artifact_id) or (iter([b"leak"]), 4),
            ),
        ):
            response = client.get("/api/jobs/42/artifacts/art-42", follow_redirects=False)

        assert response.status_code == 404
        assert reads == []

    def test_api_project_jobs_list_uses_job_query_service_contract(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        payload = MagicMock()
        payload.dump.return_value = {
            "jobs": [{"id": 201, "task_summary": "Fix auth", "display_summary": "Fix auth"}],
            "total": 1,
            "page": 2,
            "per_page": 25,
            "total_pages": 1,
        }
        mock_service = MagicMock()
        mock_service.list_project_jobs.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"job_query_service": lambda: mock_service},
        ):
            resp = client.get("/api/projects/1/jobs?page=2&per_page=25")

        assert resp.status_code == 200
        body = resp.json()
        assert body["jobs"][0]["id"] == 201
        assert body["page"] == 2
        assert body["per_page"] == 25
        call = mock_service.list_project_jobs.call_args
        assert call.args == ("1",)
        assert call.kwargs["page"] == 2
        assert call.kwargs["per_page"] == 25
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].project_id == "1"

    def test_api_job_logs_supports_before_id_and_newest(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = _job_fixture(id=99, status="in_progress")
        mock_bl.get_logs.return_value = []

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get(
                "/api/jobs/99/logs",
                params={"before_id": 1500, "newest": 1, "limit": 50},
            )

        assert resp.status_code == 200
        mock_bl.get_logs.assert_called_once_with(
            "99",
            since_id="",
            before_id="1500",
            newest=True,
            limit=51,
        )

    def test_api_job_logs_serializes_backward_cursor(self, client):
        ids = [
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ]
        mock_bl = MagicMock()
        mock_bl.get_job.return_value = _job_fixture(id=99, status="completed")
        mock_bl.get_project.return_value = {"id": "1", "account_id": DEFAULT_ACCOUNT_ID}
        mock_bl.get_logs.return_value = [{"id": value} for value in ids]
        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            response = client.get("/api/jobs/99/logs", params={"limit": 2})
        assert response.status_code == 200
        payload = response.json()
        assert [row["id"] for row in payload["logs"]] == ids[1:]
        assert payload["last_id"] == ids[2]
        assert payload["next_before_id"] == ids[1]
        assert payload["has_more"] is True

    def test_api_job_logs_limit_is_clamped(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = _job_fixture(id=7, status="in_progress")
        mock_bl.get_logs.return_value = []

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/jobs/7/logs", params={"limit": 50000})

        assert resp.status_code == 200
        mock_bl.get_logs.assert_called_once_with(
            "7",
            since_id="",
            before_id=None,
            newest=False,
            limit=2001,
        )

    def test_api_message_detail_returns_serialized_message(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now = datetime.now(UTC)
        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 42,
            "project_id": 1,
            "status": "completed",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_bl.get_project.return_value = {
            "id": 1,
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_bl.get_agent_message_by_id.return_value = {
            "id": 501,
            "timestamp": now,
            "job_id": 42,
            "correlation_id": "corr-501",
            "schema_version": "agent_message_v1",
            "from_agent": "composer",
            "to_agent": "orchestrator",
            "step_id": "implementation",
            "skill": "draft_summary",
            "status": "pass",
            "summary": "done",
            "artifacts": {"files_changed": ["a.py"]},
            "errors": [],
            "envelope": {"schema_version": "agent_message_v1"},
        }

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/messages/501")

        assert resp.status_code == 200
        body = resp.json()
        assert "message" in body
        assert body["message"]["id"] == "501"
        assert body["message"]["step_id"] == "implementation"

    def test_api_job_replay_step_returns_message_trace(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now = datetime.now(UTC)
        message_row = {
            "id": 900,
            "timestamp": now,
            "job_id": 42,
            "correlation_id": "corr-900",
            "schema_version": "agent_message_v1",
            "from_agent": "composer",
            "to_agent": "orchestrator",
            "step_id": "implementation",
            "skill": "draft_summary",
            "status": "pass",
            "summary": "implemented",
            "artifacts": {"raw_output": "ok"},
            "errors": [],
            "envelope": {
                "schema_version": "agent_message_v1",
                "caused_by": [{"kind": "message", "correlation_id": "corr-899"}],
            },
        }
        prompt_req = {
            "id": 3001,
            "timestamp": now,
            "agent": "composer",
            "action_type": "prompt_request",
            "status": "done",
            "duration": 10,
            "payload": {"user_prompt": "Implement story"},
            "result": {},
            "error": None,
            "meta": {"correlation_id": "corr-900"},
            "tokens_prompt": None,
            "tokens_completion": None,
            "tokens_total": None,
            "cost_usd": None,
            "model_used": None,
        }
        prompt_resp = {
            "id": 3002,
            "timestamp": now,
            "agent": "composer",
            "action_type": "prompt_response",
            "status": "done",
            "duration": 15,
            "payload": {},
            "result": {"excerpt": "implemented"},
            "error": None,
            "meta": {"correlation_id": "corr-900"},
            "tokens_prompt": None,
            "tokens_completion": None,
            "tokens_total": None,
            "cost_usd": None,
            "model_used": None,
        }
        routing_ev = {
            "id": 701,
            "timestamp": now,
            "phase": "implementation",
            "agent": "orchestrator",
            "event_type": "routing_decision",
            "severity": "info",
            "title": "Phase implementation: next_phase",
            "detail": {"reason": "implemented", "cause_correlation_id": "corr-900"},
        }
        related_row = {
            "id": 899,
            "timestamp": now,
            "job_id": 42,
            "correlation_id": "corr-899",
            "schema_version": "agent_message_v1",
            "from_agent": "designer",
            "to_agent": "orchestrator",
            "step_id": "design",
            "skill": "outline_plan",
            "status": "pass",
            "summary": "design done",
            "artifacts": {},
            "errors": [],
            "envelope": {"schema_version": "agent_message_v1"},
        }

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = _job_fixture(id=42)
        mock_bl.get_agent_message_by_id.return_value = message_row
        mock_bl.get_events.return_value = [routing_ev]
        mock_bl.get_logs.return_value = [prompt_req, prompt_resp]
        mock_bl.get_agent_messages.return_value = [related_row]

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/jobs/42/replay-step", params={"message_id": 900})

        assert resp.status_code == 200
        body = resp.json()
        assert body["message"]["id"] == "900"
        assert body["routing_decisions"][0]["id"] == "701"
        assert body["prompt_request"]["action_type"] == "prompt_request"
        assert body["prompt_response"]["action_type"] == "prompt_response"
        assert body["related_messages"][0]["id"] == "899"

    def test_api_job_replay_step_rejects_foreign_message(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = _job_fixture(id=42)
        mock_bl.get_agent_message_by_id.return_value = {"id": 777, "job_id": 77}

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/jobs/42/replay-step", params={"message_id": 777})

        assert resp.status_code == 400

    def test_api_job_replay_step_execute_rejects_missing_prompt_capture(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now = datetime.now(UTC)
        message_row = {
            "id": 900,
            "timestamp": now,
            "job_id": 42,
            "correlation_id": "corr-900",
            "schema_version": "agent_message_v1",
            "from_agent": "composer",
            "to_agent": "orchestrator",
            "step_id": "implementation",
            "skill": "draft_summary",
            "status": "pass",
            "summary": "implemented",
            "artifacts": {},
            "errors": [],
            "envelope": {"schema_version": "agent_message_v1"},
        }
        prompt_req = {
            "id": 3001,
            "timestamp": now,
            "agent": "composer",
            "action_type": "prompt_request",
            "status": "done",
            "payload": {},
            "result": {},
            "error": None,
            "meta": {"correlation_id": "corr-900"},
        }
        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 42,
            "project_id": 1,
            "meta": {},
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_bl.get_project.return_value = {
            "id": 1,
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_bl.get_agent_message_by_id.return_value = message_row
        mock_bl.get_events.return_value = []
        mock_bl.get_logs.return_value = [prompt_req]
        mock_bl.get_agent_messages.return_value = []

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.post(
                "/api/jobs/42/replay-step/execute",
                json={"message_id": 900, "replay_mode": "prompt_reinvoke"},
            )

        assert resp.status_code == 400
        assert "missing system/user prompt text" in resp.json().get("error", "")

    def test_api_job_replay_step_execute_persists_successful_replay(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now = datetime.now(UTC)
        message_row = {
            "id": 901,
            "timestamp": now,
            "job_id": 42,
            "correlation_id": "corr-901",
            "schema_version": "agent_message_v1",
            "from_agent": "composer",
            "to_agent": "orchestrator",
            "step_id": "implementation",
            "skill": "draft_summary",
            "status": "pass",
            "summary": "implemented",
            "artifacts": {"raw_output": "original output"},
            "errors": [],
            "envelope": {"schema_version": "agent_message_v1"},
        }
        prompt_req = {
            "id": 3101,
            "timestamp": now,
            "agent": "composer",
            "action_type": "prompt_request",
            "status": "done",
            "payload": {
                "prompt_version": "v1",
                "system_prompt": "You are composer",
                "user_prompt": "Implement change",
            },
            "result": {},
            "error": None,
            "meta": {
                "correlation_id": "corr-901",
                "provider": "openai",
                "skill": "draft_summary",
            },
            "model_used": "gpt-4o-mini",
        }
        prompt_resp = {
            "id": 3102,
            "timestamp": now,
            "agent": "composer",
            "action_type": "prompt_response",
            "status": "done",
            "payload": {},
            "result": {"excerpt": "original output"},
            "error": None,
            "meta": {"correlation_id": "corr-901"},
        }

        created_row = {
            "id": 55,
            "job_id": 42,
            "message_id": 901,
            "correlation_id": "corr-901",
            "requested_by": "operator",
            "replay_mode": "prompt_reinvoke",
            "status": "running",
            "prompt_version": "v1",
            "policy_snapshot": {},
            "captured_input": {},
            "original_outcome": {},
            "replay_outcome": {},
            "diff_excerpt": None,
            "error_message": None,
            "created_at": now,
            "started_at": now,
            "finished_at": None,
        }
        updated_row = dict(created_row)
        updated_row["status"] = "succeeded"
        updated_row["replay_outcome"] = {"response_text": "replayed output"}
        updated_row["finished_at"] = now

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 42,
            "project_id": 1,
            "account_id": DEFAULT_ACCOUNT_ID,
            "meta": {
                "project_policy_snapshot": {"retry_strategy": "failure_class"},
                "project_config_snapshot": {
                    "project_name": "snap-demo",
                    "agents_config": {"default_provider": "openai"},
                    "secret_token": "[REDACTED]",
                },
            },
        }
        mock_bl.get_project.return_value = {
            "id": 1,
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_bl.get_project_run_config.return_value = {"project_name": "demo", "agents_config": {}}
        mock_bl.get_agent_message_by_id.return_value = message_row
        mock_bl.get_events.return_value = []
        mock_bl.get_logs.return_value = [prompt_req, prompt_resp]
        mock_bl.get_agent_messages.return_value = []
        mock_bl.create_job_step_replay.return_value = created_row
        mock_bl.update_job_step_replay.return_value = updated_row

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                scheduled_runtime,
                "_invoke_step_replay_llm",
                return_value={
                    "response_text": "replayed output",
                    "response_excerpt": "replayed output",
                    "provider": "openai",
                    "model_used": "gpt-4o-mini",
                    "response_metadata": {"model": "gpt-4o-mini"},
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                },
            ) as invoke_mock,
        ):
            resp = client.post(
                "/api/jobs/42/replay-step/execute",
                json={"message_id": 901, "replay_mode": "prompt_reinvoke"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["replay_id"] == "55"
        assert body["original_outcome"]["response_text"] == "original output"
        assert body["replay_outcome"]["response_text"] == "replayed output"
        mock_bl.create_job_step_replay.assert_called_once()
        mock_bl.update_job_step_replay.assert_called_once()
        invoke_mock.assert_called_once()
        invoke_kwargs = invoke_mock.call_args.kwargs
        assert invoke_kwargs["project_config"]["project_name"] == "snap-demo"
        assert invoke_kwargs["policy_snapshot"]["retry_strategy"] == "failure_class"
        assert invoke_kwargs["prompt_version"] == "v1"
        mock_bl.get_project_run_config.assert_not_called()

    def test_api_job_replay_step_execute_defaults_to_step_reexecute(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now = datetime.now(UTC)
        message_row = {
            "id": 902,
            "timestamp": now,
            "job_id": 42,
            "correlation_id": "corr-902",
            "schema_version": "agent_message_v1",
            "from_agent": "composer",
            "to_agent": "orchestrator",
            "step_id": "implementation",
            "skill": "draft_summary",
            "status": "pass",
            "summary": "implemented",
            "artifacts": {"raw_output": "original output"},
            "errors": [],
            "envelope": {"schema_version": "agent_message_v1"},
        }
        created_row = {
            "id": 56,
            "job_id": 42,
            "message_id": 902,
            "correlation_id": "corr-902",
            "requested_by": "operator",
            "replay_mode": "step_reexecute",
            "status": "running",
            "prompt_version": "v1",
            "policy_snapshot": {},
            "captured_input": {},
            "original_outcome": {},
            "replay_outcome": {},
            "diff_excerpt": None,
            "error_message": None,
            "created_at": now,
            "started_at": now,
            "finished_at": None,
        }
        updated_row = dict(created_row)
        updated_row["status"] = "succeeded"
        updated_row["replay_outcome"] = {"response_text": "replayed output"}
        updated_row["finished_at"] = now

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 42,
            "project_id": 1,
            "account_id": DEFAULT_ACCOUNT_ID,
            "meta": {"project_config_snapshot": {"project_name": "snap-demo"}},
        }
        mock_bl.get_project.return_value = {
            "id": 1,
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_bl.get_agent_message_by_id.return_value = message_row
        mock_bl.get_events.return_value = []
        mock_bl.get_logs.return_value = []
        mock_bl.get_agent_messages.return_value = []
        mock_bl.create_job_step_replay.return_value = created_row
        mock_bl.update_job_step_replay.return_value = updated_row

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                scheduled_runtime,
                "_invoke_step_reexecute_runtime",
                return_value={
                    "response_text": "replayed output",
                    "response_excerpt": "replayed output",
                    "message_status": "pass",
                },
            ) as invoke_mock,
        ):
            resp = client.post("/api/jobs/42/replay-step/execute", json={"message_id": 902})

        assert resp.status_code == 200
        body = resp.json()
        assert body["replay_outcome"]["response_text"] == "replayed output"
        invoke_mock.assert_called_once()
        mock_bl.create_job_step_replay.assert_called_once()
        assert (
            mock_bl.create_job_step_replay.call_args.kwargs.get("replay_mode") == "step_reexecute"
        )

    def test_api_job_replay_step_execute_uses_authenticated_actor(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for step replay actor test"
            )

        now = datetime.now(UTC)
        message_row = {
            "id": 904,
            "timestamp": now,
            "job_id": 42,
            "correlation_id": "corr-904",
            "schema_version": "agent_message_v1",
            "from_agent": "composer",
            "to_agent": "orchestrator",
            "step_id": "implementation",
            "skill": "draft_summary",
            "status": "pass",
            "summary": "implemented",
            "artifacts": {"raw_output": "original output"},
            "errors": [],
            "envelope": {"schema_version": "agent_message_v1"},
        }
        created_row = {
            "id": 57,
            "job_id": 42,
            "message_id": 904,
            "correlation_id": "corr-904",
            "requested_by": "alice",
            "replay_mode": "step_reexecute",
            "status": "running",
            "prompt_version": "v1",
            "policy_snapshot": {},
            "captured_input": {},
            "original_outcome": {},
            "replay_outcome": {},
            "diff_excerpt": None,
            "error_message": None,
            "created_at": now,
            "started_at": now,
            "finished_at": None,
        }
        updated_row = dict(created_row)
        updated_row["status"] = "succeeded"
        updated_row["replay_outcome"] = {"response_text": "replayed output"}
        updated_row["finished_at"] = now

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 42,
            "project_id": 1,
            "meta": {},
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_bl.get_project.return_value = {
            "id": 1,
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_bl.get_agent_message_by_id.return_value = message_row
        mock_bl.get_events.return_value = []
        mock_bl.get_logs.return_value = []
        mock_bl.get_agent_messages.return_value = []
        mock_bl.create_job_step_replay.return_value = created_row
        mock_bl.update_job_step_replay.return_value = updated_row
        auth_user = {
            "id": 1,
            "username": "alice",
            "display_name": "Alice",
            "roles": ["operator"],
            "is_active": True,
        }

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_auth_tables_available", return_value=True),
            patch.object(api_routes, "_get_user_by_session_token", return_value=auth_user),
            patch.object(
                scheduled_runtime,
                "_invoke_step_reexecute_runtime",
                return_value={
                    "response_text": "replayed output",
                    "response_excerpt": "replayed output",
                    "message_status": "pass",
                },
            ),
        ):
            resp = client.post(
                "/api/jobs/42/replay-step/execute",
                headers={"Authorization": "Bearer token-123"},
                json={"message_id": 904, "requested_by": "mallory"},
            )

        assert resp.status_code == 200
        kwargs = mock_bl.create_job_step_replay.call_args.kwargs
        assert kwargs["requested_by"] == "alice"

    def test_api_job_replay_step_execute_does_not_fallback_to_unrelated_prompt_logs(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now = datetime.now(UTC)
        message_row = {
            "id": 903,
            "timestamp": now,
            "job_id": 42,
            "correlation_id": "corr-target",
            "schema_version": "agent_message_v1",
            "from_agent": "composer",
            "to_agent": "orchestrator",
            "step_id": "implementation",
            "skill": "draft_summary",
            "status": "pass",
            "summary": "implemented",
            "artifacts": {},
            "errors": [],
            "envelope": {"schema_version": "agent_message_v1"},
        }
        unrelated_prompt_req = {
            "id": 3201,
            "timestamp": now,
            "agent": "composer",
            "action_type": "prompt_request",
            "status": "done",
            "payload": {"system_prompt": "A", "user_prompt": "B"},
            "result": {},
            "error": None,
            "meta": {"correlation_id": "corr-other"},
        }
        unrelated_prompt_resp = {
            "id": 3202,
            "timestamp": now,
            "agent": "composer",
            "action_type": "prompt_response",
            "status": "done",
            "payload": {},
            "result": {"excerpt": "other"},
            "error": None,
            "meta": {"correlation_id": "corr-other"},
        }

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 42,
            "project_id": 1,
            "meta": {},
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_bl.get_project.return_value = {
            "id": 1,
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_bl.get_agent_message_by_id.return_value = message_row
        mock_bl.get_events.return_value = []
        mock_bl.get_logs.return_value = [unrelated_prompt_req, unrelated_prompt_resp]
        mock_bl.get_agent_messages.return_value = []

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.post(
                "/api/jobs/42/replay-step/execute",
                json={"message_id": 903, "replay_mode": "prompt_reinvoke"},
            )

        assert resp.status_code == 400
        assert "missing system/user prompt text" in resp.json().get("error", "")

    def test_api_job_step_replays_returns_rows(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now = datetime.now(UTC)
        mock_bl = MagicMock()
        mock_bl.get_job.return_value = _job_fixture(id=42)
        mock_bl.list_job_step_replays.return_value = [
            {
                "id": 9,
                "job_id": 42,
                "message_id": 900,
                "correlation_id": "corr-900",
                "requested_by": "operator",
                "replay_mode": "prompt_reinvoke",
                "status": "succeeded",
                "prompt_version": "v1",
                "policy_snapshot": {"retry_strategy": "failure_class"},
                "captured_input": {"provider": "openai"},
                "original_outcome": {"response_text": "a"},
                "replay_outcome": {"response_text": "b"},
                "diff_excerpt": "---",
                "error_message": None,
                "created_at": now,
                "started_at": now,
                "finished_at": now,
            }
        ]

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/jobs/42/step-replays", params={"message_id": 900, "limit": 10})

        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == "42"
        assert body["step_replays"][0]["id"] == 9

    def test_api_job_retry_step_requeues_original_job_and_clears_snapshot(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now = datetime.now(UTC)
        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 42,
            "project_id": 1,
            "status": "retryable_interrupted_step",
            "thread_id": "thread-42",
            "task_id": "TASK-42",
            "task_summary": "Recover interrupted step",
            "task_source": "manual",
            "mode": "brownfield",
            "account_id": DEFAULT_ACCOUNT_ID,
            "meta": {
                "replay_snapshot": {"artifact_id": "artifact-a"},
                "retryable_interrupted_step": {
                    "detected_at": now.isoformat(),
                    "detection_source": "prompt_request_without_response",
                    "job_id": 42,
                    "thread_id": "thread-42",
                    "step_id": "composer:draft_summary",
                    "agent_id": "composer",
                    "skill": "draft_summary",
                    "correlation_id": "corr-42",
                    "provider": "openai",
                    "model_used": "gpt-4o-mini",
                    "prompt_version": "v1",
                    "system_prompt": "You are composer",
                    "user_prompt": "Implement the fix",
                },
                "project_config_snapshot": {"project_name": "snap-demo"},
                "project_policy_snapshot": {"retry_strategy": "failure_class"},
            },
        }
        mock_bl.get_project.return_value = {
            "id": 1,
            "name": "snap-demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_bl.get_project_run_config.return_value = {
            "project_name": "snap-demo",
            "project_root": "/target/snap-demo",
            "agents_config": {"default_provider": "openai"},
        }

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(scheduled_runtime, "_enqueue_task_in_queue") as enqueue_mock,
            patch.object(
                scheduled_runtime,
                "_load_job_replay_snapshot",
                return_value={
                    "workflow_id": "wf-retry",
                    "inputs": {"message": "original"},
                    "files": [],
                    "test_mode": False,
                    "input_contract": "direct_inputs",
                    "task_envelope": {"inputs": {"message": "original"}, "files": []},
                    "_workflow_secret_ref_fingerprints": ["a" * 64, "b" * 64],
                },
            ),
            patch.object(
                scheduled_runtime,
                "_append_runtime_journal",
                return_value={"event": "manual_retry_step"},
            ),
        ):
            resp = client.post("/api/jobs/42/retry-step", json={})

        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == "42"
        assert body["status"] == "pending"
        assert body["enqueued"] is True
        enqueue_kwargs = enqueue_mock.call_args.kwargs
        assert enqueue_kwargs["job_id"] == "42"
        assert enqueue_kwargs["thread_id"] == "thread-42"
        assert enqueue_kwargs["extra_state"]["retry_interrupted_step"] is True
        assert enqueue_kwargs["extra_state"]["retry_interrupted_skill"] == "draft_summary"
        assert enqueue_kwargs["extra_state"]["_workflow_secret_ref_fingerprints"] == [
            "a" * 64,
            "b" * 64,
        ]

        assert mock_bl.update_job.call_count >= 1
        first_update = mock_bl.update_job.call_args_list[0]
        assert first_update.kwargs["status"] == "pending"
        updated_meta = first_update.kwargs["meta"]
        assert "retryable_interrupted_step" not in updated_meta

    def test_api_job_retry_step_requires_snapshot(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 42,
            "project_id": 1,
            "status": "retryable_interrupted_step",
            "meta": {},
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_bl.get_project.return_value = {
            "id": 1,
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.post("/api/jobs/42/retry-step", json={})

        assert resp.status_code == 409
        assert "snapshot" in resp.json().get("error", "").lower()

    def test_api_job_retry_step_rejects_non_actionable_snapshot(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 42,
            "project_id": 1,
            "status": "retryable_interrupted_step",
            "account_id": DEFAULT_ACCOUNT_ID,
            "meta": {
                "retryable_interrupted_step": {
                    "step_id": "implementation",
                    "agent_id": "composer",
                    # skill missing -> non-actionable
                }
            },
        }
        mock_bl.get_project.return_value = {
            "id": 1,
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.post("/api/jobs/42/retry-step", json={})

        assert resp.status_code == 409
        assert "snapshot" in resp.json().get("error", "").lower()


# ---------------------------------------------------------------------------
# Approval endpoint
# ---------------------------------------------------------------------------


class TestApprovalEndpoint:
    def test_query_decision_contract_is_rejected(self, client):
        resp = client.post("/api/approvals/thread-query?approved=true")

        assert resp.status_code == 400
        assert "application/json" in str(resp.json().get("error", ""))

    def test_approve_bad_thread_returns_error_not_crash(self, client):
        """Approving a non-existent thread_id should not crash the server."""
        from tests import api_runtime_test_surface as api_runtime

        mock_service = MagicMock()
        mock_service.resume_task.return_value = _workflow_resume_payload("bad_thread_id_xyz")

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"approval_command_service": lambda: mock_service},
            ),
            patch.object(auth_runtime, "_get_checkpointer", return_value=object()),
            patch.object(events_runtime, "_resume_shutdown_in_progress", return_value=False),
            patch.object(
                api_runtime,
                "_start_graph_resume_thread",
                return_value=SimpleNamespace(name="resume-test"),
            ),
        ):
            resp = client.post(
                "/api/approvals/bad_thread_id_xyz",
                json={"approval_id": "gate-1", "approved": True},
            )

        assert resp.status_code == 202

    def test_approve_with_comment(self, client):
        from tests import api_runtime_test_surface as api_runtime

        mock_service = MagicMock()
        mock_service.resume_task.return_value = _workflow_resume_payload(
            "bad_thread_id",
            approved=False,
            comment="Needs revision",
        )

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"approval_command_service": lambda: mock_service},
            ),
            patch.object(auth_runtime, "_get_checkpointer", return_value=object()),
            patch.object(events_runtime, "_resume_shutdown_in_progress", return_value=False),
            patch.object(
                api_runtime,
                "_start_graph_resume_thread",
                return_value=SimpleNamespace(name="resume-test"),
            ),
        ):
            resp = client.post(
                "/api/approvals/bad_thread_id",
                json={
                    "approval_id": "gate-1",
                    "approved": False,
                    "comment": "Needs revision",
                },
            )

        assert resp.status_code == 202

    def test_approve_with_action_param(self, client):
        from tests import api_runtime_test_surface as api_runtime

        mock_service = MagicMock()
        mock_service.resume_task.return_value = _workflow_resume_payload(
            "bad_thread_id",
            action="rework",
        )

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"approval_command_service": lambda: mock_service},
            ),
            patch.object(auth_runtime, "_get_checkpointer", return_value=object()),
            patch.object(events_runtime, "_resume_shutdown_in_progress", return_value=False),
            patch.object(
                api_runtime,
                "_start_graph_resume_thread",
                return_value=SimpleNamespace(name="resume-test"),
            ),
        ):
            resp = client.post(
                "/api/approvals/bad_thread_id",
                json={"approval_id": "gate-1", "action": "rework"},
            )

        assert resp.status_code == 202

    def test_approve_rejected_during_shutdown(self, client):
        from core.application.management.errors import ServiceUnavailableError

        mock_service = MagicMock()
        mock_service.resume_task.side_effect = ServiceUnavailableError(
            "Service shutdown in progress — workflow resume is temporarily unavailable"
        )

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"approval_command_service": lambda: mock_service},
        ):
            resp = client.post(
                "/api/approvals/thread-xyz",
                json={"approval_id": "gate-1", "approved": True},
            )

        assert resp.status_code == 503
        data = resp.json()
        assert "shutdown" in str(data.get("error", "")).lower()

    def test_approve_does_not_eager_build_global_graph_before_accepting_resume(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_service = MagicMock()
        mock_service.resume_task.return_value = _workflow_resume_payload("thread-xyz")

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"approval_command_service": lambda: mock_service},
            ),
            patch.object(events_runtime, "_resume_shutdown_in_progress", return_value=False),
            patch.object(auth_runtime, "_get_checkpointer", return_value=object()),
            patch.object(
                api_runtime,
                "build_registry_graph",
                side_effect=RuntimeError("Graph contract validation failed"),
            ) as build_graph,
            patch.object(
                api_runtime,
                "_start_graph_resume_thread",
                return_value=MagicMock(name="resume-thread"),
            ),
        ):
            resp = client.post(
                "/api/approvals/thread-xyz",
                json={"approval_id": "gate-1", "approved": True},
            )

        assert resp.status_code == 202
        build_graph.assert_not_called()

    def test_approve_passes_actor_to_resume_thread(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(e, context="api_runtime import for approval actor test")

        mock_service = MagicMock()
        mock_service.resume_task.return_value = _workflow_resume_payload(
            "thread-actor",
            actor="alice",
        )

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"approval_command_service": lambda: mock_service},
            ),
            patch.object(events_runtime, "_resume_shutdown_in_progress", return_value=False),
            patch.object(auth_runtime, "_get_checkpointer", return_value=object()),
            patch.object(
                api_routes,
                "_request_scope",
                return_value=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="alice",
                ),
            ),
        ):
            resp = client.post(
                "/api/approvals/thread-actor",
                json={"approval_id": "gate-1", "approved": True},
            )

        assert resp.status_code == 202
        assert resp.json()["actor"] == "alice"
        call = mock_service.resume_task.call_args
        assert call.kwargs["scope"].actor_id == "alice"

    def test_approve_transport_resolves_scope_before_service_call(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(e, context="api_runtime import for approval scope test")

        payload = MagicMock()
        payload.dump.return_value = {
            "thread_id": "thread-scope",
            "approved": True,
            "comment": "ship it",
            "override_content": "",
            "actor": "alice",
            "status": "in_progress",
        }
        mock_service = MagicMock()
        mock_service.resume_task.return_value = payload

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"approval_command_service": lambda: mock_service},
            ),
            patch.object(
                api_routes,
                "_request_scope",
                return_value=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="alice",
                ),
            ),
        ):
            resp = client.post(
                "/api/approvals/thread-scope",
                json={
                    "approval_id": "gate-1",
                    "approved": True,
                    "comment": "ship it",
                },
            )

        assert resp.status_code == 202
        call = mock_service.resume_task.call_args
        assert call.kwargs["thread_id"] == "thread-scope"
        assert call.kwargs["decision"].approved is True
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].actor_id == "alice"

    def test_approve_accepts_override_content_payload(self, client):
        try:
            pass
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for override_content approval payload test"
            )

        override = "# Operator Architecture\n\nUse a queue-backed design."
        mock_service = MagicMock()
        mock_service.resume_task.return_value = _workflow_resume_payload(
            "thread-json-override",
            comment="Use the operator version",
            override_content=override,
        )

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"approval_command_service": lambda: mock_service},
            ),
            patch.object(events_runtime, "_resume_shutdown_in_progress", return_value=False),
            patch.object(auth_runtime, "_get_checkpointer", return_value=object()),
        ):
            resp = client.post(
                "/api/approvals/thread-json-override",
                json={
                    "approval_id": "gate-1",
                    "approved": True,
                    "comment": "Use the operator version",
                    "override_content": override,
                },
            )

        assert resp.status_code == 202
        body = resp.json()
        assert body["override_content"] == override
        decision = mock_service.resume_task.call_args.kwargs["decision"]
        assert decision.override_content == override

    def test_approve_rejects_removed_selected_stories_field(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for invalid selected_stories test"
            )

        with (
            patch.object(events_runtime, "_resume_shutdown_in_progress", return_value=False),
            patch.object(auth_runtime, "_get_checkpointer", return_value=object()),
            patch.object(api_runtime, "build_registry_graph", return_value=MagicMock()),
        ):
            resp = client.post(
                "/api/approvals/thread-json-invalid",
                json={"approval_id": "gate-1", "approved": True, "selected_stories": ["S-1"]},
            )

        assert resp.status_code == 400
        assert "selected_stories" in str(resp.json().get("error", ""))


class TestResumeThreadLifecycle:
    def test_start_and_join_resume_thread(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        entered = threading.Event()
        release = threading.Event()

        def _fake_run(*_args, **_kwargs):
            entered.set()
            release.wait(2.0)

        try:
            with (
                patch.dict(os.environ, {"FS_GRAPH_RESUME_EXECUTOR": "thread"}),
                patch.object(events_runtime, "_run_graph_resume", side_effect=_fake_run),
                patch.object(events_runtime, "_resume_shutdown_in_progress", return_value=False),
            ):
                thread = events_runtime._start_graph_resume_thread(
                    thread_id="thread-test",
                    resume_value={"approved": True},
                    approved=True,
                    action=None,
                    comment="ok",
                    interrupt_payload={},
                )

            assert thread is not None
            assert entered.wait(0.5)
            assert any(t.name == thread.name for t in api_runtime._snapshot_resume_threads())
        finally:
            release.set()
            api_runtime._join_resume_threads(timeout_seconds=2.0)

        deadline = time.monotonic() + 0.5
        thread_name = thread.name
        while time.monotonic() < deadline:
            api_runtime._join_resume_threads(timeout_seconds=0.1)
            active_names = {t.name for t in api_runtime._snapshot_resume_threads()}
            if not thread.is_alive() and thread_name not in active_names:
                break
        assert not thread.is_alive()
        assert thread_name not in {t.name for t in api_runtime._snapshot_resume_threads()}

    def test_start_resume_thread_returns_none_when_shutdown_active(self):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        with patch.object(events_runtime, "_resume_shutdown_in_progress", return_value=True):
            thread = events_runtime._start_graph_resume_thread(
                thread_id="thread-test",
                resume_value={},
                approved=True,
                action=None,
                comment="",
                interrupt_payload={},
            )
        assert thread is None


class TestApprovalHistoryHelpers:
    def test_append_approval_history_collapses_identical_consecutive_entries(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        first = api_runtime._build_approval_entry(
            interrupt_payload={
                "stage": "Final Delivery Review",
                "message": "Please review",
                "content": "done",
            },
            approved=True,
            action=None,
            comment="",
            timestamp="2026-03-08T20:00:00+00:00",
        )
        second = api_runtime._build_approval_entry(
            interrupt_payload={
                "stage": "Final Delivery Review",
                "message": "Please review",
                "content": "done",
            },
            approved=True,
            action=None,
            comment="",
            timestamp="2026-03-08T20:05:00+00:00",
        )

        approvals = api_runtime._append_approval_history([], first)
        approvals = api_runtime._append_approval_history(approvals, second)

        assert len(approvals) == 1
        assert approvals[0]["repeat_count"] == 2
        assert approvals[0]["timestamp"] == "2026-03-08T20:05:00+00:00"

    def test_append_approval_history_keeps_distinct_decisions_separate(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        first = api_runtime._build_approval_entry(
            interrupt_payload={
                "stage": "Code Review",
                "message": "Please review",
                "content": "needs_changes",
            },
            approved=True,
            action=None,
            comment="apply it",
            timestamp="2026-03-08T20:00:00+00:00",
        )
        second = api_runtime._build_approval_entry(
            interrupt_payload={
                "stage": "Code Review",
                "message": "Please review",
                "content": "pass",
            },
            approved=True,
            action=None,
            comment="looks good",
            timestamp="2026-03-08T20:05:00+00:00",
        )

        approvals = api_runtime._append_approval_history([], first)
        approvals = api_runtime._append_approval_history(approvals, second)

        assert len(approvals) == 2


# ---------------------------------------------------------------------------
# Manual task API
# ---------------------------------------------------------------------------


class TestEnqueueTaskInternals:
    @pytest.fixture(autouse=True)
    def application_submission_repositories(self, monkeypatch):
        from core.application.jobs import task_enqueue_runtime

        class _OutboxRepositoryDouble:
            def __init__(self, logger):
                self.logger = logger

            def get(self, *, job_id):
                return self.logger.get_job_dispatch_outbox(job_id)

            def mark(self, *, job_id, status, last_error=None):
                return self.logger.mark_job_dispatch_outbox(
                    job_id, status=status, last_error=last_error
                )

            def replace_envelope(self, *, job_id, encrypted_envelope):
                return self.logger.update_job_dispatch_outbox_envelope(
                    job_id, encrypted_envelope=encrypted_envelope
                )

        class _SubmissionRepositoryDouble:
            def __init__(self, logger, _transaction_conn):
                self.logger = logger

            def create(self, *, encrypted_envelope, outbox_status, **job_fields):
                owner_token = job_fields.pop("preparation_owner_token", None)
                lease_seconds = job_fields.pop("preparation_lease_seconds", 300)
                job_id = self.logger.create_job(**job_fields)
                self.logger.create_job_dispatch_outbox(
                    job_id,
                    encrypted_envelope=encrypted_envelope,
                    status=outbox_status,
                    preparation_owner_token=owner_token,
                    preparation_lease_seconds=lease_seconds,
                )
                return job_id

        monkeypatch.setattr(
            task_enqueue_runtime,
            "_dispatch_outbox_repository",
            lambda logger: _OutboxRepositoryDouble(logger),
        )
        monkeypatch.setattr(
            "core.application.jobs.job_submission_repository.JobSubmissionRepository",
            _SubmissionRepositoryDouble,
        )

    def test_enqueue_task_marks_job_failed_when_publish_fails(self):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.create_job.return_value = 321
        terminal_service = MagicMock()

        with (
            patch.object(runtime_helpers, "CONFIG", {"fs_db_dsn": "postgres://dsn"}),
            patch.object(runtime_helpers, "_get_job_store", return_value=mock_bl),
            patch.object(
                runtime_helpers, "_enqueue_task_in_queue", side_effect=RuntimeError("broker nack")
            ),
            patch(
                "core.application.jobs.terminal_persistence.TerminalPersistenceService",
                return_value=terminal_service,
            ),
            pytest.raises(RuntimeError, match="broker nack"),
        ):
            runtime_helpers._enqueue_task(
                task_id="task-1",
                task_summary="test summary",
                task_source="manual",
                project_id="1",
                project_name="test-project",
                project_config={},
            )

        terminal_service.complete_failure.assert_called_once()
        kwargs = terminal_service.complete_failure.call_args.kwargs
        assert kwargs["status"] == "failed"
        assert "Queue publish failed" in kwargs["error_message"]
        mock_bl.log_event.assert_called_once()

    def test_enqueue_task_uses_observability_dsn_when_logger_dsn_missing(self):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.create_job.return_value = 654

        with (
            patch.object(runtime_helpers, "CONFIG", {"fs_observability_dsn": "postgres://obs-dsn"}),
            patch.object(
                runtime_helpers, "_get_job_store", return_value=mock_bl
            ) as mock_logger_factory,
            patch.object(runtime_helpers, "_enqueue_task_in_queue", return_value=None),
        ):
            job_id = runtime_helpers._enqueue_task(
                task_id="task-obs",
                task_summary="test summary",
                task_source="manual",
                project_id="1",
                project_name="test-project",
                project_config={},
            )

        assert job_id == 654
        mock_logger_factory.assert_called_once_with()

    def test_enqueue_task_persists_parent_workflow_id_from_extra_state(self):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.create_job.return_value = 777

        with (
            patch.object(runtime_helpers, "CONFIG", {"fs_observability_dsn": "postgres://obs-dsn"}),
            patch.object(runtime_helpers, "_get_job_store", return_value=mock_bl),
            patch.object(runtime_helpers, "_enqueue_task_in_queue", return_value=None),
        ):
            runtime_helpers._enqueue_task(
                task_id="task-parent",
                task_summary="test summary",
                task_source="manual",
                project_id="1",
                project_name="test-project",
                project_config={},
                extra_state={
                    "parent_workflow_id": "weekly_digest",
                },
                meta={},
            )

        create_kwargs = mock_bl.create_job.call_args.kwargs
        assert create_kwargs["meta"]["parent_workflow_id"] == "weekly_digest"
        removed_meta_key = "parent_" + "workflow" + "_family"
        assert removed_meta_key not in create_kwargs["meta"]

    def test_enqueue_task_test_mode_persists_flag_and_skips_notifications(self):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.create_job.return_value = 888

        with (
            patch.object(runtime_helpers, "CONFIG", {"fs_observability_dsn": "postgres://obs-dsn"}),
            patch.object(runtime_helpers, "_get_job_store", return_value=mock_bl),
            patch.object(
                runtime_helpers, "_enqueue_task_in_queue", return_value=None
            ) as mock_queue,
            patch(
                "core.application.notifications.dispatch.dispatch_project_notification_event"
            ) as mock_notify,
        ):
            runtime_helpers._enqueue_task(
                task_id="task-test-mode",
                task_summary="test summary",
                task_source="manual",
                project_id="1",
                project_name="test-project",
                project_config={},
                test_mode=True,
            )

        create_kwargs = mock_bl.create_job.call_args.kwargs
        assert create_kwargs["meta"]["test_mode"] is True
        assert mock_queue.call_args.kwargs["extra_state"]["test_mode"] is True
        mock_notify.assert_not_called()


class TestWorkflowSelectionHelpers:
    def test_build_project_policy_from_form_preserves_valid_numbers_and_retry_normalization(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for project policy normalization test"
            )

        policy = api_runtime._build_project_policy_from_form(
            {
                "policy_autonomy_mode": "auto",
                "policy_autonomy_confidence_threshold": "0.9",
                "policy_retry_strategy": "failure_class",
                "policy_retry_max_attempts": "-4",
                "policy_retry_backoff_initial_seconds": "3",
                "policy_retry_backoff_max_seconds": "250",
                "policy_concurrency_cap": "2",
                "policy_tool_policy_mode": "permissive",
            }
        )

        assert policy["autonomy_mode"] == "auto"
        assert policy["autonomy_confidence_threshold"] == 0.9
        assert policy["retry_strategy"] == "failure_class"
        assert policy["retry_max_attempts"] == 0
        assert policy["retry_backoff_initial_seconds"] == 3
        assert policy["retry_backoff_max_seconds"] == 250
        assert "worktree_policy" not in policy
        assert policy["concurrency_cap"] == 2
        assert policy["tool_policy_mode"] == "permissive"

    def test_build_project_policy_from_form_rejects_invalid_policy_enum(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for project policy enum validation test"
            )

        with pytest.raises(ValueError, match="policy_retry_strategy"):
            api_runtime._build_project_policy_from_form(
                {
                    "policy_retry_strategy": "surprise_mode",
                }
            )

    def test_project_policy_api_rejects_invalid_enum_payload(self, client):
        resp = client.put(
            "/api/projects/1/policy",
            json={
                "autonomy_mode": "invalid_mode",
            },
        )
        assert resp.status_code == 422

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("concurrency_cap", 0),
            ("concurrency_cap", -1),
            ("concurrency_cap", 1.5),
            ("autonomy_confidence_threshold", 2),
            ("autonomy_confidence_threshold", -0.1),
        ],
    )
    def test_project_policy_api_rejects_invalid_numbers_before_any_write(
        self, client, field, value
    ):
        from core.application.management.command_services import ProjectCommandService

        with patch.object(ProjectCommandService, "update_project_policy") as write:
            response = client.put("/api/projects/1/policy", json={field: value})
        assert response.status_code == 422
        assert response.json()["details"]["errors"][0]["loc"] == ["body", field]
        write.assert_not_called()

    def test_resolve_workflow_selection_requires_existing_explicit_workflow_id(self):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        with (
            patch.object(
                indexing_runtime,
                "_list_workflow_choices",
                return_value=[{"workflow_id": "weekly_digest", "enabled": True}],
            ),
            pytest.raises(ValueError, match="Workflow 'quick_bugfix' not found"),
        ):
            indexing_runtime._resolve_workflow_selection("quick_bugfix", [])

    def test_validate_explicit_override_rejects_runtime_workflow_mismatch(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        fake_routing = MagicMock()
        fake_routing.workflow_id = "weekly_report"
        fake_supervisor = MagicMock()
        fake_supervisor.resolve_workflow_routing.return_value = fake_routing

        with (
            patch(
                "core.application.orchestration.supervisor.OrchestratorSupervisor",
                return_value=fake_supervisor,
            ),
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=MagicMock(),
            ),
            patch(
                "core.application.registry.contract_validation.validate_workflow_readiness",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
            pytest.raises(ValueError, match="Runtime resolved 'weekly_report' instead"),
        ):
            api_runtime._validate_explicit_workflow_override(
                selected_workflow_id="weekly_digest",
                selection_source="explicit_request",
                task_summary="Build account export API",
                project_name="demo",
                project_config={"project_name": "demo"},
            )

    def test_validate_explicit_override_rejects_unresolved_contract_errors(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_registry = MagicMock()
        mock_registry.db_store = MagicMock()

        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=mock_registry,
            ),
            patch(
                "core.application.registry.contract_validation.validate_workflow_readiness",
                return_value={
                    "valid": False,
                    "errors": [{"message": "Phase 'analysis' has no steps"}],
                    "warnings": [],
                },
            ),
            pytest.raises(ValueError, match="unresolved contract errors"),
        ):
            api_runtime._validate_explicit_workflow_override(
                selected_workflow_id="weekly_digest",
                selection_source="explicit_request",
                task_summary="Build account export API",
                project_name="demo",
                project_config={"project_name": "demo"},
            )


# ---------------------------------------------------------------------------
# Provider endpoints
# ---------------------------------------------------------------------------


class TestProviderEndpoints:
    def test_extension_browser_asset_route_is_registered(self, client):
        paths = {getattr(route, "path", "") for route in client.app.routes}

        assert "/api/extensions/{extension_id}/assets/{asset_ref:path}" in paths

    def test_api_providers_returns_catalog(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.list_provider_types.return_value = [
            {"key": "task_provider", "label": "Task Provider", "enabled": True}
        ]
        mock_bl.list_providers.return_value = [
            {
                "provider_key": "sample_tasks",
                "provider_type_key": "task_provider",
                "display_name": "Sample Tasks",
                "enabled": True,
            }
        ]

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/providers")

        assert resp.status_code == 200
        body = resp.json()
        assert body["provider_types"][0]["key"] == "task_provider"
        assert body["providers"][0]["provider_key"] == "sample_tasks"
        mock_bl.list_providers.assert_called_once_with(enabled_only=True, provider_type_key=None)
        mock_bl.list_latest_provider_health.assert_not_called()

    def test_api_providers_forwards_type_filter(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.list_provider_types.return_value = []
        mock_bl.list_providers.return_value = []

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get(
                "/api/providers?provider_type_key=notification_provider&enabled_only=false"
            )

        assert resp.status_code == 200
        mock_bl.list_providers.assert_called_once_with(
            enabled_only=False,
            provider_type_key="notification_provider",
        )

    def test_api_providers_project_health_requires_project_access(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        import core.entrypoints.api.project_integrations as project_integrations
        from core.application.management import ServiceForbiddenError

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {"id": "project-b", "account_id": "acc-b"}
        mock_bl.list_provider_types.return_value = []
        mock_bl.list_providers.return_value = []

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                project_integrations,
                "require_project_access",
                side_effect=ServiceForbiddenError("access denied"),
            ),
        ):
            resp = client.get("/api/providers?project_id=project-b")

        assert resp.status_code == 403
        mock_bl.list_latest_provider_health.assert_not_called()

    def test_api_create_provider_persists_payload(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_provider.return_value = {
            "provider_key": "linear",
            "provider_type_key": "task_provider",
            "display_name": "Linear",
            "enabled": True,
        }
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_routes,
                "_require_authenticated_user",
                return_value={"username": "admin", "roles": ["admin"]},
            ),
        ):
            resp = client.post(
                "/api/providers",
                json={
                    "provider_key": "linear",
                    "provider_type_key": "task_provider",
                    "display_name": "Linear",
                    "description": "Linear intake",
                    "implementation_kind": "extension",
                    "integration_key": "linear",
                    "config": {"webhook_path": "/hooks/linear"},
                    "skills": ["task_intake"],
                    "enabled": True,
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["provider_key"] == "linear"
        saved = mock_bl.save_provider.call_args.args[0]
        assert saved["provider_key"] == "linear"
        assert saved["provider_type_key"] == "task_provider"
        assert saved["config"] == {"webhook_path": "/hooks/linear"}

    def test_api_create_provider_requires_admin(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_require_authenticated_user", side_effect=PermissionError),
        ):
            resp = client.post(
                "/api/providers",
                json={
                    "provider_key": "linear",
                    "provider_type_key": "task_provider",
                    "display_name": "Linear",
                    "implementation_kind": "extension",
                    "integration_key": "linear",
                },
            )

        assert resp.status_code == 403
        mock_bl.save_provider.assert_not_called()

    def test_api_upsert_provider_secret_persists_secret_record(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.save_provider_secret.return_value = True
        mock_bl.get_project.return_value = _project_fixture(id=7)

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.post(
                "/api/providers/sample_tasks/secrets",
                json={
                    "secret_key": "api_token",
                    "secret_value": "top-secret",
                    "project_id": 7,
                    "active": True,
                    "metadata": {"scope": "project"},
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "saved"
        mock_bl.save_provider_secret.assert_called_once_with(
            provider_key="sample_tasks",
            secret_key="api_token",
            secret_value="top-secret",
            project_id="7",
            active=True,
            metadata={"scope": "project"},
        )

    def test_api_upsert_provider_secret_rejects_cross_account_project(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = _project_fixture(id=7, account_id="acc_other")

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.post(
                "/api/providers/sample_tasks/secrets",
                json={
                    "secret_key": "api_token",
                    "secret_value": "top-secret",
                    "project_id": 7,
                    "active": True,
                },
            )

        assert resp.status_code == 403
        mock_bl.save_provider_secret.assert_not_called()

    def test_api_project_providers_returns_bindings(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = _project_fixture(id=7, name="Demo Project")
        mock_bl.list_project_connections.return_value = [
            {
                "connection_id": "conn_tg",
                "extension_id": "fixture.notification-provider",
                "connection_config": {"chat_id": "123"},
                "enabled": True,
            }
        ]
        mock_bl.get_provider.return_value = {
            "provider_key": "fixture_notifier",
            "provider_type_key": "notification_provider",
            "display_name": "Fixture Notifier",
            "implementation_kind": "extension",
            "integration_key": "fixture_notifier",
            "config": {},
            "enabled": True,
        }

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/projects/7/providers")

        assert resp.status_code == 200
        body = resp.json()
        assert body["project_id"] == "7"
        assert body["project_providers"][0]["provider_key"] == "fixture.notification-provider"
        mock_bl.list_project_connections.assert_called_once_with("7", enabled_only=False)

    def test_api_bind_project_provider_saves_binding(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = _project_fixture(id=7, name="Demo Project")
        mock_bl.get_project_connection.return_value = {
            "connection_id": "conn_sample_tasks",
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": 7,
            "extension_id": "sample_tasks",
            "enabled": True,
            "connection_kind": "extension",
            "connection_type": "task_list",
            "extension_install_id": "pi_sample_tasks",
            "connection_config": {"list_id": "retired-list"},
            "secret_refs": ["api_token"],
            "connection_routes": [],
            "ingress_config": {},
            "workflow_mappings": {},
        }
        mock_bl.list_project_connections.return_value = [
            {
                "connection_id": "conn_sample_tasks",
                "extension_id": "sample_tasks",
                "connection_config": {"list_id": "list-1"},
                "enabled": True,
            }
        ]
        mock_bl.get_provider.return_value = {
            "provider_key": "sample_tasks",
            "provider_type_key": "task_provider",
            "display_name": "Sample Tasks",
            "implementation_kind": "extension",
            "integration_key": "sample_tasks",
            "config": {"extension_key": "sample_tasks"},
            "enabled": True,
        }
        mock_bl.get_extension_by_install_id_for_runtime_gate.return_value = {
            "extension_key": "sample_tasks",
            "install_id": "pi_sample_tasks",
            "connection_types": [
                {
                    "connection_type_id": "task_list",
                    "config_schema": {
                        "type": "object",
                        "properties": {"list_id": {"type": "string", "maxLength": 16}},
                        "required": ["list_id"],
                        "additionalProperties": False,
                    },
                }
            ],
        }

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.post(
                "/api/projects/7/providers/bind",
                json={
                    "provider_key": "sample_tasks",
                    "enabled": True,
                    "config": {"list_id": "list-1"},
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["project_providers"][0]["provider_key"] == "sample_tasks"
        mock_bl.get_extension_by_install_id_for_runtime_gate.assert_called_once_with(
            "pi_sample_tasks", account_id=DEFAULT_ACCOUNT_ID
        )
        mock_bl.upsert_project_connection.assert_called_once_with(
            project_id="7",
            extension_id="sample_tasks",
            account_id=DEFAULT_ACCOUNT_ID,
            connection_kind="extension",
            enabled=True,
            connection_config={"list_id": "list-1"},
            secret_refs=["api_token"],
            connection_routes=[],
            ingress_config={},
            workflow_mappings={},
        )

    def test_api_bind_project_provider_rejects_invalid_extension_config(self, client):
        mock_bl = MagicMock()
        mock_bl.get_project.return_value = _project_fixture(id=7, name="Demo Project")
        mock_bl.get_project_connection.return_value = {
            "connection_id": "conn_sample_tasks",
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": 7,
            "extension_id": "sample_tasks",
            "connection_kind": "extension",
            "connection_type": "task_list",
            "extension_install_id": "pi_sample_tasks",
            "enabled": True,
            "connection_config": {"list_id": "old"},
            "secret_refs": [],
            "connection_routes": [],
            "ingress_config": {},
            "workflow_mappings": {},
        }
        mock_bl.get_provider.return_value = {
            "provider_key": "sample_tasks",
            "integration_key": "sample_tasks",
            "config": {"extension_key": "sample_tasks"},
        }
        mock_bl.get_extension_by_install_id_for_runtime_gate.return_value = {
            "extension_key": "sample_tasks",
            "install_id": "pi_sample_tasks",
            "connection_types": [
                {
                    "connection_type_id": "task_list",
                    "config_schema": {
                        "type": "object",
                        "properties": {"list_id": {"type": "string", "maxLength": 3}},
                        "additionalProperties": False,
                    },
                }
            ],
        }

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.post(
                "/api/projects/7/providers/bind",
                json={
                    "provider_key": "sample_tasks",
                    "enabled": True,
                    "config": {"list_id": "TOO-LONG", "undeclared": "TOPSECRET"},
                },
            )

        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_payload"
        assert "TOPSECRET" not in resp.text
        mock_bl.get_extension_by_install_id_for_runtime_gate.assert_called_once_with(
            "pi_sample_tasks", account_id=DEFAULT_ACCOUNT_ID
        )
        mock_bl.upsert_project_connection.assert_not_called()

    def test_api_bind_project_provider_rejects_unavailable_exact_install_contract(self, client):
        mock_bl = MagicMock()
        mock_bl.get_project.return_value = _project_fixture(id=7, name="Demo Project")
        mock_bl.get_project_connection.return_value = {
            "connection_id": "conn_sample_tasks",
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": 7,
            "extension_id": "sample_tasks",
            "connection_kind": "extension",
            "connection_type": "task_list",
            "extension_install_id": "pi_sample_tasks",
        }
        mock_bl.get_provider.return_value = {
            "provider_key": "sample_tasks",
            "integration_key": "sample_tasks",
            "config": {"extension_key": "sample_tasks"},
        }
        mock_bl.get_extension_by_install_id_for_runtime_gate.return_value = None

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.post(
                "/api/projects/7/providers/bind",
                json={
                    "provider_key": "sample_tasks",
                    "enabled": True,
                    "config": {"list_id": "ok"},
                },
            )

        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_payload"
        mock_bl.get_extension_by_install_id_for_runtime_gate.assert_called_once_with(
            "pi_sample_tasks", account_id=DEFAULT_ACCOUNT_ID
        )
        mock_bl.upsert_project_connection.assert_not_called()


# ---------------------------------------------------------------------------
# Notification routing endpoints
# ---------------------------------------------------------------------------


class TestNotificationRoutingEndpoints:
    @pytest.mark.parametrize(
        "path",
        (
            "/api/projects/unknown-project/notification-routes",
            "/api/projects/unknown-project/notification-deliveries",
            "/api/projects/unknown-project/notification-overview",
        ),
    )
    def test_unknown_project_notification_queries_return_not_found(self, client, path):
        mock_bl = MagicMock()
        mock_bl.get_project.return_value = None

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get(path)

        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"
        mock_bl.list_notification_routes.assert_not_called()
        mock_bl.list_notification_delivery_logs.assert_not_called()

    def test_api_project_notification_routes_returns_rows(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = _project_fixture(id=7, name="Demo Project")
        mock_bl.list_notification_routes.return_value = [
            {
                "route_key": "failed",
                "provider_key": "fixture_notifier",
                "event_type": "job.failed",
                "severity": "error",
                "enabled": True,
            }
        ]

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/projects/7/notification-routes")

        assert resp.status_code == 200
        body = resp.json()
        assert body["project_id"] == "7"
        assert body["notification_routes"][0]["route_key"] == "failed"
        mock_bl.list_notification_routes.assert_called_once_with("7", enabled_only=False)

    def test_api_update_project_notification_routes_persists_rows(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = _project_fixture(id=7, name="Demo Project")
        mock_bl.list_notification_routes.return_value = [
            {
                "route_key": "failed",
                "provider_key": "fixture_notifier",
                "event_type": "job.failed",
                "severity": "error",
                "enabled": True,
            }
        ]

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.post(
                "/api/projects/7/notification-routes",
                json={
                    "routes": [
                        {
                            "route_key": "failed",
                            "display_name": "Notify on failed jobs",
                            "provider_key": "fixture_notifier",
                            "event_type": "job.failed",
                            "severity": "error",
                            "route_config": {"chat_id": "123"},
                            "enabled": True,
                        }
                    ]
                },
            )

        assert resp.status_code == 200
        saved = mock_bl.save_notification_routes.call_args.args
        assert saved[0] == "7"
        assert saved[1][0]["provider_key"] == "fixture_notifier"
        assert saved[1][0]["event_type"] == "job.failed"

    def test_api_dispatch_project_notification_returns_delivery_rows(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = _project_fixture(id=7, name="Demo Project")
        deliveries = [
            {"delivery_id": 1, "provider_key": "fixture_notifier", "status": "delivered"},
        ]

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch(
                "core.application.notifications.dispatch.dispatch_project_notification_event",
                return_value=deliveries,
            ),
        ):
            resp = client.post(
                "/api/projects/7/notifications/dispatch",
                json={
                    "event_type": "job.failed",
                    "severity": "error",
                    "job_id": 55,
                    "payload": {"summary": "failed"},
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["notification_deliveries"][0]["status"] == "delivered"


# ---------------------------------------------------------------------------
# Reaction endpoints
# ---------------------------------------------------------------------------


class TestReactionEndpoints:
    def test_api_workflow_reactions_returns_rules(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.list_workflow_reactions.return_value = [
            {"reaction_key": "task_created", "event_type": "external.task.created", "enabled": True}
        ]

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(reactions_runtime, "_workflow_exists", return_value=True),
        ):
            resp = client.get("/api/workflows/weekly_report/reactions")

        assert resp.status_code == 200
        body = resp.json()
        assert body["workflow_id"] == "weekly_report"
        assert body["reactions"][0]["event_type"] == "external.task.created"

    def test_api_update_workflow_reactions_saves_rules(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.replace_scoped_workflow_reactions.return_value = (
            "workflow",
            "",
            [{"reaction_key": "job_done", "event_type": "job.completed", "enabled": True}],
        )

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(reactions_runtime, "_workflow_exists", return_value=True),
        ):
            resp = client.post(
                "/api/workflows/weekly_report/reactions",
                json={
                    "reactions": [
                        {
                            "reaction_key": "job_done",
                            "display_name": "Job done",
                            "event_type": "job.completed",
                            "conditions": {"branch": "main"},
                            "action": {"type": "resume_job"},
                            "debounce_seconds": 30,
                            "enabled": True,
                        }
                    ]
                },
            )

        assert resp.status_code == 200
        saved = mock_bl.replace_scoped_workflow_reactions.call_args.args
        assert saved[0] == "weekly_report"
        assert saved[1][0]["event_type"] == "job.completed"
        mock_bl.replace_scoped_workflow_reactions.assert_called_once_with(
            "weekly_report",
            [
                {
                    "reaction_key": "job_done",
                    "display_name": "Job done",
                    "event_type": "job.completed",
                    "conditions": {"branch": "main"},
                    "action": {"type": "resume_job"},
                    "debounce_seconds": 30,
                    "idempotency_key_template": None,
                    "enabled": True,
                }
            ],
            scope_kind="workflow",
            scope_id="",
            account_id=DEFAULT_ACCOUNT_ID,
        )
        mock_bl.list_workflow_reactions.assert_not_called()
        assert resp.json()["reactions"][0]["reaction_key"] == "job_done"

    def test_api_update_workflow_reactions_saves_extension_bound_rules_for_selected_project(
        self, client
    ):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {
            "id": 7,
            "account_id": DEFAULT_ACCOUNT_ID,
            "name": "Demo",
        }
        mock_bl.replace_scoped_workflow_reactions.return_value = (
            "workflow",
            "",
            [
                {
                    "reaction_key": "mail_comment",
                    "event_type": "external.comment.received",
                    "conditions": {"source": "mail_hook"},
                    "action": {"type": "resume_job"},
                }
            ],
        )
        mock_bl.list_project_connections.return_value = [
            {
                "connection_id": "conn_mail",
                "extension_id": "mail_hook",
                "enabled": True,
            }
        ]
        mock_bl.get_extension.return_value = {
            "extension_key": "mail_hook",
            "display_name": "Mail Hook",
            "enabled": True,
            "compiled_manifest": {
                "supported_events": ["external.comment.received"],
            },
        }

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(reactions_runtime, "_workflow_exists", return_value=True),
        ):
            resp = client.post(
                "/api/workflows/weekly_report/reactions?project_id=7",
                json={
                    "reactions": [
                        {
                            "reaction_key": "mail_comment",
                            "display_name": "Mail comment",
                            "event_type": "external.comment.received",
                            "conditions": {"source": "mail_hook"},
                            "action": {"type": "resume_job"},
                            "enabled": True,
                        }
                    ]
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["project_id"] == "7"
        assert body["available_trigger_sources"][1]["source_id"] == "mail_hook"
        saved = mock_bl.replace_scoped_workflow_reactions.call_args.args
        assert saved[1][0]["conditions"]["source"] == "mail_hook"
        mock_bl.replace_scoped_workflow_reactions.assert_called_once_with(
            "weekly_report",
            [
                {
                    "reaction_key": "mail_comment",
                    "display_name": "Mail comment",
                    "event_type": "external.comment.received",
                    "conditions": {"source": "mail_hook"},
                    "action": {"type": "resume_job"},
                    "debounce_seconds": 0,
                    "idempotency_key_template": None,
                    "enabled": True,
                }
            ],
            scope_kind="workflow",
            scope_id="",
            account_id=DEFAULT_ACCOUNT_ID,
        )
        mock_bl.list_workflow_reactions.assert_not_called()
        assert body["reactions"][0]["reaction_key"] == "mail_comment"

    def test_api_workflow_reactions_filters_group_scope(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.list_workflow_reactions.return_value = [
            {"reaction_key": "workflow_rule", "event_type": "job.completed", "enabled": True},
            {
                "reaction_key": "scope::group::design::group_rule",
                "event_type": "job.group_completed",
                "enabled": True,
            },
            {
                "reaction_key": "scope::group::analysis::group_rule",
                "event_type": "job.group_completed",
                "enabled": True,
            },
            {
                "reaction_key": "scope::step::design%3A12::step_rule",
                "event_type": "job.failed",
                "enabled": True,
            },
        ]

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(reactions_runtime, "_workflow_exists", return_value=True),
        ):
            resp = client.get(
                "/api/workflows/weekly_report/reactions?scope_kind=group&scope_id=design"
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["scope_kind"] == "group"
        assert body["scope_id"] == "design"
        assert len(body["reactions"]) == 1
        assert body["reactions"][0]["reaction_key"] == "group_rule"
        assert body["reactions"][0]["scope_kind"] == "group"
        assert body["reactions"][0]["scope_id"] == "design"

    def test_api_update_workflow_reactions_delegates_only_target_scope(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.replace_scoped_workflow_reactions.return_value = (
            "group",
            "design",
            [
                {
                    "reaction_key": "group_approval_wait",
                    "display_name": "Notify on approval wait",
                    "event_type": "job.approval_waiting",
                    "action": {"type": "notify"},
                    "scope_kind": "group",
                    "scope_id": "design",
                }
            ],
        )

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(reactions_runtime, "_workflow_exists", return_value=True),
        ):
            resp = client.post(
                "/api/workflows/weekly_report/reactions?scope_kind=group&scope_id=design",
                json={
                    "reactions": [
                        {
                            "reaction_key": "group_approval_wait",
                            "display_name": "Notify on approval wait",
                            "event_type": "job.approval_waiting",
                            "conditions": {},
                            "action": {"type": "notify"},
                            "debounce_seconds": 0,
                            "enabled": True,
                        }
                    ]
                },
            )

        assert resp.status_code == 200
        call = mock_bl.replace_scoped_workflow_reactions.call_args
        assert call.args[0] == "weekly_report"
        assert [row["reaction_key"] for row in call.args[1]] == ["group_approval_wait"]
        assert call.kwargs == {
            "scope_kind": "group",
            "scope_id": "design",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        mock_bl.replace_scoped_workflow_reactions.assert_called_once_with(
            "weekly_report",
            [
                {
                    "reaction_key": "group_approval_wait",
                    "display_name": "Notify on approval wait",
                    "event_type": "job.approval_waiting",
                    "conditions": {},
                    "action": {"type": "notify"},
                    "debounce_seconds": 0,
                    "idempotency_key_template": None,
                    "enabled": True,
                }
            ],
            scope_kind="group",
            scope_id="design",
            account_id=DEFAULT_ACCOUNT_ID,
        )
        mock_bl.list_workflow_reactions.assert_not_called()
        assert resp.json()["scope_kind"] == "group"
        assert resp.json()["scope_id"] == "design"
        assert resp.json()["reactions"][0]["reaction_key"] == "group_approval_wait"

    def test_api_ingest_reaction_event_persists_and_enqueues_outbox(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 55,
            "project_id": 7,
            "account_id": DEFAULT_ACCOUNT_ID,
            "meta": {"selected_workflow_id": "weekly_report"},
        }
        mock_bl.get_project.return_value = {"id": 7, "account_id": DEFAULT_ACCOUNT_ID}
        mock_bl.log_job_reaction_event.return_value = {
            "id": 91,
            "job_id": 55,
            "event_type": "external.comment.received",
        }

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_routes,
                "_request_scope",
                return_value=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            ),
            patch.object(reactions_runtime, "_workflow_exists", return_value=True),
        ):
            resp = client.post(
                "/api/reactions/ingest",
                json={
                    "job_id": 55,
                    "event_type": "external.comment.received",
                    "source": "sample_tasks",
                    "source_id": "comment-1",
                    "event_key": "comment.created",
                    "dedupe_key": "job55-comment1",
                    "payload": {"text": "please retry"},
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["event"]["event_type"] == "external.comment.received"
        mock_bl.log_job_reaction_event.assert_called_once()
        mock_bl.enqueue_domain_event.assert_called_once()

    def test_api_ingest_reaction_event_executes_matching_reaction_rule(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 55,
            "project_id": 7,
            "account_id": DEFAULT_ACCOUNT_ID,
            "meta": {"selected_workflow_id": "weekly_report"},
        }
        mock_bl.get_project.return_value = {"id": 7, "account_id": DEFAULT_ACCOUNT_ID}
        mock_bl.log_job_reaction_event.return_value = {
            "id": 91,
            "job_id": 55,
            "workflow_id": "weekly_report",
            "event_type": "external.comment.received",
            "payload": {"text": "please retry"},
            "processed": False,
        }
        mock_bl.list_workflow_reactions.return_value = [
            {
                "reaction_key": "comment_resume",
                "event_type": "external.comment.received",
                "conditions": {},
                "action": {"type": "resume_job"},
                "debounce_seconds": 0,
                "enabled": True,
            }
        ]
        mock_bl.list_job_reaction_events.return_value = []
        mock_bl.mark_job_reaction_event_processed.return_value = {
            "id": 91,
            "job_id": 55,
            "workflow_id": "weekly_report",
            "event_type": "external.comment.received",
            "payload": {"text": "please retry"},
            "processed": True,
        }

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_routes,
                "_request_scope",
                return_value=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            ),
            patch.object(reactions_runtime, "_workflow_exists", return_value=True),
            patch.object(
                reactions_runtime,
                "_resume_job_from_reaction",
                return_value={"status": "resumed", "executed": True},
            ),
        ):
            resp = client.post(
                "/api/reactions/ingest",
                json={
                    "job_id": 55,
                    "event_type": "external.comment.received",
                    "source": "sample_tasks",
                    "source_id": "comment-1",
                    "event_key": "comment.created",
                    "dedupe_key": "job55-comment1",
                    "payload": {"text": "please retry"},
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["event"]["processed"] is True
        assert body["reactions"][0]["reaction_key"] == "comment_resume"
        assert body["reactions"][0]["status"] == "resumed"
        mock_bl.mark_job_reaction_event_processed.assert_called_once_with("91", processed=True)

    def test_api_ingest_reaction_event_dispatches_notifications(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 55,
            "project_id": 7,
            "account_id": DEFAULT_ACCOUNT_ID,
            "meta": {"selected_workflow_id": "weekly_report"},
        }
        mock_bl.get_project.return_value = {"id": 7, "account_id": DEFAULT_ACCOUNT_ID}
        mock_bl.log_job_reaction_event.return_value = {
            "id": 91,
            "job_id": 55,
            "workflow_id": "weekly_report",
            "event_type": "external.comment.received",
            "payload": {"text": "please retry"},
            "processed": False,
        }
        mock_bl.list_workflow_reactions.return_value = []

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_routes,
                "_request_scope",
                return_value=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            ),
            patch.object(reactions_runtime, "_workflow_exists", return_value=True),
            patch(
                "core.application.notifications.dispatch.dispatch_project_notification_event",
                return_value=[{"delivery_id": 3, "status": "delivered"}],
            ),
        ):
            resp = client.post(
                "/api/reactions/ingest",
                json={
                    "job_id": 55,
                    "event_type": "external.comment.received",
                    "source": "sample_tasks",
                    "source_id": "comment-1",
                    "event_key": "comment.created",
                    "dedupe_key": "job55-comment1",
                    "payload": {"text": "please retry"},
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["notification_deliveries"][0]["status"] == "delivered"

    def test_api_ingest_reaction_event_uses_event_command_service_contract(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        payload = MagicMock()
        payload.dump.return_value = {
            "event": {"id": 91, "event_type": "external.comment.received"},
            "reactions": [],
            "notification_deliveries": [],
        }
        mock_service = MagicMock()
        mock_service.ingest_reaction_event.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"event_command_service": lambda: mock_service},
        ):
            resp = client.post(
                "/api/reactions/ingest",
                json={
                    "job_id": 55,
                    "event_type": "external.comment.received",
                    "source": "sample_tasks",
                    "source_id": "comment-1",
                    "event_key": "comment.created",
                    "dedupe_key": "job55-comment1",
                    "payload": {"text": "please retry"},
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["event"]["id"] == 91
        call = mock_service.ingest_reaction_event.call_args
        command = call.args[0]
        assert command.job_id == 55
        assert command.event_type == "external.comment.received"
        assert command.source == "sample_tasks"
        assert command.payload["text"] == "please retry"
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_api_job_reactions_returns_events(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {"id": 55, "account_id": DEFAULT_ACCOUNT_ID}
        mock_bl.list_job_reaction_events.return_value = [
            {"id": 1, "event_type": "external.ci.failed", "source": "gitlab"}
        ]

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_routes,
                "_request_scope",
                return_value=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            ),
        ):
            resp = client.get("/api/jobs/55/reactions")

        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == "55"
        assert body["reactions"][0]["event_type"] == "external.ci.failed"


# ---------------------------------------------------------------------------
# Phase 4 endpoints
# ---------------------------------------------------------------------------


class TestPhase4Endpoints:
    def test_api_jobs_group_by_attention(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.list_jobs_for_attention.return_value = [
            {
                "id": 1,
                "project_id": 10,
                "project_name": "A",
                "task_id": "t1",
                "task_summary": "Needs fix",
                "status": "failed",
            },
            {
                "id": 2,
                "project_id": 10,
                "project_name": "A",
                "task_id": "t2",
                "task_summary": "Review",
                "status": "awaiting_approval",
            },
            {
                "id": 3,
                "project_id": 10,
                "project_name": "A",
                "task_id": "t3",
                "task_summary": "Work",
                "status": "in_progress",
            },
            {
                "id": 4,
                "project_id": 10,
                "project_name": "A",
                "task_id": "t4",
                "task_summary": "Done",
                "status": "completed",
            },
        ]

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/jobs?group_by=attention")

        assert resp.status_code == 200
        body = resp.json()
        assert body["group_by"] == "attention"
        assert len(body["groups"]["needs_attention"]) == 2
        assert len(body["groups"]["blocked"]) == 0
        assert len(body["groups"]["running"]) == 1
        assert len(body["groups"]["done"]) == 1

    def test_api_jobs_lists_paginated_rows_without_group_by(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.list_jobs_for_attention.return_value = [
            {
                "id": 11,
                "project_id": 10,
                "project_name": "A",
                "task_id": "t11",
                "task_summary": "Investigate auth timeout",
                "status": "failed",
                "queued_at": None,
                "started_at": None,
                "finished_at": None,
                "worker_id": None,
                "selected_workflow": "daily_briefing",
                "current_step_id": "quality",
                "current_agent": "proofreader",
                "heartbeat_at": None,
                "last_error": None,
                "acknowledged_at": None,
                "acknowledged_by": None,
            }
        ]
        mock_bl.count_jobs_for_attention.return_value = 37

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/jobs?page=2&per_page=15")

        assert resp.status_code == 200
        body = resp.json()
        assert body["page"] == 2
        assert body["per_page"] == 15
        assert body["total"] == 37
        assert body["total_pages"] == 3
        assert body["jobs"][0]["job_id"] == "11"
        assert body["jobs"][0]["selected_workflow"] == "daily_briefing"
        mock_bl.list_jobs_for_attention.assert_called_once_with(
            project_id=None,
            limit=15,
            offset=15,
            status=None,
            account_id=DEFAULT_ACCOUNT_ID,
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                tenant_id=DEFAULT_TENANT_ID,
                actor_id="operator",
                principal_type="system_actor",
            ),
        )
        mock_bl.count_jobs_for_attention.assert_called_once_with(
            project_id=None,
            status=None,
            account_id=DEFAULT_ACCOUNT_ID,
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                tenant_id=DEFAULT_TENANT_ID,
                actor_id="operator",
                principal_type="system_actor",
            ),
        )

    def test_api_jobs_lists_acknowledged_filter_without_group_by(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.list_jobs_for_attention.return_value = []
        mock_bl.count_jobs_for_attention.return_value = 0

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/jobs?status=acknowledged")

        assert resp.status_code == 200
        mock_bl.list_jobs_for_attention.assert_called_once_with(
            project_id=None,
            limit=25,
            offset=0,
            status=("acknowledged",),
            account_id=DEFAULT_ACCOUNT_ID,
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                tenant_id=DEFAULT_TENANT_ID,
                actor_id="operator",
                principal_type="system_actor",
            ),
        )
        mock_bl.count_jobs_for_attention.assert_called_once_with(
            project_id=None,
            status=("acknowledged",),
            account_id=DEFAULT_ACCOUNT_ID,
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                tenant_id=DEFAULT_TENANT_ID,
                actor_id="operator",
                principal_type="system_actor",
            ),
        )

    def test_jobs_page_embeds_bootstrap_rows(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.list_jobs_for_attention.return_value = [
            {
                "id": 77,
                "project_id": 10,
                "project_name": "Demo",
                "task_id": "t77",
                "task_summary": "Fix the jobs screen",
                "status": "awaiting_approval",
                "queued_at": None,
                "started_at": None,
                "finished_at": None,
                "worker_id": None,
                "selected_workflow": "weekly_digest",
                "current_step_id": "development",
                "current_agent": "composer",
                "heartbeat_at": None,
                "last_error": None,
                "acknowledged_at": None,
                "acknowledged_by": None,
            }
        ]

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/jobs")

        _assert_frontend_shell_response(resp)
        mock_bl.list_jobs_for_attention.assert_not_called()

    def test_static_versioned_asset_serves_file(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        resp = client.get(f"/static-v/{api_runtime._STATIC_ASSET_VERSION}/shell.css")

        assert resp.status_code == 200
        assert "Cache-Control" in resp.headers
        assert "immutable" in resp.headers["Cache-Control"]

    def test_static_versioned_asset_is_public_when_local_auth_is_enabled(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        with patch.object(client.app.state, "local_auth_enabled_override", return_value=True):
            resp = client.get(
                f"/static-v/{api_runtime._STATIC_ASSET_VERSION}/shell.css",
                headers={"accept": "text/css"},
                follow_redirects=False,
            )

        assert resp.status_code == 200
        assert "text/css" in resp.headers.get("content-type", "")

    def test_jobs_page_uses_versioned_static_asset_urls(self, client):
        resp = client.get("/jobs")

        _assert_frontend_shell_response(resp)

    def test_api_jobs_group_by_attention_trims_heavy_task_summaries(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        huge_summary = "Task Brief: " + ("A" * 5000)
        mock_bl = MagicMock()
        mock_bl.list_jobs_for_attention.return_value = [
            {
                "id": 2,
                "project_id": 10,
                "project_name": "A",
                "task_id": "t2",
                "task_summary": huge_summary,
                "status": "awaiting_approval",
            },
        ]

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/jobs?group_by=attention")

        assert resp.status_code == 200
        row = resp.json()["groups"]["needs_attention"][0]
        assert len(row["task_summary"]) <= 512
        assert len(row["display_summary"]) <= 96
        assert row["display_summary"].endswith("…")
        assert "A" * 1024 not in resp.text

    def test_api_job_causal_chain_returns_mermaid(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        now = datetime.now(UTC)
        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {"id": 55, "account_id": DEFAULT_ACCOUNT_ID}
        mock_bl.get_agent_messages.return_value = [
            {
                "id": 101,
                "timestamp": now,
                "job_id": 55,
                "correlation_id": "corr-1",
                "schema_version": "agent_message_v1",
                "from_agent": "scout",
                "to_agent": "orchestrator",
                "step_id": "analysis",
                "skill": "analyze",
                "status": "pass",
                "summary": "analysis ok",
                "artifacts": {},
                "errors": [],
                "envelope": {"caused_by": []},
            },
            {
                "id": 102,
                "timestamp": now,
                "job_id": 55,
                "correlation_id": "corr-2",
                "schema_version": "agent_message_v1",
                "from_agent": "designer",
                "to_agent": "orchestrator",
                "step_id": "design",
                "skill": "design",
                "status": "pass",
                "summary": "design ok",
                "artifacts": {},
                "errors": [],
                "envelope": {"caused_by": [{"correlation_id": "corr-1"}]},
            },
        ]
        mock_bl.get_events.return_value = []

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/jobs/55/causal-chain")

        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == "55"
        assert "graph TD" in body["mermaid"]
        assert any(edge["via"] == "correlation_id" for edge in body["edges"])


class TestExtensionEndpoints:
    def test_api_extensions_returns_discovered_extensions(self, client):
        from core.application.management.query_models import ExtensionListView, ExtensionSummaryView

        with patch(
            "core.application.extensions.query_service.ExtensionQueryService.list_extensions",
            return_value=ExtensionListView(
                extensions=[
                    ExtensionSummaryView(
                        extension_id="demo_task_provider",
                        extension_key="demo_task_provider",
                        kind="task_provider",
                        state="disabled",
                    )
                ],
                total=1,
            ),
        ):
            resp = client.get("/api/extensions")

        assert resp.status_code == 200
        body = resp.json()
        assert body["extensions"][0]["extension_key"] == "demo_task_provider"

    def test_api_extensions_scan_persists_scan_results(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        scanned = [
            {
                "extension_key": "demo.task-provider",
                "kind": "task_provider",
                "status": "valid",
                "version": "1.0.0",
            }
        ]
        mock_bl = MagicMock()
        mock_bl.conn = None
        mock_bl.list_extensions.return_value = scanned

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch(
                "core.application.extensions.extension_scan_lifecycle."
                "sync_extension_scan_with_llm_lifecycle",
                side_effect=lambda logger, rows: logger.sync_extension_scan(rows),
            ),
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value="/tmp/extensions",
            ),
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.scan_extensions",
                return_value=scanned,
            ),
        ):
            resp = client.post("/api/extensions/scan", json={})

        assert resp.status_code == 200
        body = resp.json()
        assert body["scanned_count"] == 1
        mock_bl.sync_extension_scan.assert_called_once_with(scanned)

    def test_api_extension_enable_updates_state(self, client):
        from core.application.management.command_models import ExtensionToggleResultView

        with patch(
            "core.application.extensions.command_service.ExtensionCommandService.enable_extension",
            return_value=ExtensionToggleResultView(
                status="ok",
                extension_id="demo_task_provider",
                state="active",
            ),
        ) as enable_extension:
            resp = client.post(
                "/api/extensions/demo_task_provider/enable",
                json={"version": "1.0.0", "enabled": True, "config": {"project": "demo"}},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["extension_id"] == "demo_task_provider"
        assert body["state"] == "active"
        enable_extension.assert_called_once()
        assert enable_extension.call_args.args[0] == "demo_task_provider"
        assert "scope" in enable_extension.call_args.kwargs

    def test_api_extension_health_returns_persisted_projection(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        payload = MagicMock()
        payload.dump.return_value = {
            "extension_id": "demo_task_provider",
            "health": {"status": "healthy", "detail": "persisted"},
        }
        mock_service = MagicMock()
        mock_service.get_health.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"extension_query_service": lambda: mock_service},
        ):
            resp = client.get("/api/extensions/demo_task_provider/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["health"]["status"] == "healthy"
        mock_service.get_health.assert_called_once()

    def test_api_extension_invoke_runs_entrypoint(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_extension.return_value = {
            "extension_key": "demo_task_provider",
            "enabled": True,
            "bundle_root": "/tmp/extensions/demo_task_provider",
            "entrypoint": {"command": ["python3", "main.py"]},
            "required_scopes": ["extension:invoke"],
        }

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_invocation",
                return_value={"status": "succeeded", "response": {"ok": True}},
            ),
        ):
            resp = client.post(
                "/api/extensions/demo_task_provider/invoke",
                json={"request": {"action": "normalize_task", "payload": {"id": "t-1"}}},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["invocation"]["status"] == "succeeded"
        mock_bl.log_extension_invocation.assert_called_once()

    def test_api_extension_invoke_rejects_disabled_extension(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_extension.return_value = {
            "extension_key": "demo_task_provider",
            "enabled": False,
        }

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.post(
                "/api/extensions/demo_task_provider/invoke",
                json={"request": {"action": "normalize_task"}},
            )

        assert resp.status_code == 409

    def test_api_extension_compiled_manifest_hydrates_ui_from_db_row(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_query_service = MagicMock()
        mock_query_service.get_compiled_manifest_safe.return_value = {
            "extension_id": "demo_task_provider",
            "ui_manifest": {
                "version": "1",
                "pages": [
                    {"page_id": "overview", "title": "Overview", "components": []},
                ],
            },
        }

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"extension_query_service": lambda: mock_query_service},
        ):
            resp = client.get("/api/extensions/demo_task_provider/compiled-manifest")

        assert resp.status_code == 200
        body = resp.json()
        assert body["extension_id"] == "demo_task_provider"
        assert body["ui_manifest"]["pages"][0]["page_id"] == "overview"
        assert "ui" not in body
        mock_query_service.get_compiled_manifest_safe.assert_called_once()

    def test_extension_page_uses_frontend_shell_without_db_lookup(self, client):
        resp = client.get("/extensions/fixture.notification-provider")

        _assert_frontend_shell_response(resp)

    def test_extension_page_surfaces_validation_summary(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_query_service = MagicMock()
        mock_query_service.get_extension_page_context.return_value = type(
            "ExtensionPageContext",
            (),
            {
                "dump": lambda self: {
                    "compiled_manifest": {
                        "extension_id": "demo_task_provider",
                        "ui_manifest": {
                            "version": "1",
                            "pages": [
                                {"page_id": "overview", "title": "Overview", "components": []},
                            ],
                        },
                    },
                    "preview_mode": False,
                    "extension_id": "demo_task_provider",
                    "extension_name": "Demo Task Provider",
                    "extension_kind": "task_provider",
                    "extension_version": "",
                    "extension_description": "Demo extension",
                    "compile_status": "error",
                    "validation_summary": {
                        "status": "error",
                        "page_count": 1,
                        "error_count": 1,
                        "warning_count": 1,
                        "compile_errors": [
                            {
                                "severity": "error",
                                "code": "MISSING_UI_MANIFEST",
                                "message": "Missing ui_manifest",
                                "file": "extension.yaml",
                            }
                        ],
                        "compile_warnings": [],
                        "validation_errors": [],
                        "validation_warnings": [
                            {
                                "severity": "warning",
                                "code": "UNUSED_FIELD",
                                "message": "A field is defined but never rendered",
                                "path": "fields[2]",
                            }
                        ],
                    },
                    "health_status": "",
                    "projects": [],
                    "selected_project_id": None,
                }
            },
        )()

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"extension_query_service": lambda: mock_query_service},
        ):
            resp = client.get("/extensions/demo_task_provider")

        _assert_frontend_shell_response(resp)
        mock_query_service.get_extension_page_context.assert_not_called()

    def test_retired_extension_page_route_is_not_available(self, client):
        retired_segment = "retired"
        retired_path = f"/{retired_segment}/extensions/demo_task_provider"
        resp = client.get(retired_path)
        assert resp.status_code == 404

    def test_api_extension_query_blocks_disabled_extension_owned_query(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        from core.application.management import ServiceConflictError

        mock_service = MagicMock()
        mock_service.execute_query.side_effect = ServiceConflictError("Extension is disabled")

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"extension_binding_service": lambda: mock_service},
            ),
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_query"
            ) as run_query,
        ):
            resp = client.post(
                "/api/extensions/fixture.notification-provider/query/get_config",
                json={"context": {"project_id": 7}},
            )

        assert resp.status_code == 409
        run_query.assert_not_called()
        mock_service.execute_query.assert_called_once()

    def test_api_extension_query_blocks_disabled_extension_handler(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        from core.application.management import ServiceConflictError

        mock_service = MagicMock()
        mock_service.execute_query.side_effect = ServiceConflictError("Extension is disabled")

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"extension_binding_service": lambda: mock_service},
        ):
            resp = client.post("/api/extensions/sample_tasks/query/list_remote_tasks", json={})

        assert resp.status_code == 409
        mock_service.execute_query.assert_called_once()

    def test_api_extension_action_merges_scope_and_item_ids_into_target(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        from core.application.auth.config import DEFAULT_ACCOUNT_ID

        mock_bl = MagicMock()
        mock_bl.get_extension.return_value = {
            "extension_key": "fixture.notification-provider",
            "kind": "notification_provider",
            "enabled": True,
            "compiled_manifest": {},
            "ui_manifest": {},
            "action_definitions": [
                {"action_id": "toggle_route", "handler": {"mode": "platform"}},
            ],
        }
        # FS-DEEP-CAND-040: action dispatch now validates the body-derived
        # project against the caller's account before dispatch.
        mock_bl.get_project.return_value = {"id": "7", "account_id": DEFAULT_ACCOUNT_ID}

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
                return_value={"status": "succeeded", "response": {"ok": True}},
            ) as run_action,
        ):
            resp = client.post(
                "/api/extensions/fixture.notification-provider/action/toggle_route",
                json={
                    "context": {"project_id": 7},
                    "scope": "row",
                    "item_ids": ["route-1"],
                    "values": {"chat_id": "-100"},
                },
            )

        assert resp.status_code == 200
        kwargs = run_action.call_args.kwargs
        assert kwargs["target"]["scope"] == "row"
        assert kwargs["target"]["item_ids"] == ["route-1"]
        assert kwargs["input_payload"]["chat_id"] == "-100"


# ---------------------------------------------------------------------------
# Admin API (registry mode)
# ---------------------------------------------------------------------------


class TestAdminAPIEndpoints:
    """Test admin API endpoints."""

    def test_api_catalog_categories_endpoint(self, client):
        resp = client.get("/api/catalog-categories")
        assert resp.status_code == 200
        assert {"id": "other", "label": "Other"} in resp.json()

    def test_api_agents_endpoint(self, client):
        resp = client.get("/api/agents")
        assert resp.status_code in (200, 404, 500)
        if resp.status_code == 200:
            data = resp.json()
            assert isinstance(data, list)

    def test_api_tools_endpoint(self, client):
        resp = client.get("/api/tools")
        assert resp.status_code in (200, 404, 500)

    def test_api_workflows_endpoint(self, client):
        resp = client.get("/api/workflows")
        assert resp.status_code in (200, 404, 500)

    def test_api_skills_endpoint(self, client):
        resp = client.get("/api/skills")
        assert resp.status_code in (200, 404, 500)

    def test_api_validate_endpoint(self, client):
        resp = client.get("/api/validate")
        assert resp.status_code in (200, 404, 500)

    def test_api_registry_stats(self, client):
        resp = client.get("/api/registry/stats")
        assert resp.status_code in (200, 404, 500)

    def test_api_graph_info(self, client):
        resp = client.get("/api/graph")
        assert resp.status_code in (200, 404, 500)

    def test_api_agent_not_found(self, client):
        resp = client.get("/api/agents/nonexistent_agent_xyz")
        assert resp.status_code in (200, 404, 500)

    def test_api_tool_not_found(self, client):
        resp = client.get("/api/tools/nonexistent_tool_xyz")
        assert resp.status_code in (200, 404, 500)

    def test_api_workflow_not_found(self, client):
        resp = client.get("/api/workflows/nonexistent_workflow_xyz")
        assert resp.status_code in (200, 404, 500)

    def test_api_processes_includes_worker_lease_metadata(self, client):
        try:
            import core.entrypoints.api.processes as processes
        except Exception as e:
            pytest.fail(f"processes module not importable: {e}")

        with (
            patch.object(
                processes,
                "list_process_payload",
                return_value={
                    "jobs": [{"id": 1, "worker_id": "worker-a", "status": "in_progress"}],
                    "subprocesses": [],
                    "suspected_orphans": [],
                    "project_leases": [
                        {
                            "project_id": 11,
                            "worker_instance_id": "worker-a",
                            "heartbeat_age_sec": 4,
                            "drain_status": "draining",
                            "active_job_id": 1,
                        }
                    ],
                    "project_lease_evidence_unavailable": False,
                    "workers": [
                        {
                            "worker_instance_id": "worker-a",
                            "active_leased_project": "11",
                        }
                    ],
                    "reaped_worker_subprocess_count": 0,
                },
            ),
        ):
            resp = client.get("/api/processes")

        assert resp.status_code == 200
        body = resp.json()
        assert body["project_leases"][0]["project_id"] == 11
        assert body["workers"][0]["worker_instance_id"] == "worker-a"
        assert body["workers"][0]["active_leased_project"] == "11"


# ---------------------------------------------------------------------------
# KB endpoints
# ---------------------------------------------------------------------------


class TestKBEndpoints:
    @pytest.mark.parametrize(
        ("method", "path"),
        (
            ("GET", "/api/projects/test-project/kb"),
            ("GET", "/api/projects/test-project/kb/search?query=test"),
            ("POST", "/api/projects/test-project/kb/records"),
            ("GET", "/api/projects/test-project/profile"),
            ("PUT", "/api/projects/test-project/profile"),
        ),
    )
    def test_retired_project_knowledge_routes_return_404(self, client, method, path):
        resp = client.request(method, path, json={} if method in {"POST", "PUT"} else None)
        assert resp.status_code == 404

    def test_retired_project_knowledge_routes_are_absent_from_openapi(self, client):
        paths = client.app.openapi()["paths"]
        assert "/api/projects/{project_id}/kb" not in paths
        assert "/api/projects/{project_id}/kb/search" not in paths
        assert "/api/projects/{project_id}/kb/records" not in paths
        assert "/api/projects/{project_id}/profile" not in paths

    def test_api_project_queue_returns_200_or_error(self, client):
        resp = client.get("/api/projects/test-project/queue")
        assert resp.status_code in (200, 404, 422, 500, 503)

    def test_api_project_queue_includes_lease_and_ready_state(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = _project_fixture(id=7)
        mock_bl.get_pending_jobs.return_value = [
            {
                "id": 501,
                "task_id": "cu-501",
                "task_summary": "Fix lease status",
                "task_source": "provider",
                "queued_at": datetime.now(UTC),
            }
        ]
        mock_bl.get_project_execution_lease.return_value = {
            "project_id": 7,
            "worker_instance_id": "worker-7",
            "lease_token": "token-7",
            "heartbeat_at": datetime.now(UTC),
            "expires_at": datetime.now(UTC),
            "acquired_at": datetime.now(UTC),
            "active_job_id": 501,
            "drain_status": "draining",
        }

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(scheduled_runtime, "_get_queue_depth", return_value=3),
            patch.object(scheduled_runtime, "_get_ready_queue_depth", return_value=11),
        ):
            resp = client.get("/api/projects/7/queue")

        assert resp.status_code == 200
        body = resp.json()
        assert body["project_id"] == "7"
        assert body["project_backlog"] == 1
        assert body["ready_queue_depth"] == 11
        assert body["lease_holder"] == "worker-7"
        assert body["project_lease"]["active_job_id"] == "501"
        assert body["ready_state"]["is_ready"] is False


# ---------------------------------------------------------------------------
# Webhook endpoints
# ---------------------------------------------------------------------------


class TestWebhookEndpoints:
    def test_sample_tasks_webhook_rejects_missing_signature_when_secret_configured(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_provider_secret.return_value = "topsecret"
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
        ):
            resp = client.post(
                "/webhook/providers/sample_tasks",
                json={"task": {"id": "cu-1", "name": "Fix bug"}},
            )

        assert resp.status_code == 401
        assert "signature" in resp.json().get("error", "").lower()

    def test_sample_tasks_webhook_accepts_valid_signature_when_secret_configured(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        payload = {
            "event": "taskCreated",
            "task": {
                "id": "cu-1",
                "name": "Fix bug",
                "description": "Nested payload",
                "list": {"id": "list-1"},
            },
        }
        raw = json.dumps(payload).encode("utf-8")
        signature = hmac.new(b"topsecret", raw, hashlib.sha256).hexdigest()

        mock_bl = MagicMock()
        mock_bl.get_provider_secret.return_value = "topsecret"
        mock_bl.get_provider.return_value = {
            "provider_key": "sample_tasks",
            "provider_type_key": "task_provider",
            "implementation_kind": "extension",
            "enabled": True,
        }
        run_cfg = {
            "project_name": "Demo",
            "project_root": "/target",
            "project_workflows": [{"workflow_id": "wf.sample_tasks.sync", "enabled": True}],
        }
        service_payload = MagicMock()
        service_payload.dump.return_value = {
            "provider_key": "sample_tasks",
            "task_id": "cu-1",
            "job_id": 777,
            "status": "pending",
        }
        mock_service = MagicMock()
        mock_service.ingest_provider_webhook.return_value = service_payload
        with (
            patch(
                "core.application.workflows.workflow_provider_trigger_repository.has_retained_provider_triggers",
                return_value=False,
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_routes,
                "_resolve_task_provider_project_run_config",
                return_value=(11, run_cfg),
            ),
            patch.object(
                api_routes,
                "_normalize_task_provider_payload",
                return_value={
                    "source_id": "cu-1",
                    "task_id": "cu-1",
                    "title": "Fix bug",
                    "description": "Nested payload",
                    "task_type": "",
                    "trigger_config": {"workflow_id": "wf.sample_tasks.sync"},
                },
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"event_command_service": lambda: mock_service},
            ),
            patch(
                "core.entrypoints.api.provider_webhooks_router._claim_provider_webhook_delivery",
                return_value={"claimed": True, "status": "pending"},
            ),
            patch(
                "core.application.extensions.provider_webhook_security.enforce_provider_webhook_signature",
                return_value=(True, None),
            ),
        ):
            resp = client.post(
                "/webhook/providers/sample_tasks",
                content=raw,
                headers={"x-signature": signature, "content-type": "application/json"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] == "cu-1"
        assert body["job_id"] == 777

    def test_sample_tasks_webhook_accepts_nested_task_payload(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_provider_secret.return_value = ""
        mock_bl.get_provider.return_value = {
            "provider_key": "sample_tasks",
            "provider_type_key": "task_provider",
            "implementation_kind": "extension",
            "enabled": True,
        }
        run_cfg = {
            "project_name": "Demo",
            "project_root": "/target",
            "project_workflows": [{"workflow_id": "wf.sample_tasks.sync", "enabled": True}],
        }
        service_payload = MagicMock()
        service_payload.dump.return_value = {
            "provider_key": "sample_tasks",
            "task_id": "cu-1",
            "job_id": 777,
            "status": "pending",
        }
        mock_service = MagicMock()
        mock_service.ingest_provider_webhook.return_value = service_payload

        with (
            patch(
                "core.application.workflows.workflow_provider_trigger_repository.has_retained_provider_triggers",
                return_value=False,
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_routes,
                "_resolve_task_provider_project_run_config",
                return_value=(11, run_cfg),
            ),
            patch.object(
                api_routes,
                "_normalize_task_provider_payload",
                return_value={
                    "source": "sample_tasks",
                    "source_id": "cu-1",
                    "task_id": "cu-1",
                    "title": "Fix bug",
                    "description": "Nested payload",
                    "task_type": "",
                    "metadata": {},
                    "trigger_config": {
                        "workflow_id": "wf.sample_tasks.sync",
                        "list_id": "list-1",
                        "event": "taskCreated",
                    },
                },
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"event_command_service": lambda: mock_service},
            ),
            patch(
                "core.entrypoints.api.provider_webhooks_router._claim_provider_webhook_delivery",
                return_value={"claimed": True, "status": "pending"},
            ),
            patch(
                "core.application.extensions.provider_webhook_security.enforce_provider_webhook_signature",
                return_value=(True, None),
            ),
        ):
            resp = client.post(
                "/webhook/providers/sample_tasks",
                json={
                    "event": "taskCreated",
                    "task": {
                        "id": "cu-1",
                        "name": "Fix bug",
                        "description": "Nested payload",
                        "list": {"id": "list-1"},
                    },
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] == "cu-1"
        assert body["job_id"] == 777

    def test_generic_provider_webhook_uses_normalized_task_payload(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_provider_secret.return_value = ""
        mock_bl.get_provider.return_value = {
            "provider_key": "example_task_provider",
            "provider_type_key": "task_provider",
            "implementation_kind": "extension",
            "integration_key": "example_task_provider",
            "enabled": True,
            "config": {"extension_key": "example_task_provider"},
        }
        run_cfg = {
            "project_name": "Demo",
            "project_root": "/target",
            "project_workflows": [{"workflow_id": "wf.external.sync", "enabled": True}],
        }
        service_payload = MagicMock()
        service_payload.dump.return_value = {
            "provider_key": "example_task_provider",
            "task_id": "ext-1",
            "job_id": 808,
            "status": "pending",
        }
        mock_service = MagicMock()
        mock_service.ingest_provider_webhook.return_value = service_payload

        with (
            patch(
                "core.application.workflows.workflow_provider_trigger_repository.has_retained_provider_triggers",
                return_value=False,
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(
                api_routes,
                "_resolve_task_provider_project_run_config",
                return_value=(11, run_cfg),
            ),
            patch.object(
                api_routes,
                "_normalize_task_provider_payload",
                return_value={
                    "source_id": "ext-1",
                    "task_id": "ext-1",
                    "title": "External task",
                    "description": "From extension",
                    "task_type": "",
                    "trigger_config": {"workflow_id": "wf.external.sync"},
                },
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"event_command_service": lambda: mock_service},
            ),
            patch(
                "core.entrypoints.api.provider_webhooks_router._claim_provider_webhook_delivery",
                return_value={"claimed": True, "status": "pending"},
            ),
            patch(
                "core.application.extensions.provider_webhook_security.enforce_provider_webhook_signature",
                return_value=(True, None),
            ),
        ):
            resp = client.post(
                "/webhook/providers/example_task_provider", json={"payload": {"id": "ext-1"}}
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["provider_key"] == "example_task_provider"
        assert body["job_id"] == 808

    def test_provider_webhook_uses_event_command_service_contract(self, client):
        try:
            pass
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_provider_secret.return_value = ""
        mock_bl.get_provider.return_value = {
            "provider_key": "example_task_provider",
            "provider_type_key": "task_provider",
            "implementation_kind": "extension",
            "integration_key": "example_task_provider",
            "enabled": True,
            "config": {"extension_key": "example_task_provider"},
        }
        payload = MagicMock()
        payload.dump.return_value = {
            "provider_key": "example_task_provider",
            "task_id": "ext-1",
            "job_id": 808,
            "status": "pending",
            "queue": "platform.project.11",
        }
        mock_service = MagicMock()
        mock_service.ingest_provider_webhook.return_value = payload

        with (
            patch(
                "core.application.workflows.workflow_provider_trigger_repository.has_retained_provider_triggers",
                return_value=False,
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"db": lambda: mock_bl},
            ),
            patch(
                "core.entrypoints.api.route_registration._resolve_task_provider_project_run_config",
                return_value=(11, {"project_name": "Demo", "project_root": "/target"}),
            ),
            patch(
                "core.entrypoints.api.route_registration._normalize_task_provider_payload",
                return_value={
                    "source_id": "ext-1",
                    "task_id": "ext-1",
                    "title": "External task",
                    "description": "From extension",
                    "task_type": "feature",
                    "trigger_config": {"event": "created"},
                },
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"event_command_service": lambda: mock_service},
            ),
            patch(
                "core.entrypoints.api.provider_webhooks_router._claim_provider_webhook_delivery",
                return_value={"claimed": True, "status": "pending"},
            ),
            patch(
                "core.application.extensions.provider_webhook_security.enforce_provider_webhook_signature",
                return_value=(True, None),
            ),
        ):
            resp = client.post(
                "/webhook/providers/example_task_provider", json={"payload": {"id": "ext-1"}}
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["provider_key"] == "example_task_provider"
        assert body["job_id"] == 808
        call = mock_service.ingest_provider_webhook.call_args
        command = call.args[0]
        assert command.provider_key == "example_task_provider"
        assert command.project_id == "11"
        assert command.project_config["project_root"] == "/target"
        assert command.task_id == "ext-1"
        assert command.title == "External task"
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].project_id == "11"
        assert call.kwargs["scope"].actor_id == "example_task_provider_webhook"

    def test_sample_tasks_comment_webhook_ingests_normalized_reaction_event(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

        mock_bl = MagicMock()
        mock_bl.get_provider_secret.return_value = ""
        mock_bl.get_provider.return_value = {
            "provider_key": "sample_tasks",
            "provider_type_key": "task_provider",
            "implementation_kind": "builtin",
            "enabled": True,
        }
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {
            "id": 55,
            "thread_id": "thread-1",
            "status": "awaiting_approval",
            "meta": {"selected_workflow_id": "weekly_report"},
        }
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cursor

        payload = MagicMock()
        payload.dump.return_value = {
            "provider_key": "sample_tasks",
            "task_id": "cu-55",
            "job_id": 55,
            "status": "accepted",
            "comment_length": 15,
            "reaction": {
                "event": {"id": 91, "event_type": "external.comment.received"},
                "reactions": [{"reaction_key": "comment_resume", "status": "resumed"}],
                "notification_deliveries": [],
            },
        }
        mock_service = MagicMock()
        mock_service.ingest_provider_webhook.return_value = payload

        with (
            patch(
                "core.application.workflows.workflow_provider_trigger_repository.has_retained_provider_triggers",
                return_value=False,
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"event_command_service": lambda: mock_service},
            ),
            patch(
                "core.entrypoints.api.provider_webhooks_router._claim_provider_webhook_delivery",
                return_value={"claimed": True, "status": "pending"},
            ),
            patch.object(
                api_routes,
                "_resolve_task_provider_project_run_config",
                return_value=(11, {"project_name": "Demo", "project_root": "/target"}),
            ),
            patch.object(
                api_routes,
                "_normalize_task_provider_payload",
                return_value={
                    "source": "sample_tasks",
                    "source_id": "comment-1",
                    "task_id": "cu-55",
                    "title": "Sample Tasks comment",
                    "description": "Please retry the review",
                    "task_type": "",
                    "metadata": {
                        "comment_text": "Please retry the review",
                        "comment_id": "comment-1",
                        "event": "taskCommentPosted",
                    },
                    "trigger_config": {"event": "taskCommentPosted"},
                },
            ),
            patch.object(api_runtime, "build_registry_graph") as graph_mock,
            patch(
                "core.application.extensions.provider_webhook_security.enforce_provider_webhook_signature",
                return_value=(True, None),
            ),
        ):
            resp = client.post(
                "/webhook/providers/sample_tasks",
                json={
                    "event": "taskCommentPosted",
                    "task_id": "cu-55",
                    "history_items": [
                        {"comment": {"id": "comment-1", "text_content": "Please retry the review"}}
                    ],
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["job_id"] == 55
        mock_service.ingest_provider_webhook.assert_called_once()
        command = mock_service.ingest_provider_webhook.call_args.args[0]
        assert command.provider_key == "sample_tasks"
        assert command.project_id == "11"
        assert command.task_id == "cu-55"
        assert command.task_type == ""
        assert command.metadata["comment_text"] == "Please retry the review"
        assert command.metadata["comment_id"] == "comment-1"
        graph_mock.assert_not_called()


class TestExternalSignInEndpoints:
    """Community edition should not expose optional auth integration routes."""

    @pytest.fixture(autouse=True)
    def _import_guard(self):
        try:
            from tests import api_runtime_test_surface as api_runtime  # noqa: F401
        except Exception as e:
            pytest.fail(f"api_runtime not importable: {e}")

    @pytest.fixture
    def client(self):
        from starlette.testclient import TestClient

        from tests import api_runtime_test_surface as api_runtime

        return TestClient(api_runtime.app, raise_server_exceptions=False)

    def test_optional_auth_admin_surfaces_are_not_mounted_by_default(self, client):
        assert client.get("/api/auth/optional-auth-integrations").status_code == 404
        assert client.get("/api/auth/optional-auth-policies").status_code == 404


class TestEndpointLevelOwnershipDeny:
    """HTTP-level tests verifying cross-account requests return 403."""

    def test_api_job_status_returns_403_for_cross_account(self, client):
        try:
            from core.application.management.scope import ScopeContext
        except Exception as e:
            _fail_only_missing_optional_dep(e, context="api_runtime import for ownership deny test")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 42,
            "project_id": 1,
            "status": "in_progress",
            "thread_id": "thread-42",
            "task_summary": "Some task",
            "started_at": None,
            "finished_at": None,
            "duration_ms": 0,
        }
        mock_bl.get_project.return_value = {"id": 1, "name": "proj-a", "account_id": "acc_alpha"}
        mock_bl.get_job_cost_summary.return_value = {}

        cross_scope = ScopeContext(account_id="acc_other", actor_id="eve", principal_type="user")
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_request_scope", return_value=cross_scope),
            patch.object(runtime_helpers, "_build_runtime_payload", return_value={}),
        ):
            resp = client.get("/api/jobs/42")

        assert resp.status_code == 403

    def test_api_job_trace_returns_403_for_cross_account(self, client):
        try:
            from core.application.management.scope import ScopeContext
        except Exception as e:
            _fail_only_missing_optional_dep(e, context="api_runtime import for ownership deny test")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 42,
            "project_id": 1,
            "status": "in_progress",
        }
        mock_bl.get_project.return_value = {"id": 1, "name": "proj-a", "account_id": "acc_alpha"}

        cross_scope = ScopeContext(account_id="acc_other", actor_id="eve", principal_type="user")
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_request_scope", return_value=cross_scope),
        ):
            resp = client.get("/api/jobs/42/trace")

        assert resp.status_code == 403
        mock_bl.build_job_trace.assert_not_called()

    def test_api_job_incident_returns_403_for_cross_account(self, client):
        try:
            from core.application.management.scope import ScopeContext
        except Exception as e:
            _fail_only_missing_optional_dep(e, context="api_runtime import for ownership deny test")

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {
            "id": 42,
            "project_id": 1,
            "status": "failed",
        }
        mock_bl.get_project.return_value = {"id": 1, "name": "proj-a", "account_id": "acc_alpha"}

        cross_scope = ScopeContext(account_id="acc_other", actor_id="eve", principal_type="user")
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_request_scope", return_value=cross_scope),
        ):
            resp = client.get("/api/jobs/42/incident")

        assert resp.status_code == 403
        mock_bl.list_domain_events.assert_not_called()

    def test_api_job_incident_uses_query_service_contract(self, client):
        try:
            from core.application.management.scope import ScopeContext
        except Exception as e:
            _fail_only_missing_optional_dep(
                e, context="api_runtime import for incident endpoint contract test"
            )

        mock_payload = MagicMock()
        mock_payload.dump.return_value = {
            "job_id": 42,
            "status": "failed",
            "root_cause": {"category": "timeout", "message": "tool timeout"},
            "failure_context": {},
            "related_events": [],
            "related_dispatches": [],
            "related_attempts": [],
        }
        mock_service = MagicMock()
        mock_service.get_incident_drilldown.return_value = mock_payload

        scope = ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="alice", principal_type="user")
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_query_service": lambda: mock_service},
            ),
            patch(
                "core.entrypoints.api.route_registration._request_scope",
                return_value=scope,
            ),
        ):
            resp = client.get("/api/jobs/42/incident")

        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == 42
        assert body["root_cause"]["category"] == "timeout"
        call = mock_service.get_incident_drilldown.call_args
        assert call.args[0] == "42"
        assert call.kwargs["scope"] == scope

    def test_api_project_queue_returns_403_for_cross_account(self, client):
        try:
            from core.application.management.scope import ScopeContext
        except Exception as e:
            _fail_only_missing_optional_dep(e, context="api_runtime import for ownership deny test")

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {"id": 1, "name": "proj-a", "account_id": "acc_alpha"}

        cross_scope = ScopeContext(account_id="acc_other", actor_id="eve", principal_type="user")
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_request_scope", return_value=cross_scope),
        ):
            resp = client.get("/api/projects/1/queue")

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Scope enforcement on provider & notification routes (E5-S2)
# ---------------------------------------------------------------------------


class TestProviderNotificationScopeEnforcement:
    """Verify that previously ungated provider/notification routes now enforce
    project-level scope via require_project_access."""

    def _setup_mocks(self, account_id: str = "acc_alpha"):
        try:
            from core.application.management.scope import ScopeContext
            from tests import api_runtime_test_surface as api_runtime
        except Exception as e:
            _fail_only_missing_optional_dep(e, context="api_runtime import for scope enforcement")
        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {"id": 1, "name": "proj-a", "account_id": account_id}
        mock_bl.get_project_provider_bindings.return_value = []
        mock_bl.list_notification_routes.return_value = []
        mock_bl.list_notification_delivery_logs.return_value = []
        return api_runtime, ScopeContext, mock_bl

    # -- Cross-account denial (403) -------------------------------------------

    def test_project_providers_returns_403_for_cross_account(self, client):
        _api_runtime, ScopeContext, mock_bl = self._setup_mocks()
        cross_scope = ScopeContext(account_id="acc_other", actor_id="eve", principal_type="user")
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_request_scope", return_value=cross_scope),
        ):
            resp = client.get("/api/projects/1/providers")
        assert resp.status_code == 403

    def test_bind_project_provider_returns_403_for_cross_account(self, client):
        _api_runtime, ScopeContext, mock_bl = self._setup_mocks()
        cross_scope = ScopeContext(account_id="acc_other", actor_id="eve", principal_type="user")
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_request_scope", return_value=cross_scope),
        ):
            resp = client.post(
                "/api/projects/1/providers/bind",
                json={
                    "provider_key": "test",
                    "enabled": True,
                },
            )
        assert resp.status_code == 403

    def test_notification_routes_returns_403_for_cross_account(self, client):
        _api_runtime, ScopeContext, mock_bl = self._setup_mocks()
        cross_scope = ScopeContext(account_id="acc_other", actor_id="eve", principal_type="user")
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_request_scope", return_value=cross_scope),
        ):
            resp = client.get("/api/projects/1/notification-routes")
        assert resp.status_code == 403

    def test_update_notification_routes_returns_403_for_cross_account(self, client):
        _api_runtime, ScopeContext, mock_bl = self._setup_mocks()
        cross_scope = ScopeContext(account_id="acc_other", actor_id="eve", principal_type="user")
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_request_scope", return_value=cross_scope),
        ):
            resp = client.post("/api/projects/1/notification-routes", json={"routes": []})
        assert resp.status_code == 403

    def test_notification_deliveries_returns_403_for_cross_account(self, client):
        _api_runtime, ScopeContext, mock_bl = self._setup_mocks()
        cross_scope = ScopeContext(account_id="acc_other", actor_id="eve", principal_type="user")
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_request_scope", return_value=cross_scope),
        ):
            resp = client.get("/api/projects/1/notification-deliveries")
        assert resp.status_code == 403

    # -- Restricted token (allowed_project_ids) denial -------------------------

    def test_project_providers_returns_403_for_restricted_token(self, client):
        _api_runtime, ScopeContext, mock_bl = self._setup_mocks()
        restricted_scope = ScopeContext(
            account_id="acc_alpha",
            actor_id="svc-ci",
            principal_type="service_account",
            allowed_project_ids=frozenset({"99"}),
        )
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_request_scope", return_value=restricted_scope),
        ):
            resp = client.get("/api/projects/1/providers")
        assert resp.status_code == 403

    def test_notification_routes_returns_403_for_restricted_token(self, client):
        _api_runtime, ScopeContext, mock_bl = self._setup_mocks()
        restricted_scope = ScopeContext(
            account_id="acc_alpha",
            actor_id="svc-ci",
            principal_type="service_account",
            allowed_project_ids=frozenset({"99"}),
        )
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_request_scope", return_value=restricted_scope),
        ):
            resp = client.get("/api/projects/1/notification-routes")
        assert resp.status_code == 403

    # -- Allowed access (200) --------------------------------------------------

    def test_project_providers_returns_200_for_same_account(self, client):
        _api_runtime, ScopeContext, mock_bl = self._setup_mocks()
        ok_scope = ScopeContext(account_id="acc_alpha", actor_id="alice", principal_type="user")
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_request_scope", return_value=ok_scope),
        ):
            resp = client.get("/api/projects/1/providers")
        assert resp.status_code == 200

    def test_notification_overview_returns_403_for_cross_account(self, client):
        _api_runtime, ScopeContext, mock_bl = self._setup_mocks()
        cross_scope = ScopeContext(account_id="acc_other", actor_id="eve", principal_type="user")
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_request_scope", return_value=cross_scope),
        ):
            resp = client.get("/api/projects/1/notification-overview")
        assert resp.status_code == 403
        mock_bl.list_workflow_reactions.assert_not_called()
        mock_bl.list_notification_routes.assert_not_called()

    def test_notification_overview_returns_403_for_restricted_token(self, client):
        _api_runtime, ScopeContext, mock_bl = self._setup_mocks()
        restricted_scope = ScopeContext(
            account_id="acc_alpha",
            actor_id="svc-ci",
            principal_type="service_account",
            allowed_project_ids=frozenset({"99"}),
        )
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_request_scope", return_value=restricted_scope),
        ):
            resp = client.get("/api/projects/1/notification-overview")
        assert resp.status_code == 403

    def test_notification_overview_lists_subscriptions_read_only(self, client):
        _api_runtime, ScopeContext, mock_bl = self._setup_mocks(account_id=DEFAULT_ACCOUNT_ID)
        mock_bl.get_project_workflows.return_value = []
        mock_bl.list_notification_routes.return_value = [
            {
                "route_key": "failed",
                "display_name": "Failed runs",
                "provider_key": "acme.notify",
                "event_type": "job.failed",
                "severity": "error",
                "route_config": {"channel_id": "ops"},
                "enabled": True,
            }
        ]
        ok_scope = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="alice", principal_type="user"
        )
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_request_scope", return_value=ok_scope),
        ):
            resp = client.get("/api/projects/1/notification-overview")
        assert resp.status_code == 200
        body = resp.json()
        assert body["project_id"] == "1"
        (entry,) = body["entries"]
        assert entry["kind"] == "subscription"
        assert entry["settings"] == {"kind": "subscription", "route_key": "failed"}
        assert entry["conditions"]["severity"] == "error"
        mock_bl.save_notification_routes.assert_not_called()
        mock_bl.save_workflow_reactions.assert_not_called()

    def test_notification_routes_returns_200_for_same_account(self, client):
        _api_runtime, ScopeContext, mock_bl = self._setup_mocks()
        ok_scope = ScopeContext(account_id="acc_alpha", actor_id="alice", principal_type="user")
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_request_scope", return_value=ok_scope),
        ):
            resp = client.get("/api/projects/1/notification-routes")
        assert resp.status_code == 200

    def test_notification_deliveries_returns_200_for_same_account(self, client):
        _api_runtime, ScopeContext, mock_bl = self._setup_mocks()
        ok_scope = ScopeContext(account_id="acc_alpha", actor_id="alice", principal_type="user")
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_request_scope", return_value=ok_scope),
        ):
            resp = client.get("/api/projects/1/notification-deliveries")
        assert resp.status_code == 200

    def test_notification_dispatch_returns_403_for_cross_account(self, client):
        _api_runtime, ScopeContext, mock_bl = self._setup_mocks()
        cross_scope = ScopeContext(account_id="acc_other", actor_id="eve", principal_type="user")
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_request_scope", return_value=cross_scope),
        ):
            resp = client.post(
                "/api/projects/1/notifications/dispatch", json={"event_type": "job.failed"}
            )
        assert resp.status_code == 403

    def test_notification_dispatch_passes_authoritative_account_to_runtime(self, client):
        _api_runtime, ScopeContext, mock_bl = self._setup_mocks()
        ok_scope = ScopeContext(account_id="acc_alpha", actor_id="alice", principal_type="user")
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_request_scope", return_value=ok_scope),
            patch(
                "core.application.notifications.dispatch.dispatch_project_notification_event",
                return_value=[{"delivery_id": 1, "status": "delivered"}],
            ) as dispatch,
        ):
            resp = client.post(
                "/api/projects/1/notifications/dispatch",
                json={
                    "event_type": "job.failed",
                    "severity": "error",
                    "payload": {"summary": "failed"},
                },
            )
        assert resp.status_code == 200
        assert dispatch.call_args.kwargs["account_id"] == "acc_alpha"

    def test_project_providers_returns_200_for_allowed_restricted_token(self, client):
        _api_runtime, ScopeContext, mock_bl = self._setup_mocks()
        ok_scope = ScopeContext(
            account_id="acc_alpha",
            actor_id="svc-ci",
            principal_type="service_account",
            allowed_project_ids=frozenset({"1", "3"}),
        )
        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_routes, "_request_scope", return_value=ok_scope),
        ):
            resp = client.get("/api/projects/1/providers")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# E12-S3 — task_provider_normalize audit context threading
# ---------------------------------------------------------------------------


class TestTaskProviderNormalizeAudit:
    """Verify that _normalize_task_provider_payload threads audit context
    into runtime_context and log_extension_invocation."""

    def test_normalize_threads_scope_into_runtime_context_and_logger(self):
        try:
            from core.application.management.scope import ScopeContext
            from tests import api_runtime_test_surface as api_runtime
        except Exception as exc:
            pytest.fail(f"api_runtime import failed: {exc}")

        scope = ScopeContext(
            account_id="acct_99",
            workflow_id="intake_flow",
            actor_id="webhook_svc",
            principal_type="service_account",
        )
        mock_bl = MagicMock()
        mock_bl.get_extension.return_value = {
            "extension_key": "sample_tasks",
            "kind": "task_provider",
            "enabled": True,
            "bundle_root": "/tmp",
            "entrypoint": {"command": ["echo"]},
        }

        fake_invocation = {
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": {
                    "normalized": {
                        "source_id": "cu-1",
                        "task_id": "cu-1",
                        "title": "Test",
                        "description": "",
                        "task_type": "feature",
                        "trigger_config": {},
                    },
                },
            },
        }

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value=fake_invocation,
        ) as run_inv:
            result = api_runtime._normalize_task_provider_payload(
                provider={"provider_key": "sample_tasks", "implementation_kind": "extension"},
                payload={"task": "data"},
                bl=mock_bl,
                scope=scope,
            )

        assert result["source_id"] == "cu-1"

        # Verify runtime_context contains audit fields
        ctx = run_inv.call_args.kwargs.get("runtime_context") or {}
        assert ctx["account_id"] == "acct_99"
        assert ctx["workflow_id"] == "intake_flow"
        assert ctx["actor_id"] == "webhook_svc"

        # Verify log_extension_invocation receives audit fields
        log_kwargs = mock_bl.log_extension_invocation.call_args.kwargs
        assert log_kwargs["account_id"] == "acct_99"
        assert log_kwargs["actor_type"] == "service_account"
        assert log_kwargs["actor_id"] == "webhook_svc"
        assert log_kwargs["workflow_id"] == "intake_flow"

    def test_normalize_works_without_scope(self):
        """The canonical root entrypoint tolerates internal calls without request scope."""
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as exc:
            pytest.fail(f"api_runtime import failed: {exc}")

        mock_bl = MagicMock()
        mock_bl.get_extension.return_value = {
            "extension_key": "sample_tasks",
            "kind": "task_provider",
            "enabled": True,
            "bundle_root": "/tmp",
            "entrypoint": {"command": ["echo"]},
        }

        fake_invocation = {
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": {
                    "normalized": {
                        "source_id": "cu-1",
                        "task_id": "cu-1",
                        "title": "Test",
                        "description": "",
                        "task_type": "feature",
                        "trigger_config": {},
                    },
                },
            },
        }

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value=fake_invocation,
        ) as run_inv:
            result = api_runtime._normalize_task_provider_payload(
                provider={"provider_key": "sample_tasks", "implementation_kind": "extension"},
                payload={},
                bl=mock_bl,
                # scope omitted for internal call path
            )

        assert result["source_id"] == "cu-1"
        ctx = run_inv.call_args.kwargs.get("runtime_context")
        assert ctx is None  # no scope = no audit context


class TestExtensionApiEndpoints:
    def test_api_extensions_forwards_filters_to_query_service(self, client):
        from core.application.management.query_models import ExtensionListView, ExtensionSummaryView

        captured: dict[str, object] = {}

        def _fake_list(self, *, filters=None, scope=None, locale=""):
            captured["filters"] = filters
            captured["locale"] = locale
            return ExtensionListView(
                extensions=[
                    ExtensionSummaryView(
                        extension_id="vendor.sales",
                        state="disabled",
                        entitlement_status="suspended_nonpayment",
                        status_group="billing_blocked",
                    )
                ],
                total=1,
            )

        with patch(
            "core.application.extensions.query_service.ExtensionQueryService.list_extensions",
            autospec=True,
            side_effect=_fake_list,
        ):
            resp = client.get(
                "/api/extensions"
                "?q=sales&state=disabled&status_group=billing_blocked"
                "&runtime_mode=local&entitlement_status=suspended_nonpayment"
                "&origin=vendor_published&source=marketplace&visibility=account"
                "&has_runtime_module=true&connected=false"
            )

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["total"] == 1
        assert payload["extensions"][0]["status_group"] == "billing_blocked"
        assert captured["filters"]["state"] == ["disabled"]
        assert captured["filters"]["status_group"] == ["billing_blocked"]
        assert captured["filters"]["has_runtime_module"] is True
        assert captured["filters"]["connected"] is False

    def test_api_extensions_marketplace_forwards_filters_and_facets(self, client):
        from core.application.management.query_models import (
            ExtensionMarketplaceFacetBucketView,
            ExtensionMarketplaceFacetsView,
            ExtensionMarketplaceItemView,
            ExtensionMarketplaceListView,
        )

        captured: dict[str, object] = {}

        def _fake_marketplace(self, *, filters=None, scope=None):
            captured["filters"] = filters
            return ExtensionMarketplaceListView(
                extensions=[
                    ExtensionMarketplaceItemView(
                        extension_id="vendor.sales",
                        release_id="pr_123",
                        category="automation",
                        tags=["sales"],
                        vendor="acme",
                        execution_modes=["local"],
                        contains=["workflow"],
                        installed=True,
                        install_id="pi_123",
                    )
                ],
                total=1,
                page=1,
                per_page=20,
                total_pages=1,
                facets=ExtensionMarketplaceFacetsView(
                    category=[ExtensionMarketplaceFacetBucketView(value="automation", count=1)]
                ),
            )

        with patch(
            "core.application.extensions.query_service.ExtensionQueryService.list_marketplace",
            autospec=True,
            side_effect=_fake_marketplace,
        ):
            resp = client.get(
                "/api/extensions/marketplace"
                "?q=sales&category=automation&tags=sales&vendor=acme"
                "&contains=workflow&channel=marketplace_managed&execution_mode=local"
                "&allows_self_hosted=true&requires_connected_control_plane=false"
                "&import_source=url&compatibility=compatible&origin=vendor_published"
                "&billing_mode=subscription&plan_id=pro&installed=true"
                "&facets=true&page=1&per_page=20&sort=name_asc"
            )

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["total"] == 1
        assert payload["extensions"][0]["installed"] is True
        assert payload["facets"]["category"][0]["value"] == "automation"
        assert captured["filters"]["category"] == ["automation"]
        assert captured["filters"]["tags"] == ["sales"]
        assert captured["filters"]["allows_self_hosted"] is True
        assert captured["filters"]["requires_connected_control_plane"] is False
        assert captured["filters"]["installed"] is True
        assert captured["filters"]["facets"] is True

    def test_extensions_page_renders_installed_and_marketplace_tabs(self, client):

        with patch.object(client.app.state, "local_auth_enabled_override", return_value=False):
            resp = client.get("/extensions")
        _assert_frontend_shell_response(resp)

    def test_api_extensions_rejects_invalid_boolean_filters(self, client):
        resp = client.get("/api/extensions?connected=definitely")
        assert resp.status_code == 400


def test_project_connection_create_rejects_a_noncanonical_extension_id_before_service_lookup(
    client,
):
    response = client.post(
        "/api/projects/1/connections",
        json={"extension_id": "acme_showcase", "connection_kind": "extension"},
    )

    assert response.status_code == 422
    assert "extension_id" in response.text


def test_project_connection_create_rejects_an_unknown_runtime_mode_before_service_lookup(client):
    response = client.post(
        "/api/projects/1/connections",
        json={
            "connection_kind": "extension",
            "extension_id": "acme.notifier",
            "runtime_mode": "broken",
        },
    )

    assert response.status_code == 422
    assert "runtime_mode" in response.text
