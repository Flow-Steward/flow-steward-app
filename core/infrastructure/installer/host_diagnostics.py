"""Read-only host prerequisites used by Doctor before installation."""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

_MESSAGE_DOCKER_CLI = "Docker CLI"
_MESSAGE_NO_ACTION_REQUIRED = "No action required."
_MESSAGE_DOCKER_CONTEXT_AND_SOCKET = "Docker context and socket"
_MESSAGE_DOCKER_CLIENT_AND_BUILDX_CONFIGURATION = "Docker client and Buildx configuration"
_MESSAGE_REPOSITORY_ACCESS = "Repository access"
_MESSAGE_DEFAULT_HOST_PORTS = "Default host ports"

_DOCKER_TIMEOUT_SECONDS = 5
_MINIMUM_FREE_DISK_BYTES = 4 * 1024**3
_SUPPORTED_ARCHITECTURES = frozenset({"amd64", "arm64", "aarch64", "x86_64"})
_SUPPORTED_SYSTEMS = frozenset({"Darwin", "Linux"})
_DEFAULT_PORTS = (
    (8080, socket.SOCK_STREAM, "8080/tcp"),
    (8099, socket.SOCK_STREAM, "8099/tcp"),
    (8099, socket.SOCK_DGRAM, "8099/udp"),
    (5673, socket.SOCK_STREAM, "5673/tcp"),
    (15673, socket.SOCK_STREAM, "15673/tcp"),
    (1025, socket.SOCK_STREAM, "1025/tcp"),
    (8025, socket.SOCK_STREAM, "8025/tcp"),
    (55432, socket.SOCK_STREAM, "55432/tcp"),
    (15173, socket.SOCK_STREAM, "15173/tcp"),
    (8100, socket.SOCK_STREAM, "8100/tcp"),
    (9000, socket.SOCK_STREAM, "9000/tcp"),
    (17474, socket.SOCK_STREAM, "17474/tcp"),
    (17687, socket.SOCK_STREAM, "17687/tcp"),
    (18333, socket.SOCK_STREAM, "18333/tcp"),
    (19333, socket.SOCK_STREAM, "19333/tcp"),
)

Finding = dict[str, object]
HostChecker = Callable[[Path], Finding]


def _finding(
    check_id: str,
    title: str,
    status: str,
    detail: str,
    remediation: str,
) -> Finding:
    return {
        "id": check_id,
        "category": "core",
        "title": title,
        "status": status,
        "detail": detail,
        "remediation": remediation,
    }


def _run_docker(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("docker", *arguments),
        check=False,
        capture_output=True,
        text=True,
        timeout=_DOCKER_TIMEOUT_SECONDS,
    )


def check_platform(repo_root: Path) -> Finding:
    """Report whether the host OS and architecture are supported."""
    del repo_root
    try:
        system = platform.system()
        machine = platform.machine()
    except OSError:
        return _finding(
            "host_platform",
            "Host platform",
            "fail",
            "The host operating system and architecture could not be determined.",
            "Run the installer on macOS or Linux using amd64 or arm64 hardware.",
        )
    supported = system in _SUPPORTED_SYSTEMS and machine.lower() in _SUPPORTED_ARCHITECTURES
    return _finding(
        "host_platform",
        "Host platform",
        "pass" if supported else "fail",
        f"Detected {system or 'unknown'} on {machine or 'unknown'}.",
        (
            _MESSAGE_NO_ACTION_REQUIRED
            if supported
            else "Run the installer on macOS or Linux using amd64 or arm64 hardware."
        ),
    )


def docker_cli_available() -> bool:
    """Read executable availability without contacting Docker or changing host state."""
    return shutil.which("docker") is not None


def check_docker_cli(repo_root: Path) -> Finding:
    """Report Docker CLI presence and a bounded client-version result."""
    del repo_root
    try:
        if not docker_cli_available():
            return _finding(
                "docker_cli",
                _MESSAGE_DOCKER_CLI,
                "fail",
                "Docker CLI was not found on PATH.",
                "Install Docker Desktop or Docker Engine, including the Docker CLI.",
            )
        result = _run_docker("version", "--format", "{{.Client.Version}}")
    except (OSError, subprocess.SubprocessError):
        return _finding(
            "docker_cli",
            _MESSAGE_DOCKER_CLI,
            "fail",
            "Docker CLI version could not be read.",
            "Repair the Docker CLI installation and ensure docker is executable on PATH.",
        )
    version = result.stdout.strip()
    if result.returncode != 0 or not version:
        return _finding(
            "docker_cli",
            _MESSAGE_DOCKER_CLI,
            "fail",
            "Docker CLI is present, but its client version could not be read.",
            "Repair the Docker CLI installation and rerun Doctor.",
        )
    return _finding(
        "docker_cli",
        _MESSAGE_DOCKER_CLI,
        "pass",
        f"Docker CLI client version {version} is available.",
        _MESSAGE_NO_ACTION_REQUIRED,
    )


def check_docker_daemon(repo_root: Path) -> Finding:
    """Report whether the local Docker daemon answers a bounded probe."""
    del repo_root
    try:
        result = _run_docker("info", "--format", "{{.ServerVersion}}")
    except (OSError, subprocess.SubprocessError):
        result = None
    version = result.stdout.strip() if result is not None and result.returncode == 0 else ""
    if not version:
        return _finding(
            "docker_daemon",
            "Docker daemon",
            "fail",
            "Docker daemon is not reachable.",
            "Start Docker Desktop or Docker Engine, then rerun Doctor.",
        )
    return _finding(
        "docker_daemon",
        "Docker daemon",
        "pass",
        f"Docker daemon version {version} is reachable.",
        _MESSAGE_NO_ACTION_REQUIRED,
    )


def check_docker_context(repo_root: Path) -> Finding:
    """Report the active local Docker context and effective socket access."""
    del repo_root
    try:
        result = _run_docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
    except (OSError, subprocess.SubprocessError):
        result = None
    endpoint = result.stdout.strip() if result is not None and result.returncode == 0 else ""
    if not endpoint:
        return _finding(
            "docker_context",
            _MESSAGE_DOCKER_CONTEXT_AND_SOCKET,
            "fail",
            "The active Docker context endpoint could not be resolved.",
            "Select a local Docker context with an accessible Unix socket.",
        )
    if not endpoint.startswith("unix://"):
        return _finding(
            "docker_context",
            _MESSAGE_DOCKER_CONTEXT_AND_SOCKET,
            "fail",
            "The active Docker context uses an unsupported remote endpoint.",
            "Select a local Docker context backed by a Unix socket.",
        )
    socket_path = Path(endpoint.removeprefix("unix://"))
    try:
        accessible = (
            socket_path.is_absolute()
            and socket_path.exists()
            and os.access(socket_path, os.R_OK | os.W_OK)
        )
    except OSError:
        accessible = False
    if not accessible:
        return _finding(
            "docker_context",
            _MESSAGE_DOCKER_CONTEXT_AND_SOCKET,
            "fail",
            f"Docker socket {socket_path} is missing or not readable and writable.",
            "Select a local Docker context and grant the current user access to its socket.",
        )
    return _finding(
        "docker_context",
        _MESSAGE_DOCKER_CONTEXT_AND_SOCKET,
        "pass",
        f"Docker context resolves to accessible socket {socket_path}.",
        _MESSAGE_NO_ACTION_REQUIRED,
    )


def check_docker_compose(repo_root: Path) -> Finding:
    """Report whether a supported Docker Compose plugin is usable."""
    del repo_root
    try:
        result = _run_docker("compose", "version", "--short")
    except (OSError, subprocess.SubprocessError):
        result = None
    version = result.stdout.strip() if result is not None and result.returncode == 0 else ""
    normalized = version.removeprefix("v")
    major = normalized.partition(".")[0]
    supported = major.isascii() and major.isdecimal() and int(major) >= 2
    if not supported:
        detail = (
            f"Docker Compose version {version} is not supported."
            if version
            else "Docker Compose plugin version 2 or newer is not available."
        )
        return _finding(
            "docker_compose",
            "Docker Compose",
            "fail",
            detail,
            "Install or enable Docker Compose plugin version 2 or newer.",
        )
    return _finding(
        "docker_compose",
        "Docker Compose",
        "pass",
        f"Docker Compose {version} is available.",
        _MESSAGE_NO_ACTION_REQUIRED,
    )


def _nearest_existing_path(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _docker_config_root() -> Path:
    return Path.home() / ".docker"


def check_docker_config(repo_root: Path) -> Finding:
    """Report credential-helper and Buildx writability risks without rendering config values."""
    del repo_root
    try:
        config_root = _docker_config_root()
        config_path = config_root / "config.json"
        config: Mapping[str, object] = {}
        if config_path.exists():
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, Mapping):
                raise ValueError("Docker config must be an object")
            config = loaded
        buildx_root = config_root / "buildx"
        buildx_parent = _nearest_existing_path(buildx_root)
        helper_configured = bool(config.get("credsStore") or config.get("credHelpers"))
        buildx_writable = os.access(buildx_parent, os.W_OK)
    except (OSError, ValueError):
        return _finding(
            "docker_config",
            _MESSAGE_DOCKER_CLIENT_AND_BUILDX_CONFIGURATION,
            "fail",
            "Docker client configuration could not be read safely.",
            "Repair the Docker config JSON and verify access to its parent directory.",
        )
    risks = []
    if helper_configured:
        risks.append("a host credential helper is configured")
    if not buildx_writable:
        risks.append("the Buildx configuration path is not writable")
    if risks:
        return _finding(
            "docker_config",
            _MESSAGE_DOCKER_CLIENT_AND_BUILDX_CONFIGURATION,
            "warn",
            "Docker bootstrap risk detected: " + "; ".join(risks) + ".",
            "Use the installer's isolated Docker configuration; do not make host credentials writable.",
        )
    return _finding(
        "docker_config",
        _MESSAGE_DOCKER_CLIENT_AND_BUILDX_CONFIGURATION,
        "pass",
        "No host credential-helper or Buildx writability risk was detected.",
        _MESSAGE_NO_ACTION_REQUIRED,
    )


def check_repository_access(repo_root: Path) -> Finding:
    """Report repository and existing installer-state writability without creating paths."""
    try:
        root = Path(repo_root).resolve()
        state_root = root / ".flow-steward"
        root_writable = root.is_dir() and os.access(root, os.R_OK | os.W_OK | os.X_OK)
        state_writable = not state_root.exists() or (
            state_root.is_dir()
            and not state_root.is_symlink()
            and os.access(state_root, os.R_OK | os.W_OK | os.X_OK)
        )
    except OSError:
        return _finding(
            "repository_access",
            _MESSAGE_REPOSITORY_ACCESS,
            "fail",
            "Repository or installer-state access could not be inspected safely.",
            "Grant the current user read and write access to the repository and .flow-steward.",
        )
    if not root_writable or not state_writable:
        return _finding(
            "repository_access",
            _MESSAGE_REPOSITORY_ACCESS,
            "fail",
            f"Repository or installer state is not safely writable under {root}.",
            "Grant the current user read and write access to the repository and .flow-steward.",
        )
    return _finding(
        "repository_access",
        _MESSAGE_REPOSITORY_ACCESS,
        "pass",
        f"Repository and installer state are writable under {root}.",
        _MESSAGE_NO_ACTION_REQUIRED,
    )


def check_free_disk(repo_root: Path) -> Finding:
    """Report whether the repository filesystem has the installer minimum free space."""
    try:
        root = Path(repo_root).resolve()
        free_bytes = shutil.disk_usage(root).free
    except OSError:
        return _finding(
            "free_disk",
            "Free disk space",
            "fail",
            "Free disk space could not be measured for the repository filesystem.",
            "Verify filesystem access and keep at least 4 GiB free before installation.",
        )
    free_gib = free_bytes / 1024**3
    sufficient = free_bytes >= _MINIMUM_FREE_DISK_BYTES
    return _finding(
        "free_disk",
        "Free disk space",
        "pass" if sufficient else "fail",
        f"{free_gib:.1f} GiB is free on the repository filesystem.",
        _MESSAGE_NO_ACTION_REQUIRED
        if sufficient
        else "Keep at least 4 GiB free before installation.",
    )


def _port_is_available(port: int, *, socket_type: socket.SocketKind) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket_type) as candidate:
            candidate.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def check_default_ports(repo_root: Path) -> Finding:
    """Report availability of the default first-release ingress ports."""
    del repo_root
    try:
        busy = tuple(
            label
            for port, socket_type, label in _DEFAULT_PORTS
            if not _port_is_available(port, socket_type=socket_type)
        )
    except (OSError, ValueError):
        return _finding(
            "default_ports",
            _MESSAGE_DEFAULT_HOST_PORTS,
            "fail",
            "Default host-port availability could not be checked.",
            "Verify local socket permissions and free the required default ports.",
        )
    if busy:
        return _finding(
            "default_ports",
            _MESSAGE_DEFAULT_HOST_PORTS,
            "fail",
            "Default host ports are already in use: " + ", ".join(busy) + ".",
            "Stop the conflicting listeners or free the listed ports before installation.",
        )
    return _finding(
        "default_ports",
        _MESSAGE_DEFAULT_HOST_PORTS,
        "pass",
        "All required default host ports are available.",
        _MESSAGE_NO_ACTION_REQUIRED,
    )


HOST_DIAGNOSTIC_CHECKERS: tuple[HostChecker, ...] = (
    check_platform,
    check_docker_cli,
    check_docker_daemon,
    check_docker_context,
    check_docker_compose,
    check_docker_config,
    check_repository_access,
    check_free_disk,
    check_default_ports,
)


def collect_host_diagnostics(repo_root: Path) -> tuple[dict[str, object], ...]:
    """Collect one complete, read-only finding from each bounded host checker."""
    return tuple(checker(repo_root) for checker in HOST_DIAGNOSTIC_CHECKERS)
