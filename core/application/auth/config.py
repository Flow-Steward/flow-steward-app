"""Auth configuration constants — extracted from serve.py."""

from core.infrastructure.config.platform_identity import (
    DEFAULT_ACCOUNT_ID as DEFAULT_ACCOUNT_ID,
)
from core.infrastructure.config.platform_identity import (
    DEFAULT_TENANT_ID as DEFAULT_TENANT_ID,
)
from core.infrastructure.config.platform_identity import (
    DEFAULT_WORKSPACE_ID as DEFAULT_WORKSPACE_ID,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env


def env_flag(name: str, default: bool) -> bool:
    raw = _runtime_env.get(name)
    if raw is None:
        return bool(default)
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return bool(default)


# ---------------------------------------------------------------------------
# Public / health paths
# ---------------------------------------------------------------------------
PUBLIC_HEALTH_PATHS = {
    "/health",
    "/health/live",
    "/health/ready",
    "/api/health",
    "/metrics",
}

AUTH_PUBLIC_PATHS = {
    "/api/app/bootstrap",
    "/api/auth/bootstrap",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/password-reset/request",
    "/api/auth/password-reset/verify",
    "/api/auth/password-reset/confirm",
    "/verify-email-change",
    "/login",
    "/forgot-password",
    "/reset-password",
    "/bootstrap",
    "/favicon.ico",
    # ADR 0047 decision 4: the inbound OAuth protocol endpoints. They never derive
    # identity from a cookie; the middleware passes them through untouched.
    "/.well-known/oauth-authorization-server",
    "/oauth/authorize",
    "/oauth/token",
    "/oauth/revoke",
    # The consent shell: its error variant must render before sign-in; the
    # request variant loads data only through session-authenticated /api routes.
    "/oauth/consent",
    # ADR 0083: external sign-in. The start and callback are browser navigations that
    # happen before a session exists; the option list feeds the sign-in page.
    "/api/auth/external/options",
    "/api/auth/external/start",
    "/api/auth/external/callback",
    # ADR 0085: an invitation link is answered by its token and the signed-in person,
    # also one with a role nowhere yet (P5). The route itself requires a person's
    # session and refuses tokens; the middleware still authenticates and checks CSRF.
    "/api/auth/invitations/preview",
    "/api/auth/invitations/accept",
    "/api/auth/invitations/decline",
}

PUBLIC_WEBHOOK_PATH_PREFIXES = (
    "/webhook/workflows/",
    "/webhook/extensions/",
    # AC17-R123: an extension subprocess fetching an artifact the platform had to
    # serve itself. Exempt from the session check for the same reason the webhooks
    # above are -- there is no session to present -- and authenticated the same way,
    # by a signed, expiring, single-purpose token the route verifies before it looks
    # at anything else. The prefix is listed here rather than given its own mechanism
    # so one place still answers "what does not require a session".
    "/api/extension-artifacts/",
    "/webhook/providers/",
)

PUBLIC_FRONTEND_PATH_PREFIXES = ("/app",)

# ---------------------------------------------------------------------------
# Cookie / session
# ---------------------------------------------------------------------------
AUTH_COOKIE_NAME = _runtime_env.get("FS_AUTH_COOKIE_NAME", "fs_session")
AUTH_CSRF_COOKIE_NAME = _runtime_env.get("FS_AUTH_CSRF_COOKIE_NAME", "fs_csrf")
AUTH_CSRF_HEADER_NAME = (
    _runtime_env.get("FS_AUTH_CSRF_HEADER_NAME", "x-csrf-token").strip().lower() or "x-csrf-token"
)
AUTH_SESSION_TTL_HOURS = max(1, int(_runtime_env.get("FS_AUTH_SESSION_TTL_HOURS", "24")))
AUTH_REMEMBER_SESSION_TTL_HOURS = max(
    AUTH_SESSION_TTL_HOURS,
    int(_runtime_env.get("FS_AUTH_REMEMBER_SESSION_TTL_HOURS", "720")),
)

# ---------------------------------------------------------------------------
# Password policy
# ---------------------------------------------------------------------------
AUTH_PASSWORD_ITERATIONS = max(
    100_000, int(_runtime_env.get("FS_AUTH_PASSWORD_ITERATIONS", "260000"))
)
AUTH_PASSWORD_MIN_LENGTH = max(12, int(_runtime_env.get("FS_AUTH_PASSWORD_MIN_LENGTH", "12")))

# ---------------------------------------------------------------------------
# Login rate limiting
# ---------------------------------------------------------------------------
AUTH_LOGIN_RATE_WINDOW_SECONDS = max(
    30, int(_runtime_env.get("FS_AUTH_LOGIN_RATE_WINDOW_SECONDS", "300"))
)
AUTH_LOGIN_MAX_ATTEMPTS = max(3, int(_runtime_env.get("FS_AUTH_LOGIN_MAX_ATTEMPTS", "5")))
AUTH_LOGIN_LOCKOUT_SECONDS = max(30, int(_runtime_env.get("FS_AUTH_LOGIN_LOCKOUT_SECONDS", "900")))

# Paid model-provider verification abuse protection. The shared database-backed
# limiter applies both an account-wide and an exact-provider window.
MODEL_PROVIDER_VERIFY_RATE_WINDOW_SECONDS = max(
    30,
    int(_runtime_env.get("FS_MODEL_PROVIDER_VERIFY_RATE_WINDOW_SECONDS", "300")),
)
MODEL_PROVIDER_VERIFY_MAX_ATTEMPTS = max(
    1,
    int(_runtime_env.get("FS_MODEL_PROVIDER_VERIFY_MAX_ATTEMPTS", "3")),
)

# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------
AUTH_PASSWORD_RESET_TTL_MINUTES = max(
    5, int(_runtime_env.get("FS_AUTH_PASSWORD_RESET_TTL_MINUTES", "30"))
)
AUTH_PASSWORD_RESET_RATE_WINDOW_SECONDS = max(
    30,
    int(_runtime_env.get("FS_AUTH_PASSWORD_RESET_RATE_WINDOW_SECONDS", "300")),
)
AUTH_PASSWORD_RESET_MAX_ATTEMPTS = max(
    3,
    int(_runtime_env.get("FS_AUTH_PASSWORD_RESET_MAX_ATTEMPTS", "5")),
)
AUTH_PASSWORD_RESET_LOCKOUT_SECONDS = max(
    30,
    int(_runtime_env.get("FS_AUTH_PASSWORD_RESET_LOCKOUT_SECONDS", "900")),
)

# ---------------------------------------------------------------------------
# Role / scope constants
# ---------------------------------------------------------------------------
AUTH_WRITE_ROLES = {"admin", "operator"}
AUTH_ADMIN_ROLES = {"admin"}
AUTH_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
AUTH_BOOTSTRAP_TOKEN_HEADER = "x-platform-setup-token"
MACHINE_MUTATION_SCOPES = {
    "admin:all",
    "extension:invoke",
    "notify:dispatch",
    "event:subscribe",
    "mcp:use",
    "a2a:use",  # A2A deferred — not implemented; scope reserved for future use
}
METRICS_TOKEN = str(_runtime_env.get("FS_METRICS_TOKEN", "") or "").strip()
