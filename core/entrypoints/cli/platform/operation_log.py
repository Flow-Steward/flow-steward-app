"""Secure, redacted operation-log creation for CLI invocations."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

#: Where a command's own record of itself is written when nothing says
#: otherwise: inside the project's own state directory.
DEFAULT_OPERATION_LOG_ROOT = Path(".flow-steward/logs")

#: Container runtimes mount the project read-only and cannot rely on a bind
#: nested inside it — Docker Desktop's virtiofs drops such a mount without
#: warning, and intermittently — so they point this at a directory of their own.
OPERATION_LOG_ROOT_ENV = "FS_OPERATION_LOG_ROOT"


def operation_log_root() -> Path:
    """The directory this host writes command operation logs to.

    The two launcher-set names are read as literals rather than through
    constants: the tracked env audit matches direct literal reads, so naming
    them would drop both variables' read locations from
    ``docs/configuration/flow-steward-env-audit.json`` and make the CLI look
    like it never reads them.
    """

    configured = str(_runtime_env.get(OPERATION_LOG_ROOT_ENV) or "").strip()
    if configured:
        return Path(configured)
    # Set by the Compact/Scalable launcher for the CLI container it starts; it
    # predates OPERATION_LOG_ROOT_ENV and means the same thing.
    cli_configured = str(_runtime_env.get("FS_CLI_OPERATION_LOG_ROOT") or "").strip()
    if cli_configured:
        return Path(cli_configured).resolve()
    # Containerised CLI against a bind-mounted host checkout: the log belongs
    # beside that checkout, and the launcher already exports the host path.
    project_root = str(_runtime_env.get("FS_DOCKER_HOST_PROJECT_ROOT") or "").strip()
    if project_root:
        return Path(project_root).resolve() / ".flow-steward/logs"
    return DEFAULT_OPERATION_LOG_ROOT


#: Where the Compose files mount the installation's state directory into a CLI
#: container, and the part of it each mount is (docker-compose*.yaml).
_STATE_MOUNTS = (
    (Path("/run/flow-steward-install-state"), ""),
    (Path("/run/flow-steward-logs"), "logs"),
    (Path("/run/flow-steward-runtime"), "runtime"),
)


def reported_log_path(path: Path | str) -> str:
    """The log path as the operator reaches it, which is not always where it was written.

    2026-11-11: `database upgrade` runs in a one-shot application container that writes
    its log to /app/logs/cli, and the error said `Log: /app/logs/cli/...`, a path that
    does not exist on the host. The launcher names the host directory of that log root
    in FS_CLI_OPERATION_LOG_HOST_ROOT; a path under the log root is reported there.
    The 0.9.20 jump tests found the same in the Compact CLI container, whose state
    directory is mounted at /run/flow-steward-install-state: such a path is reported
    under the installation's own state directory on the host.
    """
    host_root = str(_runtime_env.get("FS_CLI_OPERATION_LOG_HOST_ROOT") or "").strip()
    if host_root:
        try:
            relative = Path(path).resolve().relative_to(operation_log_root().resolve())
        except ValueError:
            pass
        else:
            return str(Path(host_root) / relative)
    host_state = _host_state_dir()
    if host_state is not None:
        for mount, part in _STATE_MOUNTS:
            try:
                relative = Path(path).relative_to(mount)
            except ValueError:
                continue
            return str(host_state / part / relative)
    return str(path)


def _host_state_dir() -> Path | None:
    """The installation's state directory as the host names it."""
    configured = str(_runtime_env.get("FS_CLI_HOST_STATE_DIR") or "").strip()
    if configured:
        return Path(configured)
    project_root = str(_runtime_env.get("FS_DOCKER_HOST_PROJECT_ROOT") or "").strip()
    return Path(project_root) / ".flow-steward" if project_root else None


class OperationLogUnavailable(RuntimeError):
    """A mutating command could not open the operation log it records itself in.

    Mutations are gated on having that record, so this stops the command. It is
    an environment problem with a name and a fix — a read-only state directory,
    the wrong owner — and reporting it as an unexpected internal error told an
    operator nothing and hid it behind a correlation id.
    """

    def __init__(self, root: Path, cause: OSError) -> None:
        super().__init__(f"Cannot write the operation log in '{root}': {cause}")
        self.root = root
        self.cause = cause


class OperationLog:
    """Append only entrypoint-supplied, already-redacted text."""

    def __init__(self, command_id: str, *, root: Path | None = None) -> None:
        root = root if root is not None else operation_log_root()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        operation_id = uuid4().hex
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.operation_id = operation_id
        self.path = root / f"{command_id}-{timestamp}-{operation_id}.log"
        self._file = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        _give_to_directory_owner(self._file, root)

    def write(self, message: str) -> None:
        os.write(self._file, f"{datetime.now(UTC).isoformat()} {message}\n".encode())

    def close(self) -> None:
        os.close(self._file)


def _give_to_directory_owner(descriptor: int, root: Path) -> None:
    """A log a root process writes into a host user's directory belongs to that user.

    Compact's `database upgrade` runs in a one-shot runtime container as root, with the
    installation's logs directory mounted in. A root-owned log there is one the
    operator cannot read and doctor reports as not theirs.
    """
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0:
        return
    try:
        owner = os.stat(root)
        if owner.st_uid != 0:
            os.fchown(descriptor, owner.st_uid, owner.st_gid)
    except OSError:
        return
