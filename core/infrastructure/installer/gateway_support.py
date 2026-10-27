"""Shared installer adapter values, records, and pure technical helpers."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import ssl
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from core.application.installer.contracts import (
    InstallConfig,
)
from core.application.installer.errors import InstallerFailure
from core.application.lifecycle.contracts import (
    publishes_host_port,
)
from core.infrastructure.config.env_registry import (
    StackEnvSelection,
    generated_secret_env_keys_for_selection,
)
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.public_probe import (
    _routed_urlopen as _routed_urlopen,
)
from core.infrastructure.installer.public_probe import (
    _RoutedHTTPConnection as _RoutedHTTPConnection,
)
from core.infrastructure.installer.public_probe import (
    _RoutedHTTPSConnection as _RoutedHTTPSConnection,
)
from core.infrastructure.installer.registry_image_resolver import (
    LocalImageContentIdentity,
)

_TOKEN_STACK_TOML = "stack.toml"
_TOKEN_COMPOSE_ENV = "compose.env"
_PATH_ENV_PROD = ".env.prod"
_MESSAGE_VERIFY_LOCAL_DOCKER_ACCESS_AND_RERUN_INSTALL_SH = (
    "Verify local Docker access and rerun ./install.sh."
)
_TOKEN_ORG_OPENCONTAINERS_IMAGE_REVISION = "org.opencontainers.image.revision"
_TOKEN_HEAD_COMMIT = "HEAD^{commit}"
_PATH_RELEASE_RECORDS = ".flow-steward/release"
_PATH_CANDIDATE_ENV = "candidate.env"
_FORMAT_IMAGE_ID = "{{.Id}}"
_MESSAGE_RUNNING = "Running: "
_REMEDIATION_INFRASTRUCTURE_IMAGE = (
    "Resolve the infrastructure image failure and resume ./install.sh."
)
_TOKEN_SHA256 = "sha256:"
_MESSAGE_INVALID_PRODUCTION_CLI_CANDIDATE_MOUNT_PATHS = (
    "invalid Production CLI candidate mount paths"
)
_MESSAGE_RESOLVE_INFRASTRUCTURE_IMAGE_FAILURE = (
    "Resolve the infrastructure image failure and resume ./install.sh."
)

_MINIMUM_DISK_BYTES = 4 * 1024**3
_SUPPORTED_ARCHITECTURES = frozenset({"amd64", "arm64", "aarch64", "x86_64"})
_READINESS_ATTEMPTS = 30
_PUBLIC_ROUTE_READINESS_ATTEMPTS = 90
_READINESS_DELAY_SECONDS = 2.0
_DOCKER_INVENTORY_TIMEOUT_SECONDS = 30.0
_ROLLBACK_TIMEOUT_SECONDS = 30.0
_TRANSIENT_CANDIDATE_READINESS_IDS = frozenset(
    {
        "api_ready",
        "job_dispatch",
        "mcp_auth",
        "queue_worker_health",
        "rabbitmq_socket",
    }
)
_DURABLE_SECRET_NAMES = (
    "FS_SECRET_KEY",
    "ENCRYPTION_KEY",
    "FS_BACKUP_ENCRYPTION_KEY",
)
_INSTALLER_SECRET_NAMES = (
    *_DURABLE_SECRET_NAMES,
    "FS_METRICS_TOKEN",
    # A non-empty opaque bootstrap value lets production Compose parse before
    # the canonical database-backed worker credential is issued after schema.
    "FS_WORKER_MCP_BEARER_TOKEN",
)
_BOOTSTRAP_ISOLATION_ENV_NAMES = (
    "FS_COMPOSE_PROJECT_NAME",
    "HTTP_PORT",
    "POSTGRES_HOST_PORT",
    "RABBITMQ_AMQP_HOST_PORT",
    "RABBITMQ_MANAGEMENT_HOST_PORT",
    "MAILPIT_SMTP_PORT",
    "MAILPIT_WEB_PORT",
    "MAILPIT_HTTP_PORT",
    "FRONTEND_HOST_PORT",
    "MCP_HOST_PORT",
    "SONAR_PORT",
    "SEAWEEDFS_S3_HOST_PORT",
    "SEAWEEDFS_MASTER_HOST_PORT",
    "FS_APP_IMAGE",
    "FS_WORKER_IMAGE",
    "FS_CONTROL_IMAGE",
    "FS_FRONTEND_IMAGE",
    "MCP_IMAGE",
    "FS_CLI_IMAGE",
    "POSTGRES_IMAGE",
    # AC17-R81: the Compact runtime has no `build:` stanza, so its image is an operator
    # input rather than something the installer can produce. Carrying these the same way
    # as every other image selection is what makes the documented registry reference in
    # docs/operations/compact-scalable-deployment.md reachable on a clean tree, where
    # there is no .env.prod yet to write them into by hand.
    "FS_COMPACT_IMAGE",
    "FS_COMPACT_IMAGE_REGISTRY_REF",
    "FS_COMPACT_IMAGE_REVISION",
    "FS_COMPACT_IMAGE_PLATFORM",
    "AWS_CA_BUNDLE",
)
# Only these are operator inputs. Every other FS_COMPACT_IMAGE_* value is derived by the
# installer from the resolved image and must never be accepted from the environment.
_COMPACT_IMAGE_INPUT_NAMES = ("FS_COMPACT_IMAGE_REGISTRY_REF", "FS_COMPACT_IMAGE")
# Everything a previous install recorded about the image it pinned. A rebuild has to
# clear all of it: leaving the digests behind lets the superseded image be resolved
# again even though a newer one was just built.
_COMPACT_IMAGE_PIN_NAMES = (
    "FS_COMPACT_IMAGE_REGISTRY_REF",
    "FS_COMPACT_IMAGE_INDEX_DIGEST",
    "FS_COMPACT_IMAGE_MANIFEST_DIGEST",
    "FS_COMPACT_IMAGE_CONFIG_DIGEST",
    "FS_COMPACT_IMAGE_REVISION",
    "FS_COMPACT_IMAGE_ROLE",
    "FS_COMPACT_IMAGE_PLATFORM",
)
_COMPACT_DOCKERFILE = "infra/docker/Dockerfile"
_COMPACT_BUILD_TARGET = "compact-runtime"
_COMPACT_BUILD_TIMEOUT_SECONDS = 3600.0
# AC17-R157: how long an installed Compact waits for a changed worker count to register.
_COMPACT_WORKER_WAIT_SECONDS = 180.0
_COMPACT_WORKER_POLL_SECONDS = 2.0
_COMPACT_HEALTH_DETAILS = (
    "import urllib.request;"
    "print(urllib.request.urlopen('http://127.0.0.1:9100/health/details', timeout=5)"
    ".read().decode())"
)
_RUNTIME_CERTIFICATE_PATH = Path(".ssl/cert.pem")
_RUNTIME_PRIVATE_KEY_PATH = Path(".ssl/key.pem")
_CLI_RUNTIME_HELPER = Path("core/infrastructure/cli_runtime/runtime_identity.sh")
_CLI_RUNTIME_LABELS = {
    "com.flow-steward.cli-runtime": "true",
    "com.flow-steward.cli-runtime-contract": "1",
}
_DOCKER_SOCKET_PATH = Path("/var/run/docker.sock")
_FULL_GIT_REVISION = re.compile(r"^[0-9a-f]{40}$")
# AC17-R141: the long-running services whose image an update replaces, and the
# .env.prod name the installer records that image under. The first entry of each is
# the one whose image label dates the running installation.
_RUNTIME_IMAGES: dict[str, tuple[tuple[str, str], ...]] = {
    "compact": (("compact-runtime", "FS_COMPACT_IMAGE"),),
    "scalable": (
        ("fs-app", "FS_APP_IMAGE"),
        ("fs-worker", "FS_WORKER_IMAGE"),
        ("control-plane", "FS_CONTROL_IMAGE"),
        ("fs-mcp", "MCP_IMAGE"),
    ),
}
_SOURCE_REVISION_FAILURE_CODE = "source_revision_unavailable"
_SOURCE_REVISION_REMEDIATION = (
    "Run ./install.sh from a Git clone whose history is readable: "
    "`git -C <installer directory> rev-parse HEAD` must succeed. A source archive "
    "without .git, a host without Git, or a worktree whose gitdir is unreachable "
    "cannot pin image provenance."
)
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_DIGEST_PINNED_IMAGE_REF = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_RELEASE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,126}$")
_COMPOSE_PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_BOOTSTRAP_DOCKER_MODE = "0"
_BOOTSTRAP_HOST_ROUTE = "host.docker.internal"
_HOSTS_FILE = Path("/etc/hosts")
_WORKER_TOKEN_ENV = "FS_WORKER_MCP_BEARER_TOKEN"
_MAX_CLI_READINESS_OUTPUT_BYTES = 1024 * 1024
_MAX_CLI_READINESS_CHECKS = 256
_MAX_CLI_READINESS_FIELD_LENGTH = 4096
_EXACT_IMAGE_KEYS = {
    "compact": "FS_COMPACT_IMAGE",
    "app": "FS_APP_IMAGE",
    "worker": "FS_WORKER_IMAGE",
    "control": "FS_CONTROL_IMAGE",
    "mcp": "MCP_IMAGE",
    "cli": "FS_CLI_IMAGE",
    "frontend": "FS_FRONTEND_IMAGE",
}
_EXACT_IMAGE_DIGEST_KEYS = {
    "compact": "FS_COMPACT_IMAGE_CONFIG_DIGEST",
    "app": "FS_APP_IMAGE_DIGEST",
    "worker": "FS_WORKER_IMAGE_DIGEST",
    "control": "FS_CONTROL_IMAGE_DIGEST",
    "mcp": "MCP_IMAGE_DIGEST",
}
_EXACT_IMAGE_REVISION_KEYS = {
    "compact": "FS_COMPACT_IMAGE_REVISION",
    "app": "FS_APP_IMAGE_REVISION",
    "worker": "FS_WORKER_IMAGE_REVISION",
    "control": "FS_CONTROL_IMAGE_REVISION",
    "mcp": "MCP_IMAGE_REVISION",
    "cli": "FS_CLI_IMAGE_REVISION",
    "frontend": "FS_FRONTEND_IMAGE_REVISION",
}
_EXACT_IMAGE_SERVICE_LABELS = {
    "compact": "compact-runtime",
    "app": "fs-app",
    "worker": "fs-worker",
    "control": "control-plane",
    "mcp": "fs-mcp",
    "frontend": "fs-frontend",
}
_EXACT_IMAGE_COMMAND_MARKERS = {
    "compact": "core.entrypoints.cli.compact_provider_runtime",
    "app": "start_server.sh",
    "worker": "core.entrypoints.cli.worker_process",
    "control": "core.entrypoints.cli.control_process",
    "mcp": "core.entrypoints.mcp.server",
    "cli": "core.entrypoints.cli.lifecycle_click_app",
    "frontend": "start_frontend_dev.sh",
}
_CLI_READINESS_CHECK_FIELDS = {
    "id",
    "category",
    "title",
    "status",
    "detail",
    "remediation",
}


def _repository_release_version(repo_root: Path) -> str:
    version = (repo_root / "VERSION").read_text(encoding="utf-8").strip()
    if _RELEASE_VERSION.fullmatch(version) is None:
        raise ValueError("invalid VERSION")
    return version


def _image_environment(value: object) -> dict[str, str]:
    """Read an image's baked-in environment from `docker image inspect` Config.Env.

    Docker reports it as a list of "NAME=value" strings, and a value may itself
    contain "=". Anything unparsable is ignored rather than raising: this feeds a
    consistency check, not the identity the installer relies on.
    """
    if not isinstance(value, list):
        return {}
    environment: dict[str, str] = {}
    for item in value:
        text = str(item)
        name, separator, entry = text.partition("=")
        if separator and name:
            environment[name] = entry.strip()
    return environment


def _normalized_docker_architecture(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"arm64", "aarch64"}:
        return "arm64"
    if normalized in {"amd64", "x86_64"}:
        return "amd64"
    return normalized


def _immutable_image_ref(value: str) -> bool:
    return bool(_IMAGE_ID.fullmatch(value) or _DIGEST_PINNED_IMAGE_REF.fullmatch(value))


_CLI_READINESS_SCRIPT = (
    "import json,sys;"
    "from core.application.lifecycle.contracts import DeploymentConfig;"
    "from core.entrypoints.cli.lifecycle_runtime import LifecycleCLIRuntime;"
    "config=DeploymentConfig.model_validate_json(sys.stdin.read());"
    "print(json.dumps(LifecycleCLIRuntime().explicit_config_readiness(config),"
    "separators=(',',':')))"
)
_COMPACT_CLI_READINESS_SCRIPT = (
    "import json;"
    "from core.entrypoints.cli.compact_platform_runtime import _readiness_payload;"
    "print(json.dumps(_readiness_payload(),separators=(',',':')))"
)


def _inventory_names(result: ProcessOutcome) -> tuple[str, ...]:
    if result.returncode != 0:
        return ()
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def _valid_cli_readiness_payload(payload: object) -> bool:
    if not isinstance(payload, dict) or set(payload) != {"checks"}:
        return False
    checks = payload.get("checks")
    if not isinstance(checks, list) or not 1 <= len(checks) <= _MAX_CLI_READINESS_CHECKS:
        return False
    return all(_valid_cli_readiness_check(row) for row in checks)


def _valid_cli_readiness_check(row: object) -> bool:
    if not isinstance(row, dict) or set(row) != _CLI_READINESS_CHECK_FIELDS:
        return False
    for field in _CLI_READINESS_CHECK_FIELDS:
        value = row[field]
        if (
            not isinstance(value, str)
            or len(value.encode("utf-8")) > _MAX_CLI_READINESS_FIELD_LENGTH
        ):
            return False
    if any(not row[field].strip() for field in ("id", "category", "title", "status")):
        return False
    return row["status"] in {"pass", "warn", "fail"}


def _cli_readiness_failure_payload() -> dict[str, object]:
    return {
        "checks": [
            {
                "id": "cli_runtime_readiness",
                "category": "core",
                "title": "CLI runtime readiness",
                "status": "fail",
                "detail": "The CLI runtime readiness probe failed.",
                "remediation": "Verify the retained CLI runtime and rerun ./install.sh.",
            }
        ]
    }


@dataclass(frozen=True)
class _CliRuntimeContext:
    image: str
    exact_image: bool
    build_revision: str
    runtime_revision: str
    environment: dict[str, str]
    compose_prefix: tuple[str, ...]
    host_uid: str
    host_gid: str
    docker_gid: str
    state_runtime_root: Path
    candidate_state_dir: Path | None
    candidate_prod_env: Path | None
    host_directory_mounts: tuple[tuple[Path, str], ...]


@dataclass(frozen=True)
class _ExactImageIdentity:
    image_id: str
    revision: str
    role: str
    platform: str
    content: LocalImageContentIdentity | None = None


class ProcessExecutor(Protocol):
    def run(self, command: Sequence[str], **kwargs: object) -> ProcessOutcome: ...


_DEFAULT_REDIRECT_HTTP_PORT = 8080
# The TLS modes whose certificate belongs to the ingress, so the installation holds no
# certificate or key file of its own. One definition: ensure_tls creates no files for
# these, and the Scalable candidate build must not demand them (AC17-R143).
_INGRESS_OWNED_TLS_MODES = frozenset(
    {"reverse_proxy", "external_resolver", "external_certificate", "platform_managed", "acme"}
)
_PUBLISHED_PORT = re.compile(r"^(?:.*:)?(\d+)(?:-(\d+))?->")


def _last_output_line(output: str) -> str:
    """The last non-empty line of a command's output, where a script says why it stopped."""
    lines = [line.strip() for line in str(output or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _published_ports(ports_listing: str) -> set[int]:
    """Every host port in `docker ps --format {{.Ports}}` output.

    AC17-R141 acceptance: Docker collapses consecutive published ports into one range,
    `0.0.0.0:8595-8596->8595-8596/tcp`, and the check this replaces looked for
    `:8595->`, which a range never contains. So an installation publishing two
    adjacent ports did not recognise either as its own, and a rerun of ./install.sh
    was refused by the installation it was run on. Entries without `->` are exposed,
    not published, and hold no host port.
    """
    ports: set[int] = set()
    for line in ports_listing.splitlines():
        for entry in line.split(","):
            match = _PUBLISHED_PORT.match(entry.strip())
            if match is None:
                continue
            first = int(match.group(1))
            last = int(match.group(2) or first)
            ports.update(range(first, last + 1))
    return ports


def _redirect_http_port(environment: Mapping[str, str]) -> int:
    """The host port bundled Traefik binds for its HTTP->HTTPS redirect entrypoint.

    Kept in step with `${HTTP_PORT:-8080}` in infra/docker/*/ingress-bundled_traefik.yaml
    and docker-compose.prod.yaml.
    """
    raw = str(environment.get("HTTP_PORT") or "").strip()
    if raw.isdigit() and 1 <= int(raw) <= 65535:
        return int(raw)
    return _DEFAULT_REDIRECT_HTTP_PORT


def _publishes_no_host_port(config: InstallConfig) -> bool:
    """Name the ingress modes whose selected overlay declares no host `ports:`.

    Kept in step with infra/docker/{compact,scalable}/ingress-*.yaml: bundled_traefik,
    external_proxy with host placement, and platform each publish a host port and must
    keep the port precondition.
    """
    return not publishes_host_port(config)


# The environment registry keys artifact storage off these exact selection values, so a
# backend that is not translated here silently becomes a different backend downstream.
_FILE_STORAGE_SELECTION = {
    "bundled": "bundled_seaweedfs",
    "local_fs": "local_fs",
    "external_s3": "external_s3",
}


def _env_selection(config: InstallConfig) -> StackEnvSelection:
    development = config.environment == "development"
    selected = _FILE_STORAGE_SELECTION.get(config.file_storage)
    if selected is None:
        raise InstallerFailure(
            code="storage_backend_unsupported",
            cause=(
                f"The selected artifact storage backend {config.file_storage!r} has no "
                "environment translation."
            ),
            remediation=(
                "Choose one of "
                + ", ".join(sorted(_FILE_STORAGE_SELECTION))
                + " with --file-storage, then rerun ./install.sh."
            ),
            exit_code=3,
            retryable=False,
            step="configuration",
        )
    return StackEnvSelection(
        environment="local_development" if config.environment == "development" else "production",
        graph_storage="postgres_age",
        vector_search="pgvector",
        file_storage=selected,
        test_runner=development,
        audit_tools=development,
        quality_tools=development,
    )


def _required_generated_secret_names(config: InstallConfig) -> tuple[str, ...]:
    names = [
        *_INSTALLER_SECRET_NAMES,
        *generated_secret_env_keys_for_selection(_env_selection(config)),
    ]
    if config.environment == "production" and config.file_storage == "bundled":
        names.extend(("ARTIFACT_S3_ACCESS_KEY", "ARTIFACT_S3_SECRET_KEY"))
    return tuple(dict.fromkeys(names))


def _certificate_fingerprint_of_file(path: Path) -> str:
    """SHA-256 of the first PEM certificate in a file, truncated for a readable message."""
    try:
        pem = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    marker = "-----BEGIN CERTIFICATE-----"
    if marker not in pem:
        return ""
    block = marker + pem.split(marker, 1)[1].split("-----END CERTIFICATE-----", 1)[0]
    block += "-----END CERTIFICATE-----\n"
    try:
        der = ssl.PEM_cert_to_DER_cert(block)
    except ValueError:
        return ""
    return hashlib.sha256(der).hexdigest()[:16]


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


_SWITCH_APPLY_TIMEOUT_SECONDS = 1800
_SWITCH_TREE_MOUNT = "/run/flow-steward-trees"
_SWITCH_HOST_GROUP_MOUNT = "/run/flow-steward-host-group"
# Where each shared tree lives on a Scalable host, and Compose's own default for it.
_SWITCH_TREE_PROBE = "/run/flow-steward-tree-probe"
_SWITCH_HOST_TREES = (
    ("artifacts", "FS_LOGS_DIR", "../../logs", "artifacts"),
    ("extensions", "FS_EXTENSIONS_DIR", "../../extensions", ""),
)


def _last_error_text(outcome: ProcessOutcome) -> str:
    for stream in (outcome.stderr, outcome.stdout):
        lines = [line.strip() for line in str(stream or "").splitlines() if line.strip()]
        if lines:
            try:
                return str(json.loads(lines[-1]).get("error") or lines[-1])
            except (ValueError, AttributeError):
                return lines[-1]
    return ""


# What start_core brings up; everything else expected is started by start_runtime.
_CORE_START_SERVICES = frozenset({"traefik", "rabbitmq-fs", "pg-fs", "seaweedfs"})


def _compose_ps_rows(text: str) -> list[dict[str, object]]:
    stripped = text.strip()
    if not stripped:
        return []
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        rows: list[dict[str, object]] = []
        for line in stripped.splitlines():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
        return rows
    if isinstance(payload, list):
        return [dict(row) for row in payload if isinstance(row, dict)]
    return [dict(payload)] if isinstance(payload, dict) else []


def _port_is_available(host: str, port: int) -> bool:
    bind_host = host if _is_ip(host) else "127.0.0.1"
    family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((bind_host, port))
        except OSError:
            return False
    return True


def _docker_socket_group_id() -> int:
    try:
        return _DOCKER_SOCKET_PATH.stat().st_gid
    except OSError:
        return 0


def _is_ip(value: str) -> bool:
    try:
        socket.inet_pton(socket.AF_INET6 if ":" in value else socket.AF_INET, value)
    except OSError:
        return False
    return True


def _is_loopback(value: str) -> bool:
    normalized = value.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _public_bind_host(value: str) -> str:
    import ipaddress

    normalized = value.strip().strip("[]").lower()
    if normalized in {"localhost", "127.0.0.1"}:
        return "127.0.0.1"
    if normalized == "::1":
        return "[::1]"
    return str(ipaddress.ip_address(0))


def _future_timestamp(value: str) -> bool:
    try:
        expires_at = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (AttributeError, TypeError, ValueError):
        return False
    return expires_at > datetime.now(UTC)


def _minted_bootstrap_token() -> tuple[str, str]:
    """Return a fresh one-time setup token and the ISO instant it stops working."""
    import secrets

    return (
        secrets.token_urlsafe(32),
        (datetime.now(UTC) + timedelta(minutes=60)).isoformat(),
    )


def _secret_value() -> str:
    import secrets

    return secrets.token_urlsafe(48)


def _hosts_file_address(path: Path, name: str) -> str | None:
    """What a hosts file maps `name` to, verbatim; None when it does not mention it."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        if name in entry.split()[1:]:
            return entry.split(name, 1)[0].strip() or None
    return None


def _is_ip_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True
