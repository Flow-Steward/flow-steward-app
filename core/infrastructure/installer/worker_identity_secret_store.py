"""Owner-only deployment environment adapter for the worker machine token."""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from pathlib import Path

from core.infrastructure.installer.state_store import InstallerStateStore

_TOKEN_ENV_KEY = "FS_WORKER_MCP_BEARER_TOKEN"


class EnvWorkerIdentitySecretStore:
    def __init__(self, *, env_path: Path, state_store: InstallerStateStore) -> None:
        self._env_path = env_path
        self._state_store = state_store

    def read(self) -> str:
        return str(self._state_store.read_env_values(self._env_path).get(_TOKEN_ENV_KEY) or "")

    def replace(self, value: str) -> None:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("worker machine token is required")
        self._state_store.update_env_values(self._env_path, {_TOKEN_ENV_KEY: normalized})


class FileWorkerIdentitySecretStore:
    """Atomic owner-only token storage for the Dockerless Compact supervisor."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def read(self) -> str:
        try:
            return self._path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def replace(self, value: str) -> None:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("worker machine token is required")
        self._path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self._path.name}.", dir=self._path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(normalized + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            directory = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)


__all__ = ["EnvWorkerIdentitySecretStore", "FileWorkerIdentitySecretStore"]
