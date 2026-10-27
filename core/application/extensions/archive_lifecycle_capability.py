"""Fail-closed capability boundary for managed extension archive mutations."""

from __future__ import annotations

import errno
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import NoReturn

from core.application.management.errors import (
    ServicePreconditionError,
    ServiceUnavailableError,
)
from core.application.management.query_models import ExtensionArchiveLifecycleAvailabilityView

_MESSAGE_EXTENSION_ARCHIVE_LIFECYCLE_IS_UNAVAILABLE_IN_THIS_DEPLOYMENT = (
    "Extension archive lifecycle is unavailable in this deployment."
)

_PERMISSION_ERRNOS = frozenset({errno.EROFS, errno.EACCES, errno.EPERM})
_INSTALL_COMMAND = "./flow-steward extensions install --archive <path>"
_UNINSTALL_COMMAND = "./flow-steward extensions uninstall <extension-id>"


class ExtensionArchiveLifecycleCapability:
    def __init__(self, *, root_provider: Callable[[], str | Path] | None = None) -> None:
        self._root_provider = root_provider or self._default_root

    @staticmethod
    def _default_root() -> str:
        from core.infrastructure.extension_runtime.extension_runtime import default_extensions_root

        return default_extensions_root()

    @staticmethod
    def _remediation() -> dict[str, list[str]]:
        return {
            "install_update": [_INSTALL_COMMAND],
            "uninstall": [_UNINSTALL_COMMAND],
        }

    @staticmethod
    def _delegates_to_installer_process() -> bool:
        """True where a separate process owns the managed root and performs the writes.

        AC17-R59: on Compact the API runs as uid 10001 while ``/data/extensions`` is owned by
        the extension installer, uid 10005, mode 0750 — and the API proxies archive lifecycle
        to that installer on 127.0.0.1:8101 rather than writing anything itself. The probe
        below asks "can *this* process write?", which on that profile is deliberately no. So
        it reported the capability unavailable, the UI offered a CLI command a Compact
        launcher refuses, and `require_available` closed the API path too — while the
        installer that can do the work was running alongside, idle.

        The predicate stays fail-closed where the app is the writer: this only says the
        question belongs to someone else. A delegated install that cannot be served fails at
        the proxy, with that service's own error, instead of being refused in advance by a
        process whose permissions were never the relevant ones.
        """
        from core.infrastructure.config.runtime_env import runtime_env

        profile = str(runtime_env.get("FS_DEPLOYMENT_PROFILE") or "scalable").strip().lower()
        return profile == "compact"

    def availability(self) -> ExtensionArchiveLifecycleAvailabilityView:
        if self._delegates_to_installer_process():
            return ExtensionArchiveLifecycleAvailabilityView(
                available=True,
                reason_code="delegated_to_installer_process",
                message="",
                remediation={"install_update": [], "uninstall": []},
            )
        root = Path(self._root_provider())
        if not root.is_dir():
            return ExtensionArchiveLifecycleAvailabilityView(
                available=False,
                reason_code="managed_root_unavailable",
                message=_MESSAGE_EXTENSION_ARCHIVE_LIFECYCLE_IS_UNAVAILABLE_IN_THIS_DEPLOYMENT,
                remediation=self._remediation(),
            )
        try:
            flags = os.statvfs(root).f_flag
        except OSError as exc:
            if exc.errno not in _PERMISSION_ERRNOS:
                raise ServiceUnavailableError(
                    "Could not verify extension archive lifecycle availability."
                ) from exc
            flags = os.ST_RDONLY
        if flags & os.ST_RDONLY:
            return ExtensionArchiveLifecycleAvailabilityView(
                available=False,
                reason_code="managed_root_read_only",
                message=_MESSAGE_EXTENSION_ARCHIVE_LIFECYCLE_IS_UNAVAILABLE_IN_THIS_DEPLOYMENT,
                remediation=self._remediation(),
            )
        if not os.access(root, os.W_OK | os.X_OK):
            return ExtensionArchiveLifecycleAvailabilityView(
                available=False,
                reason_code="managed_root_not_writable",
                message=_MESSAGE_EXTENSION_ARCHIVE_LIFECYCLE_IS_UNAVAILABLE_IN_THIS_DEPLOYMENT,
                remediation=self._remediation(),
            )
        probe_fd: int | None = None
        probe_path = ""
        try:
            probe_fd, probe_path = tempfile.mkstemp(
                dir=root,
                prefix=".flow-steward-archive-capability-",
            )
            os.close(probe_fd)
            probe_fd = None
            os.unlink(probe_path)
            probe_path = ""
        except OSError as exc:
            if exc.errno not in _PERMISSION_ERRNOS:
                raise ServiceUnavailableError(
                    "Could not verify extension archive lifecycle availability."
                ) from exc
            reason_code = (
                "managed_root_read_only"
                if exc.errno == errno.EROFS
                else "managed_root_not_writable"
            )
            return ExtensionArchiveLifecycleAvailabilityView(
                available=False,
                reason_code=reason_code,
                message=_MESSAGE_EXTENSION_ARCHIVE_LIFECYCLE_IS_UNAVAILABLE_IN_THIS_DEPLOYMENT,
                remediation=self._remediation(),
            )
        finally:
            if probe_fd is not None:
                with suppress(OSError):
                    os.close(probe_fd)
            if probe_path:
                with suppress(OSError):
                    os.unlink(probe_path)
        return ExtensionArchiveLifecycleAvailabilityView(
            available=True,
            reason_code="available",
            message="",
            remediation={"install_update": [], "uninstall": []},
        )

    def require_available(self, *, operation: str) -> None:
        availability = self.availability()
        if availability.available:
            return
        if operation == "uninstall":
            message = "Extension archive uninstall is unavailable in this deployment."
        else:
            message = (
                "Extension archive installation and updates are unavailable in this deployment."
            )
        raise ServicePreconditionError(
            message,
            payload={
                "code": "extension_archive_lifecycle_unavailable",
                "message": message,
                "details": {"remediation": list(availability.remediation.get(operation) or [])},
            },
        )

    def translate_managed_root_error(self, exc: OSError, *, operation: str) -> NoReturn:
        if exc.errno not in _PERMISSION_ERRNOS or not exc.filename:
            raise exc
        root = Path(self._root_provider()).resolve()
        failed_path = Path(exc.filename).resolve()
        if not failed_path.is_relative_to(root):
            raise exc
        if self._delegates_to_installer_process():
            raise ServiceUnavailableError(self._delegated_write_failure_message()) from exc
        if exc.errno == errno.EPERM:
            # Not a denied write (EACCES) or a read-only tree (EROFS): the filesystem
            # refused one operation on an entry it let us create, such as a mode. That
            # names a fault in the mount, not a deployment that cannot install.
            raise self._refused_write(exc, root=root, failed_path=failed_path) from exc
        self._raise_permission_precondition(operation=operation)

    @staticmethod
    def _refused_write(exc: OSError, *, root: Path, failed_path: Path) -> ServiceUnavailableError:
        relative = failed_path.relative_to(root).as_posix() or "."
        reason = exc.strerror or os.strerror(exc.errno or 0)
        return ServiceUnavailableError(
            "The extension installer could not finish: the filesystem refused a change "
            f"to {relative} in the extensions directory ({reason}). The directory is "
            "writable, so check the mount it is on rather than the deployment."
        )

    @staticmethod
    def _delegated_write_failure_message() -> str:
        """Name what did not line up, rather than blaming the deployment.

        AC17-R101: a managed-root permission error inside *this* process while archive
        lifecycle is delegated means the request was never proxied to the installer.
        That is an internal routing fault, not a missing capability. The precondition
        this used to raise said "unavailable in this deployment" and pointed at a CLI
        that a Compact launcher refuses, which reads as a dead end in both directions
        and hides the one thing worth knowing: which half of the delegation is down.
        """
        from core.infrastructure.config.runtime_env import runtime_env

        enabled = str(runtime_env.get("FS_EXTENSION_INSTALLER_PROXY_ENABLED") or "").strip()
        if enabled.lower() not in {"1", "true", "yes", "on"}:
            cause = (
                "the extension installer proxy is not enabled for this process, so the "
                "request was never forwarded (FS_EXTENSION_INSTALLER_PROXY_ENABLED)"
            )
        else:
            origin = str(runtime_env.get("FS_EXTENSION_INSTALLER_ORIGIN") or "")
            cause = (
                "the extension installer proxy is enabled but did not forward the request; "
                f"check that the installer is reachable at {origin or 'its configured origin'}"
            )
        return (
            "Extension archive lifecycle is delegated to the extension installer, but this "
            f"process attempted the write itself and was denied: {cause}."
        )

    def _raise_permission_precondition(self, *, operation: str) -> NoReturn:
        if operation == "uninstall":
            message = "Extension archive uninstall is unavailable in this deployment."
            remediation = [_UNINSTALL_COMMAND]
        else:
            message = (
                "Extension archive installation and updates are unavailable in this deployment."
            )
            remediation = [_INSTALL_COMMAND]
        raise ServicePreconditionError(
            message,
            payload={
                "code": "extension_archive_lifecycle_unavailable",
                "message": message,
                "details": {"remediation": remediation},
            },
        )


__all__ = ["ExtensionArchiveLifecycleCapability"]
