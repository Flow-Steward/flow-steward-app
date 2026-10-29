"""Auth configuration constants — extracted from serve.py."""

import re

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
AUTH_PASSWORD_MIN_LENGTH = max(8, int(_runtime_env.get("FS_AUTH_PASSWORD_MIN_LENGTH", "10")))

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

# ---------------------------------------------------------------------------
# Admin-only route patterns
# ---------------------------------------------------------------------------
AUTH_ADMIN_ONLY_PATTERNS = (
    re.compile(r"^/admin(?:/|$)"),
    re.compile(r"^/api/agents(?:/|$)"),
    re.compile(r"^/api/catalog-categories(?:/|$)"),
    re.compile(r"^/api/tools(?:/|$)"),
    re.compile(r"^/api/skills(?:/|$)"),
    # NOTE: `/api/workflows` is intentionally NOT admin-only. It is covered by the
    # registry-read patterns instead, so operators can READ the global workflow
    # catalog/detail/contract (GET) while writes (POST/PUT/PATCH/DELETE) still
    # require admin via the middleware's registry-read write gate. Viewers remain
    # denied (registry read requires operator+). Keep this out of the admin-only
    # list or the read gate is shadowed and operators get 403 on the catalog.
    re.compile(r"^/api/data-contracts(?:/|$)"),
    re.compile(r"^/api/datasets(?:/|$)"),
    re.compile(r"^/api/fields(?:/|$)"),
    re.compile(r"^/api/guardrails(?:/|$)"),
    re.compile(r"^/api/mcp(?:/|$)"),
    re.compile(r"^/api/model-providers(?:/|$)"),
    re.compile(r"^/api/llm-provider-extensions(?:/|$)"),
    re.compile(r"^/api/preview(?:/|$)"),
    re.compile(r"^/api/published-blocks(?:/|$)"),
    re.compile(r"^/api/workspaces(?:/|$)"),
    re.compile(r"^/api/auth/users(?:/|$)"),
    re.compile(r"^/api/auth/accounts(?:/|$)"),
    re.compile(r"^/api/auth/service-accounts(?:/|$)"),
    re.compile(r"^/api/projects/[^/]+/providers(?:/|$)"),
    # Project workflow management is admin-only for writes (e.g. PUT workflow
    # bindings), but the read-only diagnostics — contract / validate / suggestions
    # GETs — are operator/viewer-readable for parity with the project
    # workflow-catalog and workflow-entrypoints reads. The negative lookahead
    # excludes those three diagnostic leaf paths from the admin-only gate.
    re.compile(
        r"^(?!/api/projects/[^/]+/workflows/[^/]+/(?:contract|validate|suggestions)$)"
        r"/api/projects/[^/]+/workflows(?:/|$)"
    ),
    re.compile(r"^/api/processes/[^/]+/kill$"),
    # Watchdog recovery (kill/retry) terminates workers and re-enqueues jobs;
    # admin-only, mirroring the kill route above. The GET scan route is read-only
    # and remains operator-readable; only the POST recover mutation is gated here.
    re.compile(r"^/api/watchdog/jobs/[^/]+/recover$"),
    # Extension deployment-lifecycle / runtime-admin endpoints. These read/write
    # local extension paths, persist descriptor releases, or mint extension-runtime
    # tokens and must be restricted to lifecycle/runtime admins, not operators.
    re.compile(r"^/api/extensions/descriptors/import/(?:file|url)$"),
    re.compile(r"^/api/extensions/runtime/token$"),
)
