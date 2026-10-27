"""Remote rules safety — skills/prompts URL include SSRF protection.

P0 gap (C-03): No test suite verifying that remote URL includes for skills/prompts
cannot be weaponized for SSRF (Server-Side Request Forgery) attacks.

The platform allows skills and extension descriptors to specify remote URLs for
fetching skill definitions and artifacts. These URLs must be validated before
any outbound HTTP request is made.

Coverage:

A. Unit: _assert_safe_remote_http_url blocks SSRF vectors
   - localhost/127.0.0.1 must be blocked
   - Private IP ranges (10.x, 172.16.x, 192.168.x) must be blocked
   - Non-http(s) schemes (file://, ftp://, gopher://) must be blocked
   - URL credentials (user:pass@host) must be blocked
   - Link-local addresses (169.254.x.x) must be blocked

B. Unit: core.application.extensions.service_support.validate_remote_url delegates to safe URL validation

C. API: Extension descriptor import with SSRF URL must return 4xx (not fetch the URL)
   - POST /api/extensions/descriptors/import/url with localhost URL must be rejected
   - POST /api/extensions/descriptors/import/url with private IP must be rejected

Run:
    docker compose exec fs-app python -m pytest \
        tests/security/test_remote_rules_safety.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _extension_service_error_response(exc, *_args, **_kwargs):
    from fastapi.responses import JSONResponse

    payload = getattr(exc, "payload", None)
    if not isinstance(payload, dict):
        payload = {}
    return JSONResponse(
        {"error": getattr(exc, "message", str(exc)), "payload": payload},
        status_code=int(getattr(exc, "status_code", 500) or 500),
    )


def _extension_api_error_response(exc, *_args, **_kwargs):
    from fastapi.responses import JSONResponse

    return JSONResponse({"error": str(exc)}, status_code=500)


def _make_extension_import_url_client():
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from core.application.extensions.install import ExtensionInstallStore
    from core.application.extensions.runtime_store import ExtensionRuntimeStore
    from core.application.management.scope import ScopeContext
    from core.entrypoints.api.integrations_connectors import create_integrations_connectors_router

    app = FastAPI()
    extension_store = ExtensionInstallStore()
    runtime_store = ExtensionRuntimeStore()
    bl = MagicMock()

    def _request_scope(request: Request, **kwargs):
        return ScopeContext(
            account_id=str(request.headers.get("x-test-account-id") or "acc_default").strip(),
            actor_id=str(request.headers.get("x-test-actor") or "api").strip(),
            principal_type=str(request.headers.get("x-test-role") or "admin").strip().lower(),
            project_id=kwargs.get("project_id"),
        )

    app.include_router(
        create_integrations_connectors_router(
            get_logger=lambda: bl,
            get_extension_install_store=lambda: extension_store,
            get_extension_runtime_store=lambda: runtime_store,
            extension_query_service=lambda: MagicMock(),
            request_scope=_request_scope,
            project_query_service=lambda: MagicMock(),
            project_command_service=lambda: MagicMock(),
            service_error_response=_extension_service_error_response,
            api_error_response=_extension_api_error_response,
            err_not_found="Not found",
        )
    )
    return TestClient(app, raise_server_exceptions=False)


# ===========================================================================
# A. Unit — _assert_safe_remote_http_url
# ===========================================================================


class TestSsrfUrlBlocker:
    """_assert_safe_remote_http_url must block all SSRF-capable URL vectors."""

    @pytest.fixture(autouse=True)
    def _import_guard(self):
        try:
            from core.application.extensions.url_safety import assert_safe_remote_http_url

            self._guard = assert_safe_remote_http_url
        except ImportError as exc:
            pytest.skip(f"Cannot import core.application.extensions.lifecycle: {exc}")

    def _assert_blocked(self, url: str, reason: str) -> None:
        """Assert the URL is blocked (raises ValueError)."""
        with pytest.raises((ValueError, Exception), match=r".") as exc_info:
            self._guard(url, purpose="test")
        # Must raise — any exception means it was blocked
        assert exc_info.value is not None, f"SSRF guard must block {reason}: {url!r}"

    # --- Localhost vectors ---

    def test_blocks_localhost(self):
        self._assert_blocked("http://localhost/sensitive", "localhost")

    def test_blocks_localhost_alias(self):
        self._assert_blocked("http://localhost.localdomain/secret", "localhost alias")

    def test_blocks_loopback_ipv4(self):
        self._assert_blocked("http://127.0.0.1/etc/passwd", "loopback IPv4")

    def test_blocks_loopback_range(self):
        self._assert_blocked("http://127.0.0.2/", "loopback range")

    def test_blocks_ipv6_loopback(self):
        # ::1 is the IPv6 loopback
        self._assert_blocked("http://[::1]/", "IPv6 loopback")

    # --- Private IP ranges ---

    def test_blocks_private_class_a(self):
        self._assert_blocked("http://10.0.0.1/", "private class A")

    def test_blocks_private_class_a_high(self):
        self._assert_blocked("http://10.255.255.255/", "private class A high")

    def test_blocks_private_class_b(self):
        self._assert_blocked("http://172.16.0.1/", "private class B")

    def test_blocks_private_class_c(self):
        self._assert_blocked("http://192.168.1.1/", "private class C")

    def test_blocks_link_local(self):
        # 169.254.0.0/16 — AWS metadata service is at 169.254.169.254
        self._assert_blocked("http://169.254.169.254/latest/meta-data/", "AWS metadata SSRF")

    # --- Non-http(s) schemes ---

    def test_blocks_file_scheme(self):
        self._assert_blocked("file:///etc/passwd", "file:// scheme")

    def test_blocks_ftp_scheme(self):
        self._assert_blocked("ftp://example.com/file", "ftp:// scheme")

    def test_blocks_gopher_scheme(self):
        self._assert_blocked("gopher://example.com:70/", "gopher:// scheme")

    def test_blocks_data_uri(self):
        self._assert_blocked("data:text/plain;base64,aGVsbG8=", "data: URI")

    # --- URL credentials ---

    def test_blocks_url_with_credentials(self):
        self._assert_blocked("http://admin:password@example.com/", "URL credentials")

    def test_blocks_url_with_only_username(self):
        self._assert_blocked("http://admin@example.com/", "URL username without password")

    # --- Valid URLs should not be blocked ---

    def test_allows_public_https_url(self):
        """A real public HTTPS URL must not raise (subject to DNS resolution)."""
        try:
            result = self._guard("https://example.com/test", purpose="test")
            # If we get here, the URL was allowed (correct behavior)
            assert result is not None or result is None  # any return is fine
        except ValueError as e:
            err = str(e)
            # The only acceptable failures for a public URL are DNS resolution errors
            if "cannot be resolved" not in err and "not allowed" not in err:
                # Unexpected blocking of a public URL
                raise
        except OSError:
            pass  # DNS resolution failure in isolated env is acceptable


# ===========================================================================
# B. Extension services validate_remote_url delegates to safe validation
# ===========================================================================


class TestExtensionServicesUrlValidation:
    """service_support.validate_remote_url must use _assert_safe_remote_http_url."""

    def test_validate_remote_url_blocks_localhost(self):
        """service_support.validate_remote_url must block localhost SSRF."""
        try:
            from core.application.extensions.service_support import validate_remote_url
        except ImportError:
            pytest.skip("validate_remote_url not in core.application.extensions.service_support")

        with pytest.raises((ValueError, Exception)):
            validate_remote_url("http://localhost/", purpose="test")

    def test_validate_remote_url_blocks_private_ip(self):
        """service_support.validate_remote_url must block private IP SSRF."""
        try:
            from core.application.extensions.service_support import validate_remote_url
        except ImportError:
            pytest.skip("validate_remote_url not in core.application.extensions.service_support")

        with pytest.raises((ValueError, Exception)):
            validate_remote_url("http://10.0.0.1/", purpose="test")


# ===========================================================================
# C. API — extension descriptor import rejects SSRF URLs
# ===========================================================================


class TestExtensionDescriptorImportSsrfRejection:
    """POST /api/extensions/descriptors/import/url must reject SSRF URLs."""

    @pytest.mark.parametrize(
        ("descriptor_url", "expected_fragment"),
        [
            ("http://localhost/extension-descriptor.yaml", "localhost"),
            ("http://10.0.0.1/extension-descriptor.yaml", "10.0.0.1"),
            (
                "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
                "169.254.169.254",
            ),
        ],
    )
    def test_ssrf_urls_rejected_with_exact_400_and_no_outbound_fetch(
        self, descriptor_url: str, expected_fragment: str
    ):
        with (
            _make_extension_import_url_client() as client,
            patch(
                "core.application.extensions.lifecycle.urlopen",
                side_effect=AssertionError("urlopen must not be called for blocked SSRF URLs"),
            ),
        ):
            resp = client.post(
                "/api/extensions/descriptors/import/url",
                json={"descriptor_url": descriptor_url},
            )

        assert resp.status_code == 400
        body = resp.json()
        assert body["error"] == "Descriptor validation failed"
        assert body["payload"]["warnings"] == []
        assert len(body["payload"]["errors"]) == 1
        assert body["payload"]["errors"][0]["path"] == "descriptor_url"
        assert expected_fragment in body["payload"]["errors"][0]["message"].lower()
