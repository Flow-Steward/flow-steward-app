"""Host inspection and collision-free endpoint allocation adapters."""

from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

_FULL_SHA = re.compile(r"[0-9a-f]{40}")


class EndpointInputError(ValueError):
    """The caller supplied an invalid endpoint allocation request."""


class EndpointRegistryError(RuntimeError):
    """The durable endpoint ownership registry is malformed."""


class EndpointUnavailableError(RuntimeError):
    """The requested host endpoint cannot be allocated safely."""


class SubprocessHostInspection:
    """Read-only Git and Docker checks rooted at one repository."""

    def __init__(self, repository_root: Path) -> None:
        self.repository_root = repository_root

    def current_commit(self) -> str:
        result = self._run("git", "rev-parse", "HEAD^{commit}")
        if result.returncode != 0:
            raise EndpointUnavailableError("Git cannot resolve the current commit")
        return result.stdout.strip()

    def tracked_tree_clean(self) -> bool:
        result = self._run("git", "status", "--porcelain", "--untracked-files=no")
        if result.returncode != 0:
            raise EndpointUnavailableError("Git cannot inspect the tracked working tree")
        return not result.stdout.strip()

    def docker_storage_readable(self) -> bool:
        result = self._run("docker", "info", "--format", "{{.Driver}}")
        return result.returncode == 0 and bool(result.stdout.strip())

    def _run(self, *command: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                cwd=self.repository_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(command, 124, stdout="", stderr="timed out")


class EndpointAllocator:
    """Allocate candidate-owned loopback ports under an inter-process lock."""

    def __init__(
        self,
        state_dir: Path,
        *,
        bind_probe: Callable[[int], bool] | None = None,
        first_port: int = 18000,
    ) -> None:
        self.state_dir = state_dir
        self.registry_path = state_dir / "endpoint-allocations.json"
        self.lock_path = state_dir / "endpoint-allocations.lock"
        self._bind_probe = bind_probe or _loopback_port_available
        self._first_port = first_port

    def allocate(
        self,
        candidate_commit: str,
        *,
        count: int,
        explicit_ports: Sequence[int] = (),
    ) -> tuple[int, ...]:
        if not _FULL_SHA.fullmatch(candidate_commit):
            raise EndpointInputError("endpoint owner must be a full lowercase Git SHA")
        if count <= 0:
            raise EndpointInputError("endpoint allocation count must be positive")
        if explicit_ports and len(explicit_ports) != count:
            raise EndpointInputError("explicit endpoint count does not match the requested count")

        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- endpoint state is an owner-only directory.
        os.chmod(self.state_dir, 0o700)
        lock_descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            registry = self._load_registry()
            existing = registry.get(candidate_commit)
            if existing is not None:
                if len(existing) != count:
                    raise EndpointInputError("candidate endpoint allocation count cannot change")
                return tuple(existing)

            owned_by_others = {
                port
                for owner, ports in registry.items()
                if owner != candidate_commit
                for port in ports
            }
            if explicit_ports:
                selected = tuple(explicit_ports)
                for port in selected:
                    if not 1 <= port <= 65535:
                        raise EndpointInputError(f"endpoint port {port} is outside the valid range")
                    if port in owned_by_others:
                        raise EndpointUnavailableError(
                            f"endpoint port {port} is owned by another candidate"
                        )
                    if not self._bind_probe(port):
                        raise EndpointUnavailableError(f"endpoint port {port} is unavailable")
            else:
                selected = self._select_available(count=count, excluded=owned_by_others)
            registry[candidate_commit] = list(selected)
            _write_owner_only_json(self.registry_path, registry)
            return selected
        finally:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)

    def release(self, candidate_commit: str) -> None:
        """Release only the exact candidate's durable endpoint claim."""

        if not _FULL_SHA.fullmatch(candidate_commit):
            raise EndpointInputError("endpoint owner must be a full lowercase Git SHA")
        if not self.lock_path.exists():
            return
        lock_descriptor = os.open(self.lock_path, os.O_RDWR)
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            registry = self._load_registry()
            if registry.pop(candidate_commit, None) is not None:
                _write_owner_only_json(self.registry_path, registry)
        finally:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)

    def _select_available(self, *, count: int, excluded: set[int]) -> tuple[int, ...]:
        selected: list[int] = []
        for port in range(self._first_port, 65536):
            if port in excluded or not self._bind_probe(port):
                continue
            selected.append(port)
            if len(selected) == count:
                return tuple(selected)
        raise EndpointUnavailableError(f"cannot allocate {count} candidate endpoint ports")

    def _load_registry(self) -> dict[str, list[int]]:
        if not self.registry_path.exists():
            return {}
        payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise EndpointRegistryError("candidate endpoint registry is invalid")
        registry: dict[str, list[int]] = {}
        allocated_ports: set[int] = set()
        for owner, ports in payload.items():
            if not isinstance(owner, str) or not _FULL_SHA.fullmatch(owner):
                raise EndpointRegistryError("candidate endpoint registry owner is invalid")
            if not isinstance(ports, list) or not all(
                isinstance(port, int) and 1 <= port <= 65535 for port in ports
            ):
                raise EndpointRegistryError("candidate endpoint registry ports are invalid")
            if len(set(ports)) != len(ports) or allocated_ports.intersection(ports):
                raise EndpointRegistryError(
                    "candidate endpoint registry contains duplicate ownership"
                )
            registry[owner] = ports
            allocated_ports.update(ports)
        return registry


def _loopback_port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def _write_owner_only_json(path: Path, payload: object) -> None:
    content = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
