from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.application.auth import request_principal_runtime, serve_auth_runtime


@pytest.fixture(autouse=True)
def _isolate_bootstrap_state_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep bootstrap policy tests independent of the deployment environment."""
    monkeypatch.delenv("FS_AUTH_BOOTSTRAP_STATE_FILE", raising=False)


class _FakeRequest:
    def __init__(
        self,
        *,
        headers=None,
        cookies=None,
        client_host: str = "127.0.0.1",
        auth_user=None,
        auth_principal=None,
        base_url: str = "http://localhost:8099/",
    ) -> None:
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.client = SimpleNamespace(host=client_host)
        self.base_url = base_url
        self.state = SimpleNamespace(auth_user=auth_user, auth_principal=auth_principal)


class _FakePrincipal:
    def __init__(self, username: str = "alice") -> None:
        self.username = username

    def model_dump(self) -> dict[str, str]:
        return {"username": self.username}


def test_public_base_url_falls_back_to_first_trusted_origin(monkeypatch) -> None:
    monkeypatch.delenv("FS_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv(
        "FS_AUTH_TRUSTED_ORIGINS",
        "https://first.example.com, https://second.example.com",
    )

    assert serve_auth_runtime.public_base_url() == "https://first.example.com"


def test_csrf_request_valid_accepts_matching_header_and_cookie() -> None:
    request = _FakeRequest(
        headers={"x-csrf-token": "same"},
        cookies={"csrf_token": "same"},
    )

    assert (
        serve_auth_runtime.csrf_request_valid(
            request,
            csrf_header_name="x-csrf-token",
            csrf_cookie_name="csrf_token",
            same_origin_request_fn=lambda _request: False,
        )
        is True
    )


def test_same_origin_request_uses_origin_and_referer_fallback() -> None:
    assert (
        serve_auth_runtime.same_origin_request(
            _FakeRequest(headers={"origin": "https://app.example.com/path"}),
            trusted_auth_origins_fn=lambda: {"https://app.example.com"},
            expected_request_origin_fn=lambda request: "",
        )
        is True
    )
    assert (
        serve_auth_runtime.same_origin_request(
            _FakeRequest(headers={"referer": "https://app.example.com/path/page"}),
            trusted_auth_origins_fn=lambda: {"https://app.example.com"},
            expected_request_origin_fn=lambda request: "",
        )
        is True
    )


def test_expected_request_origin_prefers_valid_origin_scheme() -> None:
    assert (
        serve_auth_runtime.expected_request_origin(
            _FakeRequest(
                headers={
                    "host": "app.example.com",
                    "origin": "https://app.example.com",
                }
            ),
            auth_cookie_secure_fn=lambda: False,
        )
        == "https://app.example.com"
    )
    assert (
        serve_auth_runtime.expected_request_origin(
            _FakeRequest(
                headers={
                    "host": "localhost:8099",
                    "origin": "http://localhost:8099",
                }
            ),
            auth_cookie_secure_fn=lambda: True,
        )
        == "http://localhost:8099"
    )


def test_bootstrap_request_allowed_requires_token_or_localhost(monkeypatch) -> None:
    monkeypatch.setenv("FS_AUTH_BOOTSTRAP_TOKEN", "secret")
    monkeypatch.setenv("FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT", "2999-01-01T00:00:00+00:00")

    denied = serve_auth_runtime.bootstrap_request_allowed(
        _FakeRequest(headers={"x-bootstrap-token": "wrong"}, client_host="10.0.0.2"),
        env_flag=lambda name, default=False: True,
        bootstrap_token_header="x-bootstrap-token",
    )
    allowed = serve_auth_runtime.bootstrap_request_allowed(
        _FakeRequest(headers={"x-bootstrap-token": "secret"}, client_host="10.0.0.2"),
        env_flag=lambda name, default=False: True,
        bootstrap_token_header="x-bootstrap-token",
    )

    assert denied == (False, "Invalid bootstrap setup token")
    assert allowed == (True, "")


def test_bootstrap_request_fails_closed_when_authoritative_state_is_missing(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("FS_AUTH_BOOTSTRAP_STATE_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("FS_AUTH_BOOTSTRAP_TOKEN", "legacy-token")
    monkeypatch.setenv("FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT", "2999-01-01T00:00:00+00:00")

    result = serve_auth_runtime.bootstrap_request_allowed(
        _FakeRequest(headers={"x-bootstrap-token": "legacy-token"}, client_host="10.0.0.2"),
        env_flag=lambda name, default=False: True,
        bootstrap_token_header="x-bootstrap-token",
    )

    assert result == (False, "Bootstrap setup token state is missing or invalid")


def test_bootstrap_request_allowed_rejects_trusted_proxy_without_explicit_opt_in(
    monkeypatch,
) -> None:
    monkeypatch.delenv("FS_AUTH_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.setattr(
        serve_auth_runtime,
        "trusted_local_proxy_hosts",
        lambda: {"192.168.65.1"},
    )
    serve_auth_runtime.trusted_local_proxy_networks.cache_clear()

    denied = serve_auth_runtime.bootstrap_request_allowed(
        _FakeRequest(
            headers={
                "host": "localhost:8099",
                "origin": "https://localhost:8099",
                "referer": "https://localhost:8099/bootstrap",
            },
            client_host="192.168.65.1",
        ),
        env_flag=lambda name, default=False: name != "FS_AUTH_BOOTSTRAP_ALLOW_TRUSTED_PROXY",
        bootstrap_token_header="x-bootstrap-token",
    )

    assert denied == (False, "Bootstrap is restricted to localhost requests")


def test_trusted_local_proxy_hosts_includes_exact_compose_reverse_proxy(monkeypatch) -> None:
    resolved = {
        "host.docker.internal": "192.168.65.254",
        "gateway.docker.internal": "192.168.65.1",
        "traefik": "172.19.0.7",
    }
    monkeypatch.setattr(
        serve_auth_runtime.socket,
        "gethostbyname",
        lambda hostname: resolved[hostname],
    )
    monkeypatch.setattr(serve_auth_runtime, "_default_gateway_host", lambda: "172.19.0.1")
    serve_auth_runtime.trusted_local_proxy_hosts.cache_clear()

    assert serve_auth_runtime.trusted_local_proxy_hosts() == {
        "172.19.0.1",
        "172.19.0.7",
        "192.168.65.1",
        "192.168.65.254",
    }

    serve_auth_runtime.trusted_bootstrap_proxy_hosts.cache_clear()
    assert serve_auth_runtime.trusted_bootstrap_proxy_hosts() == {"172.19.0.7"}


def test_bootstrap_request_allowed_accepts_localhost_via_opted_in_trusted_proxy(
    monkeypatch,
) -> None:
    monkeypatch.delenv("FS_AUTH_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.setattr(
        serve_auth_runtime,
        "trusted_local_proxy_hosts",
        lambda: {"192.168.65.1"},
    )
    monkeypatch.setattr(
        serve_auth_runtime,
        "trusted_bootstrap_proxy_hosts",
        lambda: {"192.168.65.1"},
    )
    serve_auth_runtime.trusted_local_proxy_networks.cache_clear()

    allowed = serve_auth_runtime.bootstrap_request_allowed(
        _FakeRequest(
            headers={
                "x-forwarded-for": "127.0.0.1",
                "host": "localhost:8099",
                "origin": "https://localhost:8099",
                "referer": "https://localhost:8099/bootstrap",
            },
            client_host="192.168.65.1",
        ),
        env_flag=lambda name, default=False: True,
        bootstrap_token_header="x-bootstrap-token",
    )

    assert allowed == (True, "")


def test_bootstrap_request_allowed_rejects_remote_original_client_via_trusted_proxy(
    monkeypatch,
) -> None:
    monkeypatch.delenv("FS_AUTH_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.setattr(
        serve_auth_runtime,
        "trusted_local_proxy_hosts",
        lambda: {"192.168.65.1"},
    )

    denied = serve_auth_runtime.bootstrap_request_allowed(
        _FakeRequest(
            headers={
                "x-forwarded-for": "198.51.100.27",
                "host": "localhost:8099",
                "origin": "https://localhost:8099",
            },
            client_host="192.168.65.1",
        ),
        env_flag=lambda name, default=False: True,
        bootstrap_token_header="x-bootstrap-token",
    )

    assert denied == (False, "Bootstrap is restricted to localhost requests")


def test_bootstrap_request_allowed_rejects_forged_forwarding_from_untrusted_peer(
    monkeypatch,
) -> None:
    monkeypatch.delenv("FS_AUTH_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.setattr(
        serve_auth_runtime,
        "trusted_local_proxy_hosts",
        lambda: {"192.168.65.1"},
    )

    denied = serve_auth_runtime.bootstrap_request_allowed(
        _FakeRequest(
            headers={
                "x-forwarded-for": "127.0.0.1",
                "host": "localhost:8099",
            },
            client_host="203.0.113.8",
        ),
        env_flag=lambda name, default=False: True,
        bootstrap_token_header="x-bootstrap-token",
    )

    assert denied == (False, "Bootstrap is restricted to localhost requests")


def test_bootstrap_rejects_docker_gateway_that_is_not_the_exact_reverse_proxy(
    monkeypatch,
) -> None:
    monkeypatch.delenv("FS_AUTH_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.setattr(
        serve_auth_runtime,
        "trusted_local_proxy_hosts",
        lambda: {"192.168.65.1", "172.19.0.7"},
    )
    monkeypatch.setattr(
        serve_auth_runtime,
        "trusted_bootstrap_proxy_hosts",
        lambda: {"172.19.0.7"},
        raising=False,
    )

    denied = serve_auth_runtime.bootstrap_request_allowed(
        _FakeRequest(
            headers={"x-forwarded-for": "127.0.0.1"},
            client_host="192.168.65.1",
        ),
        env_flag=lambda name, default=False: True,
        bootstrap_token_header="x-bootstrap-token",
    )

    assert denied == (False, "Bootstrap is restricted to localhost requests")


@pytest.mark.parametrize(
    "forwarded_for",
    ("", "not-an-ip", "127.0.0.1, 192.168.65.1"),
)
def test_bootstrap_request_allowed_rejects_incomplete_proxy_client_chain(
    monkeypatch, forwarded_for: str
) -> None:
    monkeypatch.delenv("FS_AUTH_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.setattr(
        serve_auth_runtime,
        "trusted_local_proxy_hosts",
        lambda: {"192.168.65.1"},
    )

    denied = serve_auth_runtime.bootstrap_request_allowed(
        _FakeRequest(
            headers={
                "x-forwarded-for": forwarded_for,
                "host": "localhost:8099",
            },
            client_host="192.168.65.1",
        ),
        env_flag=lambda name, default=False: True,
        bootstrap_token_header="x-bootstrap-token",
    )

    assert denied == (False, "Bootstrap is restricted to localhost requests")


def test_bootstrap_request_allowed_accepts_localhost_via_trusted_proxy_subnet(monkeypatch) -> None:
    monkeypatch.delenv("FS_AUTH_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.setattr(
        serve_auth_runtime,
        "trusted_local_proxy_hosts",
        lambda: {"172.19.0.1"},
    )
    serve_auth_runtime.trusted_local_proxy_networks.cache_clear()

    denied = serve_auth_runtime.bootstrap_request_allowed(
        _FakeRequest(
            headers={
                "host": "localhost:8099",
                "origin": "https://localhost:8099",
                "referer": "https://localhost:8099/bootstrap",
            },
            client_host="172.19.0.7",
        ),
        env_flag=lambda name, default=False: name != "FS_AUTH_BOOTSTRAP_ALLOW_TRUSTED_PROXY",
        bootstrap_token_header="x-bootstrap-token",
    )

    assert denied == (False, "Bootstrap is restricted to localhost requests")


def test_bootstrap_request_allowed_rejects_non_loopback_origin_via_trusted_proxy(
    monkeypatch,
) -> None:
    monkeypatch.delenv("FS_AUTH_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.setattr(
        serve_auth_runtime,
        "trusted_local_proxy_hosts",
        lambda: {"192.168.65.1"},
    )
    serve_auth_runtime.trusted_local_proxy_networks.cache_clear()

    denied = serve_auth_runtime.bootstrap_request_allowed(
        _FakeRequest(
            headers={
                "host": "localhost:8099",
                "origin": "https://evil.example.com",
            },
            client_host="192.168.65.1",
        ),
        env_flag=lambda name, default=False: True,
        bootstrap_token_header="x-bootstrap-token",
    )

    assert denied == (False, "Bootstrap is restricted to localhost requests")


def test_bootstrap_request_allowed_rejects_internal_proxy_without_original_client(
    monkeypatch,
) -> None:
    monkeypatch.delenv("FS_AUTH_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.setattr(
        serve_auth_runtime,
        "trusted_local_proxy_hosts",
        lambda: {"172.19.0.1"},
    )
    serve_auth_runtime.trusted_local_proxy_networks.cache_clear()

    denied = serve_auth_runtime.bootstrap_request_allowed(
        _FakeRequest(
            headers={
                "host": "fs-app:8099",
                "origin": "http://fs-app:8099",
                "referer": "http://fs-app:8099/bootstrap",
            },
            client_host="172.19.0.1",
        ),
        env_flag=lambda name, default=False: True,
        bootstrap_token_header="x-bootstrap-token",
    )

    assert denied == (False, "Bootstrap is restricted to localhost requests")


def test_server_does_not_trust_forwarding_headers_from_every_peer() -> None:
    script = (
        Path(__file__).resolve().parents[2] / "core/entrypoints/cli/scripts/start_server.sh"
    ).read_text(encoding="utf-8")

    assert "--forwarded-allow-ips=*" not in script
    assert '--forwarded-allow-ips="$FORWARDED_ALLOW_IPS"' in script


def test_public_proxy_ports_support_an_installer_owned_loopback_bind() -> None:
    root = Path(__file__).resolve().parents[2]

    for relative in (
        "infra/docker/docker-compose.yaml",
        "infra/docker/docker-compose.prod.yaml",
    ):
        compose = (root / relative).read_text(encoding="utf-8")
        assert (
            '"${FS_PUBLIC_BIND_HOST:-0.0.0.0}:${HTTP_PORT:-8080}:${HTTP_PORT:-8080}/tcp"'
        ) in compose
        assert (
            '"${FS_PUBLIC_BIND_HOST:-0.0.0.0}:${APP_PORT:-8099}:${APP_PORT:-8099}/tcp"'
        ) in compose


def test_local_auth_bootstrap_required_respects_local_auth_and_user_count() -> None:
    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql):
            self.sql = sql

        def fetchone(self):
            return (0,)

    class _Conn:
        def cursor(self):
            return _Cursor()

    class _Logger:
        conn = _Conn()

    assert (
        serve_auth_runtime.local_auth_bootstrap_required(
            _Logger(),
            local_auth_enabled_fn=lambda: False,
            env_flag=lambda name, default=False: True,
            get_logger_only_fn=lambda: None,
            auth_tables_available_fn=lambda _bl: True,
        )
        is False
    )
    assert (
        serve_auth_runtime.local_auth_bootstrap_required(
            _Logger(),
            local_auth_enabled_fn=lambda: True,
            env_flag=lambda name, default=False: True,
            get_logger_only_fn=lambda: None,
            auth_tables_available_fn=lambda _bl: True,
        )
        is True
    )


def test_build_password_reset_url_prefers_public_base_url() -> None:
    url = serve_auth_runtime.build_password_reset_url(
        _FakeRequest(),
        "tok-1",
        public_base_url_fn=lambda: "https://prod.example.com",
    )

    assert url == "https://prod.example.com/reset-password?token=tok-1"


def test_build_password_reset_url_rejects_unconfigured_request_host_fallback() -> None:
    _raises_input_497_1 = _FakeRequest(base_url="https://attacker.example/")
    with pytest.raises(ValueError, match="FS_PUBLIC_BASE_URL"):
        serve_auth_runtime.build_password_reset_url(
            _raises_input_497_1, "tok-1", public_base_url_fn=lambda: ""
        )


def test_dispatch_password_reset_email_does_not_send_without_canonical_base_url() -> None:
    send = MagicMock(return_value={"ok": True})
    result = serve_auth_runtime.dispatch_password_reset_email(
        _FakeRequest(base_url="https://attacker.example/"),
        {"username": "alice@example.test"},
        "tok-1",
        send_password_reset_email_fn=send,
        build_password_reset_url_fn=lambda request, token: (
            serve_auth_runtime.build_password_reset_url(
                request,
                token,
                public_base_url_fn=lambda: "",
            )
        ),
        auth_password_reset_ttl_minutes=15,
        logger=MagicMock(),
    )

    assert result == {"ok": False, "reason": "dispatch_exception"}
    send.assert_not_called()


def test_verify_password_validates_pbkdf2_hashes() -> None:
    salt_hex = "00" * 16
    password = "supersecret"
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        100_000,
    ).hex()
    encoded = f"pbkdf2_sha256$100000${salt_hex}${digest}"

    assert serve_auth_runtime.verify_password(password, encoded) is True
    assert serve_auth_runtime.verify_password("wrong", encoded) is False


def test_dispatch_password_reset_email_returns_dispatch_exception() -> None:
    logger = MagicMock()
    result = serve_auth_runtime.dispatch_password_reset_email(
        _FakeRequest(),
        {"username": "ops@example.com"},
        "tok-1",
        send_password_reset_email_fn=lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("smtp down")
        ),
        build_password_reset_url_fn=lambda request, token: (
            "https://prod.example.com/reset-password"
        ),
        auth_password_reset_ttl_minutes=30,
        logger=logger,
    )

    assert result == {"ok": False, "reason": "dispatch_exception"}
    logger.warning.assert_called_once()


def test_dispatch_password_reset_email_uses_default_for_unsupported_locale() -> None:
    """Unsupported profile/cookie locales must not select another language."""
    logger = MagicMock()
    received_kwargs: dict = {}

    def _capture(**kwargs):
        received_kwargs.update(kwargs)
        return {"ok": True, "reason": "sent"}

    # Unsupported profile and cookie values fall back to English.
    user = {"username": "ops@example.com", "locale": "ru"}
    request = _FakeRequest(
        auth_user=user,
        cookies={"ui-locale": "ru"},
    )

    serve_auth_runtime.dispatch_password_reset_email(
        request,
        user,
        "tok-1",
        send_password_reset_email_fn=_capture,
        build_password_reset_url_fn=lambda req, token: "https://prod.example.com/reset-password",
        auth_password_reset_ttl_minutes=30,
        logger=logger,
    )

    assert received_kwargs.get("locale") == "en"


def test_dispatch_password_reset_email_defaults_locale_to_en() -> None:
    """When no locale hint is available, locale defaults to 'en'."""
    logger = MagicMock()
    received_kwargs: dict = {}

    def _capture(**kwargs):
        received_kwargs.update(kwargs)
        return {"ok": True, "reason": "sent"}

    serve_auth_runtime.dispatch_password_reset_email(
        _FakeRequest(),
        {"username": "ops@example.com"},
        "tok-1",
        send_password_reset_email_fn=_capture,
        build_password_reset_url_fn=lambda req, token: "https://prod.example.com/reset-password",
        auth_password_reset_ttl_minutes=30,
        logger=logger,
    )

    assert received_kwargs.get("locale") == "en"


def test_log_auth_audit_event_writes_auth_log_action() -> None:
    logger_ref = MagicMock()

    serve_auth_runtime.log_auth_audit_event(
        logger_ref,
        action="login",
        username="alice",
        actor="auth",
        status="ok",
        detail={"ip": "127.0.0.1"},
        to_json_obj=lambda value: dict(value or {}),
    )

    kwargs = logger_ref.log_action.call_args.kwargs
    assert kwargs["action_type"] == "auth_login"
    assert kwargs["payload"] == {"username": "alice"}
    assert kwargs["meta"] == {"audit_category": "auth"}


def test_log_auth_audit_event_reports_safe_write_failure(caplog) -> None:
    class BrokenAuditStore:
        def log_action(self, **_kwargs):
            raise RuntimeError("database-password-must-not-leak")

    with caplog.at_level("WARNING", logger="core.application.auth.audit"):
        serve_auth_runtime.log_auth_audit_event(
            BrokenAuditStore(),
            action="login",
            username="alice",
            actor="auth",
            status="ok",
            detail={"ip": "127.0.0.1"},
            to_json_obj=lambda value: dict(value or {}),
        )

    assert "Auth audit write failed for auth_login" in caplog.text
    assert "RuntimeError[detail_sha256=" in caplog.text
    assert "database-password-must-not-leak" not in caplog.text


def test_is_public_auth_path_supports_static_and_webhook_routes() -> None:
    assert (
        serve_auth_runtime.is_public_auth_path(
            "/api/auth/login",
            public_health_paths={"/health"},
            auth_public_paths={"/api/auth/login"},
            public_webhook_path_prefixes=("/webhook/providers/",),
            public_frontend_path_prefixes=("/app",),
            path_login="/login",
        )
        is True
    )
    assert (
        serve_auth_runtime.is_public_auth_path(
            "/api/mcp/oauth/client-metadata/mcp_alpha.json",
            public_health_paths={"/health"},
            auth_public_paths={"/api/auth/login"},
            public_webhook_path_prefixes=("/webhook/providers/",),
            public_frontend_path_prefixes=("/app",),
            path_login="/login",
        )
        is True
    )
    assert (
        serve_auth_runtime.is_public_auth_path(
            "/app/dashboard",
            public_health_paths={"/health"},
            auth_public_paths={"/api/auth/login"},
            public_webhook_path_prefixes=("/webhook/providers/",),
            public_frontend_path_prefixes=("/app",),
            path_login="/login",
        )
        is True
    )
    assert (
        serve_auth_runtime.is_public_auth_path(
            "/api/projects",
            public_health_paths={"/health"},
            auth_public_paths={"/api/auth/login"},
            public_webhook_path_prefixes=("/webhook/providers/",),
            public_frontend_path_prefixes=("/app",),
            path_login="/login",
        )
        is False
    )


def test_roles_intersect_and_self_service_routes_normalize_inputs() -> None:
    assert request_principal_runtime.roles_intersect(["Admin", "viewer"], {"admin"}) is True
    assert request_principal_runtime.roles_intersect(["viewer"], {"admin"}) is False
    assert serve_auth_runtime.is_self_service_auth_write_route("/api/auth/logout") is True
    assert serve_auth_runtime.is_self_service_auth_write_route("/api/auth/tokens/7/revoke") is True
    assert (
        serve_auth_runtime.is_self_service_auth_write_route("/api/users/me/email-change/request")
        is True
    )
    assert serve_auth_runtime.is_self_service_auth_write_route("/api/projects") is False
    assert request_principal_runtime.normalize_requested_roles(
        ["Admin", "viewer", "viewer", "unknown"]
    ) == [
        "admin",
        "viewer",
    ]


def test_optional_route_path_allowed_supports_paths_and_predicates() -> None:
    assert (
        serve_auth_runtime.optional_route_path_allowed(
            "/api/auth/optional-self-service/setup",
            paths={"/api/auth/optional-self-service/setup"},
        )
        is True
    )
    assert (
        serve_auth_runtime.optional_route_path_allowed(
            "/api/auth/optional-self-service/complete",
            predicates=[
                lambda path: path.startswith("/api/auth/optional-self-service/"),
                object(),
            ],
        )
        is True
    )
    assert (
        serve_auth_runtime.optional_route_path_allowed(
            "/api/projects",
            paths={"/api/auth/optional-self-service/setup"},
            predicates=[lambda _path: False],
        )
        is False
    )


def test_request_auth_helpers_resolve_payload_actor_token_and_ip() -> None:
    request = _FakeRequest(
        headers={
            "authorization": "Bearer tok-1",
            "x-forwarded-for": "10.0.0.5, 10.0.0.6",
        },
        cookies={"session": "cookie-token"},
        auth_user={"username": "alice", "roles": ["admin"]},
    )

    assert request_principal_runtime.request_auth_user_payload(
        request,
        resolved_principal_type=_FakePrincipal,
    ) == {"username": "alice", "roles": ["admin"]}
    assert (
        request_principal_runtime.resolve_actor_username(
            request,
            "mallory",
            default="operator",
            request_auth_user_payload_fn=lambda req: (
                request_principal_runtime.request_auth_user_payload(
                    req,
                    resolved_principal_type=_FakePrincipal,
                )
            ),
            optional_actor_username_fn=lambda req, default="operator": (
                request_principal_runtime.optional_actor_username(
                    req,
                    default=default,
                    request_auth_user_payload_fn=lambda inner_req: (
                        request_principal_runtime.request_auth_user_payload(
                            inner_req,
                            resolved_principal_type=_FakePrincipal,
                        )
                    ),
                )
            ),
        )
        == "alice"
    )
    assert (
        request_principal_runtime.request_auth_token(request, auth_cookie_name="session") == "tok-1"
    )
    assert serve_auth_runtime.request_client_ip(request) == "10.0.0.6"


def test_request_client_ip_uses_rightmost_untrusted_forwarded_hop() -> None:
    request = _FakeRequest(
        headers={"x-forwarded-for": "198.51.100.10, 203.0.113.20"},
        client_host="127.0.0.1",
    )

    assert serve_auth_runtime.request_client_ip(request) == "203.0.113.20"


def test_request_client_ip_ignores_forwarded_headers_from_untrusted_client() -> None:
    request = _FakeRequest(
        headers={
            "x-forwarded-for": "10.0.0.5, 10.0.0.6",
            "x-real-ip": "10.0.0.9",
        },
        client_host="203.0.113.10",
    )

    assert serve_auth_runtime.request_client_ip(request) == "203.0.113.10"


def test_require_authenticated_user_raises_without_payload_or_role() -> None:
    request = _FakeRequest(auth_user={"username": "alice", "roles": ["viewer"]})

    assert (
        request_principal_runtime.require_authenticated_user(
            request,
            required_roles={"viewer"},
            request_auth_user_payload_fn=lambda req: (
                request_principal_runtime.request_auth_user_payload(
                    req,
                    resolved_principal_type=_FakePrincipal,
                )
            ),
            roles_intersect_fn=request_principal_runtime.roles_intersect,
            err_authentication_required="Authentication required",
        )["username"]
        == "alice"
    )

    def _raises_input_826_1(req):
        return request_principal_runtime.request_auth_user_payload(
            req, resolved_principal_type=_FakePrincipal
        )

    with pytest.raises(PermissionError, match="Insufficient role"):
        request_principal_runtime.require_authenticated_user(
            request,
            required_roles={"admin"},
            request_auth_user_payload_fn=_raises_input_826_1,
            roles_intersect_fn=request_principal_runtime.roles_intersect,
            err_authentication_required="Authentication required",
        )


def test_a_deployment_that_declares_no_environment_is_treated_as_production(monkeypatch):
    """AC17-R129: silence used to mean a laptop, and it permitted disabling auth.

    Every Compose file sets one of the three markers, so none of them set means an
    image started without Compose -- a platform deployment. Reading that as local
    development let `FS_AUTH_ALLOW_DISABLED` be honoured there, and turned security
    scanning down to monitor. One resolver answers this now, rather than two copies of
    the same three lookups drifting apart.
    """
    from core.application.auth.auth_http_runtime import _auth_disabled_override_allowed
    from core.infrastructure.security.config import _deployment_environment

    for marker in ("FS_STACK_ENVIRONMENT", "ENVIRONMENT", "APP_ENV"):
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.delenv("FS_AUTH_ALLOW_DISABLED", raising=False)

    assert _deployment_environment() == "production"
    assert _auth_disabled_override_allowed() is False

    # A deployment that says what it is keeps saying it.
    monkeypatch.setenv("FS_STACK_ENVIRONMENT", "local_development")
    assert _deployment_environment() == "local_development"
    assert _auth_disabled_override_allowed() is True
