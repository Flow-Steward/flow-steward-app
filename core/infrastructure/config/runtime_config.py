from pathlib import Path

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - fallback for lightweight environments

    def load_dotenv(*_args, **_kwargs):
        return False


from core.infrastructure.config.connection_strings import resolve_langgraph_postgres_uri
from core.infrastructure.config.env_registry import (
    registry_default_csv_tuple,
    registry_default_value,
)
from core.infrastructure.config.public_workflow import public_workflow_runtime_settings

_REPO_ROOT = Path(__file__).resolve().parents[3]
_EXAMPLE_ENV_PATH = _REPO_ROOT / ".env.example"
_EXAMPLE_ENV_DEFAULTS: dict[str, str] | None = None


def _load_example_env_defaults() -> dict[str, str]:
    global _EXAMPLE_ENV_DEFAULTS
    if _EXAMPLE_ENV_DEFAULTS is not None:
        return _EXAMPLE_ENV_DEFAULTS

    defaults: dict[str, str] = {}
    if _EXAMPLE_ENV_PATH.is_file():
        for line in _EXAMPLE_ENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            normalized_key = str(key or "").strip()
            if not normalized_key:
                continue
            defaults[normalized_key] = str(value or "").strip().strip('"').strip("'")

    _EXAMPLE_ENV_DEFAULTS = defaults
    return _EXAMPLE_ENV_DEFAULTS


def _parse_csv_items(raw: str) -> tuple[str, ...]:
    items: list[str] = []
    seen: set[str] = set()
    for item in str(raw or "").split(","):
        candidate = str(item or "").strip().lower()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        items.append(candidate)
    return tuple(items)


def _example_env_value(name: str) -> str:
    registry_value = str(registry_default_value(name) or "").strip()
    if registry_value:
        return registry_value
    value = str(_load_example_env_defaults().get(name, "") or "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required default for {name} in {_EXAMPLE_ENV_PATH.relative_to(_REPO_ROOT)}"
        )
    return value


def _example_env_csv_tuple(name: str) -> tuple[str, ...]:
    items = registry_default_csv_tuple(name)
    if not items:
        items = _parse_csv_items(_example_env_value(name))
    if not items:
        raise RuntimeError(
            f"Missing CSV defaults for {name} in {_EXAMPLE_ENV_PATH.relative_to(_REPO_ROOT)}"
        )
    return items


def _env_float(name: str, default: float) -> float:
    raw = str(_runtime_env.get(name, str(default)) or "").strip()
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = str(_runtime_env.get(name, str(default)) or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default)
    return max(int(minimum), value)


def _env_str(name: str, default: str = "") -> str:
    return str(_runtime_env.get(name, default) or "").strip()


def _registry_default(name: str, fallback: str = "") -> str:
    try:
        value = registry_default_value(name)
    except KeyError:
        return str(fallback)
    return str(fallback if value is None else value)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _runtime_env.get(name)
    if raw is None:
        return bool(default)
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _env_csv_tuple(name: str, default_items: tuple[str, ...]) -> tuple[str, ...]:
    raw = _runtime_env.get(name)
    if raw is None:
        items = default_items
    else:
        parsed = [str(item or "").strip().lower() for item in str(raw).split(",")]
        items = tuple(item for item in parsed if item)
        if not items:
            items = default_items

    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        candidate = str(item or "").strip().lower()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return tuple(normalized)


def _env_csv_set(
    name: str,
    default_items: tuple[str, ...],
    *,
    normalize_extension: bool = False,
) -> set[str]:
    raw = _runtime_env.get(name)
    if raw is None:
        items = default_items
    else:
        parsed = [str(item or "").strip().lower() for item in str(raw).split(",")]
        items = tuple(item for item in parsed if item)
        if not items:
            items = default_items

    normalized: set[str] = set()
    for item in items:
        candidate = str(item or "").strip().lower()
        if not candidate:
            continue
        if normalize_extension and not candidate.startswith("."):
            candidate = f".{candidate}"
        normalized.add(candidate)
    return normalized


def _runtime_environment() -> str:
    for key in ("FS_RUNTIME_ENVIRONMENT", "ENVIRONMENT", "FS_STACK_ENVIRONMENT"):
        value = str(_runtime_env.get(key, "") or "").strip().lower().replace("-", "_")
        if value:
            return value
    return ""


def _should_autoload_repo_dotenv() -> bool:
    mode = str(_runtime_env.get("FS_LOAD_REPO_DOTENV", "") or "").strip().lower()
    if mode in {"0", "false", "no", "off"}:
        return False
    if mode in {"1", "true", "yes", "on"}:
        return True
    return _runtime_environment() not in {"production"}


def _autoload_repo_dotenv() -> bool:
    if not _should_autoload_repo_dotenv():
        return False
    dotenv_path = _REPO_ROOT / ".env"
    return bool(load_dotenv(str(dotenv_path)))


_autoload_repo_dotenv()

_PUBLIC_WORKFLOW_SETTINGS = public_workflow_runtime_settings()


def _db_dsn_env() -> str:
    return _runtime_env.get("FS_DB_DSN", "")


def _observability_dsn_env() -> str:
    return _runtime_env.get("FS_OBSERVABILITY_DSN", "") or _registry_default(
        "FS_OBSERVABILITY_DSN", _db_dsn_env()
    )


def _langgraph_postgres_uri_env() -> str:
    return resolve_langgraph_postgres_uri(_runtime_env) or _db_dsn_env()


# ---------------------------------------------------------------------------
# Global infrastructure config (DB, LLM, etc.)
# Project-specific config (agents, assigned data connections, etc.) lives in the
# DB projects table and is loaded per-request via the Flow Steward logger.
# ---------------------------------------------------------------------------

CONFIG = {
    "fs_db_dsn": _db_dsn_env(),
    "idempotency_result_retention_days": (
        _PUBLIC_WORKFLOW_SETTINGS.idempotency_result_retention_days
    ),
    "public_sse_connections_per_principal": (
        _PUBLIC_WORKFLOW_SETTINGS.sse_connections_per_principal
    ),
    "public_sse_connections_per_account": _PUBLIC_WORKFLOW_SETTINGS.sse_connections_per_account,
    # Dedicated observability DSN (defaults to FS_DB_DSN when unset)
    "fs_observability_dsn": _observability_dsn_env(),
    # LangGraph state persistence (PostgresSaver)
    "langgraph_postgres_uri": _langgraph_postgres_uri_env(),
    # Default LLM provider settings (overridden per-agent via project agents_config)
    "default_provider": _runtime_env.get(
        "FS_DEFAULT_PROVIDER",
        _registry_default("FS_DEFAULT_PROVIDER", ""),
    ),
    "default_model": _runtime_env.get(
        "FS_DEFAULT_MODEL",
        _registry_default("FS_DEFAULT_MODEL", ""),
    ),
    "default_temperature": float(
        _runtime_env.get(
            "FS_DEFAULT_TEMPERATURE", _registry_default("FS_DEFAULT_TEMPERATURE", "0.1")
        )
    ),
    # Sentry (optional): if empty, runtime skips initialization.
    "sentry_dsn": _runtime_env.get("SENTRY_DSN", _registry_default("SENTRY_DSN", "")),
    "sentry_environment": _runtime_env.get(
        "SENTRY_ENVIRONMENT", _runtime_env.get("ENVIRONMENT", "")
    ),
    "sentry_traces_sample_rate": _runtime_env.get("SENTRY_TRACES_SAMPLE_RATE", "0.0"),
    "sentry_profiles_sample_rate": _runtime_env.get("SENTRY_PROFILES_SAMPLE_RATE", "0.0"),
    # Disable Sentry auto-enabling integrations by default on AI stacks unless explicitly enabled.
    "sentry_auto_enabling_integrations": _runtime_env.get("SENTRY_AUTO_ENABLING_INTEGRATIONS", "0"),
    # Runtime logging (stderr + daily file sink).
    "runtime_log_level": _runtime_env.get(
        "FS_LOG_LEVEL", _registry_default("FS_LOG_LEVEL", "INFO")
    ),
    "runtime_file_log_level": _runtime_env.get(
        "FS_LOG_FILE_LEVEL",
        _runtime_env.get("FS_LOG_LEVEL", _registry_default("FS_LOG_LEVEL", "INFO")),
    ),
    "runtime_stderr_log_level": _runtime_env.get(
        "FS_LOG_STDERR_LEVEL",
        _runtime_env.get("FS_LOG_LEVEL", _registry_default("FS_LOG_LEVEL", "INFO")),
    ),
    # AC17-R129: machine-readable by default. The development Compose file overrides
    # this to text for a human reading a terminal, which is a profile making its own
    # choice; a deployment with no Compose at all is a platform collecting logs, and
    # text there breaks the collector for no one's benefit.
    "runtime_log_format": _runtime_env.get("FS_LOG_FORMAT", "json"),
    "runtime_log_dir": _runtime_env.get("FS_LOG_DIR", ""),
    "runtime_log_basename": _runtime_env.get("FS_LOG_FILE_BASENAME", "platform"),
    "runtime_service_name": _runtime_env.get(
        "FS_SERVICE_NAME",
        _runtime_env.get("FS_LOG_FILE_BASENAME", "platform"),
    ),
    "runtime_log_file_enabled": _env_bool("FS_LOG_FILE_ENABLED", True),
    "runtime_log_stderr_enabled": _env_bool("FS_LOG_STDERR_ENABLED", True),
    "runtime_log_capture_warnings": _env_bool("FS_LOG_CAPTURE_WARNINGS", True),
    "runtime_log_retention_days": _env_int(
        "FS_LOG_RETENTION_DAYS",
        30,
        minimum=0,
    ),
    "runtime_log_utc": _env_bool("FS_LOG_UTC", False),
    # Runtime transport / static controls.
    "app_gzip_enabled": _env_bool("FS_GZIP_ENABLED", True),
    "gzip_minimum_size": _env_int("FS_GZIP_MINIMUM_SIZE", 512, minimum=256),
    "static_cache_max_age_seconds": _env_int(
        "FS_STATIC_CACHE_MAX_AGE_SECONDS",
        300,
        minimum=0,
    ),
    "static_asset_version": str(_runtime_env.get("FS_STATIC_ASSET_VERSION", "") or "").strip(),
    "dashboard_metrics_page_size": max(
        100,
        min(
            _env_int(
                "FS_DASHBOARD_METRICS_PAGE_SIZE",
                1000,
                minimum=100,
            ),
            5000,
        ),
    ),
    "dashboard_metrics_max_rows": _env_int("FS_DASHBOARD_METRICS_MAX_ROWS", 0, minimum=0),
    "dashboard_triage_limit": max(
        10,
        min(_env_int("FS_DASHBOARD_TRIAGE_LIMIT", 25, minimum=10), 200),
    ),
    "dashboard_rollup_read_enabled": _env_bool("FS_DASHBOARD_ROLLUP_READ_ENABLED", True),
    "workload_throttle_active_jobs": _env_int(
        "FS_WORKLOAD_THROTTLE_ACTIVE_JOBS",
        5,
        minimum=1,
    ),
    "workload_throttle_queue_depth": _env_int(
        "FS_WORKLOAD_THROTTLE_QUEUE_DEPTH",
        10,
        minimum=1,
    ),
    "workload_shed_active_jobs": _env_int(
        "FS_WORKLOAD_SHED_ACTIVE_JOBS",
        10,
        minimum=1,
    ),
    "workload_shed_queue_depth": _env_int(
        "FS_WORKLOAD_SHED_QUEUE_DEPTH",
        50,
        minimum=1,
    ),
    "workload_pause_active_jobs": _env_int(
        "FS_WORKLOAD_PAUSE_ACTIVE_JOBS",
        20,
        minimum=1,
    ),
    "workload_pause_queue_depth": _env_int(
        "FS_WORKLOAD_PAUSE_QUEUE_DEPTH",
        100,
        minimum=1,
    ),
    "extension_installer_proxy_enabled": _env_bool("FS_EXTENSION_INSTALLER_PROXY_ENABLED", False),
    # ---------------------------------------------------------------------------
    # Read-only static catalogs (workflow templates, extensions, MCP servers)
    # ---------------------------------------------------------------------------
    # Each value is the absolute HTTPS URL of one JSON index. The catalogs are
    # independent: one may be configured while the other is not. An empty value
    # leaves that catalog unconfigured and the product's existing non-catalog
    # paths untouched.
    "workflow_template_catalog_url": _env_str(
        "FS_WORKFLOW_TEMPLATE_CATALOG_URL", _registry_default("FS_WORKFLOW_TEMPLATE_CATALOG_URL")
    ),
    "extension_catalog_url": _env_str(
        "FS_EXTENSION_CATALOG_URL", _registry_default("FS_EXTENSION_CATALOG_URL")
    ),
    "mcp_server_catalog_url": _env_str(
        "FS_MCP_CATALOG_URL", _registry_default("FS_MCP_CATALOG_URL")
    ),
    "resume_join_timeout_seconds": _env_float("FS_RESUME_JOIN_TIMEOUT_SECONDS", 15.0),
    "job_runtime_payload_timeout_seconds": _env_float(
        "FS_JOB_RUNTIME_PAYLOAD_TIMEOUT_SECONDS",
        2.5,
    ),
    "workspaces_root": str(_runtime_env.get("FS_WORKSPACES_ROOT", "logs/workspaces") or "").strip()
    or "logs/workspaces",
    "metrics_token": _runtime_env.get("FS_METRICS_TOKEN", ""),
    "process_metrics_host": _runtime_env.get("FS_PROCESS_METRICS_HOST", "0.0.0.0"),
    "worker_metrics_port": _env_int("FS_WORKER_METRICS_PORT", 0, minimum=0),
    "control_metrics_port": _env_int("FS_CONTROL_METRICS_PORT", 0, minimum=0),
    # Attachment upload policy
    "attachment_max_size_bytes": _env_int(
        "FS_ATTACHMENT_MAX_SIZE_BYTES",
        20 * 1024 * 1024,
        minimum=0,
    ),
    "attachment_max_files": _env_int("FS_ATTACHMENT_MAX_FILES", 10, minimum=0),
    "allowed_attachment_mimes": _env_csv_set(
        "FS_ALLOWED_ATTACHMENT_MIMES",
        _example_env_csv_tuple("FS_ALLOWED_ATTACHMENT_MIMES"),
    ),
    "allowed_attachment_extensions": _env_csv_set(
        "FS_ALLOWED_ATTACHMENT_EXTENSIONS",
        _example_env_csv_tuple("FS_ALLOWED_ATTACHMENT_EXTENSIONS"),
        normalize_extension=True,
    ),
    # Extension payload redaction policy
    "extension_debug_payloads": _env_bool("FS_EXTENSION_DEBUG_PAYLOADS", False),
    "extension_payload_redact_keys": frozenset(
        _env_csv_tuple(
            "FS_EXTENSION_PAYLOAD_REDACT_KEYS",
            _example_env_csv_tuple("FS_EXTENSION_PAYLOAD_REDACT_KEYS"),
        )
    ),
    "extension_payload_redact_exact_keys": frozenset(
        _env_csv_tuple(
            "FS_EXTENSION_PAYLOAD_REDACT_EXACT_KEYS",
            _example_env_csv_tuple("FS_EXTENSION_PAYLOAD_REDACT_EXACT_KEYS"),
        )
    ),
    "extension_payload_redact_key_fragments": _env_csv_tuple(
        "FS_EXTENSION_PAYLOAD_REDACT_KEY_FRAGMENTS",
        _example_env_csv_tuple("FS_EXTENSION_PAYLOAD_REDACT_KEY_FRAGMENTS"),
    ),
    "extension_payload_redact_max_depth": _env_int(
        "FS_EXTENSION_PAYLOAD_REDACT_MAX_DEPTH",
        8,
        minimum=1,
    ),
    "extension_payload_string_truncate_length": _env_int(
        "FS_EXTENSION_PAYLOAD_STRING_TRUNCATE_LENGTH",
        500,
        minimum=32,
    ),
    "trace_redact_max_length": _env_int(
        "FS_TRACE_REDACT_MAX_LENGTH",
        1000,
        minimum=32,
    ),
}
