"""Structured Docker output parsing and local runtime safety helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import socket
import stat
import subprocess
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from core.application.lifecycle.contracts import ComposeTarget
from core.application.lifecycle.stack_service import (
    StackContainerState,
    StackGatewayError,
    StackVolumeState,
)
from core.infrastructure.installer.state_store import _atomic_write_text

SAFE_PROJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PATH_FLOW_STEWARD = ".flow-steward"
_TOKEN_INSTALLER_COMPOSE_OVERRIDE_YAML = "installer.compose.override.yaml"
_IMAGE_PREPARATION_TIMEOUT_SECONDS = 1800
_RETAINED_MANAGEMENT_SERVICES = frozenset({"fs-cli"})
_TRANSITION_RETAINED_SERVICES = frozenset({"fs-cli", "pg-fs", "rabbitmq-fs"})
_TRANSIENT_CANDIDATE_READINESS_IDS = frozenset(
    {
        "api_ready",
        "compact_runtime",
        "compose_runtime",
        "job_dispatch",
        "mcp_auth",
        "queue_worker_health",
        "rabbitmq_socket",
        "security_posture",
        "workflow_knowledge_sync",
    }
)
_SAFE_SERVICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True)
class _PublishedBinding:
    host: str
    port: int
    protocol: str


def run_process(command: Sequence[str], *, cwd: Path, timeout: float) -> Any:
    return subprocess.run(
        list(command), cwd=cwd, check=False, capture_output=True, text=True, timeout=timeout
    )


def decode_inspections(raw: str) -> tuple[dict[str, Any], ...]:
    decoded: list[dict[str, Any]] = []
    for line in str(raw).splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("Docker volume inspection must be a JSON object")
        decoded.append(value)
    return tuple(decoded)


def volume_identity(inspect: Mapping[str, Any]) -> dict[str, str]:
    name = str(inspect.get("Name") or "").strip()
    if not name:
        raise ValueError("Docker volume inspection omitted its name")
    identity_source = {
        "name": name,
        "driver": inspect.get("Driver"),
        "scope": inspect.get("Scope"),
        "created_at": inspect.get("CreatedAt"),
        "labels": inspect.get("Labels"),
        "options": inspect.get("Options"),
    }
    canonical = json.dumps(
        identity_source, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return {
        "name": name,
        "volume_id": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def validate_project_name(project_name: str) -> None:
    if not SAFE_PROJECT_NAME.fullmatch(str(project_name)):
        raise StackGatewayError(
            action="configuration", detail="Compose project name contains unsafe characters."
        )


def validate_services(services: Sequence[str]) -> None:
    if any(not _SAFE_SERVICE_NAME.fullmatch(str(service)) for service in services):
        raise StackGatewayError(
            action="configuration", detail="Compose service name contains unsafe characters."
        )


def inspection_service(item: Mapping[str, Any]) -> str:
    config = item.get("Config")
    labels = config.get("Labels") if isinstance(config, Mapping) else None
    if not isinstance(labels, Mapping):
        return ""
    return str(labels.get("com.docker.compose.service") or "").strip()


def project_path(project_root: Path, raw_path: str) -> Path:
    candidate = Path(str(raw_path))
    resolved = (
        candidate.resolve() if candidate.is_absolute() else (project_root / candidate).resolve()
    )
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise StackGatewayError(
            action="configuration",
            detail="Installed Compose configuration references a path outside the project.",
        ) from exc
    return resolved


# What Compose names a container while it recreates it. Measured on Compose 5.5.1
# (2026-09-25, docker events): the NEW container is created as "<the old container's
# short id>_<name>", the old one is stopped and removed, and then the new one is renamed.
# Older Compose renamed the old container to "<its own short id>_<name>" instead.
_RECREATE_TEMPORARY_NAME = re.compile(r"^(?P<id>[0-9a-f]{12})_(?P<name>.+)$")
_NOT_RUNNING = frozenset({"created", "exited", "dead"})
_LISTING_FORMAT = "{{.ID}}\t{{.Names}}\t{{.State}}"


@dataclass(frozen=True)
class RecreateRepair:
    """Finish what a killed recreate left: give a container its name back, first removing
    the stopped replaced container that still holds it, as Compose itself would."""

    container_id: str
    name: str
    replaced_id: str = ""


def recreate_listing_command(project_name: str) -> tuple[str, ...]:
    """The listing recreate_repairs() reads: every container of the project, any state."""
    validate_project_name(project_name)
    return (
        "docker",
        "ps",
        "--all",
        "--filter",
        f"label=com.docker.compose.project={project_name}",
        "--format",
        _LISTING_FORMAT,
    )


def restore_temporary_names(run: Callable[[tuple[str, ...]], Any], *, project_name: str) -> None:
    """Finish every recreate of this project a kill interrupted (W4)."""
    listing = run(recreate_listing_command(project_name))
    for repair in recreate_repairs(
        str(getattr(listing, "stdout", "") or ""), project_name=project_name
    ):
        if repair.replaced_id:
            removed = run(("docker", "rm", repair.replaced_id))
            if int(getattr(removed, "returncode", 0) or 0) != 0:
                continue  # Docker refused (it started meanwhile): leave both as they are
        run(("docker", "rename", repair.container_id, repair.name))


def temporary_name_renames(listing: str, *, project_name: str) -> tuple[tuple[str, str], ...]:
    """The renames recreate_repairs() makes, as (container id, name)."""
    return tuple(
        (repair.container_id, repair.name)
        for repair in recreate_repairs(listing, project_name=project_name)
    )


def recreate_repairs(listing: str, *, project_name: str) -> tuple[RecreateRepair, ...]:
    """What finishes the recreates a kill left, from `ID<TAB>Name[<TAB>State]` rows.

    W4 (auditor): after `config apply` was killed mid-recreate, fs-app kept a temporary
    "<id>_fs-ext-fs-app-1", and every documented `docker exec fs-ext-fs-app-1` answered
    "No such container". Only the exact form is touched -- a 12-hex prefix, a name of
    this project's -- and a name is given back only when it is free, or held by the
    container that was being replaced (its id starts with the prefix) and that one no
    longer runs. A prefix naming another live container of the project, or a replaced
    container still running, is left alone.
    """
    rows = []
    for line in listing.splitlines():
        fields = [field.strip() for field in line.split("\t")]
        if len(fields) in (2, 3):
            rows.append(
                (fields[0].lower(), fields[1], fields[2].lower() if len(fields) == 3 else "")
            )
    holders = {name: (container_id, state) for container_id, name, state in rows}
    repairs: list[RecreateRepair] = []
    for container_id, name, _state in rows:
        temporary = _RECREATE_TEMPORARY_NAME.fullmatch(name)
        if temporary is None:
            continue
        prefix, canonical = temporary.group("id"), temporary.group("name")
        if not canonical.startswith(f"{project_name}-"):
            continue
        holder = holders.get(canonical)
        if holder is None:
            others = [row for row in rows if row[0] != container_id]
            if container_id.startswith(prefix) or not any(
                other_id.startswith(prefix) for other_id, _name, _state in others
            ):
                repairs.append(RecreateRepair(container_id, canonical))
                holders[canonical] = (container_id, "")
        elif holder[0].startswith(prefix) and holder[1] in _NOT_RUNNING:
            repairs.append(RecreateRepair(container_id, canonical, replaced_id=holder[0]))
            holders[canonical] = (container_id, "")
    return tuple(repairs)


def json_objects(raw: str) -> tuple[dict[str, Any], ...]:
    objects: list[dict[str, Any]] = []
    try:
        for line in str(raw).splitlines():
            if not line.strip():
                continue
            decoded = json.loads(line)
            candidates = decoded if isinstance(decoded, list) else (decoded,)
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    raise ValueError("Docker JSON output must contain objects")
                objects.append(candidate)
    except ValueError as exc:
        raise StackGatewayError(
            action="status", detail="Docker returned malformed structured status output."
        ) from exc
    return tuple(objects)


def container_state(item: Mapping[str, Any]) -> StackContainerState:
    service = str(item.get("Service") or "").strip()
    if not service:
        raise StackGatewayError(
            action="status", detail="Docker status omitted a Compose service identity."
        )
    publishers = item.get("Publishers")
    published_ports = (
        tuple(
            _publisher_text(publisher) for publisher in publishers if isinstance(publisher, Mapping)
        )
        if isinstance(publishers, list)
        else ()
    )
    return StackContainerState(
        service=service,
        container_id=str(item.get("ID") or ""),
        container_name=str(item.get("Name") or ""),
        state=str(item.get("State") or "unknown"),
        health=str(item.get("Health") or ""),
        exit_code=integer(item.get("ExitCode")),
        published_ports=published_ports,
    )


def published_binding(mapping: str) -> _PublishedBinding | None:
    try:
        published, target = str(mapping).rsplit("->", 1)
        host, raw_port = published.rsplit(":", 1)
        _target_port, protocol = target.rsplit("/", 1)
        port = int(raw_port)
    except (TypeError, ValueError):
        return None
    normalized_host = host.strip().strip("[]").lower()
    normalized_protocol = protocol.strip().lower()
    if not normalized_host or not (1 <= port <= 65535) or not normalized_protocol:
        return None
    return _PublishedBinding(host=normalized_host, port=port, protocol=normalized_protocol)


def bind_addresses_conflict(requested: str, published: str) -> bool:
    requested_host = str(requested).strip().strip("[]").lower()
    published_host = str(published).strip().strip("[]").lower()
    if requested_host == published_host:
        return True
    try:
        requested_ip = ip_address(requested_host)
        published_ip = ip_address(published_host)
    except ValueError:
        return False
    if requested_ip.version != published_ip.version:
        return False
    return requested_ip.is_unspecified or published_ip.is_unspecified


def port_is_available(host: str, port: int, protocol: str) -> bool:
    socket_type = {"tcp": socket.SOCK_STREAM, "udp": socket.SOCK_DGRAM}.get(protocol.lower())
    if socket_type is None:
        return False
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket_type) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def assert_control_directory_safe(project_root: Path) -> None:
    control = project_root / ".flow-steward"
    try:
        metadata = control.lstat()
    except FileNotFoundError:
        return
    if control.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("repository control directory must be a real directory")


def storage_probe_script(target: ComposeTarget) -> str:
    if target.file_storage == "local_fs":
        return local_storage_write_probe_script()
    return bundled_storage_write_probe_script()


def local_storage_write_probe_script() -> str:
    return """import os
import tempfile
from pathlib import Path

root = Path(os.environ["ARTIFACT_LOCAL_ROOT_DIR"])
if root.is_symlink() or not root.is_dir():
    raise RuntimeError("local artifact root is unavailable")
descriptor, raw_path = tempfile.mkstemp(prefix=".flow-steward-probe-", dir=root)
path = Path(raw_path)
payload = b"flow-steward-local-storage-probe"
try:
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    if path.read_bytes() != payload:
        raise RuntimeError("local artifact probe payload mismatch")
finally:
    path.unlink(missing_ok=True)
"""


def bundled_storage_write_probe_script() -> str:
    return (
        _bundled_client_prelude()
        + """import time
import uuid
from botocore.exceptions import BotoCoreError, ClientError
probe_id = uuid.uuid4().hex
key = ".flow-steward/probes/" + probe_id
payload = ("flow-steward-storage-probe:" + probe_id).encode()
readiness_deadline = time.monotonic() + 30

def probe_once():
    put_attempted = False
    primary_failure = None
    try:
        try:
            client.head_bucket(Bucket=bucket)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code not in {'404', 'NoSuchBucket', 'NotFound'}:
                raise
            client.create_bucket(Bucket=bucket)
        put_attempted = True
        client.put_object(Bucket=bucket, Key=key, Body=payload)
        response = client.get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        try:
            if body.read() != payload:
                raise RuntimeError("bundled storage probe payload mismatch")
        finally:
            body.close()
    except BaseException as exc:
        primary_failure = exc
        raise
    finally:
        if put_attempted:
            try:
                client.delete_object(Bucket=bucket, Key=key)
            except BaseException:
                if primary_failure is None:
                    raise

while True:
    try:
        probe_once()
        break
    except BotoCoreError:
        remaining = readiness_deadline - time.monotonic()
        if remaining <= 0:
            raise
        time.sleep(min(0.5, remaining))
"""
    )


def bundled_storage_inventory_script() -> str:
    return (
        _bundled_client_prelude()
        + """import json
client.head_bucket(Bucket=bucket)
count = 0
size = 0
for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
    for item in page.get("Contents") or ():
        count += 1
        size += max(0, int(item.get("Size") or 0))
print(json.dumps({"approximate_count": count, "approximate_size_bytes": size,
                  "completeness": "complete"}, separators=(",", ":")))
"""
    )


def workflow_knowledge_reconciliation_script() -> str:
    """Build a one-shot durable sync request from the installed control-plane runtime."""

    return """from core.infrastructure.runtime.control.config import ControlConfig
from core.infrastructure.runtime.control.scans import workflow_knowledge_sync_scan
from core.infrastructure.config.runtime_config import CONFIG
from core.infrastructure.runtime.entrypoint_db_adapters import create_control_stores, resolve_platform_dsn

dsn = resolve_platform_dsn(str(CONFIG.get("fs_observability_dsn") or CONFIG.get("fs_db_dsn") or ""))
stores = create_control_stores(dsn)
workflow_knowledge_sync_scan(stores["projects"], stores["events"], ControlConfig.from_env())
"""


def security_posture_reconciliation_script() -> str:
    """Run one durable security-posture iteration in the installed runtime."""

    return """from core.infrastructure.runtime.control.config import ControlConfig
from core.infrastructure.runtime.control.scans import security_posture_scan
from core.infrastructure.config.runtime_config import CONFIG
from core.infrastructure.runtime.entrypoint_db_adapters import create_control_stores, resolve_platform_dsn

dsn = resolve_platform_dsn(str(CONFIG.get("fs_observability_dsn") or CONFIG.get("fs_db_dsn") or ""))
stores = create_control_stores(dsn)
security_posture_scan(stores["projects"], ControlConfig.from_env())
"""


def stack_volume_state(item: Mapping[str, Any]) -> StackVolumeState:
    name = str(item.get("Name") or "").strip()
    if not name:
        raise StackGatewayError(
            action="volume_status", detail="Docker volume status omitted its name."
        )
    return StackVolumeState(
        name=name,
        driver=str(item.get("Driver") or ""),
        scope=str(item.get("Scope") or ""),
        created_at=str(item.get("CreatedAt") or ""),
    )


def _bundled_client_prelude() -> str:
    return """import os
import boto3
from botocore.config import Config
access_key = os.environ.get("ARTIFACT_S3_ACCESS_KEY") or ""
secret_key = os.environ.get("ARTIFACT_S3_SECRET_KEY") or ""
session_token = os.environ.get("AWS_SESSION_TOKEN") or ""
if not access_key or not secret_key:
    raise RuntimeError("authenticated artifact storage credentials are required")
options = {
    "endpoint_url": os.environ["ARTIFACT_S3_ENDPOINT_URL"],
    "region_name": os.environ.get("ARTIFACT_S3_REGION") or "us-east-1",
    # AC17-R129: on, like every other reader and like the manifest. A probe that
    # defaults the transport off tests something the runtime does not do.
    "use_ssl": os.environ.get("ARTIFACT_S3_USE_SSL", "true").lower() == "true",
    "aws_access_key_id": access_key,
    "aws_secret_access_key": secret_key,
    "config": Config(connect_timeout=1, read_timeout=2,
                     retries={"total_max_attempts": 1, "mode": "standard"}),
}
if session_token:
    options["aws_session_token"] = session_token
client = boto3.client("s3", **options)
bucket = os.environ["ARTIFACT_S3_BUCKET"]
"""


def _publisher_text(publisher: Mapping[str, Any]) -> str:
    protocol = str(publisher.get("Protocol") or "tcp")
    target = integer(publisher.get("TargetPort"))
    published = integer(publisher.get("PublishedPort"))
    url = str(publisher.get("URL") or "")
    return f"{url}:{published}->{target}/{protocol}" if published else f"{target}/{protocol}"


def integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def write_rollback_override(
    project_root: Path,
    *,
    operation_id: str,
    images: Mapping[str, str],
) -> Path:
    """Write the compose override that pins a rollback's images."""
    path = (
        project_root / _PATH_FLOW_STEWARD / "operations" / f"{operation_id}.rollback.compose.json"
    )
    payload = {"services": {service: {"image": image} for service, image in images.items()}}
    _atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        mode=0o600,
    )
    return path


@contextmanager
def temporary_candidate_environment(
    project_root: Path,
    *,
    env_file: str,
    environment: Mapping[str, str],
) -> Iterator[None]:
    """Supply a missing target env file only while Compose resolves images."""
    path = project_path(project_root, env_file)
    created_identity: tuple[int, int] | None = None
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            yield
            return
        created = os.fstat(descriptor)
        created_identity = (created.st_dev, created.st_ino)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for key, value in sorted(environment.items()):
                handle.write(f"{key}={shlex.quote(str(value))}\n")
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        if created_identity is not None:
            try:
                current = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if (current.st_dev, current.st_ino) == created_identity:
                    path.unlink()


def rollback_topologies(
    by_service: Mapping[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """The services a rollback snapshot recorded running, stopped and absent."""
    running, stopped, absent = (
        tuple(service for service, item in by_service.items() if item.topology == topology)
        for topology in ("running", "stopped", "absent")
    )
    return running, stopped, absent


def restore_scale_args(by_service: Mapping[str, Any], services: Iterable[str]) -> tuple[str, ...]:
    """`--scale` for the restored services, at the count the snapshot recorded.

    C4 (External review): a rollback restores the previous environment, whose
    FS_WORKER_REPLICAS may not be what ran -- recorded 1, three running by hand, then an
    apply of `--worker-replicas 3` that failed. Compose then brought fs-worker up at 1,
    the restore check expected the 3 it had captured, and recovery refused forever. The
    count comes from the snapshot, like everything else a restore puts back. Services
    recorded with one container and a fixed container_name cannot take --scale, so only
    fs-worker and services recorded with several containers are named.
    """
    args: list[str] = []
    for service in services:
        item = by_service.get(service)
        replicas = int(getattr(item, "replicas", 1) or 1)
        if service == "fs-worker" or replicas != 1:
            args.extend(("--scale", f"{service}={replicas}"))
    return tuple(args)
