"""Pure policy of the inbound OAuth authorization server for the MCP endpoint.

ADR 0047: readiness and its blocker codes, the public-HTTPS rule, resource and scope
normalization, redirect matching, PKCE S256, token formats and lifetimes, and the
MCP -> API forwarding rule. No database and no HTTP framework here; the service and
the transport adapters call these functions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core.application.auth.agent_access_presets import AGENT_ACCESS_SCOPES, OPERATIONAL_SCOPES
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.public_identity import mcp_auth_urls, public_origin

# ---------------------------------------------------------------------------
# Formats, lifetimes and names (decision 10). Constants, not configuration.
# ---------------------------------------------------------------------------
CODE_PREFIX = "fs_oac_"
ACCESS_TOKEN_PREFIX = "fs_oat_"
REFRESH_TOKEN_PREFIX = "fs_ort_"
BROWSER_BINDING_COOKIE = "fs_oauth_ar"
MCP_FORWARD_HEADER = "X-Flow-Steward-Mcp-Forward"
MIN_FORWARD_SECRET_LENGTH = 32

PENDING_AUTHORIZATION_LIFETIME = timedelta(minutes=10)
CODE_LIFETIME = timedelta(seconds=60)
ACCESS_TOKEN_LIFETIME = timedelta(minutes=60)
GRANT_EXPIRY_DAY_CHOICES: tuple[int, ...] = (7, 30, 90)
DEFAULT_GRANT_EXPIRY_DAYS = 30
MAX_GRANT_EXPIRY_DAYS = 90
LAST_USED_WRITE_INTERVAL = timedelta(seconds=60)
#: Revoked and expired grants stay visible this long before cleanup deletes them.
GRANT_RETENTION_AFTER_END = timedelta(days=30)
#: Clients with no grant are forgotten after this long without use.
CLIENT_RETENTION_UNUSED = timedelta(days=30)

MAX_STATE_LENGTH = 1024
MAX_CLIENT_ID_LENGTH = 2048
_CODE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_CODE_VERIFIER_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")

#: The account setting that switches the authorization server on (decision 16).
INBOUND_OAUTH_SETTING_KEY = "mcp_oauth"
INBOUND_OAUTH_TABLES: tuple[str, ...] = (
    "oauth_clients",
    "oauth_authorizations",
    "oauth_grants",
    "oauth_access_tokens",
    "oauth_refresh_tokens",
)

#: Scopes this authorization server advertises and accepts (decision 3).
SUPPORTED_SCOPES: tuple[str, ...] = AGENT_ACCESS_SCOPES
DEFAULT_REQUESTED_SCOPES: tuple[str, ...] = OPERATIONAL_SCOPES
OFFLINE_ACCESS_SCOPE = "offline_access"

# ---------------------------------------------------------------------------
# Readiness (decision 2)
# ---------------------------------------------------------------------------
BLOCKER_PUBLIC_HTTPS_REQUIRED = "public_https_required"
BLOCKER_ISSUER_MISMATCH = "issuer_mismatch"
BLOCKER_FORWARD_SECRET_MISSING = "forward_secret_missing"  # pragma: allowlist secret
BLOCKER_STORAGE_MISSING = "storage_missing"
BLOCKER_DISABLED_BY_ADMIN = "disabled_by_admin"

_DEFAULT_PORTS = {"http": 80, "https": 443}
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True, slots=True)
class OAuthReadiness:
    """Whether the authorization server may serve this installation and account."""

    blockers: tuple[str, ...]
    issuer: str
    resource: str

    @property
    def ready(self) -> bool:
        return not self.blockers

    def to_payload(self) -> dict[str, object]:
        return {"ready": self.ready, "blockers": list(self.blockers)}


@dataclass(frozen=True, slots=True)
class OAuthEnvironment:
    """The installation values readiness and token issuance depend on."""

    public_base_url: str = ""
    issuer_override: str = ""
    resource_override: str = ""
    forward_secret: str = field(default="", repr=False)

    def as_identity_mapping(self) -> dict[str, str]:
        return {
            "FS_PUBLIC_BASE_URL": self.public_base_url,
            "FS_MCP_ISSUER_URL": self.issuer_override,
            "FS_MCP_RESOURCE_URL": self.resource_override,
        }


def current_oauth_environment() -> OAuthEnvironment:
    """Read the four values from the canonical runtime environment of ``fs-app``."""
    return OAuthEnvironment(
        public_base_url=str(_runtime_env.get("FS_PUBLIC_BASE_URL") or "").strip(),
        issuer_override=str(_runtime_env.get("FS_MCP_ISSUER_URL") or "").strip(),
        resource_override=str(_runtime_env.get("FS_MCP_RESOURCE_URL") or "").strip(),
        forward_secret=str(_runtime_env.get("FS_MCP_FORWARD_SECRET") or "").strip(),
    )


def _host_is_public(hostname: str) -> bool:
    host = str(hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host == "localhost" or host.endswith((".localhost", ".local")):
            return False
        return "." in host
    return not (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
    )


def public_https_blocker(base_url: str) -> str | None:
    """``public_https_required`` unless ``base_url`` is a public HTTPS origin.

    The cloud clients (ChatGPT, Claude.ai) reach the installation from the internet,
    so the origin must be ``https`` with a host that is not ``localhost``,
    ``*.localhost``, ``*.local``, a single label, or a loopback, private,
    link-local or unspecified IP literal. Shared with the task 4 wizard's
    cloud-client blocker.
    """
    origin = public_origin({"FS_PUBLIC_BASE_URL": str(base_url or "")})
    if not origin:
        return BLOCKER_PUBLIC_HTTPS_REQUIRED
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not _host_is_public(parsed.hostname or ""):
        return BLOCKER_PUBLIC_HTTPS_REQUIRED
    return None


def oauth_issuer_and_resource(environment: OAuthEnvironment) -> tuple[str, str]:
    """The issuer and protected resource of this installation: ``{origin}``, ``{origin}/mcp``."""
    origin = public_origin(environment.as_identity_mapping())
    if not origin:
        return "", ""
    return origin, f"{origin}/mcp"


def inbound_oauth_readiness(
    environment: OAuthEnvironment,
    *,
    storage_available: bool,
    enabled: bool,
) -> OAuthReadiness:
    """Evaluate every readiness condition and return the fixed blocker codes in order."""
    blockers: list[str] = []
    issuer, resource = oauth_issuer_and_resource(environment)
    if public_https_blocker(environment.public_base_url):
        blockers.append(BLOCKER_PUBLIC_HTTPS_REQUIRED)
    if issuer:
        advertised_issuer, advertised_resource = mcp_auth_urls(environment.as_identity_mapping())
        if normalize_url(advertised_issuer) != issuer or normalize_url(
            advertised_resource
        ) != normalize_url(resource):
            blockers.append(BLOCKER_ISSUER_MISMATCH)
    if len(environment.forward_secret) < MIN_FORWARD_SECRET_LENGTH:
        blockers.append(BLOCKER_FORWARD_SECRET_MISSING)
    if not storage_available:
        blockers.append(BLOCKER_STORAGE_MISSING)
    if not enabled:
        blockers.append(BLOCKER_DISABLED_BY_ADMIN)
    return OAuthReadiness(blockers=tuple(blockers), issuer=issuer, resource=resource)


# ---------------------------------------------------------------------------
# URLs, resources and redirects
# ---------------------------------------------------------------------------


def normalize_url(value: str) -> str:
    """Lower-case scheme and host, drop the default port, one trailing ``/``.

    Returns ``""`` for anything that is not an absolute http(s) URL without
    userinfo, query or fragment.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return ""
    scheme = parsed.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parsed.hostname:
        return ""
    if parsed.username is not None or parsed.password is not None:
        return ""
    if parsed.query or parsed.fragment or "#" in raw:
        return ""
    hostname = parsed.hostname.lower()
    host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        host = f"{host}:{port}"
    path = parsed.path
    if path.endswith("/"):
        path = path[:-1]
    return urlunsplit((scheme, host, path, "", ""))


def normalize_resource(value: str) -> str:
    """RFC 8707 resource after normalization (decision 6.1); ``""`` when invalid."""
    return normalize_url(value)


def is_loopback_redirect_uri(uri: str) -> bool:
    """``http`` to ``127.0.0.1``, ``[::1]`` or ``localhost`` (RFC 8252 §7.3)."""
    try:
        parsed = urlsplit(str(uri or ""))
    except ValueError:
        return False
    return parsed.scheme == "http" and (parsed.hostname or "").lower() in _LOOPBACK_HOSTS


def valid_redirect_uri(uri: str) -> bool:
    """An absolute ``https`` URI or an ``http`` loopback URI, without a fragment."""
    raw = str(uri or "").strip()
    if not raw or raw != str(uri) or "#" in raw or len(raw) > MAX_CLIENT_ID_LENGTH:
        return False
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError:
        return False
    if parsed.username is not None or parsed.password is not None or not parsed.hostname:
        return False
    if parsed.scheme == "https":
        return True
    return is_loopback_redirect_uri(raw)


def redirect_uri_matches(requested: str, registered: list[str] | tuple[str, ...]) -> bool:
    """Exact string equality, except loopback URIs, which ignore the port."""
    candidate = str(requested or "")
    if not valid_redirect_uri(candidate):
        return False
    if candidate in registered:
        return True
    if not is_loopback_redirect_uri(candidate):
        return False
    wanted = urlsplit(candidate)
    for entry in registered:
        if not is_loopback_redirect_uri(entry):
            continue
        listed = urlsplit(entry)
        if (
            listed.scheme == wanted.scheme
            and (listed.hostname or "").lower() == (wanted.hostname or "").lower()
            and listed.path == wanted.path
            and listed.query == wanted.query
        ):
            return True
    return False


def redirect_with_params(redirect_uri: str, params: Mapping[str, str | None]) -> str:
    """Append response parameters to ``redirect_uri`` and keep its own query."""
    parsed = urlsplit(redirect_uri)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((key, value) for key, value in params.items() if value is not None)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def host_of(url: str) -> str:
    try:
        return (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Scopes (decision 6.2)
# ---------------------------------------------------------------------------


class InvalidScopeError(ValueError):
    """A requested scope is not one this authorization server issues."""


def parse_requested_scopes(raw: str | None) -> list[str]:
    """Split ``scope`` on spaces, drop ``offline_access``, validate the rest.

    An empty or absent scope means the operational preset's seven scopes.
    """
    values: list[str] = []
    for item in str(raw or "").split(" "):
        scope = item.strip()
        if not scope or scope == OFFLINE_ACCESS_SCOPE:
            continue
        if scope not in SUPPORTED_SCOPES:
            raise InvalidScopeError("scope is not supported")
        if scope not in values:
            values.append(scope)
    # Absent, empty, or only offline_access: the operational preset's scopes.
    return values or list(DEFAULT_REQUESTED_SCOPES)


# ---------------------------------------------------------------------------
# PKCE S256 (decision 6, 7)
# ---------------------------------------------------------------------------


def valid_code_challenge(value: str | None) -> bool:
    return bool(_CODE_CHALLENGE_RE.fullmatch(str(value or "")))


def valid_code_verifier(value: str | None) -> bool:
    return bool(_CODE_VERIFIER_RE.fullmatch(str(value or "")))


def pkce_s256_challenge(verifier: str) -> str:
    digest = hashlib.sha256(str(verifier).encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def pkce_verifier_matches(verifier: str | None, challenge: str) -> bool:
    if not valid_code_verifier(verifier):
        return False
    return hmac.compare_digest(pkce_s256_challenge(str(verifier)), str(challenge or ""))


# ---------------------------------------------------------------------------
# Opaque secrets (decision 10)
# ---------------------------------------------------------------------------


def secret_digest(value: str) -> str:
    """SHA-256 hex of a raw code, token or cookie value; the only stored form."""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def new_authorization_code() -> str:
    return f"{CODE_PREFIX}{secrets.token_urlsafe(32)}"


def new_access_token() -> str:
    return f"{ACCESS_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def new_refresh_token() -> str:
    return f"{REFRESH_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def new_browser_binding() -> str:
    """128 random bits for the ``fs_oauth_ar`` cookie."""
    return secrets.token_urlsafe(16)


def is_oauth_access_token(token: str) -> bool:
    return str(token or "").startswith(ACCESS_TOKEN_PREFIX)


def is_oauth_non_bearer_secret(token: str) -> bool:
    """Codes and refresh tokens are never accepted as bearers."""
    return str(token or "").startswith((CODE_PREFIX, REFRESH_TOKEN_PREFIX))


# ---------------------------------------------------------------------------
# Routes an OAuth principal may not use, and the MCP forwarding rule
# (decisions 4, 12, 13)
# ---------------------------------------------------------------------------
INTROSPECT_PATH = "/api/auth/introspect"
#: Interactive-only management of OAuth itself: session cookie (+ CSRF) only.
INTERACTIVE_ONLY_PREFIXES: tuple[str, ...] = (
    "/api/oauth/",
    "/api/auth/oauth-grants",
    "/api/settings/mcp-oauth",
)
#: The four protocol endpoints, public and never authenticated by a cookie.
PROTOCOL_PATHS: frozenset[str] = frozenset(
    {
        "/.well-known/oauth-authorization-server",
        "/oauth/authorize",
        "/oauth/token",
        "/oauth/revoke",
    }
)
_SELF_SERVICE_AUTH_WRITE_PATHS = frozenset(
    {
        "/api/auth/change-password",
        "/api/auth/logout",
        "/api/auth/tokens",
        "/api/users/me/email-change/request",
    }
)
_TOKEN_REVOKE_RE = re.compile(r"^/api/auth/tokens/[^/]+/revoke$")


def is_interactive_only_path(path: str) -> bool:
    normalized = str(path or "")
    return any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in INTERACTIVE_ONLY_PREFIXES
    )


def oauth_principal_denied_route(path: str, method: str) -> bool:
    """Routes an ``oauth`` principal gets 403 on, whatever its scopes.

    Every self-service auth write (minting or revoking PATs, password and e-mail
    changes), listing PATs, and managing OAuth itself: a grant cannot create a
    credential that outlives its own expiry or revocation.
    """
    normalized = str(path or "")
    if normalized in _SELF_SERVICE_AUTH_WRITE_PATHS or _TOKEN_REVOKE_RE.fullmatch(normalized):
        return True
    if normalized == "/api/auth/tokens" and str(method or "").upper() == "GET":
        return True
    return is_interactive_only_path(normalized)


def oauth_introspection_fields(principal: Mapping[str, object], auth_source: str) -> dict:
    """ADR 0047 decision 14: only an OAuth principal names its resource, client and grant."""
    if str(auth_source or "").strip().lower() != "oauth":
        return {}
    return {
        "resource": str(principal.get("oauth_resource") or ""),
        "oauth_client_id": str(principal.get("oauth_client_id") or ""),
        "grant_id": str(principal.get("oauth_grant_id") or ""),
    }


def oauth_forward_accepted(path: str, header_value: str | None, secret: str) -> bool:
    """Whether ``fs-app`` accepts an OAuth access token on this request (decision 13).

    The introspection endpoint answers only about the caller's own principal and
    needs no proof. Every other use must come from ``fs-mcp``: the shared secret in
    ``X-Flow-Steward-Mcp-Forward`` and an ``/api/`` path.
    """
    normalized = str(path or "")
    if normalized == INTROSPECT_PATH:
        return True
    configured = str(secret or "")
    presented = str(header_value or "")
    if len(configured) < MIN_FORWARD_SECRET_LENGTH or not presented:
        return False
    if not hmac.compare_digest(presented.encode("utf-8"), configured.encode("utf-8")):
        return False
    return normalized.startswith("/api/")


__all__ = [
    "ACCESS_TOKEN_LIFETIME",
    "ACCESS_TOKEN_PREFIX",
    "BLOCKER_DISABLED_BY_ADMIN",
    "BLOCKER_FORWARD_SECRET_MISSING",
    "BLOCKER_ISSUER_MISMATCH",
    "BLOCKER_PUBLIC_HTTPS_REQUIRED",
    "BLOCKER_STORAGE_MISSING",
    "BROWSER_BINDING_COOKIE",
    "CODE_LIFETIME",
    "CODE_PREFIX",
    "DEFAULT_GRANT_EXPIRY_DAYS",
    "GRANT_EXPIRY_DAY_CHOICES",
    "INBOUND_OAUTH_SETTING_KEY",
    "INBOUND_OAUTH_TABLES",
    "MCP_FORWARD_HEADER",
    "PENDING_AUTHORIZATION_LIFETIME",
    "PROTOCOL_PATHS",
    "REFRESH_TOKEN_PREFIX",
    "SUPPORTED_SCOPES",
    "InvalidScopeError",
    "OAuthEnvironment",
    "OAuthReadiness",
    "current_oauth_environment",
    "inbound_oauth_readiness",
    "is_interactive_only_path",
    "is_oauth_access_token",
    "is_oauth_non_bearer_secret",
    "normalize_resource",
    "oauth_forward_accepted",
    "oauth_introspection_fields",
    "oauth_issuer_and_resource",
    "oauth_principal_denied_route",
    "parse_requested_scopes",
    "pkce_verifier_matches",
    "public_https_blocker",
    "redirect_uri_matches",
    "redirect_with_params",
    "secret_digest",
]
