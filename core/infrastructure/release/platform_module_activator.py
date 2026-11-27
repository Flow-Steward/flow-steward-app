"""Activate a prepared platform-module set on the shared runtime directory (ADR 0082 §5).

The filesystem work -- reading staged artifacts, checking pins, verifying a candidate
list with the loader and replacing ``modules.json`` -- lives in
``core.infrastructure.platform_modules.module_store`` and runs where the runtime
directory is writable:

* Scalable: the operator CLI mounts the runtime directory read-write, so
  ``FilesystemModuleActivator`` runs the store in this process.
* Compact: the runtime directory is inside the ``compact-runtime`` container's data
  volume, which the operator CLI mounts but cannot enter (``/data/state`` is
  ``fs-operator:fs-runtime`` mode 0750). ``CompactModuleActivator`` runs the same store
  in a disposable container of the installed runtime image through the lifecycle
  gateway -- the path the Compact database upgrade already uses -- and parses its JSON.

Staged artifacts are never deleted here; retention belongs to the deployment manager.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.stack_models import StackGatewayError
from core.application.release.platform_modules import (
    PreparedModuleRef,
    canonical_module_set,
    role_verification_unsupported,
)
from core.infrastructure.platform_modules.module_store import (
    MODULE_DESCRIPTOR_FILENAME,
    STAGED_DIRNAME,
    ModuleRef,
    ModuleStoreError,
    PlatformModuleStore,
    read_module_descriptor,
    render_module_list,
)

#: Compose services the module step stops before the list is written and restarts one
#: at a time in this order afterwards, the HTTP app last. All but ``fs-mcp`` load
#: platform modules (they mount the runtime directory); ``fs-mcp`` is a runtime writer
#: that the database step (``upgrade_target_database``) stops, so a module-only change
#: must start it again too.
SCALABLE_MODULE_SERVICES = (
    "control-plane",
    "fs-worker",
    "fs-extension-installer",
    "fs-mcp",
    "fs-app",
)
#: Compact runs every role as a supervised child of one container.
COMPACT_MODULE_SERVICES = ("compact-runtime",)

#: The roles of each module-loading service that write a load status at start
#: (``core.infrastructure.platform_modules.role_status``). ``fs-extension-installer``
#: and ``fs-mcp`` register no platform module hooks.
SERVICE_MODULE_ROLES: Mapping[str, tuple[str, ...]] = {
    "fs-app": ("app",),
    "fs-worker": ("worker",),
    "control-plane": ("control",),
    "compact-runtime": ("app", "worker", "control"),
}

_REMEDIATION = (
    "Keep maintenance enabled, let the deployment manager stage the pinned "
    "platform modules again, then repeat release upgrade."
)
#: A refusal the in-image command reported, as ``cli_error_detail`` renders it.
_REPORTED_CODE = re.compile(r"^(release_modules?_[a-z_]+):\s*(.*)$", re.DOTALL)

_logger = logging.getLogger(__name__)


class RoleRestartPort(Protocol):
    """Stop and restart module-loading roles; a restart returns once the role is ready."""

    def module_roles(self) -> tuple[str, ...]:
        """The roles of this deployment that load platform modules, in restart order."""
        ...

    def stop_role(self, role: str) -> None:
        """Stop ``role``; stopping a stopped role is harmless."""
        ...

    def restart_role(self, role: str) -> None:
        """Restart ``role`` (also when it is stopped) and wait for its readiness gate."""
        ...


#: Reads ``{"roles": {role: {"loaded", "failures"}}}`` from one running service.
ServiceModuleStatus = Callable[[str], Mapping[str, Any]]


class RoleModuleStatusReader:
    """Collect the platform modules the running roles skipped at start (ADR 0082 §5).

    A role writes its status once it has registered its modules, so right after a
    restart a status may not be there yet: with ``wait`` the reader asks again until
    every expected role answered or ``wait_seconds`` passed. A service that cannot be
    asked, or a role that never answers, is logged and left out: only a reported
    failure counts as one. The result names the role of each failure.
    """

    def __init__(
        self,
        *,
        read_service: ServiceModuleStatus,
        services: Callable[[], tuple[str, ...]],
        wait_seconds: float = 60.0,
        interval_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read_service = read_service
        self._services = services
        self.wait_seconds = wait_seconds
        self.interval_seconds = interval_seconds
        self._sleep = sleep
        self._monotonic = monotonic

    def failures(self, *, wait: bool) -> tuple[dict[str, str], ...]:
        deadline = self._monotonic() + (self.wait_seconds if wait else 0.0)
        found: list[dict[str, str]] = []
        for service in self._services():
            expected = SERVICE_MODULE_ROLES.get(service)
            if not expected:
                continue
            roles = self._service_roles(service, expected, wait=wait, deadline=deadline)
            for role in expected:
                status = roles.get(role)
                if not isinstance(status, Mapping):
                    continue
                for record in status.get("failures") or ():
                    if isinstance(record, Mapping) and str(record.get("id") or "").strip():
                        found.append(
                            {
                                "role": role,
                                "id": str(record.get("id")),
                                "version": str(record.get("version") or ""),
                                "load_error": str(record.get("load_error") or ""),
                            }
                        )
        return tuple(found)

    def _service_roles(
        self, service: str, expected: tuple[str, ...], *, wait: bool, deadline: float
    ) -> Mapping[str, Any]:
        roles: Mapping[str, Any] = {}
        while True:
            try:
                answer = self._read_service(service)
                raw = answer.get("roles") if isinstance(answer, Mapping) else None
                roles = raw if isinstance(raw, Mapping) else {}
            except (LifecycleError, StackGatewayError, OSError) as exc:
                _logger.warning(
                    "Platform module load status of %s could not be read: %s", service, exc
                )
                roles = {}
            missing = [role for role in expected if role not in roles]
            if not missing:
                return roles
            if not wait or self._monotonic() >= deadline:
                # Before a change a role without a module list writes no status.
                (_logger.warning if wait else _logger.debug)(
                    "Platform module load status of role(s) %s in %s is not available; "
                    "only reported failures are checked",
                    ", ".join(missing),
                    service,
                )
                return roles
            self._sleep(self.interval_seconds)


def _no_failures(*, wait: bool) -> tuple[dict[str, str], ...]:
    del wait
    return ()


class ModuleRoleTarget(Protocol):
    """The installed Compose target, as far as restarting module-loading roles needs it."""

    @property
    def deployment_profile(self) -> str: ...

    @property
    def runtime_services(self) -> Collection[str]: ...


def _failure(code: str, cause: str, *, step: str = "modules_activate") -> LifecycleError:
    return LifecycleError.stable_failure(
        code=code, cause=cause, remediation=_REMEDIATION, step=step
    )


def _lifecycle_error(error: ModuleStoreError) -> LifecycleError:
    return _failure(error.code, error.cause)


def _module_ref(ref: PreparedModuleRef) -> ModuleRef:
    return ModuleRef(id=ref.id, version=ref.version, digest=ref.digest)


def _prepared_ref(ref: ModuleRef) -> PreparedModuleRef:
    return PreparedModuleRef(id=ref.id, version=ref.version, digest=ref.digest)


class FilesystemStagedModules:
    """``StagedModuleSource`` over ``<root>/staged/<module id>/<version>/``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._store = PlatformModuleStore(self.root)

    def staged_versions(self) -> dict[str, tuple[str, ...]]:
        return self._store.staged_versions()

    def staged_ref(self, module_id: str, version: str) -> PreparedModuleRef:
        try:
            return _prepared_ref(self._store.staged_ref(module_id, version))
        except ModuleStoreError as exc:
            raise _lifecycle_error(exc) from exc


def _discard(_line: str) -> None:
    return None


class FilesystemModuleActivator:
    """``ModuleActivator`` over a platform-module root this process can write (Scalable).

    ``activate`` rewrites only ``modules.json``; the roles read it on their next
    start, so a release change needs no restart here and a module-only change
    calls ``restart_roles``. The candidate is verified by the loader before the
    write (``PlatformModuleStore.verify_entries``).

    The interpreter tag and machine default to this process's, which assumes the
    operator CLI image and the role images share the ``base`` stage of
    ``infra/docker/Dockerfile`` and run on the same host. The loader runs here with
    ``image_checks=False``: integrity, layout, identity, digests and names shared
    between modules are checked, but nothing that depends on what this (management)
    image can import or has installed, because its packages differ from the role
    images'. Image conflicts -- a vendored distribution or top-level name that the
    role image already provides -- are detected by the database step, which runs the
    loader in the application image, and by each role at start.

    Verification inside each role's own image (``verify_in_roles``) is NOT
    implemented on Scalable: the roles there run several images, and this adapter
    does not add a Docker invocation of its own. ``supports_role_verification`` is
    therefore ``False`` and the CLI-side check above is the only check before the
    list is written. After the write, a module-only change's database step runs the
    loader in the ``fs-app`` image, and a refusal there fails the upgrade before any
    role starts.
    """

    def __init__(
        self,
        root: Path,
        *,
        restarter: RoleRestartPort,
        cache_tag: str | None = None,
        machine: str | None = None,
        load_status: RoleModuleStatusReader | None = None,
    ) -> None:
        self.root = Path(root)
        self.restarter = restarter
        self.load_status = load_status
        self.cache_tag = cache_tag
        self.machine = machine
        self._store = PlatformModuleStore(
            self.root, cache_tag=cache_tag, machine=machine, image_checks=False
        )

    @property
    def list_path(self) -> Path:
        return self._store.list_path

    @property
    def supports_activation(self) -> bool:
        return self._store.runtime_directory_writable()

    @property
    def supports_role_verification(self) -> bool:
        return False

    @property
    def unavailable_reason(self) -> str:
        if self.supports_activation:
            return ""
        return f"This process cannot write the runtime directory {self.root.parent}."

    def verify_in_roles(self, modules: tuple[PreparedModuleRef, ...]) -> None:
        del modules
        raise role_verification_unsupported()

    def stop_roles(self) -> None:
        # The HTTP app first, so nothing serves while the others stop.
        for role in reversed(self.restarter.module_roles()):
            self.restarter.stop_role(role)

    def adds_module_migrations(
        self,
        previous: tuple[PreparedModuleRef, ...],
        target: tuple[PreparedModuleRef, ...],
    ) -> bool:
        """Compare the staged ``db/migrations/*.sql`` of both sets by id, name and content.

        Any target migration that the previous set does not ship byte for byte counts.
        A staged artifact that cannot be read answers ``True``.
        """
        return self._store.adds_module_migrations(
            [_module_ref(ref) for ref in previous], [_module_ref(ref) for ref in target]
        )

    def installed_set(self) -> tuple[PreparedModuleRef, ...]:
        try:
            refs = self._store.installed_refs()
        except ModuleStoreError as exc:
            raise _lifecycle_error(exc) from exc
        return canonical_module_set(tuple(_prepared_ref(ref) for ref in refs))

    def staged_modules(self) -> tuple[PreparedModuleRef, ...]:
        try:
            return tuple(_prepared_ref(ref) for ref in self._store.staged_refs())
        except ModuleStoreError as exc:
            raise _lifecycle_error(exc) from exc

    def activate(self, modules: tuple[PreparedModuleRef, ...]) -> None:
        ordered = canonical_module_set(tuple(modules))
        try:
            if not ordered:
                if self._store.remove_list():
                    _logger.info("Platform module set cleared; the roles start as plain Core")
                return
            entries = self._store.entries_for(_module_ref(module) for module in ordered)
            rendered = render_module_list(entries)
            if self._store.current_bytes() == rendered:
                return
            self._store.verify_entries(rendered, entries)
        except ModuleStoreError as exc:
            raise _lifecycle_error(exc) from exc
        self._write_list(rendered)
        _logger.info(
            "Platform module set activated: %s",
            ", ".join(f"{module.id}@{module.version}" for module in ordered),
        )

    def restart_roles(self) -> None:
        for role in self.restarter.module_roles():
            self.restarter.restart_role(role)

    def module_load_failures(self, *, wait: bool) -> tuple[dict[str, str], ...]:
        if self.load_status is None:
            return _no_failures(wait=wait)
        return self.load_status.failures(wait=wait)

    def _write_list(self, rendered: bytes) -> None:
        try:
            self._store.write_list(rendered)
        except ModuleStoreError as exc:
            raise _lifecycle_error(exc) from exc


#: Runs one ``core.entrypoints.cli.ops.platform_modules`` command in a disposable
#: container of the installed Compact image and returns its ``result`` object.
PlatformModuleCommand = Callable[[tuple[str, ...]], Mapping[str, Any]]


class CompactModuleActivator:
    """``ModuleActivator`` for Compact: the store runs in the installed runtime image.

    Each operation is one ``core.entrypoints.cli.ops.platform_modules`` command run by
    ``DockerLifecycleGateway.platform_module_command`` -- ``compose run --rm --no-deps
    -T --entrypoint python compact-runtime -m ...``, the same disposable-container
    path as the Compact database upgrade, as root, with the data volume mounted at
    ``/data``. Its stdout is one JSON document; a refusal exits non-zero with
    ``{"ok": false, "error": {"code", "message", "hint"}}``, which the gateway turns
    into ``StackGatewayError("<code>: <message> <hint>")`` and this adapter back into
    the ``LifecycleError`` with that code.

    Because the command runs in the image every Compact role runs, the loader there
    applies the image checks too, and ``verify_in_roles`` is a real check of the
    candidate set with the roles' own interpreter and packages.

    Roles are stopped and started as one ``compact-runtime`` container through the
    same ``ComposeRoleRestarter`` as Scalable.
    """

    def __init__(
        self,
        *,
        run_command: PlatformModuleCommand,
        restarter: RoleRestartPort,
        target_provider: Callable[[], ModuleRoleTarget],
        load_status: RoleModuleStatusReader | None = None,
    ) -> None:
        self._run_command = run_command
        self.restarter = restarter
        self.target_provider = target_provider
        self.load_status = load_status

    @property
    def supports_activation(self) -> bool:
        return not self.unavailable_reason

    @property
    def supports_role_verification(self) -> bool:
        return True

    @property
    def unavailable_reason(self) -> str:
        try:
            profile = self.target_provider().deployment_profile
        except (LifecycleError, OSError) as exc:
            return f"The installed Compact configuration cannot be read: {exc}"
        if profile != "compact":
            return "The installed configuration is not a Compact deployment."
        return ""

    def installed_set(self) -> tuple[PreparedModuleRef, ...]:
        return self._modules(self._run(("installed",), step="modules_inspect"))

    def staged_modules(self) -> tuple[PreparedModuleRef, ...]:
        return self._modules(self._run(("staged",), step="modules_inspect"))

    def verify_in_roles(self, modules: tuple[PreparedModuleRef, ...]) -> None:
        self._run(("verify", "--modules", _module_json(modules)), step="modules_activate")

    def activate(self, modules: tuple[PreparedModuleRef, ...]) -> None:
        ordered = canonical_module_set(tuple(modules))
        result = self._run(
            ("activate", "--modules", _module_json(ordered)), step="modules_activate"
        )
        if result.get("changed"):
            _logger.info(
                "Platform module set activated on Compact: %s",
                ", ".join(f"{module.id}@{module.version}" for module in ordered) or "none",
            )

    def adds_module_migrations(
        self,
        previous: tuple[PreparedModuleRef, ...],
        target: tuple[PreparedModuleRef, ...],
    ) -> bool:
        try:
            result = self._run(
                (
                    "adds-migrations",
                    "--previous",
                    _module_json(previous),
                    "--target",
                    _module_json(target),
                ),
                step="modules_rollback",
            )
        except LifecycleError:
            return True
        answer = result.get("adds_migrations")
        return answer if isinstance(answer, bool) else True

    def stop_roles(self) -> None:
        for role in reversed(self.restarter.module_roles()):
            self.restarter.stop_role(role)

    def restart_roles(self) -> None:
        for role in self.restarter.module_roles():
            self.restarter.restart_role(role)

    def module_load_failures(self, *, wait: bool) -> tuple[dict[str, str], ...]:
        if self.load_status is None:
            return _no_failures(wait=wait)
        return self.load_status.failures(wait=wait)

    def _run(self, arguments: tuple[str, ...], *, step: str) -> Mapping[str, Any]:
        try:
            return self._run_command(arguments)
        except StackGatewayError as exc:
            detail = str(getattr(exc, "detail", "") or exc).strip()
            reported = _REPORTED_CODE.match(detail)
            raise _failure(
                reported.group(1) if reported else "release_module_command_failed",
                reported.group(2).strip() if reported else detail,
                step=step,
            ) from exc

    @staticmethod
    def _modules(result: Mapping[str, Any]) -> tuple[PreparedModuleRef, ...]:
        raw = result.get("modules")
        try:
            if not isinstance(raw, list):
                raise TypeError("modules is not a list")
            return tuple(PreparedModuleRef.model_validate(item) for item in raw)
        except (TypeError, ValidationError) as exc:
            raise _failure(
                "release_module_command_invalid",
                f"The Compact platform-module command returned an invalid module list: {exc}",
                step="modules_inspect",
            ) from exc


def _module_json(modules: tuple[PreparedModuleRef, ...]) -> str:
    return json.dumps([module.model_dump(mode="json") for module in modules], separators=(",", ":"))


class ComposeRoleRestarter:
    """Stop and restart module-loading services through the lifecycle gateway.

    ``DockerLifecycleGateway.restart`` is what ``flow-steward stack restart`` uses:
    ``compose restart`` followed by ``compose up --wait --no-recreate``, so it returns
    only once the service's healthcheck reports healthy; ``compose restart`` also
    starts a stopped service. Restarting a running service again is harmless, which
    makes ``restart_roles`` idempotent. ``DockerLifecycleGateway.stop`` is
    ``compose stop``, and stopping a stopped service is harmless too.
    """

    def __init__(
        self,
        *,
        target_provider: Callable[[], ModuleRoleTarget],
        restart: Callable[..., Any],
        stop: Callable[..., Any],
        timeout_seconds: int = 300,
    ) -> None:
        self.target_provider = target_provider
        self._restart = restart
        self._stop = stop
        self.timeout_seconds = timeout_seconds

    def module_roles(self) -> tuple[str, ...]:
        target = self.target_provider()
        candidates = (
            COMPACT_MODULE_SERVICES
            if target.deployment_profile == "compact"
            else SCALABLE_MODULE_SERVICES
        )
        return tuple(service for service in candidates if service in target.runtime_services)

    def stop_role(self, role: str) -> None:
        try:
            self._stop(
                self.target_provider(),
                (role,),
                timeout_seconds=self.timeout_seconds,
                output_sink=_discard,
                heartbeat=_discard,
            )
        except StackGatewayError as exc:
            raise LifecycleError.stable_failure(
                code="release_module_stop_failed",
                cause=str(exc),
                remediation=(
                    "Keep maintenance enabled, correct the reported Docker problem, then "
                    "repeat release upgrade."
                ),
                step="modules_quiesce",
            ) from exc

    def restart_role(self, role: str) -> None:
        try:
            self._restart(
                self.target_provider(),
                (role,),
                timeout_seconds=self.timeout_seconds,
                output_sink=_discard,
                heartbeat=_discard,
            )
        except StackGatewayError as exc:
            raise LifecycleError.stable_failure(
                code="release_module_restart_failed",
                cause=str(exc),
                remediation=(
                    "Keep maintenance enabled, read ./flow-steward stack logs for the "
                    "named service, then repeat release upgrade."
                ),
                step="modules_restart",
            ) from exc


__all__ = [
    "COMPACT_MODULE_SERVICES",
    "MODULE_DESCRIPTOR_FILENAME",
    "SCALABLE_MODULE_SERVICES",
    "SERVICE_MODULE_ROLES",
    "STAGED_DIRNAME",
    "CompactModuleActivator",
    "ComposeRoleRestarter",
    "FilesystemModuleActivator",
    "FilesystemStagedModules",
    "ModuleRoleTarget",
    "PlatformModuleCommand",
    "RoleModuleStatusReader",
    "RoleRestartPort",
    "ServiceModuleStatus",
    "read_module_descriptor",
]
