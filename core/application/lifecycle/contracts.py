"""Closed contracts for durable lifecycle operations."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from threading import Event, RLock
from typing import Any, Literal, Self
from urllib.parse import SplitResult, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from core.application.management.command_specs.contracts import OperationStep


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _own_data_placement(profile: object) -> str:
    return "compact_volume" if profile == "compact" else "host_directories"


class DeploymentConfig(_ClosedModel):
    """Canonical non-secret configuration for one installed deployment."""

    environment: Literal["development", "production"] = "development"
    deployment_profile: Literal["compact", "scalable"] = "scalable"
    runtime_http_port: int = Field(default=8080, ge=1, le=65535)
    redirect_http_port: int = Field(default=8080, ge=1, le=65535)
    worker_processes: int = Field(default=2, ge=1, le=56336)
    # Review P2: Scalable scales workers by container replicas. Configuration, not a manual
    # `--scale`, because every lifecycle `compose up` returns a service to its declared
    # count and silently dropped the extra workers. Compact runs one runtime container and
    # scales worker_processes inside it instead.
    worker_replicas: int = Field(default=1, ge=1, le=64)
    # ADR 0029, decision B (item 5): where the shared trees -- artifacts, extensions and
    # the test project -- live. host_directories: the Scalable host directories;
    # compact_volume: the Compact data volume. It follows from the profile the
    # installation was created with, and a profile switch keeps it, so the other
    # profile mounts the trees where they are: nothing is copied or moved.
    data_placement: Literal["host_directories", "compact_volume"] = "host_directories"
    file_storage: Literal["local_fs", "bundled", "external_s3"] = "bundled"
    ingress_mode: Literal["bundled_traefik", "external_traefik", "external_proxy", "platform"] = (
        "bundled_traefik"
    )
    development_tools: tuple[Literal["test_runner", "audit_tools", "sonarqube"], ...] = ()
    tls_mode: Literal[
        "local",
        "self_signed",
        "provided",
        "acme",
        "reverse_proxy",
        "external_resolver",
        "external_certificate",
        "platform_managed",
    ] = "local"
    public_host: str = "localhost"
    public_port: int = Field(default=8099, ge=1, le=65535)
    reverse_proxy_backend_host: str = "127.0.0.1"
    reverse_proxy_backend_port: int = Field(default=18099, ge=1, le=65535)
    reverse_proxy_mcp_port: int = Field(default=18100, ge=1, le=65535)
    proxy_placement: Literal["host", "docker"] = "host"
    proxy_kind: Literal["nginx", "apache"] = "nginx"
    proxy_trusted_ips: tuple[str, ...] = ("127.0.0.1/32", "::1/128")
    traefik_network: str = ""
    traefik_https_entrypoint: str = "websecure"
    traefik_http_entrypoint: str = "web"
    traefik_cert_resolver: str = ""
    proxy_network: str = ""
    acme_contact: str = ""
    acme_directory_url: str = ""
    extension_installer_origin: str = "http://fs-extension-installer:8099"
    certificate_path: str = ".ssl/cert.pem"
    private_key_path: str = ".ssl/key.pem"
    s3_endpoint: str = ""
    s3_bucket: str = "flow-steward-artifacts"
    s3_region: str = "us-east-1"
    s3_credentials_path: str = ""
    allow_insecure_s3_http: bool = False
    # Host settings the operator set by name (operator_settings.py); written to the
    # managed block of the runtime environment. Only the shape is checked here, so an
    # installation keeps loading if the manifest later stops listing a setting.
    operator_settings: dict[str, str] = Field(default_factory=dict)

    @field_validator("operator_settings", mode="before")
    @classmethod
    def parse_operator_settings_text(cls, value: Any) -> dict[str, str]:
        from core.application.lifecycle.operator_settings import parse_operator_settings

        return {
            name: setting for name, setting in parse_operator_settings(value).items() if setting
        }

    @model_validator(mode="before")
    @classmethod
    def discard_hidden_storage_inputs(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if isinstance(normalized.get("proxy_trusted_ips"), str):
            normalized["proxy_trusted_ips"] = tuple(
                item.strip() for item in normalized["proxy_trusted_ips"].split(",") if item.strip()
            )
        if normalized.get("proxy_kind") == "caddy":
            # Caddy was dropped from the supported set. An installation that chose it
            # still has the value on disk, and every configuration read goes through
            # DeploymentConfig.model_validate -- so rejecting it here would not remove
            # support, it would make that installation unreadable, rollback snapshots
            # included. proxy_kind only selects which fragment the operator is shown;
            # nothing at runtime depends on it, and the operator's own proxy is not
            # ours to touch. So the stored value is carried forward as nginx and the
            # next fragment they ask for is an nginx one.
            normalized["proxy_kind"] = "nginx"
        if not normalized.get("data_placement"):
            # An installation written before the field: its trees are where its profile
            # put them.
            normalized["data_placement"] = _own_data_placement(
                normalized.get("deployment_profile", "scalable")
            )
        if "ingress_mode" not in normalized:
            legacy_tls = normalized.get("tls_mode", "local")
            normalized["ingress_mode"] = (
                "external_proxy" if legacy_tls == "reverse_proxy" else "bundled_traefik"
            )
        if (
            normalized.get("deployment_profile", "scalable") == "compact"
            and "file_storage" not in normalized
        ):
            normalized["file_storage"] = "local_fs"
        if normalized.get("file_storage", "bundled") == "external_s3":
            return normalized
        normalized.update(
            {
                "s3_endpoint": "",
                "s3_bucket": "",
                "s3_region": "",
                "s3_credentials_path": "",
                "allow_insecure_s3_http": False,
            }
        )
        return normalized

    @model_serializer(mode="wrap")
    def omit_own_data_placement(self, handler: SerializerFunctionWrapHandler) -> Any:
        """Write the placement only where it differs from the profile's own.

        The releases before it read stack.toml and the installer journal as closed
        shapes; a key they do not know makes the installation unreadable to them, and
        a rollback with it. Where the trees are the profile's own -- every installation
        until one is switched -- the key is not written, so the files, and any digest
        of them, stay what they were. Host settings follow the same rule: none set, no key.
        """
        data = handler(self)
        if isinstance(data, dict) and data.get("data_placement") == _own_data_placement(
            self.deployment_profile
        ):
            data.pop("data_placement")
        if isinstance(data, dict) and not data.get("operator_settings"):
            data.pop("operator_settings", None)
        return data

    @model_validator(mode="after")
    def validate_shared_inputs(self) -> Self:
        if self.deployment_profile == "compact" and self.environment != "production":
            raise ValueError("Compact deployment profile requires production environment")
        if self.file_storage == "external_s3" and self.s3_endpoint.strip():
            parsed_endpoint = validate_s3_endpoint(self.s3_endpoint)
            scheme = parsed_endpoint.scheme.lower()
            development_http = (
                scheme == "http"
                and self.environment == "development"
                and self.allow_insecure_s3_http
            )
            if scheme != "https" and not development_http:
                raise ValueError(
                    "External S3 requires HTTPS; development HTTP requires explicit unsafe opt-in"
                )
        if self.tls_mode == "reverse_proxy" and self.reverse_proxy_backend_host != "127.0.0.1":
            raise ValueError("reverse_proxy_backend_host must be 127.0.0.1")
        allowed_pairs = {
            "bundled_traefik": {"local", "self_signed", "provided", "acme"},
            "external_traefik": {"external_resolver", "external_certificate"},
            "external_proxy": {"reverse_proxy"},
            "platform": {"platform_managed"},
        }
        if self.tls_mode not in allowed_pairs[self.ingress_mode]:
            # AC17-R78: the table an operator needs is on the line above; saying only that the
            # pair is incompatible made them guess which of the two to change and what to.
            permitted = ", ".join(sorted(allowed_pairs[self.ingress_mode]))
            raise ValueError(
                f"ingress_mode '{self.ingress_mode}' does not allow tls_mode "
                f"'{self.tls_mode}'; permitted tls_mode values for this ingress_mode are: "
                f"{permitted}"
            )
        if self.deployment_profile == "compact" and self.worker_replicas != 1:
            raise ValueError(
                "worker_replicas is Scalable-only: Compact runs one runtime container; "
                "set worker_processes to change its worker count"
            )
        if self.data_placement == "compact_volume" and self.environment != "production":
            raise ValueError("the Compact data volume is production-only, as Compact is")
        if self.ingress_mode == "platform" and self.deployment_profile != "compact":
            raise ValueError("platform ingress requires the Compact deployment profile")
        for peer in self.proxy_trusted_ips:
            network = ipaddress.ip_network(peer, strict=False)
            if network.prefixlen == 0:
                raise ValueError("proxy_trusted_ips must not contain a wildcard network")
        validate_internal_http_origin(self.extension_installer_origin)
        if self.ingress_mode == "external_traefik" and not self.traefik_network.strip():
            raise ValueError("external Traefik requires traefik_network")
        if self.tls_mode == "external_resolver" and not self.traefik_cert_resolver.strip():
            raise ValueError("external_resolver requires traefik_cert_resolver")
        if (
            self.proxy_placement == "docker"
            and self.ingress_mode == "external_proxy"
            and not self.proxy_network.strip()
        ):
            raise ValueError("Docker external proxy requires proxy_network")
        if self.tls_mode == "acme" and (
            not self.acme_contact.strip() or not self.acme_directory_url.strip()
        ):
            raise ValueError("ACME requires contact and directory URL")
        return self


def public_base_url(config: DeploymentConfig) -> str:
    """Return the canonical credential-free HTTPS origin for a deployment."""
    if "%" in config.public_host:
        raise ValueError("public_host must not contain an IPv6 scope identifier")
    try:
        address = ipaddress.ip_address(config.public_host)
    except ValueError:
        host = config.public_host.lower()
    else:
        host = address.compressed
        if address.version == 6:
            host = f"[{host}]"
    authority = host if config.public_port == 443 else f"{host}:{config.public_port}"
    return f"https://{authority}"


class ConfigurationPatch(_ClosedModel):
    """Closed non-secret patch accepted by ``config set``."""

    deployment_profile: Literal["compact", "scalable"] | None = None
    runtime_http_port: int | None = Field(default=None, ge=1, le=65535)
    worker_processes: int | None = Field(default=None, ge=1, le=56336)
    worker_replicas: int | None = Field(default=None, ge=1, le=64)
    # Item 5, step 4: written only by ./install.sh --deployment-profile, which keeps the
    # shared trees where they are across the switch. `config set` refuses it.
    data_placement: Literal["host_directories", "compact_volume"] | None = None
    file_storage: Literal["local_fs", "bundled", "external_s3"] | None = None
    ingress_mode: (
        Literal["bundled_traefik", "external_traefik", "external_proxy", "platform"] | None
    ) = None
    public_host: str | None = None
    public_port: int | None = Field(default=None, ge=1, le=65535)
    tls_mode: (
        Literal[
            "local",
            "self_signed",
            "provided",
            "acme",
            "reverse_proxy",
            "external_resolver",
            "external_certificate",
            "platform_managed",
        ]
        | None
    ) = None
    reverse_proxy_backend_host: str | None = None
    reverse_proxy_backend_port: int | None = Field(default=None, ge=1, le=65535)
    reverse_proxy_mcp_port: int | None = Field(default=None, ge=1, le=65535)
    proxy_placement: Literal["host", "docker"] | None = None
    proxy_kind: Literal["nginx", "apache"] | None = None
    proxy_trusted_ips: tuple[str, ...] | None = None
    traefik_network: str | None = None
    traefik_https_entrypoint: str | None = None
    traefik_http_entrypoint: str | None = None
    traefik_cert_resolver: str | None = None
    proxy_network: str | None = None
    acme_contact: str | None = None
    acme_directory_url: str | None = None
    extension_installer_origin: str | None = None
    certificate_path: str | None = None
    private_key_path: str | None = None
    # Host settings to change, merged into the active ones; an empty value resets one.
    operator_settings: dict[str, str] | None = None

    @field_validator("operator_settings", mode="before")
    @classmethod
    def parse_operator_settings_text(cls, value: Any) -> dict[str, str] | None:
        from core.application.lifecycle.operator_settings import parse_operator_settings

        return None if value is None else parse_operator_settings(value)

    @model_validator(mode="before")
    @classmethod
    def normalize_proxy_peers(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not isinstance(value.get("proxy_trusted_ips"), str):
            return value
        normalized = dict(value)
        normalized["proxy_trusted_ips"] = tuple(
            item.strip() for item in normalized["proxy_trusted_ips"].split(",") if item.strip()
        )
        return normalized

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if not any(getattr(self, field_name) is not None for field_name in self.model_fields_set):
            raise ValueError("configuration patch requires at least one value")
        return self


class ComposeTarget(_ClosedModel):
    """Derived Compose inputs for one canonical deployment configuration."""

    stack_environment: Literal["development", "production"]
    file_storage: Literal["local_fs", "bundled", "external_s3"]
    deployment_profile: Literal["compact", "scalable"] = "scalable"
    runtime_http_port: int = Field(default=8080, ge=1, le=65535)
    redirect_http_port: int = Field(default=8080, ge=1, le=65535)
    worker_processes: int = Field(default=2, ge=1, le=56336)
    env_file: str
    compose_files: tuple[str, ...]
    compose_profiles: tuple[str, ...] = ()
    required_services: tuple[str, ...] = ()
    runtime_services: tuple[str, ...] = ()
    rebuild_services: tuple[str, ...] = ()
    project_name: str = "flow-steward"


class ConfigurationValidationResult(_ClosedModel):
    valid: bool
    issues: tuple[str, ...] = ()


class ConfigurationApplyResult(_ClosedModel):
    active: DeploymentConfig
    target: ComposeTarget
    snapshot_path: str
    previous_runtime_restored: bool = False


def validate_internal_http_origin(raw_origin: str) -> SplitResult:
    """Validate a credential-free HTTP origin used only between runtime roles."""
    try:
        parsed = urlsplit(str(raw_origin or ""))
        port = parsed.port
    except ValueError as exc:
        raise ValueError("internal service origin is invalid") from exc
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("internal service origin must be a credential-free HTTP origin")
    return parsed


def validate_s3_endpoint(raw_endpoint: str) -> SplitResult:
    """Validate and parse a credential-free external S3 endpoint."""
    endpoint = str(raw_endpoint or "")
    if endpoint != endpoint.strip() or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in endpoint
    ):
        raise ValueError("External S3 endpoint must not contain whitespace or control characters")
    if "\\" in endpoint:
        raise ValueError("External S3 endpoint contains an invalid authority")
    try:
        parsed = urlsplit(endpoint)
        parsed_port = parsed.port
    except ValueError as exc:
        raise ValueError("External S3 endpoint contains an invalid port or authority") from exc
    del parsed_port
    if not parsed.hostname:
        raise ValueError("External S3 endpoint requires a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("External S3 endpoint must not contain credentials")
    if parsed.query:
        raise ValueError("External S3 endpoint must not contain a query")
    if parsed.fragment:
        raise ValueError("External S3 endpoint must not contain a fragment")
    if "%" in parsed.netloc:
        raise ValueError("External S3 endpoint contains an invalid authority")
    hostname = parsed.hostname
    if ":" in hostname:
        try:
            ipaddress.ip_address(hostname)
        except ValueError as exc:
            raise ValueError("External S3 endpoint contains an invalid hostname") from exc
    elif not all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in hostname.split(".")
    ):
        raise ValueError("External S3 endpoint contains an invalid hostname")
    return parsed


class LifecycleOperationKind(StrEnum):
    """Mutating operation kinds owned by the lifecycle application layer."""

    STACK_START = "stack_start"
    STACK_STOP = "stack_stop"
    STACK_RESTART = "stack_restart"
    STACK_REBUILD = "stack_rebuild"
    ENVIRONMENT_SWITCH = "environment_switch"
    RELEASE_UPGRADE = "release_upgrade"
    BACKUP_CREATE = "backup_create"
    CLONE_CREATE = "clone_create"
    CLONE_RESTORE = "clone_restore"
    CONFIG_APPLY = "config_apply"


class LifecycleOperationStatus(StrEnum):
    """Durable lifecycle-operation states."""

    RUNNING = "running"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    RECOVERY_REQUIRED = "recovery_required"


class LifecycleOperationStep(_ClosedModel):
    """One declarative, resumable step and its cancellation boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1)
    safe_boundary: bool = True


class LifecycleOperationDefinition(_ClosedModel):
    """A complete operation plan interpreted by the common runner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: LifecycleOperationKind
    steps: tuple[LifecycleOperationStep, ...]
    operation_id: str | None = None
    mutating: bool = True
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    previous_config: dict[str, JsonValue] = Field(default_factory=dict)
    recovery_command: str = Field(min_length=1)
    allow_recovery_resume: bool = False

    @model_validator(mode="after")
    def validate_steps(self) -> LifecycleOperationDefinition:
        step_ids = tuple(step.step_id for step in self.steps)
        if not step_ids:
            raise ValueError("lifecycle operations require at least one step")
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("lifecycle operation step ids must be unique")
        return self


class LifecycleLockStatus(StrEnum):
    """Advisory-lock diagnosis without treating file existence as ownership."""

    CLEAR = "clear"
    ACTIVE = "active"
    STALE = "stale"


class LifecycleLockDiagnosis(_ClosedModel):
    """Safe operator-visible facts about the mutation lock."""

    status: LifecycleLockStatus
    operation_id: str | None = None
    pid: int | None = None
    acquired_at: str | None = None
    detail: str = ""


class LifecycleOperationJournal(_ClosedModel):
    """Secret-free durable state for one resumable operation."""

    schema_version: Literal[1] = 1
    operation_id: str
    kind: LifecycleOperationKind
    status: LifecycleOperationStatus = LifecycleOperationStatus.RUNNING
    mutating: bool = True
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    step_definitions: tuple[LifecycleOperationStep, ...] = ()
    steps: tuple[OperationStep, ...] = ()
    completed_steps: tuple[str, ...] = ()
    skipped_steps: tuple[str, ...] = ()
    step_outputs: dict[str, JsonValue] = Field(default_factory=dict)
    current_step: str | None = None
    failed_step: str | None = None
    previous_config_path: str | None = None
    log_path: str | None = None
    recovery_command: str
    last_error_code: str | None = None
    retryable: bool = True
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LifecycleOperationResult(_ClosedModel):
    """Safe successful result returned by the common lifecycle runner."""

    operation_id: str
    kind: LifecycleOperationKind
    status: LifecycleOperationStatus
    completed_steps: tuple[str, ...]
    skipped_steps: tuple[str, ...] = ()
    journal_path: str
    log_path: str
    previous_config_path: str | None = None
    recovery_command: str
    elapsed_seconds: float = Field(ge=0)


class CancellationToken:
    """Thread-safe cooperative cancellation shared across invocation layers."""

    def __init__(self) -> None:
        self._requested = Event()
        self._forced = Event()
        self._lock = RLock()
        self._force_callbacks: dict[int, Callable[[], None]] = {}
        self._next_force_callback_id = 0
        self._recovery_command = "flow-steward status"

    @property
    def requested(self) -> bool:
        return self._requested.is_set()

    @property
    def forced(self) -> bool:
        return self._forced.is_set()

    @property
    def recovery_command(self) -> str:
        with self._lock:
            return self._recovery_command

    def set_recovery_command(self, command: str) -> None:
        with self._lock:
            self._recovery_command = str(command)

    def request(self) -> bool:
        with self._lock:
            first_request = not self._requested.is_set()
            self._requested.set()
            return first_request

    def force(self) -> None:
        with self._lock:
            self._requested.set()
            first_force = not self._forced.is_set()
            self._forced.set()
            callbacks = tuple(self._force_callbacks.values()) if first_force else ()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                continue

    def register_force_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register one scoped callback and return an idempotent remover."""
        with self._lock:
            callback_id = self._next_force_callback_id
            self._next_force_callback_id += 1
            self._force_callbacks[callback_id] = callback

        def remove() -> None:
            with self._lock:
                self._force_callbacks.pop(callback_id, None)

        return remove


def publishes_host_port(config: DeploymentConfig) -> bool:
    """Whether the selected ingress overlay binds a host port for Flow Steward.

    Kept in step with infra/docker/{compact,scalable}/ingress-*.yaml: behind an existing
    Traefik, or an external proxy running in Docker, the overlay declares no host
    `ports:` at all -- the public port belongs to that component. Bundled Traefik, a
    host-placed external proxy and platform ingress do bind one. One definition for the
    installer's preflight and for config apply (P2), which had each asked separately.
    """
    if config.ingress_mode == "external_proxy":
        return config.proxy_placement != "docker"
    return config.ingress_mode != "external_traefik"


# AC17-R167: the environment values each profile owns -- container paths the processes
# read, which differ because the profiles mount the same trees at different places. The
# installer and `config apply` write them from here, for the profile being written, so
# a switch cannot carry the other profile's value across: Compact's /data/artifacts
# reached a Scalable candidate, whose artifacts are mounted at /app/logs/artifacts, and
# the application died on `makedirs('/data')`. A derived test holds this table to both
# profiles' Compose files.
PROFILE_OWNED_ENVIRONMENT: dict[str, dict[str, str]] = {
    "ARTIFACT_LOCAL_ROOT_DIR": {"compact": "/data/artifacts", "scalable": "/app/logs/artifacts"},
}


def profile_owned_environment(profile: str) -> dict[str, str]:
    """The values `profile` owns, to be written over whatever the environment held."""
    return {name: values[profile] for name, values in PROFILE_OWNED_ENVIRONMENT.items()}
