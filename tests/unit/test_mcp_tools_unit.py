"""
Unit tests for tools/mcp_tools.py — MCP tool wrapper functions.

Tests:
- JSON-RPC payload construction
- Error handling (HTTP errors, timeouts, malformed responses)
- Flow Steward MCP and external MCP endpoints

Run:
    docker compose exec fs-app python -m pytest tests/unit/test_mcp_tools_unit.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_httpx_response(json_data: dict, status_code: int = 200):
    """Create a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    if status_code >= 400:
        import httpx

        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    return resp


def _success_response(text: str = "result text"):
    return {"result": {"content": [{"text": text}]}}


def _error_response(message: str = "tool not found"):
    return {"error": {"code": -32601, "message": message}}


def _httpx2_status_error(status_code: int):
    import httpx2

    request = httpx2.Request("POST", "http://test:8100/mcp")
    response = httpx2.Response(status_code, request=request)
    return httpx2.HTTPStatusError("error", request=request, response=response)


# ---------------------------------------------------------------------------
# Tests: _call_mcp_endpoint
# ---------------------------------------------------------------------------


class TestCallMCPEndpoint:
    """Tests for the core MCP JSON-RPC caller."""

    def test_v2_client_uses_pinned_modern_stateless_transport(self, monkeypatch):
        from core.application.orchestration.tools import mcp_tools_client

        observed: dict[str, object] = {}

        class FakeHttpClient:
            def __init__(self, **kwargs):
                observed["http"] = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        def fake_transport(url, *, http_client, terminate_on_close):
            observed["transport"] = (url, http_client, terminate_on_close)
            return "transport"

        class FakeClient:
            def __init__(self, transport, *, mode, cache):
                observed["client"] = (transport, mode, cache)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def call_tool(self, name, arguments):
                observed["call"] = (name, arguments)
                return SimpleNamespace(
                    is_error=False,
                    content=[SimpleNamespace(text="modern result")],
                )

        monkeypatch.setattr(mcp_tools_client.httpx2, "AsyncClient", FakeHttpClient)
        monkeypatch.setattr(mcp_tools_client, "streamable_http_client", fake_transport)
        monkeypatch.setattr(mcp_tools_client, "Client", FakeClient)

        result = asyncio.run(
            mcp_tools_client._call_modern_mcp_endpoint(
                "http://test:8100/mcp",
                "project_list",
                {},
                headers={"Authorization": "Bearer opaque"},
            )
        )

        assert result == "modern result"
        assert observed["client"] == ("transport", "2026-07-28", None)
        assert observed["transport"][0] == "http://test:8100/mcp"
        assert observed["transport"][2] is False
        assert observed["http"]["headers"] == {"Authorization": "Bearer opaque"}
        assert observed["call"] == ("project_list", {})

    def test_nested_sdk_transport_error_is_classified_for_endpoint_fallback(self):
        import httpx2

        from core.application.orchestration.tools.mcp_tools_client import (
            _find_nested_exception,
        )

        transport_error = httpx2.ConnectError("unreachable")
        grouped = ExceptionGroup("SDK transport tasks", [transport_error])

        assert _find_nested_exception(grouped, httpx2.TransportError) is transport_error

    def test_sends_correct_jsonrpc_payload(self):
        try:
            from core.application.orchestration.tools.mcp_tools import _call_mcp_endpoint
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        with patch(
            "core.application.orchestration.tools.mcp_tools._CLIENT_MODERN_MCP_CALL",
            new=AsyncMock(return_value="ok"),
        ) as modern_call:
            _call_mcp_endpoint("http://test:8100/mcp", "project_list", {})

        args = modern_call.await_args
        assert args.args[:3] == (
            "http://test:8100/mcp",
            "project_list",
            {},
        )

    def test_returns_text_content(self):
        try:
            from core.application.orchestration.tools.mcp_tools import _call_mcp_endpoint
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        with patch(
            "core.application.orchestration.tools.mcp_tools._CLIENT_MODERN_MCP_CALL",
            new=AsyncMock(return_value="hello world"),
        ):
            result = _call_mcp_endpoint("http://test:8100/mcp", "test_tool", {})

        assert result == "hello world"

    def test_logs_bounded_mcp_tool_observability_when_job_context_available(self):
        try:
            import core.application.orchestration.tools.mcp_tools as mcp_tools
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        class _FakeLogger:
            def __init__(self):
                self.actions = []

            def get_job(self, job_id):
                return {
                    "id": job_id,
                    "project_id": "project-1",
                    "task_id": "task-1",
                    "task_summary": "Observe MCP",
                }

            def log_action(self, **kwargs):
                self.actions.append(kwargs)

        fake_logger = _FakeLogger()
        token = mcp_tools._MCP_PRINCIPAL.set("job:job-123")
        try:
            with (
                patch(
                    "core.application.orchestration.tools.mcp_tools._get_platform_auth_logger",
                    return_value=fake_logger,
                ),
                patch(
                    "core.application.orchestration.tools.mcp_tools._CLIENT_MODERN_MCP_CALL",
                    new=AsyncMock(return_value="hello world"),
                ),
            ):
                result = mcp_tools._call_mcp_endpoint(
                    "http://test:8100/mcp",
                    "workflow_list",
                    {
                        "query": "sensitive implementation detail",
                        "agent_id": "composer",
                        "correlation_id": "corr-1",
                    },
                    include_platform_auth=False,
                )
        finally:
            mcp_tools._MCP_PRINCIPAL.reset(token)

        assert result == "hello world"
        assert len(fake_logger.actions) == 1
        action = fake_logger.actions[0]
        assert action["action_type"] == "mcp_tool_call"
        assert action["agent"] == "composer"
        assert action["status"] == "done"
        assert action["payload"]["tool_name"] == "workflow_list"
        assert action["payload"]["args_chars"] > 0
        assert action["payload"]["response_chars"] == len("hello world")
        assert action["payload"]["duration_ms"] >= 0
        assert action["payload"]["correlation_id"] == "corr-1"
        assert "sensitive implementation detail" not in str(action)

    def test_mcp_observability_redacts_bearer_from_error_text(self):
        try:
            from core.application.orchestration.tools.mcp_tools import (
                _redact_mcp_observability_text,
            )
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        text = _redact_mcp_observability_text(
            "failed with Authorization: Bearer super-secret-token"
        )

        assert "super-secret-token" not in text
        assert "Bearer [REDACTED]" in text

    def test_raises_on_mcp_error_response(self):
        try:
            from core.application.orchestration.tools.mcp_tools import _call_mcp_endpoint
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        with (
            patch(
                "core.application.orchestration.tools.mcp_tools._CLIENT_MODERN_MCP_CALL",
                new=AsyncMock(side_effect=RuntimeError("MCP error: tool not found")),
            ),
            pytest.raises(RuntimeError, match="MCP error"),
        ):
            _call_mcp_endpoint("http://test:8100/mcp", "bad_tool", {})

    def test_raises_on_http_error(self):
        try:
            import httpx

            from core.application.orchestration.tools.mcp_tools import _call_mcp_endpoint
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools or httpx not importable")

        with (
            patch(
                "core.application.orchestration.tools.mcp_tools._CLIENT_MODERN_MCP_CALL",
                new=AsyncMock(side_effect=_httpx2_status_error(500)),
            ),
            pytest.raises(httpx.HTTPStatusError),
        ):
            _call_mcp_endpoint("http://test:8100/mcp", "test_tool", {})

    def test_timeout_propagates(self):
        try:
            import httpx

            from core.application.orchestration.tools.mcp_tools import _call_mcp_endpoint
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools or httpx not importable")

        import httpx2

        with (
            patch(
                "core.application.orchestration.tools.mcp_tools._CLIENT_MODERN_MCP_CALL",
                new=AsyncMock(side_effect=httpx2.ReadTimeout("timeout")),
            ),
            pytest.raises(httpx.ReadTimeout),
        ):
            _call_mcp_endpoint("http://test:8100/mcp", "test_tool", {})

    def test_flow_steward_mcp_request_includes_bearer_header_when_token_available(self):
        try:
            from core.application.orchestration.tools.mcp_tools import _call_mcp_endpoint
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        with (
            patch(
                "core.application.orchestration.tools.mcp_tools._MCP_URL", "http://test:8100/mcp"
            ),
            patch(
                "core.application.orchestration.tools.mcp_tools._fs_mcp_bearer_token",
                return_value="session-token-123",
            ),
            patch(
                "core.application.orchestration.tools.mcp_tools._CLIENT_MODERN_MCP_CALL",
                new=AsyncMock(return_value="ok"),
            ) as modern_call,
        ):
            _call_mcp_endpoint("http://test:8100/mcp", "project_list", {})

        headers = modern_call.await_args.kwargs["headers"]
        assert headers.get("Authorization") == "Bearer session-token-123"

    def test_external_mcp_request_does_not_include_platform_bearer_header(self):
        try:
            from core.application.orchestration.tools.mcp_tools import _call_mcp_endpoint
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        with (
            patch(
                "core.application.orchestration.tools.mcp_tools._MCP_URL",
                "http://flow-steward.example/mcp",
            ),
            patch(
                "core.application.orchestration.tools.mcp_tools._fs_mcp_bearer_token",
                return_value="session-token-123",
            ),
            patch(
                "core.application.orchestration.tools.mcp_tools._CLIENT_MODERN_MCP_CALL",
                new=AsyncMock(return_value="ok"),
            ) as modern_call,
        ):
            _call_mcp_endpoint(
                "https://docsmcp.example/mcp", "resolve-library-id", {"libraryName": "fastapi"}
            )

        headers = modern_call.await_args.kwargs["headers"]
        assert "Authorization" not in headers


class TestMCPAuthBootstrap:
    def test_in_process_bootstrap_supports_uuid_roles_and_default_account_membership(self):
        from core.application.orchestration.tools.mcp_tools import (
            _CLIENT_SEED_BOOTSTRAP_USER,
        )

        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.__exit__.return_value = False
        cursor.fetchone.side_effect = [
            (0,),
            (
                "10000000-0000-0000-0000-000000000001",
                "e2e_admin@example.com",
                "E2E Admin",
            ),
            ("20000000-0000-0000-0000-000000000001",),
            ("00000000-0000-0000-0000-000000000002",),
        ]
        logger = MagicMock()
        logger.conn.cursor.return_value = cursor

        @contextmanager
        def transaction(_logger):
            try:
                yield logger.conn
            except Exception:
                logger.conn.rollback()
                raise
            else:
                logger.conn.commit()

        with (
            patch(
                "core.application.orchestration.tools.mcp_tools_client._get_platform_auth_logger",
                return_value=logger,
            ),
            patch("core.application.auth.resolve.auth_tables_available", return_value=True),
            patch("core.application.auth.resolve.seed_default_roles"),
            patch("core.application.auth.password.password_hash", return_value="password-hash"),
            patch(
                "core.infrastructure.runtime.entrypoint_db_adapters.database_transaction",
                side_effect=transaction,
            ),
        ):
            assert _CLIENT_SEED_BOOTSTRAP_USER(
                "e2e_admin@example.com",
                "protected-password",
                display_name="E2E Admin",
            )

        statements = [
            (" ".join(call.args[0].split()), call.args[1] if len(call.args) > 1 else None)
            for call in cursor.execute.call_args_list
        ]
        assert any(
            "INSERT INTO user_roles" in query
            and params
            == (
                "10000000-0000-0000-0000-000000000001",
                "20000000-0000-0000-0000-000000000001",
            )
            for query, params in statements
        )
        assert any(
            "INSERT INTO user_account_memberships" in query
            and params
            == (
                "10000000-0000-0000-0000-000000000001",
                "00000000-0000-0000-0000-000000000002",
            )
            for query, params in statements
        )
        logger.conn.commit.assert_called_once()
        logger.conn.rollback.assert_not_called()

    def test_candidate_platform_mcp_urls_include_localhost_fallback(self, monkeypatch):
        try:
            import core.application.orchestration.tools.mcp_tools as mcp_tools
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        monkeypatch.setattr(mcp_tools, "_MCP_URL", "http://fs-mcp:8100/mcp")
        monkeypatch.delenv("MCP_URL", raising=False)

        candidates = mcp_tools._candidate_platform_mcp_urls()
        assert candidates[0] == "http://fs-mcp:8100/mcp"
        assert "http://localhost:8100/mcp" in candidates

    def test_candidate_platform_base_urls_prefers_env_then_internal_fallback(self, monkeypatch):
        try:
            import core.application.orchestration.tools.mcp_tools as mcp_tools
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        monkeypatch.setattr(mcp_tools, "_APP_BASE_URL", "https://localhost:8099")
        monkeypatch.setenv("FS_INTERNAL_BASE_URL", "https://fs-app:8099")
        assert mcp_tools._candidate_platform_base_urls() == [
            "https://localhost:8099",
            "http://localhost:8099",
            "https://fs-app:8099",
            "http://fs-app:8099",
        ]

    def test_bootstrap_prefers_explicit_env_token(self, monkeypatch):
        try:
            import core.application.orchestration.tools.mcp_tools as mcp_tools
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        mcp_tools._clear_fs_mcp_bearer_token_cache()
        monkeypatch.setenv("MCP_BEARER_TOKEN", "explicit-token")
        with patch(
            "core.application.orchestration.tools.mcp_tools._resolve_backend_admin_creds",
            return_value=None,
        ):
            assert mcp_tools._fs_mcp_bearer_token() == "explicit-token"
        mcp_tools._clear_fs_mcp_bearer_token_cache()
        monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)

    def test_bootstrap_prefers_explicit_token_over_local_session_when_creds_exist(
        self, monkeypatch
    ):
        try:
            import core.application.orchestration.tools.mcp_tools as mcp_tools
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        class _FakeCreds:
            username = "e2e_admin@example.com"
            password = "secret"
            display_name = "E2E Admin"

        mcp_tools._clear_fs_mcp_bearer_token_cache()
        monkeypatch.setenv("MCP_BEARER_TOKEN", "explicit-token")
        with (
            patch(
                "core.application.orchestration.tools.mcp_tools._resolve_backend_admin_creds",
                return_value=_FakeCreds(),
            ),
            patch(
                "core.application.orchestration.tools.mcp_tools._bootstrap_fs_session_token",
                return_value="session-from-db",
            ),
        ):
            assert mcp_tools._fs_mcp_bearer_token() == "explicit-token"

        mcp_tools._clear_fs_mcp_bearer_token_cache()
        monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)

    def test_bootstrap_force_refresh_keeps_explicit_env_token(self, monkeypatch):
        try:
            import core.application.orchestration.tools.mcp_tools as mcp_tools
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        class _FakeCreds:
            username = "e2e_admin@example.com"
            password = "secret"
            display_name = "E2E Admin"

        mcp_tools._clear_fs_mcp_bearer_token_cache()
        monkeypatch.setenv("MCP_BEARER_TOKEN", "stale-explicit-token")
        with (
            patch(
                "core.application.orchestration.tools.mcp_tools._resolve_backend_admin_creds",
                return_value=_FakeCreds(),
            ),
            patch(
                "core.application.orchestration.tools.mcp_tools._bootstrap_fs_session_token",
                return_value="session-from-db",
            ),
        ):
            assert mcp_tools._fs_mcp_bearer_token(force_refresh=True) == "stale-explicit-token"

        mcp_tools._clear_fs_mcp_bearer_token_cache()
        monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)

    def test_sensitive_platform_mcp_tool_requires_explicit_bearer(self, monkeypatch):
        try:
            import core.application.orchestration.tools.mcp_tools as mcp_tools
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        mcp_tools._clear_fs_mcp_bearer_token_cache()
        monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("FS_SESSION_TOKEN", raising=False)
        with (
            patch(
                "core.application.orchestration.tools.mcp_tools._candidate_platform_mcp_urls",
                return_value=["http://fs-mcp:8100/mcp"],
            ),
            patch("core.application.orchestration.tools.mcp_tools.httpx.post") as post,
            pytest.raises(RuntimeError, match="Explicit MCP bearer token is required"),
        ):
            mcp_tools._call_platform_mcp("job_cancel", {"job_id": "job-1"})

        post.assert_not_called()

    @pytest.mark.parametrize(
        "tool_name",
        [
            "project_list",
            "workflow_trigger",
            "project_jobs",
            "project_notification_deliveries",
            "dispatch_project_notification",
        ],
    )
    def test_bootstrap_auth_denied_for_read_and_notification_wrappers_without_bearer(
        self, monkeypatch, tool_name
    ):
        """No explicit caller bearer => platform-privileged MCP wrappers are denied.

        Regression for FS-DEEP-CAND-003/025/026/027/028/055: read-only,
        project-read, workflow, and notification wrappers previously fell back to
        bootstrapped backend/admin credentials when no caller token was present.
        They must now fail closed and never dispatch to the platform MCP.
        """
        try:
            import core.application.orchestration.tools.mcp_tools as mcp_tools
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        assert tool_name in mcp_tools._CALLER_AUTH_REQUIRED_MCP_TOOLS

        mcp_tools._clear_fs_mcp_bearer_token_cache()
        monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("FS_SESSION_TOKEN", raising=False)
        with (
            patch(
                "core.application.orchestration.tools.mcp_tools._candidate_platform_mcp_urls",
                return_value=["http://fs-mcp:8100/mcp"],
            ),
            # If bootstrap were attempted, this would mint a token — assert it is NOT.
            patch(
                "core.application.orchestration.tools.mcp_tools._fs_mcp_bearer_token",
                return_value="bootstrapped-admin-token",
            ) as bearer,
            patch("core.application.orchestration.tools.mcp_tools.httpx.post") as post,
            pytest.raises(RuntimeError, match="Explicit MCP bearer token is required"),
        ):
            mcp_tools._call_platform_mcp(tool_name, {"project_name": "Flow Steward App"})

        post.assert_not_called()
        bearer.assert_not_called()

    @pytest.mark.parametrize(
        "tool_name",
        ["project_list", "workflow_list", "project_jobs", "dispatch_project_notification"],
    )
    def test_explicit_bearer_allows_read_and_notification_wrappers(self, monkeypatch, tool_name):
        """A caller WITH an explicit bearer still reaches the platform MCP sink."""
        try:
            import core.application.orchestration.tools.mcp_tools as mcp_tools
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        mcp_tools._clear_fs_mcp_bearer_token_cache()
        monkeypatch.setenv("MCP_BEARER_TOKEN", "caller-supplied-token")
        monkeypatch.delenv("FS_SESSION_TOKEN", raising=False)
        with (
            patch(
                "core.application.orchestration.tools.mcp_tools._candidate_platform_mcp_urls",
                return_value=["http://fs-mcp:8100/mcp"],
            ),
            patch(
                "core.application.orchestration.tools.mcp_tools._call_mcp_endpoint",
                return_value="ok",
            ) as endpoint,
        ):
            result = mcp_tools._call_platform_mcp(tool_name, {"project_name": "Flow Steward App"})

        assert result == "ok"
        endpoint.assert_called_once()
        mcp_tools._clear_fs_mcp_bearer_token_cache()

    def test_bootstrap_prefers_direct_db_session_before_http_login(self):
        try:
            import core.application.orchestration.tools.mcp_tools as mcp_tools
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        class _FakeCreds:
            username = "e2e_admin"
            password = "secret"

        mcp_tools._clear_fs_mcp_bearer_token_cache()
        with (
            patch.dict(os.environ, {"MCP_BEARER_TOKEN": ""}, clear=False),
            patch(
                "core.application.orchestration.tools.mcp_tools._resolve_backend_admin_creds",
                return_value=_FakeCreds(),
            ),
            patch(
                "core.application.orchestration.tools.mcp_tools._bootstrap_fs_session_token",
                return_value="session-from-db",
            ),
            patch("core.application.orchestration.tools.mcp_tools.httpx.Client") as mock_client,
        ):
            assert mcp_tools._fs_mcp_bearer_token() == "session-from-db"
        mock_client.assert_not_called()
        mcp_tools._clear_fs_mcp_bearer_token_cache()

    def test_db_session_bootstrap_commits_inserted_auth_session(self):
        try:
            import core.application.orchestration.tools.mcp_tools as mcp_tools
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        fake_cursor = MagicMock()
        fake_conn = MagicMock()
        fake_conn.cursor.return_value.__enter__.return_value = fake_cursor
        fake_bl = MagicMock(conn=fake_conn)
        fake_user = {
            "id": 42,
            "account_id": "00000000-0000-0000-0000-000000000002",
            "is_active": True,
            "must_change_password": False,
            "password_hash": "stored-hash",
        }

        with (
            patch(
                "core.application.orchestration.tools.mcp_tools._get_platform_auth_logger",
                return_value=fake_bl,
            ),
            patch(
                "core.application.auth.resolve.auth_tables_available",
                return_value=True,
            ),
            patch(
                "core.application.auth.resolve.get_user_by_username",
                return_value=fake_user,
            ),
            patch(
                "core.application.auth.password.verify_password",
                return_value=True,
            ),
            patch(
                "core.application.auth.password.session_token_hash",
                return_value="hashed-session-token",
            ),
        ):
            token = mcp_tools._bootstrap_fs_session_token("e2e_admin", "secret")

        assert token
        fake_conn.commit.assert_called_once()
        fake_conn.rollback.assert_not_called()
        executed_sql = fake_cursor.execute.call_args_list[0].args[0]
        assert "INSERT INTO auth_sessions" in executed_sql
        assert "account_id" in executed_sql
        assert fake_cursor.execute.call_args_list[0].args[1][1] == (
            "00000000-0000-0000-0000-000000000002"
        )

    def test_bootstrap_seeds_local_auth_user_before_http_login(self):
        try:
            import core.application.orchestration.tools.mcp_tools as mcp_tools
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        class _FakeCreds:
            username = "e2e_admin@example.com"
            password = "secret"
            display_name = "E2E Admin"

        mcp_tools._clear_fs_mcp_bearer_token_cache()
        with (
            patch.dict(os.environ, {"MCP_BEARER_TOKEN": ""}, clear=False),
            patch(
                "core.application.orchestration.tools.mcp_tools._resolve_backend_admin_creds",
                return_value=_FakeCreds(),
            ),
            patch(
                "core.application.orchestration.tools.mcp_tools._bootstrap_fs_session_token",
                side_effect=["", "session-after-seed"],
            ) as mock_bootstrap,
            patch(
                "core.application.orchestration.tools.mcp_tools._seed_local_auth_bootstrap_user",
                return_value=True,
            ) as mock_seed,
            patch("core.application.orchestration.tools.mcp_tools.httpx.Client") as mock_client,
        ):
            assert mcp_tools._fs_mcp_bearer_token() == "session-after-seed"

        mock_seed.assert_called_once_with(
            "e2e_admin@example.com",
            "secret",
            display_name="E2E Admin",
        )
        assert mock_bootstrap.call_count == 2
        mock_client.assert_not_called()
        mcp_tools._clear_fs_mcp_bearer_token_cache()

    def test_bootstrap_falls_back_to_internal_base_url_after_primary_failure(self, monkeypatch):
        try:
            import httpx

            import core.application.orchestration.tools.mcp_tools as mcp_tools
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        class _FakeCreds:
            username = "e2e_admin"
            password = "secret"

        class _FakeClient:
            def __init__(self, *args, **kwargs):
                self.cookies = {}
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, url, json=None, headers=None):
                self.calls.append(url)
                if url.startswith(("http://localhost:8099", "https://localhost:8099")):
                    raise httpx.ConnectError("connection refused")
                self.cookies["fs_session"] = "session-from-fallback"
                return _mock_httpx_response({"user": {"username": "e2e_admin"}})

        fake_client = _FakeClient()
        monkeypatch.setattr(mcp_tools, "_APP_BASE_URL", "https://localhost:8099")
        monkeypatch.setenv("FS_INTERNAL_BASE_URL", "https://fs-app:8099")
        mcp_tools._clear_fs_mcp_bearer_token_cache()
        with (
            patch.dict(os.environ, {"MCP_BEARER_TOKEN": ""}, clear=False),
            patch(
                "core.application.orchestration.tools.mcp_tools._resolve_backend_admin_creds",
                return_value=_FakeCreds(),
            ),
            patch(
                "core.application.orchestration.tools.mcp_tools._bootstrap_fs_session_token",
                return_value="",
            ),
            patch(
                "core.application.orchestration.tools.mcp_tools.httpx.Client",
                return_value=fake_client,
            ),
        ):
            assert mcp_tools._fs_mcp_bearer_token() == "session-from-fallback"
        assert fake_client.calls == [
            "https://localhost:8099/api/auth/login",
            "http://localhost:8099/api/auth/login",
            "https://fs-app:8099/api/auth/login",
        ]
        mcp_tools._clear_fs_mcp_bearer_token_cache()

    def test_call_retries_mcp_request_after_401_refresh(self):
        try:
            import core.application.orchestration.tools.mcp_tools as mcp_tools
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        calls: list[dict[str, str]] = []

        async def modern_call(_url, _tool_name, _arguments, *, headers):
            calls.append(dict(headers))
            if len(calls) == 1:
                raise _httpx2_status_error(401)
            return "ok"

        with (
            patch(
                "core.application.orchestration.tools.mcp_tools._MCP_URL", "http://test:8100/mcp"
            ),
            patch(
                "core.application.orchestration.tools.mcp_tools._fs_mcp_bearer_token",
                side_effect=["stale-token", "fresh-token"],
            ) as mock_token,
            patch(
                "core.application.orchestration.tools.mcp_tools._CLIENT_MODERN_MCP_CALL",
                side_effect=modern_call,
            ),
        ):
            result = mcp_tools._call_mcp_endpoint("http://test:8100/mcp", "project_list", {})

        assert result == "ok"
        assert len(calls) == 2
        assert calls[0].get("Authorization") == "Bearer stale-token"
        assert calls[1].get("Authorization") == "Bearer fresh-token"
        assert mock_token.call_count == 2

    def test_real_v2_transport_retries_401_with_refreshed_bearer(self, monkeypatch):
        import importlib

        import httpx2

        from core.application.orchestration.tools import mcp_tools, mcp_tools_client

        mcp_tools_client = importlib.reload(mcp_tools_client)

        requests: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            if len(requests) == 1:
                return httpx2.Response(401, request=request)
            payload = json.loads(request.content)
            if payload["method"] == "tools/list":
                result = {
                    "resultType": "complete",
                    "cacheScope": "private",
                    "ttlMs": 0,
                    "tools": [
                        {
                            "name": "project_list",
                            "inputSchema": {"type": "object"},
                        }
                    ],
                }
            else:
                result = {
                    "resultType": "complete",
                    "content": [{"type": "text", "text": "ok"}],
                    "isError": False,
                }
            return httpx2.Response(
                200,
                request=request,
                headers={"content-type": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": result,
                },
            )

        real_async_client = httpx2.AsyncClient
        transport = httpx2.MockTransport(handler)
        monkeypatch.setattr(
            mcp_tools_client.httpx2,
            "AsyncClient",
            lambda **kwargs: real_async_client(transport=transport, **kwargs),
        )
        monkeypatch.setattr(mcp_tools, "_MCP_URL", "http://test:8100/mcp")
        monkeypatch.setattr(
            mcp_tools,
            "_CLIENT_MODERN_MCP_CALL",
            mcp_tools_client._call_modern_mcp_endpoint,
        )
        tokens = iter(("stale-token", "fresh-token"))
        monkeypatch.setattr(
            mcp_tools,
            "_fs_mcp_bearer_token",
            lambda **_kwargs: next(tokens),
        )
        reset = MagicMock()
        monkeypatch.setattr(mcp_tools, "_reset_fs_mcp_bearer_token_cache", reset)

        result = mcp_tools._call_mcp_endpoint(
            "http://test:8100/mcp",
            "project_list",
            {},
        )

        assert result == "ok"
        assert [request.headers["authorization"] for request in requests[:2]] == [
            "Bearer stale-token",
            "Bearer fresh-token",
        ]
        assert all(
            request.headers["authorization"] == "Bearer fresh-token" for request in requests[1:]
        )
        reset.assert_called_once_with()

    def test_call_fails_fast_when_platform_auth_token_is_unavailable(self):
        try:
            import core.application.orchestration.tools.mcp_tools as mcp_tools
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        with (
            patch(
                "core.application.orchestration.tools.mcp_tools._MCP_URL", "http://test:8100/mcp"
            ),
            patch(
                "core.application.orchestration.tools.mcp_tools._fs_mcp_bearer_token",
                return_value="",
            ),
            patch(
                "core.application.orchestration.tools.mcp_tools._CLIENT_MODERN_MCP_CALL",
                new=AsyncMock(),
            ) as modern_call,
            pytest.raises(RuntimeError, match="auth token unavailable"),
        ):
            mcp_tools._call_mcp_endpoint("http://test:8100/mcp", "project_list", {})

        modern_call.assert_not_awaited()

    def test_call_platform_mcp_does_not_replay_tools_call_on_fallback_endpoint(self):
        try:
            import core.application.orchestration.tools.mcp_tools as mcp_tools
        except ImportError:
            pytest.skip("core.application.orchestration.tools.mcp_tools not importable")

        calls: list[dict] = []

        async def modern_call(url, _tool_name, _arguments, *, headers):
            calls.append({"url": url, "headers": dict(headers or {})})
            import httpx2

            raise httpx2.ConnectError("name or service not known")

        with (
            patch(
                "core.application.orchestration.tools.mcp_tools._candidate_platform_mcp_urls",
                return_value=["http://fs-mcp:8100/mcp", "http://localhost:8100/mcp"],
            ),
            patch(
                "core.application.orchestration.tools.mcp_tools._fs_mcp_bearer_token",
                return_value="session-token-123",
            ),
            patch(
                "core.application.orchestration.tools.mcp_tools._explicit_fs_mcp_bearer_token",
                return_value="session-token-123",
            ),
            patch(
                "core.application.orchestration.tools.mcp_tools._CLIENT_MODERN_MCP_CALL",
                side_effect=modern_call,
            ),
            pytest.raises(Exception, match="name or service not known"),
        ):
            mcp_tools._call_platform_mcp("project_list", {})

        assert [item["url"] for item in calls] == ["http://fs-mcp:8100/mcp"]


# ---------------------------------------------------------------------------
# Tests: Plain (non-tool) functions
# ---------------------------------------------------------------------------


class TestPlainMCPFunctions:
    """Tests for plain (non-@tool) MCP functions."""

    def test_project_list_calls_platform_mcp(self):
        try:
            from core.application.orchestration.tools.mcp_tools import project_list
        except ImportError:
            pytest.skip("project_list not available")

        with patch(
            "core.application.orchestration.tools.mcp_tools._call_platform_mcp"
        ) as mock_call:
            mock_call.return_value = '["Flow Steward", "TestProject"]'
            project_list.func()

        mock_call.assert_called_once()
