"""Immutable fixed process inventory for the Compact runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from core.infrastructure.config.compact_runtime import (
    CompactPortMap,
    CompactRuntimeSettings,
)

_COMMON_ENV = frozenset(
    {
        "PATH",
        "PYTHONPATH",
        "LANG",
        "LC_ALL",
        "TZ",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "AWS_CA_BUNDLE",
        "FS_BUILD_SHA",
        "FS_EMBEDDED_IMAGE_REVISION",
        "FS_EMBEDDED_IMAGE_ROLE",
        "FS_EMBEDDED_IMAGE_PLATFORM",
    }
)
_DATABASE_ENV = frozenset(
    {
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "FS_DB_DSN",
        "LANGGRAPH_POSTGRES_URI",
    }
)
_AUTH_BOOTSTRAP_ENV = frozenset(
    {
        "FS_AUTH_BOOTSTRAP_ALLOW_TRUSTED_PROXY",
        "FS_AUTH_BOOTSTRAP_ENABLED",
        "FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY",
        "FS_AUTH_BOOTSTRAP_STATE_FILE",
        "FS_AUTH_BOOTSTRAP_TOKEN",
        "FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT",
    }
)
_CONTROL_LOOP_ENV = frozenset(
    {
        # AC17-R105: Compact runs the control plane and all six of its loops, and not
        # one of these reached it. Every loop therefore ran on its compiled-in default
        # -- the intervals, the budgets, the watchdog's grace and stuck thresholds --
        # and an operator who set them in .env.prod saw no effect and no error, which
        # is the failure mode that takes longest to notice. Read by
        # core/infrastructure/runtime/control/config.py; the contract test derives the
        # same list from the environment registry rather than trusting this one.
        "FS_ARTIFACT_CLEANUP_RECONCILIATION_BUDGET",
        "FS_DASHBOARD_ROLLUP_ENABLED",
        "FS_EXTERNAL_EFFECT_MAINTENANCE_ENABLED",
        "FS_RETENTION_ENABLED",
        "FS_SCHEDULED_JOBS_ENABLED",
        "FS_SECURITY_POSTURE_ENABLED",
        "FS_WATCHDOG_AUTO_APPLY",
        "FS_WATCHDOG_ENABLED",
        "FS_WORKFLOW_KNOWLEDGE_SYNC_ENABLED",
        "FS_DASHBOARD_ROLLUP_BACKFILL_DAYS",
        "FS_DASHBOARD_ROLLUP_INTERVAL_SECONDS",
        "FS_DASHBOARD_ROLLUP_LOOKBACK_DAYS",
        "FS_EXTERNAL_EFFECT_MAINTENANCE_INTERVAL_SECONDS",
        "FS_SCHEDULED_JOBS_BATCH_LIMIT",
        "FS_SCHEDULED_JOBS_INTERVAL_SECONDS",
        "FS_SECURITY_POSTURE_INTERVAL_SECONDS",
        "FS_WATCHDOG_INTERVAL_SECONDS",
        "FS_WATCHDOG_MAX_JOURNAL_ENTRIES",
        "FS_WATCHDOG_ORPHAN_GRACE_SECONDS",
        "FS_WATCHDOG_PICKUP_GRACE_SECONDS",
        "FS_WATCHDOG_STUCK_SECONDS",
        "FS_WORKFLOW_KNOWLEDGE_SYNC_CHANGELOG_LIMIT",
        "FS_WORKFLOW_KNOWLEDGE_SYNC_INTERVAL_SECONDS",
        "FS_RETENTION_INTERVAL_SECONDS",
        # Identity the scheduled-jobs scan uses to claim work; without it every
        # Compact control process presents the same anonymous scheduler.
        "CONTROL_PLANE_INSTANCE_ID",
        "SCHEDULED_JOB_SCHEDULER_ID",
    }
)
# Retention defaults for accounts with no saved policy: the app shows them on the
# settings page, and the control role's retention loop applies them together with
# the platform-wide windows only it reads.
_RETENTION_POLICY_ENV = frozenset(
    {
        "FS_RETENTION_LOGS_DAYS",
        "FS_RETENTION_MESSAGES_DAYS",
        "FS_RETENTION_TRASH_DAYS",
        "FS_RETENTION_TERMINAL_JOB_CHECKPOINTS_DAYS",
        "FS_RETENTION_REVISIONS_KEEP",
    }
)
_RETENTION_ENV = _RETENTION_POLICY_ENV | {
    "FS_RETENTION_JOBS_DAYS",
    "FS_RETENTION_EVENTS_DAYS",
    "FS_RETENTION_RETRY_QUEUE_DAYS",
}
_BROKER_ENV = frozenset(
    {
        "RABBITMQ_HOST",
        "RABBITMQ_PORT",
        "RABBITMQ_USER",
        "RABBITMQ_PASS",
        "RABBITMQ_URL",
        # Every broker connection validates RABBITMQ_URL against this mode, and an
        # absent mode means "tls" -- which rejects the plaintext compose broker URL,
        # so without it no Compact worker or heartbeat reporter could reach RabbitMQ
        # and a fresh install never became ready.
        "FS_RABBITMQ_TRANSPORT_MODE",
    }
)
_MONITORING_ENV = frozenset(
    {
        # The supervisor's scope and policy must reach the reporting roles and
        # control receiver. Never inherit an arbitrary FS_MONITORING_* prefix.
        "FS_COMPOSE_PROJECT_NAME",
        "FS_INSTALLATION_ID",
        "FS_MONITORING_EXPECTATIONS",
        "FS_MONITORING_DEPENDENCIES",
        "FS_MONITORING_INTERVAL_SECONDS",
        "FS_MONITORING_STALE_SECONDS",
        "FS_MONITORING_HISTORY_ENABLED",
        "FS_MONITORING_CURRENT_RETENTION_DAYS",
        "FS_MONITORING_HISTORY_RETENTION_DAYS",
        "FS_MONITORING_CLEANUP_BATCH_SIZE",
        "FS_MONITORING_QUEUE_MAX_MESSAGES",
        "FS_MONITORING_QUEUE_MAX_BYTES",
        "FS_MONITORING_OPERATION_TIMEOUT_SECONDS",
    }
)
_SECURITY_ENV = frozenset(
    {"FS_SECRET_KEY", "ENCRYPTION_KEY", "SECURITY_PII_SALT", "FS_MCP_AUTH_DISABLED"}
)
_RUNTIME_ENV = frozenset(
    {
        "ENVIRONMENT",
        "FS_STACK_ENVIRONMENT",
        "FS_DEPLOYMENT_PROFILE",
        "FS_PUBLIC_BASE_URL",
        "FS_AUTH_TRUSTED_ORIGINS",
        # AC17-R99: the catalogs are core functionality and must reach every role.
        # The app serves both catalog endpoints and the extension-installer process
        # performs the install the static catalog points at, so these belong in
        # _RUNTIME_ENV -- the one set every role inherits -- rather than in
        # application_env alone, which would leave installs from the catalog blind.
        "FS_EXTENSION_CATALOG_URL",
        "FS_MCP_CATALOG_URL",
        "FS_WORKFLOW_TEMPLATE_CATALOG_URL",
        # AC17-R105: the health endpoints gate machine-readable metrics on this token.
        # Absent, a collector can reach /health and /health/ready and nothing else, so
        # Compact could not be monitored at all -- and the installer generates the
        # token and writes it to .env.prod, so it looked configured.
        "FS_METRICS_TOKEN",
        "ARTIFACT_STORAGE_BACKEND",
        "ARTIFACT_LOCAL_ROOT_DIR",
        "ARTIFACT_S3_ENDPOINT_URL",
        "ARTIFACT_S3_BUCKET",
        "ARTIFACT_S3_REGION",
        "ARTIFACT_S3_USE_SSL",
        "ARTIFACT_S3_ACCESS_KEY",
        "ARTIFACT_S3_SECRET_KEY",
        "ARTIFACT_S3_PUBLIC_ENDPOINT_URL",
        "ARTIFACT_S3_KEY_PREFIX",
        "ARTIFACT_S3_PRESIGN_TTL_SECONDS",
        "FS_LOG_FORMAT",
        "FS_LOG_LEVEL",
        "FS_OBSERVABILITY_DSN",
        "SENTRY_DSN",
        "SENTRY_ENVIRONMENT",
        "FS_ALLOW_PRIVATE_REMOTE_URLS",
        "FS_EXTENSION_DEPS_FETCH",
        "FS_EXTENSION_DEPS_SITE_MAX_BYTES",
        "FS_EXTENSION_ARTIFACT_MAX_BYTES",
        "FS_BASE64_FILE_MAX_BYTES",
        "FS_WORKFLOW_TEST_FILE_MAX_BYTES",
        "FS_FILE_PREVIEW_MAX_BYTES",
        "FS_EXTENSION_OPERATION_TIMEOUT_SECONDS",
        "FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS",
        "FS_EXTENSIONS_ROOT",
        "FS_WORKFLOW_DISCOVERY_LEXICAL_FALLBACK_ENABLED",
        "FS_CONTEXT_GRAPH_BACKEND",
        "FS_DEFAULT_MODEL",
        "FS_DEFAULT_PROVIDER",
        "FS_DEFAULT_TEMPERATURE",
        "FS_FEED_MAX_BYTES",
        "FS_FEED_MAX_ROWS",
        "FS_MEMORY_KNOWLEDGE_BACKEND",
        "FS_MEMORY_PROFILE",
        "FS_MEMORY_REPOSITORY_BACKEND",
        "FS_MEMORY_TEXT_SEARCH_BACKEND",
        "FS_MEMORY_VECTOR_BACKEND",
        "FS_LLM_REQUEST_TIMEOUT_SECONDS",
        "FS_PGVECTOR_DSN",
        "FS_TABULAR_FEED_MAX_MATERIALIZE_BYTES",
        "FS_TABULAR_FEED_READ_ROWS_SCAN_MAX_BYTES",
        "FS_TABULAR_FEED_READ_ROWS_SCAN_MAX_ROWS",
        "MCP_URL",
    }
)


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    role: str
    command: tuple[str, ...]
    uid: int
    gid: int
    port: int | None
    environment: dict[str, str]
    pass_fds: tuple[int, ...] = ()
    supplementary_gids: tuple[int, ...] = ()


def _public_authority(parent: Mapping[str, str]) -> str:
    public_url = str(parent.get("FS_PUBLIC_BASE_URL") or "").strip()
    if not public_url:
        return ""
    parsed = urlsplit(public_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("FS_PUBLIC_BASE_URL must be a credential-free HTTP(S) origin")
    return parsed.netloc


#: Ports a client omits when it builds a URL or a Host header, per scheme.
_DEFAULT_SCHEME_PORTS = {"http": 80, "https": 443}


def _public_origin(parent: Mapping[str, str]) -> str:
    """Return the scheme://authority clients actually reach, validated like the authority.

    AC17-R55: the installer writes the public base URL with an explicit `:443`, which is why
    the MCP transport rejected the Host header real clients send. The same explicit port would
    otherwise be published in the OAuth discovery document, so it is dropped here too when it
    is the scheme default. A non-default port is preserved: an install on 8443 must advertise
    8443.
    """
    authority = _public_authority(parent)
    if not authority:
        return ""
    raw = str(parent.get("FS_PUBLIC_BASE_URL") or "").strip()
    scheme = urlsplit(raw).scheme
    parsed = urlsplit(f"//{authority}")
    if parsed.port is not None and parsed.port == _DEFAULT_SCHEME_PORTS.get(scheme):
        hostname = parsed.hostname or ""
        authority = f"[{hostname}]" if ":" in hostname else hostname
    return f"{scheme}://{authority}"


def _mcp_discovery_environment(parent: Mapping[str, str]) -> dict[str, str]:
    """AC17-R56: point OAuth discovery at the origin the client is actually on.

    `server_runtime` falls back to `http://fs-app:8099` and `http://localhost:8100`, which
    are development values naming a Scalable service and the caller's own machine. On Compact
    neither is reachable, and neither is declared in docker-compose.compact.yaml, so a 401
    from a public MCP call advertised `resource_metadata` on the client's own localhost.
    Nothing here invents a new configuration surface: both are derived from the one origin
    the operator already supplies, exactly as FS_MCP_ALLOWED_HOSTS is.
    """
    origin = _public_origin(parent)
    if not origin:
        return {}
    return {
        # The authorization server is the application, behind the same public origin.
        "FS_MCP_ISSUER_URL": origin,
        # The protected resource is this MCP endpoint at its public address. The SDK inserts
        # /.well-known/oauth-protected-resource between host and resource path, so this makes
        # the advertised document /.well-known/oauth-protected-resource/mcp.
        "FS_MCP_RESOURCE_URL": f"{origin}/mcp",
    }


def _environment(
    parent: Mapping[str, str],
    allowed: frozenset[str],
    additions: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build one child's environment from the parent's, then the explicit overrides.

    An inherited value that is empty is dropped rather than passed on. Compose has no
    way to declare a variable conditionally: every key in the overlay is substituted,
    and one the operator never set arrives as an empty string. Downstream that is not
    the same as absent -- `int(_runtime_env.get("FS_WATCHDOG_INTERVAL_SECONDS", "30"))`
    returns "30" for a missing key and raises for an empty one, and there are
    twenty-odd readings of that shape across the control and auth configuration. So a
    variable published for the operator's convenience and left unset took down the
    process that read it, and with the control plane down the whole runtime followed
    (AC17-R105, found on a real install rather than here).

    Explicit additions are passed through as given: those are the supervisor's own
    decisions about a role, and an empty one there would be deliberate.
    """
    result = {
        name: str(parent[name])
        for name in allowed
        if name in parent and str(parent[name]).strip() != ""
    }
    result.update({str(name): str(value) for name, value in (additions or {}).items()})
    return result


def _trusted_proxy_ips(parent: Mapping[str, str]) -> str:
    configured = str(parent.get("FS_PROXY_TRUSTED_IPS") or "").strip()
    return configured or "127.0.0.1/32,::1/128"


def delivered_environment_names() -> frozenset[str]:
    """Every variable name some Compact role is allowed to inherit from the supervisor.

    AC17-R121: which secrets the runtime's own processes read is already decided by
    these allowlists, so the question "which secrets must this runtime be able to
    produce for itself" can be answered from them instead of from a second list.
    """
    return (
        _COMMON_ENV
        | _DATABASE_ENV
        | _BROKER_ENV
        | _SECURITY_ENV
        | _RUNTIME_ENV
        | _AUTH_BOOTSTRAP_ENV
        | _CONTROL_LOOP_ENV
        | _MONITORING_ENV
        | _RETENTION_ENV
    )


def build_process_specs(
    settings: CompactRuntimeSettings,
    *,
    parent_environment: Mapping[str, str],
    public_port: int,
    observation_fd: int,
) -> tuple[ProcessSpec, ...]:
    """Build the only supported Compact role inventory."""
    ports = CompactPortMap.build(
        public_port=public_port,
        worker_processes=settings.worker_processes,
    )
    worker_token = str(parent_environment.get("FS_WORKER_MCP_BEARER_TOKEN") or "").strip()
    if not worker_token:
        raise ValueError("FS_WORKER_MCP_BEARER_TOKEN is required for Compact workers")
    application_env = (
        _COMMON_ENV
        | _DATABASE_ENV
        | _BROKER_ENV
        | _SECURITY_ENV
        | _RUNTIME_ENV
        | _AUTH_BOOTSTRAP_ENV
        | _MONITORING_ENV
        | _RETENTION_POLICY_ENV
    )
    worker_env = (
        _COMMON_ENV | _DATABASE_ENV | _BROKER_ENV | _SECURITY_ENV | _RUNTIME_ENV | _MONITORING_ENV
    )
    control_env = worker_env | _CONTROL_LOOP_ENV | _RETENTION_ENV
    # Installer owns a process reporter too; it needs the same broker projection,
    # but still must not inherit app auth bootstrap or worker bearer credentials.
    installer_env = worker_env

    specs = [
        ProcessSpec(
            role="router",
            command=(
                "/usr/local/bin/traefik",
                f"--entryPoints.compact.address=:{ports.public_port}",
                "--entryPoints.compact.transport.respondingTimeouts.readTimeout=30s",
                "--entryPoints.compact.transport.respondingTimeouts.writeTimeout=0s",
                "--entryPoints.compact.transport.respondingTimeouts.idleTimeout=180s",
                f"--entryPoints.compact.forwardedHeaders.trustedIPs={_trusted_proxy_ips(parent_environment)}",
                "--providers.file.filename=/run/flow-steward-compact/router.yaml",
                "--providers.file.watch=false",
                "--api.dashboard=false",
            ),
            uid=10006,
            gid=10006,
            port=ports.public_port,
            environment=_environment(parent_environment, _COMMON_ENV),
        ),
        ProcessSpec(
            role="app",
            command=(
                "python",
                "-m",
                "uvicorn",
                "core.entrypoints.api.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(ports.app_port),
                "--workers",
                str(settings.app_workers),
            ),
            uid=10001,
            gid=10001,
            port=ports.app_port,
            environment=_environment(
                parent_environment,
                application_env | {"FS_AUTH_COOKIE_SECURE"},
                {
                    # AC17-R123: a role that runs extension steps has to be able
                    # to hand one a URL for an artifact this storage cannot
                    # presign, and the address it serves from is its own. There is
                    # no default worth compiling in: the one the MCP role falls
                    # back to is a Scalable service name, wrong on every other
                    # profile, so an unset value must refuse rather than guess.
                    "FS_APP_INTERNAL_URL": f"http://127.0.0.1:{ports.app_port}",
                    "APP_PORT": str(ports.app_port),
                    "CONTROL_LOOPS_ENABLED": "0",
                    # AC17-R101: the installer proxy is what makes archive lifecycle work
                    # on Compact, and its switch reached no Compact role -- so the API
                    # process handled installs itself, was denied by a root it does not
                    # own, and the failure was reported as an unavailable capability.
                    # This is an addition rather than an allowlist entry on purpose: the
                    # app process cannot write /data/extensions under any environment, so
                    # delegation here is structural and must not be switchable.
                    "FS_EXTENSION_INSTALLER_PROXY_ENABLED": "1",
                    "FS_EXTENSION_INSTALLER_ORIGIN": f"http://127.0.0.1:{ports.installer_port}",
                    "FS_LOG_DIR": "/data/logs/app",
                },
            ),
            supplementary_gids=(10100, 10102),
        ),
        ProcessSpec(
            role="installer",
            command=(
                "python",
                "-m",
                "uvicorn",
                "core.entrypoints.api.extension_installer:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(ports.installer_port),
            ),
            uid=10005,
            gid=10005,
            port=ports.installer_port,
            environment=_environment(
                parent_environment,
                installer_env,
                {
                    "CONTROL_LOOPS_ENABLED": "0",
                    "FS_LOG_DIR": "/data/logs/installer",
                },
            ),
            supplementary_gids=(10103, 10102),
        ),
        ProcessSpec(
            role="mcp",
            command=(
                "python",
                "-m",
                "core.entrypoints.mcp.server",
                "--transport",
                "streamable-http",
                "--host",
                "127.0.0.1",
                "--port",
                str(ports.mcp_port),
            ),
            uid=10004,
            gid=10004,
            port=ports.mcp_port,
            environment=_environment(
                parent_environment,
                application_env,
                {
                    "FS_APP_INTERNAL_URL": f"http://127.0.0.1:{ports.app_port}",
                    "FS_MCP_ALLOWED_HOSTS": _public_authority(parent_environment),
                    "FS_LOG_DIR": "/data/logs/mcp",
                    **_mcp_discovery_environment(parent_environment),
                },
            ),
            supplementary_gids=(10102,),
        ),
        ProcessSpec(
            role="control",
            command=("python", "-m", "core.entrypoints.cli.control_process"),
            uid=10003,
            gid=10003,
            port=ports.control_metrics_port,
            environment=_environment(
                parent_environment,
                control_env,
                {
                    "FS_CONTROL_METRICS_PORT": str(ports.control_metrics_port),
                    "FS_PROCESS_METRICS_HOST": "127.0.0.1",
                    "FS_LOG_DIR": "/data/logs/control",
                },
            ),
            supplementary_gids=(10100, 10102),
        ),
    ]
    for index, metrics_port in enumerate(ports.worker_metrics_ports):
        role = f"worker-{index}"
        specs.append(
            ProcessSpec(
                role=role,
                command=("python", "-m", "core.entrypoints.cli.worker_process"),
                uid=10002,
                gid=10002,
                port=metrics_port,
                environment=_environment(
                    parent_environment,
                    worker_env,
                    {
                        # AC17-R123: a role that runs extension steps has to be able
                        # to hand one a URL for an artifact this storage cannot
                        # presign, and the address it serves from is its own. There is
                        # no default worth compiling in: the one the MCP role falls
                        # back to is a Scalable service name, wrong on every other
                        # profile, so an unset value must refuse rather than guess.
                        "FS_APP_INTERNAL_URL": f"http://127.0.0.1:{ports.app_port}",
                        "FS_COMPACT_ROLE": role,
                        "FS_COMPACT_OBSERVATION_FD": str(observation_fd),
                        "FS_COMPACT_HEALTH_INTERVAL_SECONDS": str(settings.health_interval_seconds),
                        "FS_MCP_GROUNDING_REQUIRED": "1",
                        "FS_PROCESS_METRICS_HOST": "127.0.0.1",
                        "FS_LOG_DIR": "/data/logs/worker",
                        "FS_WORKER_METRICS_PORT": str(metrics_port),
                        "MCP_BEARER_TOKEN": worker_token,
                        "MCP_URL": f"http://127.0.0.1:{ports.mcp_port}/mcp",
                    },
                ),
                pass_fds=(observation_fd,),
                supplementary_gids=(10100, 10102),
            )
        )
    return tuple(specs)
