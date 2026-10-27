from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from core.infrastructure.installer.secret_values import is_missing_or_placeholder_secret

HTTP_SEAWEEDFS_8333_VALUE = "http://seaweedfs:8333"


_MANIFEST_PATH = Path(__file__).with_name("flow_steward_env_manifest.json")
# Leaf secrets the installer generates. This gate is separate from the
# manifest's installer_managed flag, so a variable can be declared managed,
# required by the doctor and demanded by the lifecycle validator while nothing
# ever creates it — which is exactly what happened to FS_BACKUP_ENCRYPTION_KEY:
# validate_lifecycle_configuration adds it to required_secrets by name, but it
# was absent here, so a deployment built from the documented flow failed the
# doctor on a secret no step produced.
_PLACEHOLDER_SECRET_NAMES = frozenset(
    {
        "FS_SECRET_KEY",
        "FS_METRICS_TOKEN",
        "ENCRYPTION_KEY",
        "FS_BACKUP_ENCRYPTION_KEY",
        "POSTGRES_PASSWORD",
        "RABBITMQ_PASS",
        "SECURITY_PII_SALT",
    }
)

# These values are produced by the immutable candidate builder rather than the
# interactive installer, but production Compose requires them while the
# lifecycle service renders an in-memory candidate with ``--env-file /dev/null``.
_LIFECYCLE_COMPOSE_RUNTIME_KEYS = frozenset(
    {
        "AWS_CA_BUNDLE",
        "FS_BUILD_SHA",
        "FS_CANDIDATE_PROD_ENV",
        "FS_CANDIDATE_STATE_DIR",
        "FS_CLI_IMAGE",
        "FS_CLI_IMAGE_REVISION",
        "FS_APP_IMAGE",
        "FS_APP_IMAGE_DIGEST",
        "FS_APP_IMAGE_REVISION",
        "FS_WORKER_IMAGE",
        "FS_WORKER_IMAGE_DIGEST",
        "FS_WORKER_IMAGE_REVISION",
        "FS_CONTROL_IMAGE",
        "FS_CONTROL_IMAGE_DIGEST",
        "FS_CONTROL_IMAGE_REVISION",
        "FS_COMPACT_IMAGE",
        "FS_COMPACT_IMAGE_CONFIG_DIGEST",
        "FS_COMPACT_IMAGE_DIGEST",
        "FS_COMPACT_IMAGE_INDEX_DIGEST",
        "FS_COMPACT_IMAGE_MANIFEST_DIGEST",
        "FS_COMPACT_IMAGE_PLATFORM",
        "FS_COMPACT_IMAGE_REGISTRY_REF",
        "FS_COMPACT_IMAGE_REVISION",
        "FS_COMPACT_IMAGE_ROLE",
        "FS_FRONTEND_IMAGE",
        "FS_FRONTEND_IMAGE_REVISION",
        "FS_WORKER_MCP_BEARER_TOKEN",
        "MCP_IMAGE",
        "MCP_IMAGE_DIGEST",
        "MCP_IMAGE_REVISION",
        "POSTGRES_IMAGE",
        "RABBITMQ_IMAGE",
        "TRAEFIK_IMAGE",
    }
)

_CLONE_TARGET_OWNED_NAMES = frozenset(
    {
        "APP_PORT",
        "ENVIRONMENT",
        "FS_ACME_CONTACT",
        "FS_ACME_DIRECTORY_URL",
        "FS_AUTH_TRUSTED_ORIGINS",
        "FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY",
        "FS_BACKUP_ENCRYPTION_KEY",
        "FS_COMPOSE_PROJECT_NAME",
        "FS_INSTALLATION_ID",
        "FS_MONITORING_EXPECTATIONS",
        "FS_MONITORING_DEPENDENCIES",
        "FS_DB_DSN",
        "FS_DOCKER_GID",
        "FS_DOCKER_HOST_PROJECT_ROOT",
        "FS_EXTENSIONS_DIR",
        "FS_HOST_GID",
        "FS_HOST_UID",
        "FS_INGRESS_MODE",
        "FS_LOGS_DIR",
        "FS_MCP_ISSUER_URL",
        "FS_MCP_RESOURCE_URL",
        "FS_PROXY_NETWORK",
        "FS_PUBLIC_BASE_URL",
        "FS_PUBLIC_BIND_HOST",
        "FS_PUBLIC_HOST",
        "FS_PUBLIC_PORT",
        "FS_RUNTIME_HTTP_PORT",
        "FS_REVERSE_PROXY_BACKEND_PORT",
        "FS_TLS_CERTIFICATE_PATH",
        "FS_TLS_MODE",
        "FS_TLS_PRIVATE_KEY_PATH",
        "HTTP_PORT",
        "LANGGRAPH_POSTGRES_URI",
        "POSTGRES_DB",
        "POSTGRES_HOST",
        "POSTGRES_PASSWORD",
        "POSTGRES_PORT",
        "POSTGRES_USER",
    }
)
_CLONE_DERIVED_NAMES = frozenset(
    {
        "FS_BUILD_REVISION",
        "FS_BUILD_SHA",
        "FS_DEPLOYMENT_REVISION",
        "FS_FRONTEND_IMAGE",
        "FS_MCP_IMAGE_REVISION",
    }
)
# RabbitMQ's restored volume contains its users and password hashes. Its leaf
# credential must travel with that state; RABBITMQ_URL is regenerated from it.
# PostgreSQL server-role credentials remain target-owned for the logical DB restore.
_CLONE_SOURCE_OWNED_NAMES = frozenset({"RABBITMQ_PASS", "RABBITMQ_USER"})
_CLONE_TARGET_OWNED_GROUPS = frozenset(
    {
        "compact_runtime",
        "production_host_paths",
        "production_images",
        "release_discovery",
    }
)
_CLONE_SOURCE_OWNED_GROUPS = frozenset(
    {
        "artifact_storage_bundled_seaweedfs",
        "artifact_storage_external_s3",
        "artifact_storage_local_fs",
        "core_attachment_policy",
        "core_redaction_pii",
        "core_security_auth",
        "graph_postgres_age",
        "mcp",
        "openapi_docs",
        "ops_observability",
        "provider_anthropic",
        "provider_defaults",
        "provider_google",
        "provider_openai",
        "static_catalogs",
        "vector_pgvector",
    }
)


class CloneEnvOwner(StrEnum):
    SOURCE = "source"
    TARGET = "target"
    DERIVED = "derived"


@dataclass(frozen=True)
class EnvVarSpec:
    name: str
    groups: tuple[str, ...]
    environments: tuple[str, ...]
    installer_managed: bool
    doctor_required: bool
    doctor_secret: bool
    doctor_provider_key: bool
    secret: bool
    clone_owner: CloneEnvOwner
    preserve_existing: bool
    default: str
    production_default: str
    local_development_default: str
    owner: str
    notes: str

    @property
    def clone_source_owned(self) -> bool:
        return self.clone_owner is CloneEnvOwner.SOURCE

    def default_for_environment(self, environment: str) -> str:
        if environment == "local_development" and self.local_development_default:
            return self.local_development_default
        if environment == "production" and self.production_default:
            return self.production_default
        return self.default


@dataclass(frozen=True)
class StackEnvSelection:
    environment: str = "production"
    graph_storage: str = "postgres_age"
    vector_search: str = "pgvector"
    file_storage: str = "external_s3"
    test_runner: bool = False
    audit_tools: bool = False
    quality_tools: bool = False


def manifest_path() -> Path:
    return _MANIFEST_PATH


@lru_cache(maxsize=1)
def _manifest() -> dict[str, object]:
    import json

    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def group_metadata() -> dict[str, dict[str, str]]:
    raw = _manifest().get("groups") or {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): {str(key): str(value) for key, value in dict(meta or {}).items()}
        for name, meta in raw.items()
    }


@lru_cache(maxsize=1)
def env_specs() -> tuple[EnvVarSpec, ...]:
    raw_items = _manifest().get("variables") or []
    items = raw_items if isinstance(raw_items, list) else []
    specs: list[EnvVarSpec] = []
    for item in items:
        row = dict(item or {})
        specs.append(
            EnvVarSpec(
                name=str(row.get("name") or ""),
                groups=tuple(str(value) for value in row.get("groups") or ()),
                environments=tuple(
                    str(value)
                    for value in row.get("environments") or ("production", "local_development")
                ),
                installer_managed=bool(row.get("installer_managed", False)),
                doctor_required=bool(row.get("doctor_required", False)),
                doctor_secret=bool(row.get("doctor_secret", False)),
                doctor_provider_key=bool(row.get("doctor_provider_key", False)),
                secret=bool(row.get("secret", False)),
                clone_owner=_clone_owner(row),
                preserve_existing=bool(row.get("preserve_existing", True)),
                default=str(row.get("default") or ""),
                production_default=str(row.get("production_default") or ""),
                local_development_default=str(row.get("local_development_default") or ""),
                owner=str(row.get("owner") or ""),
                notes=str(row.get("notes") or ""),
            )
        )
    return tuple(specs)


def _clone_owner(row: Mapping[str, object]) -> CloneEnvOwner:
    """Return a closed Clone classification; unclassified values are never imported."""
    name = str(row.get("name") or "")
    raw_groups = row.get("groups")
    groups = (
        {str(value) for value in raw_groups} if isinstance(raw_groups, (list, tuple)) else set()
    )
    if name in _CLONE_TARGET_OWNED_NAMES or groups.intersection(_CLONE_TARGET_OWNED_GROUPS):
        return CloneEnvOwner.TARGET
    if name in _CLONE_DERIVED_NAMES:
        return CloneEnvOwner.DERIVED
    if name in _CLONE_SOURCE_OWNED_NAMES or groups.intersection(_CLONE_SOURCE_OWNED_GROUPS):
        return CloneEnvOwner.SOURCE
    return CloneEnvOwner.DERIVED


@lru_cache(maxsize=1)
def env_spec_map() -> dict[str, EnvVarSpec]:
    return {spec.name: spec for spec in env_specs()}


def registry_default_value(name: str, *, environment: str = "production") -> str:
    spec = env_spec_map().get(name)
    if spec is None:
        raise KeyError(name)
    return spec.default_for_environment(_normalize_environment(environment))


def registry_default_csv_tuple(name: str, *, environment: str = "production") -> tuple[str, ...]:
    value = registry_default_value(name, environment=environment)
    return tuple(_parse_csv_items(value))


def selected_groups(selection: StackEnvSelection) -> tuple[str, ...]:
    environment = _normalize_environment(selection.environment)
    if selection.graph_storage != "postgres_age":
        raise ValueError("graph_storage must be postgres_age for the first release")
    if selection.vector_search != "pgvector":
        raise ValueError("vector_search must be pgvector for the first release")
    groups = [
        "core_runtime",
        "core_security_auth",
        "core_attachment_policy",
        "core_redaction_pii",
        "provider_defaults",
        "provider_openai",
        "provider_anthropic",
        "provider_google",
    ]
    groups.append("graph_postgres_age")
    groups.append("vector_pgvector")
    if selection.file_storage in {"bundled", "bundled_seaweedfs"}:
        groups.append("artifact_storage_bundled_seaweedfs")
    elif selection.file_storage == "local_fs":
        groups.append("artifact_storage_local_fs")
    else:
        groups.append("artifact_storage_external_s3")
    if environment == "local_development":
        groups.append("local_development")
        if selection.test_runner:
            groups.append("test_tools")
        if selection.audit_tools:
            groups.append("audit_tools")
        if selection.quality_tools:
            groups.append("quality_tools")
    else:
        groups.append("production_images")
        groups.append("production_host_paths")
        groups.append("release_discovery")
    return tuple(dict.fromkeys(groups))


def selection_from_services(
    *,
    stack_environment: str,
    required_services: Iterable[str],
    runtime_services: Iterable[str] | None = None,
    file_storage: str | None = None,
) -> StackEnvSelection:
    required = set(required_services)
    runtime = set(runtime_services or ())
    all_services = required | runtime
    selected_file_storage = str(file_storage or "").strip()
    if selected_file_storage not in {
        "bundled",
        "bundled_seaweedfs",
        "external_s3",
        "local_fs",
    }:
        selected_file_storage = (
            "bundled_seaweedfs" if "seaweedfs" in all_services else "external_s3"
        )
    return StackEnvSelection(
        environment=_normalize_environment(stack_environment),
        graph_storage="postgres_age",
        vector_search="pgvector",
        file_storage=selected_file_storage,
        test_runner="fs-test-runner" in all_services,
        audit_tools="fs-audit" in all_services,
        quality_tools="sonarqube" in all_services or "sonar-scanner" in all_services,
    )


def build_installer_env(
    *,
    selection: StackEnvSelection,
    existing: Mapping[str, str],
    secret_factory,
    deployment_profile: str = "scalable",
) -> dict[str, str]:
    normalized = StackEnvSelection(
        environment=_normalize_environment(selection.environment),
        graph_storage=selection.graph_storage,
        vector_search=selection.vector_search,
        file_storage=selection.file_storage,
        test_runner=bool(selection.test_runner),
        audit_tools=bool(selection.audit_tools),
        quality_tools=bool(selection.quality_tools),
    )
    groups = set(selected_groups(normalized))
    selected_secret_names = {
        spec.name
        for spec in _selected_specs(normalized)
        if spec.installer_managed and spec.name in _PLACEHOLDER_SECRET_NAMES
    }
    secret_values = {
        name: (
            str(secret_factory()).strip()
            if is_missing_or_placeholder_secret(existing.get(name))
            else str(existing[name]).strip()
        )
        for name in selected_secret_names
    }
    base_url = str(
        existing.get("FS_PUBLIC_BASE_URL")
        or registry_default_value("FS_PUBLIC_BASE_URL", environment=normalized.environment)
    ).strip()
    result: dict[str, str] = {}
    for spec in env_specs():
        if not spec.installer_managed:
            continue
        if normalized.environment not in spec.environments:
            continue
        if not groups.intersection(spec.groups):
            continue
        if (
            normalized.environment == "production"
            and deployment_profile == "compact"
            and spec.name == "MCP_IMAGE"
            and not str(existing.get(spec.name) or "").strip()
        ):
            # Compact embeds MCP. Do not turn an absent optional image into a
            # mutable, unrelated default; explicit selections remain intact.
            continue
        result[spec.name] = _resolve_installer_value(
            spec.name,
            existing=existing,
            selection=normalized,
            secret_values=secret_values,
            base_url=base_url,
        )
    return result


def build_lifecycle_env(
    *,
    selection: StackEnvSelection,
    existing: Mapping[str, str],
    deployment_profile: str = "scalable",
) -> dict[str, str]:
    """Build managed values for canonical lifecycle configuration.

    The env manifest retains its historical internal group names, but lifecycle
    state and emitted runtime markers use only the public canonical values.
    """
    environment = _canonical_environment(selection.environment)
    file_storage = _canonical_file_storage(selection.file_storage)
    manifest_selection = StackEnvSelection(
        environment=("local_development" if environment == "development" else "production"),
        graph_storage=selection.graph_storage,
        vector_search=selection.vector_search,
        file_storage=("bundled_seaweedfs" if file_storage == "bundled" else file_storage),
        test_runner=bool(selection.test_runner),
        audit_tools=bool(selection.audit_tools),
        quality_tools=bool(selection.quality_tools),
    )
    required_secrets = set(generated_secret_env_keys_for_selection(manifest_selection))
    required_secrets.add("FS_BACKUP_ENCRYPTION_KEY")
    requires_artifact_credentials = file_storage in {"bundled", "external_s3"}
    if requires_artifact_credentials:
        required_secrets.update(("ARTIFACT_S3_ACCESS_KEY", "ARTIFACT_S3_SECRET_KEY"))
    missing = sorted(
        name for name in required_secrets if is_missing_or_placeholder_secret(existing.get(name))
    )
    if missing:
        raise ValueError(
            "Lifecycle configuration is missing installer-provisioned durable secrets: "
            + ", ".join(missing)
        )
    result = build_installer_env(
        selection=manifest_selection,
        existing=existing,
        secret_factory=_lifecycle_secret_generation_forbidden,
        deployment_profile=deployment_profile,
    )
    for name in _LIFECYCLE_COMPOSE_RUNTIME_KEYS:
        value = str(existing.get(name) or "").strip()
        if value:
            result[name] = value
    result["ENVIRONMENT"] = environment
    result["FS_STACK_ENVIRONMENT"] = environment
    if file_storage == "bundled":
        for name in ("ARTIFACT_S3_ACCESS_KEY", "ARTIFACT_S3_SECRET_KEY"):
            result[name] = str(existing[name]).strip()
    return result


def _lifecycle_secret_generation_forbidden() -> str:
    raise RuntimeError("Lifecycle configuration must not generate durable secrets")


def required_env_keys_for_selection(selection: StackEnvSelection) -> tuple[str, ...]:
    selected = _selected_specs(selection)
    return tuple(spec.name for spec in selected if spec.doctor_required)


def installer_generated_secret_names() -> frozenset[str]:
    """Return every leaf secret the installer generates, independent of any selection.

    AC17-R121: a runtime the installer never touched has to know which secrets nobody
    is going to hand it, and the answer already exists here. Exposed rather than
    copied so the two cannot drift -- the comment above this set records what happened
    the last time a secret was required by one layer and produced by none.
    """
    return _PLACEHOLDER_SECRET_NAMES


def secret_env_keys_for_selection(selection: StackEnvSelection) -> tuple[str, ...]:
    selected = _selected_specs(selection)
    return tuple(spec.name for spec in selected if spec.doctor_secret)


def generated_secret_env_keys_for_selection(selection: StackEnvSelection) -> tuple[str, ...]:
    """Return selected installer-generated leaf secrets, excluding derived DSNs."""
    selected_names = {spec.name for spec in _selected_specs(selection) if spec.installer_managed}
    return tuple(sorted(name for name in _PLACEHOLDER_SECRET_NAMES if name in selected_names))


def image_pin_keys() -> tuple[str, ...]:
    return (
        "FS_APP_IMAGE",
        "FS_WORKER_IMAGE",
        "FS_CONTROL_IMAGE",
        "MCP_IMAGE",
        "TRAEFIK_IMAGE",
        "POSTGRES_IMAGE",
        "RABBITMQ_IMAGE",
    )


def auth_default_expectations(environment: str) -> dict[str, str]:
    normalized = _normalize_environment(environment)
    return {
        "FS_LOCAL_AUTH_ENABLED": registry_default_value(
            "FS_LOCAL_AUTH_ENABLED", environment=normalized
        ),
        "FS_AUTH_COOKIE_SECURE": registry_default_value(
            "FS_AUTH_COOKIE_SECURE", environment=normalized
        ),
        "FS_AUTH_BOOTSTRAP_ENABLED": registry_default_value(
            "FS_AUTH_BOOTSTRAP_ENABLED", environment=normalized
        ),
    }


def redaction_policy_keys() -> tuple[str, ...]:
    groups = {"core_attachment_policy", "core_redaction_pii"}
    return tuple(
        spec.name
        for spec in env_specs()
        if spec.doctor_required and groups.intersection(spec.groups)
    )


def _selected_specs(selection: StackEnvSelection) -> tuple[EnvVarSpec, ...]:
    groups = set(selected_groups(selection))
    environment = _normalize_environment(selection.environment)
    return tuple(
        spec
        for spec in env_specs()
        if environment in spec.environments and groups.intersection(spec.groups)
    )


def _resolve_installer_value(
    name: str,
    *,
    existing: Mapping[str, str],
    selection: StackEnvSelection,
    secret_values: Mapping[str, str],
    base_url: str,
) -> str:
    if name in secret_values:
        return str(secret_values[name])
    if name == "FS_RABBITMQ_TRANSPORT_MODE":
        return "compose-local"
    if name == "FS_STACK_ENVIRONMENT":
        return selection.environment
    if name == "ENVIRONMENT":
        return selection.environment
    if name == "FS_AUTH_COOKIE_SECURE":
        return "0" if selection.environment == "local_development" else "1"
    if name == "FS_AUTH_BOOTSTRAP_ENABLED":
        return registry_default_value(name, environment=selection.environment)
    if name == "FS_MEMORY_VECTOR_BACKEND":
        return "postgres_pgvector"
    if name == "FS_CONTEXT_GRAPH_BACKEND":
        return "postgres_age"
    if name == "ARTIFACT_STORAGE_BACKEND":
        return "local_fs" if selection.file_storage == "local_fs" else "s3"
    if name == "ARTIFACT_LOCAL_ROOT_DIR":
        return str(existing.get(name) or "/data/artifacts")
    if name == "ARTIFACT_S3_ENDPOINT_URL":
        if selection.file_storage == "bundled_seaweedfs":
            return HTTP_SEAWEEDFS_8333_VALUE
        existing_endpoint = str(existing.get(name) or "")
        return "" if existing_endpoint == HTTP_SEAWEEDFS_8333_VALUE else existing_endpoint
    if name == "ARTIFACT_S3_USE_SSL":
        if selection.file_storage == "bundled_seaweedfs":
            return "false"
        existing_endpoint = str(existing.get("ARTIFACT_S3_ENDPOINT_URL") or "")
        existing_ssl = str(existing.get(name) or "")
        if existing_ssl and existing_endpoint and existing_endpoint != HTTP_SEAWEEDFS_8333_VALUE:
            return existing_ssl
        return registry_default_value(name, environment=selection.environment)
    existing_value = str(existing.get(name) or "")
    if existing_value:
        spec = env_spec_map().get(name)
        if spec is None or spec.preserve_existing:
            return existing_value
    postgres_password = secret_values["POSTGRES_PASSWORD"]
    rabbitmq_pass = secret_values["RABBITMQ_PASS"]
    if name == "FS_DB_DSN":
        return (
            f"dbname=flow_steward user=flow_steward password={postgres_password} "
            "host=pg-fs port=5432"
        )
    if name == "LANGGRAPH_POSTGRES_URI":
        return f"postgresql://flow_steward:{postgres_password}@pg-fs:5432/flow_steward"
    if name == "RABBITMQ_URL":
        return f"amqp://flow_steward:{rabbitmq_pass}@rabbitmq-fs:5672/"
    if name == "FS_PUBLIC_BASE_URL":
        return base_url
    if name == "FS_AUTH_TRUSTED_ORIGINS":
        return str(existing.get(name) or base_url)
    if name == "ARTIFACT_S3_BUCKET":
        return str(existing.get(name) or "flow-steward-artifacts")
    if name == "SECURITY_PII_SALT":
        return str(existing.get(name) or secret_values[name])
    return registry_default_value(name, environment=selection.environment)


def _normalize_environment(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    return (
        "local_development" if normalized in {"development", "local_development"} else "production"
    )


def _canonical_environment(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized == "development":
        return "development"
    if normalized == "production":
        return "production"
    raise ValueError("Lifecycle environment must be development or production")


def _canonical_file_storage(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized == "bundled":
        return "bundled"
    if normalized == "external_s3":
        return "external_s3"
    if normalized == "local_fs":
        return "local_fs"
    raise ValueError("Lifecycle file storage must be local_fs, bundled, or external_s3")


def _parse_csv_items(raw: str) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for item in str(raw or "").split(","):
        candidate = str(item or "").strip().lower()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        values.append(candidate)
    return tuple(values)
