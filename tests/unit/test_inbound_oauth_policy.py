"""ADR 0047: readiness, public identity, resource/scope/PKCE/redirect policy (task5b D4, D9, Z2,
Z3, C6, F10 and the forwarding rule)."""

from __future__ import annotations

import pytest

from core.application.auth import inbound_oauth_policy as policy
from core.application.auth.agent_access_presets import OPERATIONAL_SCOPES
from core.infrastructure.public_identity import mcp_auth_urls

SECRET = "s" * 32


def _environment(base_url: str, *, secret: str = SECRET, resource: str = "", issuer: str = ""):
    return policy.OAuthEnvironment(
        public_base_url=base_url,
        issuer_override=issuer,
        resource_override=resource,
        forward_secret=secret,
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "http://fs.example.com",
        "https://localhost",
        "https://app.localhost",
        "https://x.local",
        "https://10.0.0.5",
        "https://192.168.1.4:8443",
        "https://169.254.1.1",
        "https://127.0.0.1",
        "https://[::1]",
        "https://0.0.0.0",
        "https://flowsteward",
    ],
)
def test_public_https_is_required(base_url: str) -> None:
    readiness = policy.inbound_oauth_readiness(
        _environment(base_url), storage_available=True, enabled=True
    )
    assert policy.BLOCKER_PUBLIC_HTTPS_REQUIRED in readiness.blockers
    assert not readiness.ready


def test_only_a_complete_public_configuration_is_ready() -> None:
    ready = policy.inbound_oauth_readiness(
        _environment("https://fs.example.com:443"), storage_available=True, enabled=True
    )
    assert ready.ready
    assert ready.issuer == "https://fs.example.com"
    assert ready.resource == "https://fs.example.com/mcp"
    # A public IP literal is a public origin.
    assert policy.public_https_blocker("https://162.55.214.63:8611") is None


@pytest.mark.parametrize(
    ("secret", "blocked"),
    [("", True), ("s" * 31, True), ("s" * 32, False)],
)
def test_forward_secret_needs_32_characters(secret: str, blocked: bool) -> None:
    readiness = policy.inbound_oauth_readiness(
        _environment("https://fs.example.com", secret=secret),
        storage_available=True,
        enabled=True,
    )
    assert (policy.BLOCKER_FORWARD_SECRET_MISSING in readiness.blockers) is blocked


def test_a_different_resource_override_is_issuer_mismatch() -> None:
    readiness = policy.inbound_oauth_readiness(
        _environment("https://fs.example.com", resource="https://other.example.com/mcp"),
        storage_available=True,
        enabled=True,
    )
    assert readiness.blockers == (policy.BLOCKER_ISSUER_MISMATCH,)
    same = policy.inbound_oauth_readiness(
        _environment(
            "https://fs.example.com",
            resource="https://fs.example.com/mcp/",
            issuer="https://FS.example.com:443",
        ),
        storage_available=True,
        enabled=True,
    )
    assert same.ready


def test_storage_and_switch_blockers_are_reported_in_order() -> None:
    readiness = policy.inbound_oauth_readiness(
        _environment("http://localhost:8099", secret=""), storage_available=False, enabled=False
    )
    assert readiness.blockers == (
        policy.BLOCKER_PUBLIC_HTTPS_REQUIRED,
        policy.BLOCKER_FORWARD_SECRET_MISSING,
        policy.BLOCKER_STORAGE_MISSING,
        policy.BLOCKER_DISABLED_BY_ADMIN,
    )
    assert readiness.to_payload() == {"ready": False, "blockers": list(readiness.blockers)}


@pytest.mark.parametrize(
    ("public", "issuer", "resource"),
    [
        (
            "https://flow.example.com:443",
            "https://flow.example.com",
            "https://flow.example.com/mcp",
        ),
        (
            "https://[2001:db8::1]:8443",
            "https://[2001:db8::1]:8443",
            "https://[2001:db8::1]:8443/mcp",
        ),
    ],
)
def test_app_and_mcp_derive_the_same_identity(public: str, issuer: str, resource: str) -> None:
    """D9: fs-app and fs-mcp call one function, including the AC17-R55 :443 case and IPv6."""
    from core.entrypoints.mcp import server_public_identity

    assert server_public_identity.mcp_auth_urls is mcp_auth_urls
    assert mcp_auth_urls({"FS_PUBLIC_BASE_URL": public}) == (issuer, resource)
    assert policy.oauth_issuer_and_resource(_environment(public)) == (issuer, resource)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://fs.example.com/mcp", "https://fs.example.com/mcp"),
        ("https://fs.example.com/mcp/", "https://fs.example.com/mcp"),
        ("HTTPS://FS.EXAMPLE.COM:443/mcp", "https://fs.example.com/mcp"),
        ("https://fs.example.com:8443/mcp", "https://fs.example.com:8443/mcp"),
        ("https://fs.example.com/mcp?x=1", ""),
        ("https://fs.example.com/mcp#f", ""),
        ("https://user@fs.example.com/mcp", ""),
        ("ftp://fs.example.com/mcp", ""),
        ("", ""),
    ],
)
def test_resource_normalization(value: str, expected: str) -> None:
    assert policy.normalize_resource(value) == expected


def test_scope_parsing() -> None:
    with pytest.raises(policy.InvalidScopeError):
        policy.parse_requested_scopes("openid")
    assert policy.parse_requested_scopes("offline_access jobs.read") == ["jobs.read"]
    assert policy.parse_requested_scopes("") == list(OPERATIONAL_SCOPES)
    assert policy.parse_requested_scopes(None) == list(OPERATIONAL_SCOPES)
    assert policy.parse_requested_scopes("offline_access") == list(OPERATIONAL_SCOPES)
    assert policy.parse_requested_scopes("admin:all jobs.read jobs.read") == [
        "admin:all",
        "jobs.read",
    ]


def test_pkce_is_s256_only() -> None:
    verifier = "v" * 43
    challenge = policy.pkce_s256_challenge(verifier)
    assert policy.valid_code_challenge(challenge)
    assert policy.pkce_verifier_matches(verifier, challenge)
    assert not policy.pkce_verifier_matches("w" * 43, challenge)
    assert not policy.pkce_verifier_matches(challenge, challenge)  # plain is never accepted
    assert not policy.pkce_verifier_matches("v" * 42, policy.pkce_s256_challenge("v" * 42))
    assert not policy.valid_code_challenge("short")
    assert not policy.valid_code_challenge("a" * 129)


def test_redirect_matching_ignores_only_the_loopback_port() -> None:
    """C6."""
    assert policy.redirect_uri_matches(
        "http://127.0.0.1:53111/callback", ["http://127.0.0.1/callback"]
    )
    assert policy.redirect_uri_matches("http://localhost:4000/cb", ["http://localhost:3000/cb"])
    assert not policy.redirect_uri_matches(
        "https://claude.ai/api/mcp/auth_callback", ["https://claude.ai/api/mcp/auth_callback/"]
    )
    assert not policy.redirect_uri_matches(
        "http://127.0.0.1:53111/other", ["http://127.0.0.1/callback"]
    )
    assert not policy.redirect_uri_matches("http://evil.example/cb", ["http://evil.example/cb"])
    assert not policy.redirect_uri_matches("https://chatgpt.com/cb#x", ["https://chatgpt.com/cb#x"])
    assert policy.redirect_uri_matches("https://chatgpt.com/cb", ["https://chatgpt.com/cb"])


def test_redirect_parameters_keep_the_client_query() -> None:
    assert (
        policy.redirect_with_params(
            "https://claude.ai/cb?a=1", {"code": "c", "state": "s t", "iss": "https://x", "n": None}
        )
        == "https://claude.ai/cb?a=1&code=c&state=s+t&iss=https%3A%2F%2Fx"
    )


def test_token_prefixes_route_bearers() -> None:
    """F10: an access token is recognised by prefix; codes and refresh tokens never bear."""
    assert policy.is_oauth_access_token(policy.new_access_token())
    assert policy.is_oauth_non_bearer_secret(policy.new_authorization_code())
    assert policy.is_oauth_non_bearer_secret(policy.new_refresh_token())
    assert not policy.is_oauth_access_token("fs_pat_x")
    assert policy.secret_digest("a") == policy.secret_digest("a") != policy.secret_digest("b")
    assert len(policy.new_browser_binding()) >= 21  # 128 random bits


def test_forwarding_rule() -> None:
    """Decision 13: introspection needs no proof; everything else needs the secret."""
    header = policy.MCP_FORWARD_HEADER
    assert header == "X-Flow-Steward-Mcp-Forward"
    assert policy.oauth_forward_accepted("/api/auth/introspect", None, "")
    assert not policy.oauth_forward_accepted("/api/projects", None, SECRET)
    assert not policy.oauth_forward_accepted("/api/projects", "wrong" * 8, SECRET)
    assert policy.oauth_forward_accepted("/api/projects", SECRET, SECRET)
    assert not policy.oauth_forward_accepted("/settings", SECRET, SECRET)
    # A configured secret shorter than 32 characters never authorizes forwarding.
    assert not policy.oauth_forward_accepted("/api/projects", "short", "short")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/auth/tokens"),
        ("GET", "/api/auth/tokens"),
        ("POST", "/api/auth/tokens/abc/revoke"),
        ("POST", "/api/auth/change-password"),
        ("POST", "/api/users/me/email-change/request"),
        ("POST", "/api/auth/logout"),
        ("GET", "/api/auth/oauth-grants"),
        ("POST", "/api/auth/oauth-grants/abc/revoke"),
        ("GET", "/api/oauth/authorizations/abc"),
        ("POST", "/api/oauth/authorizations/abc/decision"),
        ("GET", "/api/settings/mcp-oauth"),
        ("PUT", "/api/settings/mcp-oauth"),
    ],
)
def test_oauth_principal_denials(method: str, path: str) -> None:
    assert policy.oauth_principal_denied_route(path, method)


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/api/projects"), ("GET", "/api/auth/introspect"), ("POST", "/api/workflow-runs")],
)
def test_oauth_principal_keeps_operational_routes(method: str, path: str) -> None:
    assert not policy.oauth_principal_denied_route(path, method)


def test_lifetimes_are_constants() -> None:
    assert policy.CODE_LIFETIME.total_seconds() == 60
    assert policy.ACCESS_TOKEN_LIFETIME.total_seconds() == 3600
    assert policy.PENDING_AUTHORIZATION_LIFETIME.total_seconds() == 600
    assert policy.GRANT_EXPIRY_DAY_CHOICES == (7, 30, 90)
