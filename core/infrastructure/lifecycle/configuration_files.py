"""Managed configuration artifacts, TLS files, and secure filesystem operations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from typing import Any, cast

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from core.application.lifecycle.contracts import (
    ComposeTarget,
    DeploymentConfig,
)
from core.infrastructure.installer.state_store import _atomic_write_text
from core.infrastructure.lifecycle.docker_runtime_support import (
    _TOKEN_INSTALLER_COMPOSE_OVERRIDE_YAML,
)

_PATH_FLOW_STEWARD = ".flow-steward"
_SAFE_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


_RUNTIME_CERTIFICATE_RELATIVE_PATH = ".ssl/cert.pem"
_RUNTIME_PRIVATE_KEY_RELATIVE_PATH = ".ssl/key.pem"

_CONFIGURATION_MANAGED_PATHS = (
    ".flow-steward/install-state.json",
    ".flow-steward/stack.toml",
    ".flow-steward/compose.env",
    ".flow-steward/active-publication.pending.json",
    ".flow-steward/active-revision.json",
    ".flow-steward/runtime/seaweedfs-s3.json",
    ".flow-steward/installer.compose.override.yaml",
    ".env",
    ".env.prod",
    _RUNTIME_CERTIFICATE_RELATIVE_PATH,
    _RUNTIME_PRIVATE_KEY_RELATIVE_PATH,
)


def _configuration_snapshot_dir(project_root: Path, operation_id: str) -> Path:
    if not _SAFE_OPERATION_ID.fullmatch(operation_id):
        raise ValueError("Configuration operation id contains unsafe characters")
    return project_root / ".flow-steward/operations" / f"{operation_id}.config-files"


def _capture_configuration_files(project_root: Path, operation_id: str) -> None:
    snapshot_dir = _configuration_snapshot_dir(project_root, operation_id)
    manifest_path = snapshot_dir / "manifest.json"
    if manifest_path.exists():
        _load_configuration_file_snapshot(project_root, operation_id)
        return
    snapshot_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    snapshot_dir.chmod(0o700)
    files: list[dict[str, object]] = []
    for index, relative in enumerate(_CONFIGURATION_MANAGED_PATHS):
        source = project_root / relative
        if not source.exists():
            files.append(
                {
                    "path": relative,
                    "existed": False,
                    "mode": 0,
                    "sha256": "",
                    "copy": "",
                }
            )
            continue
        if not source.is_file():
            raise ValueError(f"Managed configuration path is not a regular file: {relative}")
        payload = source.read_bytes()
        copy_name = f"{index}.bin"
        _atomic_write_bytes(snapshot_dir / copy_name, payload, mode=0o600)
        files.append(
            {
                "path": relative,
                "existed": True,
                "mode": source.stat().st_mode & 0o777,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "copy": copy_name,
            }
        )
    _atomic_write_text(
        manifest_path,
        json.dumps(
            {"schema_version": 1, "operation_id": operation_id, "files": files},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        mode=0o600,
    )


def _load_configuration_file_snapshot(
    project_root: Path,
    operation_id: str,
) -> dict[str, object]:
    snapshot_dir = _configuration_snapshot_dir(project_root, operation_id)
    value = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "operation_id", "files"}
        or value.get("schema_version") != 1
        or value.get("operation_id") != operation_id
        or not isinstance(value.get("files"), list)
    ):
        raise ValueError("Configuration file recovery manifest has an invalid shape")
    rows = cast(list[Any], value["files"])
    if len(rows) != len(_CONFIGURATION_MANAGED_PATHS):
        raise ValueError("Configuration file recovery manifest is incomplete")
    for expected_path, row in zip(_CONFIGURATION_MANAGED_PATHS, rows, strict=True):
        if (
            not isinstance(row, dict)
            or set(row) != {"path", "existed", "mode", "sha256", "copy"}
            or row.get("path") != expected_path
            or not isinstance(row.get("existed"), bool)
            or not isinstance(row.get("mode"), int)
            or not isinstance(row.get("sha256"), str)
            or not isinstance(row.get("copy"), str)
        ):
            raise ValueError("Configuration file recovery manifest contains an invalid record")
        if row["existed"]:
            copy_path = snapshot_dir / str(row["copy"])
            payload = copy_path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != row["sha256"]:
                raise ValueError("Configuration file recovery evidence failed integrity check")
        elif row["mode"] != 0 or row["sha256"] or row["copy"]:
            raise ValueError("Absent configuration file recovery record carries file data")
    return value


def _restore_configuration_files(project_root: Path, operation_id: str) -> None:
    manifest = _load_configuration_file_snapshot(project_root, operation_id)
    snapshot_dir = _configuration_snapshot_dir(project_root, operation_id)
    rows = manifest.get("files")
    if not isinstance(rows, list):
        raise ValueError("Configuration file recovery manifest is incomplete")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Configuration file recovery manifest contains an invalid record")
        destination = project_root / str(row["path"])
        if not bool(row["existed"]):
            destination.unlink(missing_ok=True)
            continue
        payload = (snapshot_dir / str(row["copy"])).read_bytes()
        _atomic_write_bytes(destination, payload, mode=int(row["mode"]))


def _configuration_files_match(project_root: Path, operation_id: str) -> bool:
    try:
        manifest = _load_configuration_file_snapshot(project_root, operation_id)
        rows = manifest.get("files")
        if not isinstance(rows, list):
            return False
        for row in rows:
            if not isinstance(row, dict):
                return False
            path = project_root / str(row["path"])
            if not bool(row["existed"]):
                if path.exists():
                    return False
                continue
            if not path.is_file():
                return False
            if path.stat().st_mode & 0o777 != int(row["mode"]):
                return False
            if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                return False
    except (OSError, ValueError):
        return False
    return True


def _candidate_tls_bytes(
    project_root: Path,
    config: DeploymentConfig,
) -> tuple[bytes | None, bytes | None]:
    if config.tls_mode == "reverse_proxy":
        return None, None
    runtime_cert = project_root / _RUNTIME_CERTIFICATE_RELATIVE_PATH
    runtime_key = project_root / _RUNTIME_PRIVATE_KEY_RELATIVE_PATH
    if config.tls_mode == "provided":
        cert_path = _operator_path(project_root, config.certificate_path)
        key_path = _operator_path(project_root, config.private_key_path)
        try:
            certificate, private_key = cert_path.read_bytes(), key_path.read_bytes()
        except OSError as exc:
            raise ValueError("Provided TLS certificate or key is unavailable") from exc
        issues = _tls_pair_issues(certificate, private_key, config.public_host)
        if issues:
            raise ValueError("; ".join(issues))
        return certificate, private_key
    if runtime_cert.is_file() and runtime_key.is_file():
        certificate, private_key = runtime_cert.read_bytes(), runtime_key.read_bytes()
        if not _tls_pair_issues(certificate, private_key, config.public_host):
            return certificate, private_key
    return _generate_local_tls(config.public_host)


def _tls_pair_issues(certificate_pem: bytes, key_pem: bytes, host: str) -> tuple[str, ...]:
    try:
        certificate = x509.load_pem_x509_certificate(certificate_pem)
        private_key = serialization.load_pem_private_key(key_pem, password=None)
    except (TypeError, ValueError):
        return ("TLS certificate or private key is not valid unencrypted PEM",)
    certificate_public = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_public = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    issues: list[str] = []
    if certificate_public != private_public:
        issues.append("TLS certificate does not match its private key")
    now = datetime.now(UTC)
    if not (certificate.not_valid_before_utc <= now < certificate.not_valid_after_utc):
        issues.append("TLS certificate is not currently valid")
    try:
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        issues.append("TLS certificate has no subject alternative name")
    else:
        try:
            expected_ip = ip_address(host)
        except ValueError:
            if host.casefold() not in {
                value.casefold() for value in san.get_values_for_type(x509.DNSName)
            }:
                issues.append("TLS certificate SAN does not include the public host")
        else:
            if expected_ip not in set(san.get_values_for_type(x509.IPAddress)):
                issues.append("TLS certificate SAN does not include the public IP")
    return tuple(issues)


def _generate_local_tls(host: str) -> tuple[bytes, bytes]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    now = datetime.now(UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    try:
        primary: x509.GeneralName = x509.IPAddress(ip_address(host))
    except ValueError:
        primary = x509.DNSName(host)
    san_values: list[x509.GeneralName] = [primary, x509.DNSName("localhost")]
    if host != "127.0.0.1":
        san_values.append(x509.IPAddress(ip_address("127.0.0.1")))
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(san_values), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM),
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def _operator_path(project_root: Path, raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    return candidate if candidate.is_absolute() else project_root / candidate


def _target_override_content(config: DeploymentConfig, target: ComposeTarget) -> str | None:
    """The dynamic reverse-proxy override, when -- and only when -- the target selects it.

    P1-3. It was passed whenever TLS was reverse_proxy. Production targets now select
    the static ingress overlays (infra/docker/{scalable,compact}/ingress-external_proxy-*)
    instead of the installer override file, and the Compose command builder refuses an
    override the target does not select -- so every config apply behind an external
    Nginx or Apache stopped at validation with "Candidate reverse-proxy override has no
    selected Compose target": an ordinary Scalable change, and a switch to Compact with
    this ingress, alike. Only development behind a host proxy still selects the file.
    """
    selects_override = any(
        str(compose_file).endswith(_TOKEN_INSTALLER_COMPOSE_OVERRIDE_YAML)
        for compose_file in target.compose_files
    )
    return _reverse_proxy_override(config) if selects_override else None


def _reverse_proxy_override(config: DeploymentConfig) -> str:
    return (
        "services:\n"
        "  traefik:\n"
        '    profiles: ["installer-disabled-traefik"]\n'
        "  fs-app:\n"
        "    ports:\n"
        f'      - "{config.reverse_proxy_backend_host}:'
        f'{config.reverse_proxy_backend_port}:8099"\n'
        "    depends_on:\n"
        "      traefik:\n"
        "        condition: service_started\n"
        "        required: false\n"
    )


def _secure_atomic_write_bytes(
    project_root: Path,
    directory: str,
    name: str,
    payload: bytes,
    *,
    mode: int,
) -> None:
    directory_fd = _open_project_directory(project_root, directory, create=True)
    temporary_name = f".{name}.{uuid.uuid4().hex}"
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary_name, flags, mode, dir_fd=directory_fd)
        os.fchmod(descriptor, mode)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=directory_fd)
        os.close(directory_fd)


def _secure_unlink(project_root: Path, directory: str, name: str) -> None:
    try:
        directory_fd = _open_project_directory(project_root, directory, create=False)
    except FileNotFoundError:
        return
    try:
        with suppress(FileNotFoundError):
            os.unlink(name, dir_fd=directory_fd)
            os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _open_project_directory(project_root: Path, directory: str, *, create: bool) -> int:
    root_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        root_flags |= os.O_DIRECTORY
    root_fd = os.open(project_root, root_flags)
    try:
        if create:
            with suppress(FileExistsError):
                os.mkdir(directory, mode=0o700, dir_fd=root_fd)
        flags = root_flags
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            return os.open(directory, flags, dir_fd=root_fd)
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                raise
            raise ValueError("artifact destination must be a real directory") from exc
    finally:
        os.close(root_fd)


def _atomic_write_bytes(path: Path, payload: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(raw_path)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        path.chmod(mode)
    except BaseException:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        temp_path.unlink(missing_ok=True)
        raise
