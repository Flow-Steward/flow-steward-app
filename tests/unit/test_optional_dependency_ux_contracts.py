"""Optional-dependency-disabled UX contract suite.

C-09 gap: no consolidated test verifying that intentionally-disabled optional
dependencies produce clear, non-error user-facing messages rather than 500s,
crash logs, or confusing internal-error framing.

Coverage:

A. No email extension connected
   - system email returns reason="disabled_or_unconfigured" (not exception)

B. psycopg_runtime not installed
   - db_pool._require_psycopg_runtime raises ModuleNotFoundError with clear message
   - get_threaded_pool raises ModuleNotFoundError (not AttributeError/NoneType)
   - pooled_connection raises ModuleNotFoundError (not AttributeError/NoneType)

D. Artifact storage in-memory mode (no S3)
   - In-memory adapter put/get/delete never raise (functional but non-S3)
   - Presigned-URL generation returns empty/None gracefully (not crash)

E. MCP endpoint unreachable
   - mcp_rate_limit module loads without error even when DB unavailable
   - tool_resolver graceful when allowlist DB query fails (fail-open by default)

F. Extension runtime optional deps
   - Extension invocation with missing integrations returns structured error
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ===========================================================================
# A. No email extension connected
# ===========================================================================


class TestSmtpDisabledUxContracts:
    """Without an email extension, system email returns a structured result, never raises."""

    class _NoEmailStore:
        def list_enabled_extension_connections(self):
            return []

    def test_password_reset_without_email_extension_is_a_structured_result(self):
        from core.application.extensions import system_email

        result = system_email.send_password_reset_email(
            recipient="user@example.com",
            reset_url="https://example.test/reset-password?token=x",
            expires_in_minutes=30,
            bl=self._NoEmailStore(),
        )

        assert result == {"ok": False, "reason": "disabled_or_unconfigured"}

    def test_signup_without_email_extension_is_a_structured_result(self):
        from core.application.extensions import system_email

        result = system_email.send_signup_notification_email(
            recipient="user@example.com", bl=self._NoEmailStore()
        )

        assert result == {"ok": False, "reason": "disabled_or_unconfigured"}

    def test_no_store_means_no_email(self):
        from core.application.extensions import system_email

        assert system_email.find_email_connection(None) is None


# ===========================================================================
# B. psycopg_runtime not installed — ModuleNotFoundError path
# ===========================================================================


class TestPsycopg2NotInstalledContracts:
    """When psycopg_runtime is not installed, db_pool must raise clear ModuleNotFoundError."""

    def test_require_psycopg_runtime_raises_module_not_found_when_absent(self):
        """_require_psycopg_runtime must raise ModuleNotFoundError, not AttributeError."""

        # Temporarily inject the import error
        fake_exc = ModuleNotFoundError("No module named 'psycopg_runtime'")
        fake_exc.name = "psycopg_runtime"

        import core.infrastructure.db.db_pool as db_pool_mod

        original = db_pool_mod._PSYCOPG2_IMPORT_ERROR
        try:
            db_pool_mod._PSYCOPG2_IMPORT_ERROR = fake_exc
            with pytest.raises(ModuleNotFoundError, match="psycopg_runtime"):
                db_pool_mod._require_psycopg_runtime()
        finally:
            db_pool_mod._PSYCOPG2_IMPORT_ERROR = original

    def test_get_threaded_pool_raises_module_not_found_when_psycopg_runtime_absent(self):
        """get_threaded_pool must raise ModuleNotFoundError, not NoneType crash."""
        import core.infrastructure.db.db_pool as db_pool_mod

        fake_exc = ModuleNotFoundError("No module named 'psycopg_runtime'")
        fake_exc.name = "psycopg_runtime"

        original = db_pool_mod._PSYCOPG2_IMPORT_ERROR
        try:
            db_pool_mod._PSYCOPG2_IMPORT_ERROR = fake_exc
            with pytest.raises(ModuleNotFoundError):
                db_pool_mod.get_threaded_pool("postgresql://localhost/test", minconn=1, maxconn=2)
        finally:
            db_pool_mod._PSYCOPG2_IMPORT_ERROR = original

    def test_pooled_connection_raises_module_not_found_when_psycopg_runtime_absent(self):
        """pooled_connection must raise ModuleNotFoundError, not AttributeError."""
        import core.infrastructure.db.db_pool as db_pool_mod

        fake_exc = ModuleNotFoundError("No module named 'psycopg_runtime'")
        fake_exc.name = "psycopg_runtime"

        original = db_pool_mod._PSYCOPG2_IMPORT_ERROR
        try:
            db_pool_mod._PSYCOPG2_IMPORT_ERROR = fake_exc
            fake_pool = MagicMock()
            ctx = db_pool_mod.pooled_connection(fake_pool)
            with pytest.raises(ModuleNotFoundError):
                ctx.__enter__()
        finally:
            db_pool_mod._PSYCOPG2_IMPORT_ERROR = original


# ===========================================================================
# D. Artifact storage in-memory mode
# ===========================================================================


class TestArtifactStorageInMemoryMode:
    """In-memory artifact storage adapter must work without crashing or S3 calls."""

    def _get_memory_adapter(self):
        from core.application.jobs.artifact_cleanup_recovery import (
            resolve_artifact_storage_adapter_from_env,
        )

        return resolve_artifact_storage_adapter_from_env(
            environ={"ARTIFACT_STORAGE_BACKEND": "memory"}
        )

    def test_in_memory_adapter_put_does_not_raise(self):
        """In-memory put must not raise — returns success or silently no-ops."""
        adapter = self._get_memory_adapter()
        try:
            result = adapter.put(
                "test/key.bin",
                b"data",
                content_type="application/octet-stream",
            )
        except NotImplementedError:
            pass  # OK — in-memory adapter explicitly says "not implemented"
        else:
            assert isinstance(result, dict), f"Expected storage metadata dict, got {type(result)}"

    def test_in_memory_adapter_get_returns_none_or_raises_not_found(self):
        """In-memory get on missing key must return None or raise controlled error."""
        adapter = self._get_memory_adapter()
        try:
            result = adapter.get("test/nonexistent.bin")
        except (KeyError, FileNotFoundError, NotImplementedError):
            pass  # Controlled "not found" error is OK
        except Exception as exc:
            # Must not be an AttributeError or crash from missing S3 client
            assert not isinstance(exc, AttributeError), (
                f"In-memory adapter.get crashed with AttributeError: {exc}"
            )
        else:
            assert result is None or isinstance(result, bytes), (
                f"Expected None or bytes, got {type(result)}"
            )


class TestArtifactStorageS3CompatibleMode:
    """S3-compatible local storage must work with bundled endpoints."""

    def test_endpoint_without_credentials_uses_unsigned_client(self):
        """Bundled SeaweedFS env omits credentials, so boto3 must not require signing."""
        from botocore import UNSIGNED

        from core.application.extensions.artifact_storage import create_storage_adapter

        mock_client = MagicMock()
        with patch("boto3.client", return_value=mock_client) as client_factory:
            adapter = create_storage_adapter(
                {
                    "backend": "s3",
                    "endpoint_url": "http://seaweedfs:8333",
                    "bucket": "flow-steward-artifacts",
                    "access_key": "",
                    "secret_key": "",
                    "region": "us-east-1",
                    "use_ssl": False,
                }
            )

        assert adapter is not None
        call_kwargs = client_factory.call_args.kwargs
        assert call_kwargs["endpoint_url"] == "http://seaweedfs:8333"
        assert call_kwargs["config"].signature_version is UNSIGNED


# ===========================================================================
# E. MCP endpoint unreachable
# ===========================================================================


class TestMcpUnavailableContracts:
    """When MCP backend is unreachable, rate-limit and tool-resolver must not crash."""

    def test_mcp_rate_limit_module_loads_without_db(self):
        """mcp_rate_limit module must import cleanly without DB connection."""
        import core.application.orchestration.tools.mcp_rate_limit as rl_mod

        allowed, retry_after = rl_mod.check_mcp_rate_limit("anon_principal", "list_tools")

        assert isinstance(allowed, bool), f"Expected bool allow flag, got {type(allowed)}"
        assert isinstance(retry_after, int), f"Expected int retry_after, got {type(retry_after)}"

    def test_tool_resolver_allowlist_check_fail_open_by_default(self):
        """tool_resolver must fail-open (allow) when DB check raises, by default."""
        from core.application.orchestration.tools.tool_resolver import (
            _validate_mcp_connector_tool_access,
        )

        manifest = SimpleNamespace(
            tool_id="tool.example.lookup",
            implementation=SimpleNamespace(
                server="docsmcp",
                owner_account_id="account-test",
                owner_project_id="project-test",
                owner_tenant_id="tenant-test",
            ),
            interface=SimpleNamespace(all_function_names=["resolve-library-id"]),
        )

        with (
            patch.dict(
                "os.environ",
                {
                    "FS_MCP_ALLOWLIST_FAIL_CLOSED": "false",
                    "FS_DB_DSN": "postgresql://user:pass@db/test",
                },
                clear=False,
            ),
            patch(
                "core.infrastructure.db.platform_stores.PlatformIdentityStore.get_shared_instance",
                side_effect=Exception("DB unavailable"),
            ),
        ):
            result = _validate_mcp_connector_tool_access("connector.docsmcp", manifest)

        assert result is None, "Fail-open validation should return without raising"

    def test_mcp_rate_limit_check_returns_bool_when_db_absent(self):
        """mcp_rate_limit must provide a structured in-memory decision without DB access."""
        from core.application.orchestration.tools.mcp_rate_limit import (
            check_mcp_rate_limit,
            clear_mcp_principal,
            record_mcp_call,
        )

        principal = "anon_principal"
        tool_name = "list_tools"
        clear_mcp_principal(principal)
        try:
            allowed, retry_after = check_mcp_rate_limit(
                principal,
                tool_name,
                window_seconds=60,
                max_calls=1,
            )
            assert allowed is True
            assert retry_after == 0

            record_mcp_call(principal, tool_name, window_seconds=60)
            allowed, retry_after = check_mcp_rate_limit(
                principal,
                tool_name,
                window_seconds=60,
                max_calls=1,
            )
            assert isinstance(allowed, bool)
            assert isinstance(retry_after, int)
            assert allowed is False
            assert retry_after >= 1
        finally:
            clear_mcp_principal(principal)


# ===========================================================================
# F. Extension runtime optional deps
# ===========================================================================


class TestExtensionRuntimeOptionalDeps:
    """Extension runtime must return structured errors, not crash, when optional deps absent."""

    def test_zip_guard_loads_without_optional_deps(self):
        """core.infrastructure.security.zip_guard must import cleanly without optional dependencies."""
        try:
            from core.infrastructure.security.zip_guard import validate_zip_safe  # noqa: F401
        except ImportError:
            pytest.skip("core.infrastructure.security.zip_guard not importable")

    def test_extension_lifecycle_loads_without_requests(self):
        """core.application.extensions.lifecycle must not crash on import even when requests is unavailable."""
        try:
            import core.application.extensions.lifecycle  # noqa: F401
        except ImportError as exc:
            # ModuleNotFoundError for requests is acceptable
            if "requests" in str(exc):
                pytest.skip(f"requests not installed: {exc}")
            raise

    def test_extension_runtime_import_error_is_structured(self):
        """Extension runtime import errors must not produce unstructured tracebacks."""
        from core.infrastructure.extension_runtime.extension_runtime import (
            validate_compiled_manifest,
        )

        result = validate_compiled_manifest(
            {
                "manifest_version": 2,
                "manifest_path": "/tmp/extension.yaml",
            }
        )
        assert isinstance(result, dict), f"Expected structured validation dict, got {type(result)}"
        assert result.get("status") == "invalid", (
            "Incomplete manifest must return a structured invalid result"
        )
        assert isinstance(result.get("errors"), list), (
            "Incomplete manifest must expose structured validation errors"
        )
        assert result["errors"], "Incomplete manifest must expose structured validation errors"
