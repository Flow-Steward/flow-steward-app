"""Activate prepared platform modules from the shared runtime directory.

A platform module (ADR 0082) reaches a running deployment as a prepared tree on the
shared runtime directory instead of being baked into the image. The operator CLI
writes the tree and its list; every runtime role only reads them. This module
verifies the prepared trees against the list, appends their import roots to
``sys.path`` and, for background roles, calls the role registration hooks.

Layout under the root::

    modules.json
    <module path>/src/...    first-party import root of the module
    <module path>/site/...   vendored third-party dependencies of the module
    <module path>/db/...     module SQL migrations, read by the host migrator only

The list is a file, not an environment variable, because Compact child processes
receive an allowlisted environment and would never see an env-driven list.

Platform modules are trusted in-process code. Publisher signing is planned and not
yet verified by Core; this loader only compares each file with the digest in the
list. The checks here are defence in depth against packaging mistakes and against one
module shadowing another's import names; they are not a sandbox. Before any path is
appended, every top-level importable name of every module is accounted for: ``src/``
holds only the module's own import package, every name in ``site/`` belongs to a
``*.dist-info`` in that ``site/``, a module's own package is unique and not
importable from Core's paths, and a vendored distribution is shared with another
module or with the image only at exactly the same version (and, between modules,
the same file digests). After import, the module's own package must come from its
own ``src/``.
"""

from __future__ import annotations

import csv
import hashlib
import importlib
import importlib.machinery
import importlib.metadata
import json
import logging
import os
import platform
import re
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableSequence, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.extension_runtime.package_identity import CANONICAL_EXTENSION_ID_RE
from core.infrastructure.platform_modules.dependencies import (
    ModuleRequirement,
    parse_module_requirements,
    resolve_module_dependencies,
)
from core.infrastructure.platform_modules.module_settings import (
    ModuleSettingDeclaration,
    conflicting_module_settings,
    module_set_setting_conflicts,
    parse_module_settings,
    publish_module_settings,
)
from core.infrastructure.security.secret_detection import redact_detected_secrets

DEFAULT_PLATFORM_MODULES_ROOT = Path("/run/flow-steward-runtime/platform-modules")
PLATFORM_MODULES_ROOT_ENV = "FS_PLATFORM_MODULES_ROOT"
MODULE_LIST_FILENAME = "modules.json"
MODULE_LIST_SCHEMA_VERSION = 1

PLATFORM_MODULE_ROLES = frozenset({"app", "worker", "control"})
ROLE_ENTRYPOINTS: Mapping[str, str] = MappingProxyType(
    {"worker": "register_worker", "control": "register_control"}
)

_IMPORT_TREES = ("src", "site")
# Every tree whose files are pinned by ``files_sha256``. ``db/`` is verified but never
# importable: the host migrator executes its SQL.
_VERIFIED_TREES = (*_IMPORT_TREES, "db")
_REQUIRED_ENTRY_KEYS = frozenset(
    {"id", "version", "import", "path", "python_cache_tag", "machine", "files_sha256"}
)
#: Keys a list entry (and a staged descriptor) may carry in addition: ``provides``
#: (declared capabilities), ``requires`` (modules that must load first) and
#: ``settings`` (deployment-level settings the operator sets, see module_settings.py).
OPTIONAL_ENTRY_KEYS = frozenset({"provides", "requires", "settings"})
#: The capability that makes a module the deployment's one entitlement source. The
#: same name as ``core.application.settings.platform_contracts.ENTITLEMENT_SOURCE_CAPABILITY``.
ENTITLEMENT_SOURCE_CAPABILITY = "entitlement_source"
#: The capability that lets a module register secret backends. The same name as
#: ``core.infrastructure.security.secret_backends.SECRET_BACKEND_CAPABILITY``.
SECRET_BACKEND_CAPABILITY = "secret_backend"
#: Every capability a module may declare under ``"provides"``.
PLATFORM_MODULE_CAPABILITIES = frozenset({ENTITLEMENT_SOURCE_CAPABILITY, SECRET_BACKEND_CAPABILITY})
_IMPORT_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_HASH_CHUNK_BYTES = 1024 * 1024

_logger = logging.getLogger(__name__)


class PlatformModuleError(RuntimeError):
    """A prepared platform module cannot be activated, so it is refused or skipped."""


class PlatformModuleListError(PlatformModuleError):
    """The module list is malformed."""


class PlatformModuleIntegrityError(PlatformModuleError):
    """A prepared tree does not match the digests in the module list."""


class PlatformModuleCompatibilityError(PlatformModuleError):
    """A prepared tree was built for another interpreter or machine."""


class PlatformModuleConflictError(PlatformModuleError):
    """A prepared tree would shadow or contradict code that is already importable."""


class PlatformModuleImportError(PlatformModuleError):
    """A verified platform module cannot be imported or registered."""


class PlatformModuleDependencyError(PlatformModuleError):
    """A module requires a module that is missing, too old, failed, or in a cycle."""


#: Container metadata key listing the prepared modules that failed to load in a role.
PLATFORM_MODULE_FAILURES_KEY = "platform_module_failures"
_LOAD_ERROR_MAX_CHARS = 500


def _list_restorer(target: list[Any]) -> Callable[[], None]:
    saved = list(target)

    def restore() -> None:
        target[:] = saved

    return restore


def _dict_restorer(target: dict[Any, Any]) -> Callable[[], None]:
    saved = dict(target)

    def restore() -> None:
        target.clear()
        target.update(saved)

    return restore


def _set_restorer(target: set[Any]) -> Callable[[], None]:
    saved = set(target)

    def restore() -> None:
        target.clear()
        target.update(saved)

    return restore


def _checkpoint_restorer(target: Any) -> Callable[[], None] | None:
    take = getattr(target, "loading_checkpoint", None)
    give_back = getattr(target, "restore_loading_checkpoint", None)
    if not callable(take) or not callable(give_back):
        return None
    checkpoint = take()

    def restore() -> None:
        give_back(checkpoint)

    return restore


def _restorer_for(target: Any) -> Callable[[], None] | None:
    restorer = _checkpoint_restorer(target)
    if restorer is not None:
        return restorer
    if type(target) is list:
        return _list_restorer(target)
    if type(target) is dict:
        return _dict_restorer(target)
    if type(target) is set:
        return _set_restorer(target)
    return None


def _attribute_restorer(target: Any, name: str) -> Callable[[], None] | None:
    if target is None or not hasattr(target, name):
        return None
    saved = getattr(target, name)

    def restore() -> None:
        setattr(target, name, saved)

    return restore


def _global_registry_restorer(module_name: str) -> Callable[[], None]:
    """Checkpoint a Core module-level registry that modules can register into.

    The module offers ``loading_checkpoint()`` and
    ``restore_loading_checkpoint(checkpoint)``; ``None`` restores the empty state, for
    a registry module that was first imported while the failing module loaded.
    """
    captured = sys.modules.get(module_name)
    take = getattr(captured, "loading_checkpoint", None)
    checkpoint = take() if callable(take) else None

    def restore() -> None:
        current = sys.modules.get(module_name)
        give_back = getattr(current, "restore_loading_checkpoint", None)
        if callable(give_back):
            give_back(checkpoint if current is captured else None)

    return restore


#: Mutable FastAPI application state a module's ``register`` can add to.
_APP_ROUTER_LISTS = ("routes", "on_startup", "on_shutdown")
_APP_STATE_FIELDS = ("user_middleware", "exception_handlers", "dependency_overrides")
#: Core modules whose process-wide registries a module of any role can register into.
_CORE_GLOBAL_REGISTRY_MODULES = (
    "core.application.auth.serve_auth_runtime",
    "core.application.auth.user_lifecycle_events",
    "core.infrastructure.security.secret_backends",
)
#: The container dictionaries a module may add top-level entries to.
_CONTAINER_DICTS = ("registries", "service_factories", "auth_helpers", "metadata")


class ModuleRegistrationRollback:
    """Undo what one platform module registered when its loading fails (ADR 0082).

    Captured just before a module is imported, it remembers the state a module can
    add to: the app's routes, startup and shutdown hooks, lifespan context,
    middleware, exception handlers, dependency overrides and ``app.state`` values;
    the container's four dictionaries and every list, set or dictionary registry in
    them; every object that offers a registration checkpoint
    (``loading_checkpoint()`` and ``restore_loading_checkpoint(checkpoint)``), such
    as Core's entitlement service, the feature endpoint registry and the module
    route rule registry, found directly in the registries, one level inside a
    dictionary registry (``platform_contracts``), as ``container.feature_endpoints``
    or ``container.external_sign_in`` or ``container.secret_backends`` or among a
    background role's ``services``; and, in every role, the process-wide Core
    registries listed in ``_CORE_GLOBAL_REGISTRY_MODULES`` (the auth session issue
    guards, the user-deleted callbacks and the secret backends), which a worker
    module can reach without any ``services``.

    :meth:`restore` puts all of it back, so a failed module leaves no reachable
    route, rule, page, dependency override, feature endpoint, session guard or
    entitlement source behind. It cannot undo effects outside that state: modules
    it imported stay in ``sys.modules``, threads or OpenTelemetry instruments it
    started stay, objects of Core it patched stay patched, and database or file
    writes stay written. Such effects are the module's own responsibility; a module
    must register through Core's registries and do nothing else before its hook
    returns.
    :meth:`mark_degraded` then marks every captured object that offers
    ``mark_loading_degraded(reason)`` (Core's entitlement service), so a missing
    module turns definite "not entitled" answers into "unknown". A failure to
    restore raises :class:`PlatformModuleError`: the process then stops rather than
    run half registered. Loading is single threaded at start, so everything added
    after the capture belongs to the module being loaded.
    """

    __slots__ = ("_degradables", "_restorers", "_scoped")

    def __init__(
        self,
        restorers: Sequence[Callable[[], None]],
        degradables: Sequence[Any] = (),
        scoped: Sequence[Any] = (),
    ) -> None:
        self._restorers = tuple(restorers)
        self._degradables = tuple(degradables)
        self._scoped = tuple(scoped)

    @classmethod
    def capture(
        cls,
        container: Any = None,
        *,
        app: Any = None,
        services: Mapping[str, Any] | None = None,
    ) -> ModuleRegistrationRollback:
        restorers: list[Callable[[], None]] = [
            _global_registry_restorer(name) for name in _CORE_GLOBAL_REGISTRY_MODULES
        ]
        candidates: list[Any] = []
        if app is not None:
            router = getattr(app, "router", None)
            candidates.extend(getattr(router, name, None) for name in _APP_ROUTER_LISTS)
            candidates.extend(getattr(app, name, None) for name in _APP_STATE_FIELDS)
            candidates.append(getattr(getattr(app, "state", None), "_state", None))
            lifespan = _attribute_restorer(router, "lifespan_context")
            if lifespan is not None:
                restorers.append(lifespan)
            restorers.append(lambda: _reset_openapi_schema(app))
        nested: list[Any] = []
        if container is not None:
            candidates.extend(getattr(container, name, None) for name in _CONTAINER_DICTS)
            candidates.append(getattr(container, "feature_endpoints", None))
            candidates.append(getattr(container, "external_sign_in", None))
            candidates.append(getattr(container, "secret_backends", None))
            registries = getattr(container, "registries", None)
            for value in registries.values() if isinstance(registries, dict) else ():
                candidates.append(value)
                if isinstance(value, dict):
                    nested.extend(value.values())
        candidates.extend((services or {}).values())
        seen: set[int] = set()
        degradables: list[Any] = []
        scoped: list[Any] = []
        for candidate in candidates:
            if candidate is None or id(candidate) in seen:
                continue
            seen.add(id(candidate))
            restorer = _restorer_for(candidate)
            if restorer is not None:
                restorers.append(restorer)
            if callable(getattr(candidate, "mark_loading_degraded", None)):
                degradables.append(candidate)
            if callable(getattr(candidate, "platform_module_scope", None)):
                scoped.append(candidate)
        for candidate in nested:
            if candidate is None or id(candidate) in seen:
                continue
            seen.add(id(candidate))
            restorer = _checkpoint_restorer(candidate)
            if restorer is not None:
                restorers.append(restorer)
            if callable(getattr(candidate, "mark_loading_degraded", None)):
                degradables.append(candidate)
            if callable(getattr(candidate, "platform_module_scope", None)):
                scoped.append(candidate)
        return cls(restorers, degradables, scoped)

    @contextmanager
    def loading_scope(self, module: PreparedPlatformModule | None) -> Iterator[None]:
        """Tell every captured service which module registers, and what it declares.

        Each captured object that offers ``platform_module_scope(module_id,
        provides)`` (Core's entitlement service) is entered for the duration of one
        module's hook, so the service can refuse a registration that the module's
        descriptor does not declare (ADR 0082). ``module`` is ``None`` for a module
        without a platform module identity, which declares nothing.
        """
        module_id = module.module_id if module is not None else None
        provides = module.provides if module is not None else frozenset()
        with ExitStack() as stack:
            for target in self._scoped:
                stack.enter_context(target.platform_module_scope(module_id, provides))
            yield

    def restore(self) -> None:
        errors: list[Exception] = []
        for restorer in reversed(self._restorers):
            try:
                restorer()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise PlatformModuleError(
                "a platform module failed to load and what it registered could not be "
                f"rolled back: {type(errors[0]).__name__}: {errors[0]}"
            ) from errors[0]

    def mark_degraded(self, module: PreparedPlatformModule) -> None:
        """Tell every captured service that ``module`` failed to load in this role.

        Core's entitlement service then answers "unknown" instead of a definite
        "not entitled" (:meth:`CoreEntitlementService.mark_loading_degraded`), so a
        consumer that destroys data on "not entitled" (the approval journal purge)
        holds off while the role runs without a module that may grant the feature.
        A service that cannot be marked stops the process, like a failed restore.
        """
        self.mark_degraded_by(module.module_id, module.version)

    def mark_degraded_by(self, module_id: str, version: str) -> None:
        """:meth:`mark_degraded` for a module known only by its id and version."""
        reason = f"{module_id} {version}"
        for target in self._degradables:
            try:
                target.mark_loading_degraded(reason)
            except Exception as exc:
                raise PlatformModuleError(
                    f"platform module '{module_id}' failed to load and "
                    f"{type(target).__name__} could not be marked degraded: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc


def mark_failed_modules_degraded(
    records: Sequence[Mapping[str, str]],
    container: Any = None,
    *,
    app: Any = None,
    services: Mapping[str, Any] | None = None,
) -> None:
    """Mark Core's services degraded for modules refused before they were imported.

    A module that fails its activation checks (:func:`activate_prepared_platform_modules`
    with ``failures``) never registers anything, so there is nothing to roll back;
    its features are off, and like a module whose import failed it may have been the
    source of an entitlement, so the entitlement service answers "unknown".
    """
    if not records:
        return
    targets = ModuleRegistrationRollback.capture(container, app=app, services=services)
    for record in records:
        targets.mark_degraded_by(str(record.get("id") or ""), str(record.get("version") or ""))


def _reset_openapi_schema(app: Any) -> None:
    if getattr(app, "openapi_schema", None) is not None:
        app.openapi_schema = None


_CONNECTION_URI_PATTERN = re.compile(
    r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|rediss|amqps?)://[^\s\"']+",
    re.IGNORECASE,
)


def module_failure_reason(exc: BaseException) -> str:
    """The short reason an administrator sees for a module that failed to load.

    The exception type and the first line of its message, with detected credentials
    and connection URIs masked and the length bounded. The traceback goes to the
    log only, never into this text.
    """
    message = str(exc).strip()
    first_line = message.splitlines()[0].strip() if message else ""
    reason = f"{type(exc).__name__}: {first_line}" if first_line else type(exc).__name__
    reason = _CONNECTION_URI_PATTERN.sub("[REDACTED_URI]", redact_detected_secrets(reason))
    if len(reason) > _LOAD_ERROR_MAX_CHARS:
        reason = reason[: _LOAD_ERROR_MAX_CHARS - 3] + "..."
    return reason


def record_module_failure(
    module: PreparedPlatformModule,
    exc: BaseException,
    *,
    role: str = "app",
    logger: Any = None,
) -> dict[str, str]:
    """Log a prepared module's load failure and return the record admins see.

    The record's ``load_error`` is :func:`module_failure_reason`; the log line keeps
    the traceback for the operator.
    """
    load_error = module_failure_reason(exc)
    log = logger if callable(getattr(logger, "error", None)) else _logger
    log.error(
        "Platform module %s %s failed to load in role %s and was skipped; its features "
        "stay off: %s",
        module.module_id,
        module.version,
        role,
        load_error,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return {
        "id": module.module_id,
        "version": module.version,
        "status": "failed",
        "load_error": load_error,
    }


def claim_entitlement_source(
    module: PreparedPlatformModule, holder: PreparedPlatformModule | None
) -> PreparedPlatformModule | None:
    """Return the module that holds the one entitlement source slot after ``module``.

    A deployment has exactly one designated entitlement source (ADR 0082). The
    module store refuses a set in which two modules declare it; should a list still
    name two, the second one to load raises :class:`PlatformModuleConflictError`
    here, inside the loader's isolation, so it is skipped and listed as failed.
    """
    if ENTITLEMENT_SOURCE_CAPABILITY not in module.provides:
        return holder
    if holder is not None:
        raise PlatformModuleConflictError(
            f"platform module '{module.module_id}' declares '{ENTITLEMENT_SOURCE_CAPABILITY}', "
            f"but '{holder.module_id}' is already this deployment's entitlement source; "
            "exactly one module may provide it"
        )
    return module


@dataclass(frozen=True, slots=True)
class PreparedPlatformModule:
    """One verified and activated platform module."""

    module_id: str
    version: str
    import_name: str
    path: Path
    #: Capabilities the module's descriptor declares (``"provides"``), such as
    #: ``entitlement_source``; Core grants a registration point that needs one only
    #: to a module declaring it.
    provides: frozenset[str] = field(default_factory=frozenset)
    #: Ids of the modules this one declares under ``"requires"``; they load first.
    requires: tuple[str, ...] = ()
    #: Deployment-level settings the descriptor declares under ``"settings"``.
    settings: tuple[ModuleSettingDeclaration, ...] = ()

    def identity(self) -> Mapping[str, str]:
        """The read-only identity Core hands the module's entrypoints."""
        return MappingProxyType({"id": self.module_id, "version": self.version})


@dataclass(frozen=True, slots=True)
class _ModuleEntry:
    module_id: str
    version: str
    import_name: str
    relative_path: str
    python_cache_tag: str
    machine: str
    files_sha256: Mapping[str, str]
    provides: frozenset[str] = frozenset()
    requires: tuple[ModuleRequirement, ...] = ()
    settings: tuple[ModuleSettingDeclaration, ...] = ()


def platform_modules_root() -> Path:
    """Return the configured root of prepared platform modules."""
    configured = str(_runtime_env.get(PLATFORM_MODULES_ROOT_ENV) or "").strip()
    return Path(configured) if configured else DEFAULT_PLATFORM_MODULES_ROOT


def activate_prepared_platform_modules(
    role: str,
    *,
    root: Path | None = None,
    sys_path: MutableSequence[str] | None = None,
    cache_tag: str | None = None,
    machine: str | None = None,
    installed_version: Callable[[str], str | None] | None = None,
    image_checks: bool = True,
    failures: list[dict[str, str]] | None = None,
    logger: Any = None,
) -> tuple[PreparedPlatformModule, ...]:
    """Verify the prepared platform modules and append their import roots.

    Returns an empty tuple when no module list exists (plain Core). Without
    ``failures`` every other problem raises a ``PlatformModuleError`` before
    ``sys.path`` is changed: the operator CLI and the database step use this to
    refuse a module set as a whole.

    With ``failures`` (a runtime role starting, ADR 0082 §5) one module that fails
    its checks never stops the process: it is skipped before any of its paths is
    appended, its failure record (:func:`record_module_failure`) is appended to
    ``failures``, and the other modules are activated. Modules that conflict with
    each other are all skipped, so the result does not depend on the order of the
    list. A module list that cannot be read still raises: it names no module that
    could be skipped.

    ``image_checks=False`` keeps every check that depends only on the artifacts
    and the list (schema, ids, path containment, file digests, interpreter tag and
    machine against the values passed in, ``src/`` layout, ``site/`` accounting,
    cross-module names, standard library and built-in names) and skips the checks
    that depend on what this interpreter can import or has installed: names
    importable from ``sys_path`` and installed distribution versions. Activation
    in the management image uses it, because that image's packages differ from the
    role images; image conflicts are caught when the database step runs in the
    application image and when each role starts with the default.
    """
    if role not in PLATFORM_MODULE_ROLES:
        raise PlatformModuleError(
            f"unknown platform module role '{role}'; expected one of "
            f"{', '.join(sorted(PLATFORM_MODULE_ROLES))}"
        )
    modules_root = platform_modules_root() if root is None else Path(root)
    list_path = modules_root / MODULE_LIST_FILENAME
    if not list_path.exists() and not list_path.is_symlink():
        return ()

    target_path = sys.path if sys_path is None else sys_path
    expected_tag = sys.implementation.cache_tag if cache_tag is None else cache_tag
    expected_machine = platform.machine() if machine is None else machine
    lookup_version: Callable[[str], str | None] | None = None
    if image_checks:
        lookup_version = installed_version or _path_version_lookup(target_path)

    entries = _read_module_list(modules_root, list_path)
    resolved_root = modules_root.resolve(strict=True)
    if failures is None:
        ordered_entries, dependency_errors = _dependency_order(entries)
        if dependency_errors:
            raise PlatformModuleDependencyError(
                "; ".join(str(error) for error in dependency_errors.values())
            )
        entries = tuple(ordered_entries)
        staged_distributions: dict[str, tuple[str, str]] = {}
        verified: list[tuple[_ModuleEntry, Path]] = []
        for entry in entries:
            module_dir = _verify_entry(
                resolved_root, entry, expected_tag=expected_tag, expected_machine=expected_machine
            )
            _check_distributions(
                module_dir,
                entry,
                staged=staged_distributions,
                installed_version=lookup_version,
            )
            verified.append((entry, module_dir))
        # Every module's names are checked against Core's paths and against each other
        # before the first module path is appended, so the result cannot depend on the
        # order of the list.
        _check_top_level_names(verified, target_path, lookup_version)
    else:
        verified = _verify_isolating(
            entries,
            role=role,
            modules_root=modules_root,
            resolved_root=resolved_root,
            expected_tag=expected_tag,
            expected_machine=expected_machine,
            target_path=target_path,
            lookup_version=lookup_version,
            failures=failures,
            logger=logger,
        )
    prepared = [
        PreparedPlatformModule(
            module_id=entry.module_id,
            version=entry.version,
            import_name=entry.import_name,
            path=module_dir,
            provides=entry.provides,
            requires=tuple(item.module_id for item in entry.requires),
            settings=entry.settings,
        )
        for entry, module_dir in verified
    ]

    for module in prepared:
        for tree in _IMPORT_TREES:
            entry_path = str(module.path / tree)
            if entry_path not in target_path:
                target_path.append(entry_path)
    if prepared:
        if target_path is sys.path:
            # A role must never write into a verified module tree: a ``__pycache__``
            # written by the first import would be an unlisted file and the next start
            # would refuse the module. Compact's data volume is writable by its roles.
            sys.dont_write_bytecode = True
            # A role starting: the settings its modules declare become ordinary
            # process variables before the first module import reads them.
            publish_module_settings(
                _runtime_env, (setting for module in prepared for setting in module.settings)
            )
        importlib.invalidate_caches()
    return tuple(prepared)


def _verify_entry(
    resolved_root: Path, entry: _ModuleEntry, *, expected_tag: str, expected_machine: str
) -> Path:
    module_dir = _resolve_module_dir(resolved_root, entry)
    _check_compatibility(entry, expected_tag=expected_tag, expected_machine=expected_machine)
    _verify_tree(module_dir, entry)
    return module_dir


def _module_set_conflict(
    modules: Sequence[tuple[_ModuleEntry, Path]],
    target_path: MutableSequence[str],
    lookup_version: Callable[[str], str | None] | None,
) -> PlatformModuleError | None:
    """The first conflict of ``modules`` with each other or with Core's paths."""
    try:
        staged: dict[str, tuple[str, str]] = {}
        for entry, module_dir in modules:
            # The image versions were compared per module already.
            _check_distributions(module_dir, entry, staged=staged, installed_version=None)
        _check_top_level_names(modules, target_path, lookup_version)
    except PlatformModuleError as exc:
        return exc
    return None


def _verify_isolating(
    entries: Sequence[_ModuleEntry],
    *,
    role: str,
    modules_root: Path,
    resolved_root: Path,
    expected_tag: str,
    expected_machine: str,
    target_path: MutableSequence[str],
    lookup_version: Callable[[str], str | None] | None,
    failures: list[dict[str, str]],
    logger: Any,
) -> list[tuple[_ModuleEntry, Path]]:
    """Verify each module on its own and skip the ones that fail (ADR 0082 §5)."""
    rejected: dict[str, PlatformModuleError] = {}
    candidates: list[tuple[_ModuleEntry, Path]] = []
    for entry in entries:
        try:
            module_dir = _verify_entry(
                resolved_root, entry, expected_tag=expected_tag, expected_machine=expected_machine
            )
            _check_distributions(module_dir, entry, staged={}, installed_version=lookup_version)
            _module_top_level_names(module_dir, entry)
        except PlatformModuleError as exc:
            rejected[entry.module_id] = exc
            continue
        candidates.append((entry, module_dir))

    accepted = candidates
    if _module_set_conflict(candidates, target_path, lookup_version) is not None:
        # Find who conflicts: alone (with Core), then pairwise. Conflicts are between
        # two parties, so a set without a conflicting pair has no conflict at all.
        for candidate in candidates:
            error = _module_set_conflict([candidate], target_path, lookup_version)
            if error is not None:
                rejected[candidate[0].module_id] = error
        remaining = [item for item in candidates if item[0].module_id not in rejected]
        conflicting: dict[str, PlatformModuleError] = {}
        for first, second in combinations(remaining, 2):
            error = _module_set_conflict([first, second], target_path, lookup_version)
            if error is not None:
                conflicting.setdefault(first[0].module_id, error)
                conflicting.setdefault(second[0].module_id, error)
        rejected.update(conflicting)
        accepted = [item for item in remaining if item[0].module_id not in conflicting]
        final = _module_set_conflict(accepted, target_path, lookup_version)
        if final is not None:
            raise final

    # A module whose required module was skipped, is missing or too old, or that is
    # in a dependency cycle is skipped too; the rest load in dependency order.
    module_dirs = {entry.module_id: module_dir for entry, module_dir in accepted}
    ordered_entries, dependency_errors = _dependency_order(
        [entry for entry, _module_dir in accepted], failed=sorted(rejected)
    )
    rejected.update(dependency_errors)
    accepted = [(entry, module_dirs[entry.module_id]) for entry in ordered_entries]

    for entry in entries:
        error = rejected.get(entry.module_id)
        if error is None:
            continue
        skipped = PreparedPlatformModule(
            module_id=entry.module_id,
            version=entry.version,
            import_name=entry.import_name,
            path=modules_root / entry.relative_path,
        )
        failures.append(record_module_failure(skipped, error, role=role, logger=logger))
    return accepted


def register_platform_modules_for_role(
    role: str,
    *,
    config: Mapping[str, Any],
    root: Path | None = None,
    logger: logging.Logger | None = None,
    import_module: Callable[[str], Any] = importlib.import_module,
    services: Mapping[str, Any] | None = None,
    failures: list[dict[str, str]] | None = None,
    installation_id: str | None = None,
) -> tuple[PreparedPlatformModule, ...]:
    """Activate prepared modules for a background role and call its hooks.

    A module without the role hook is valid and skipped. A module that fails its
    activation checks is skipped before it is imported (see
    :func:`activate_prepared_platform_modules` with ``failures``). A module that fails to
    load (its import fails, it comes from outside its own ``src/``, its hook is not
    callable or raises) is skipped too and the role keeps running: what its hook
    registered into the role's ``services`` and into Core's process-wide registries
    is rolled back (:class:`ModuleRegistrationRollback`, which also lists what it
    cannot undo) and the error is logged with its id and version. Each hook receives a read-only context with ``role``, ``config`` and
    ``platform_module``, the ``{"id", "version"}`` identity under which Core loaded
    that module, and ``installation_id``, the installation id Core resolved for this
    deployment (``None`` when it has none), plus the role's ``services``: in the
    worker and control roles ``entitlement_service`` (the role's own decider, which
    only the module declaring ``entitlement_source`` registers into) and ``db``, a
    factory for the role's database connection store. ``services`` cannot replace
    the four fixed keys. After a failure every service
    that offers ``mark_loading_degraded`` is marked (fail closed: the role's
    entitlement service then answers "unknown" rather than a definite "not
    entitled", see :meth:`ModuleRegistrationRollback.mark_degraded`).

    Returns the modules that loaded; a failed module is not among them. When
    ``failures`` is given, the record of each failed module is appended to it.
    """
    entrypoint = ROLE_ENTRYPOINTS.get(role)
    if entrypoint is None:
        raise PlatformModuleError(f"role '{role}' has no platform module registration hook")
    log = logger or _logger
    activation_failures: list[dict[str, str]] = []
    modules = activate_prepared_platform_modules(
        role, root=root, failures=activation_failures, logger=log
    )
    if activation_failures:
        mark_failed_modules_degraded(activation_failures, services=services)
        if failures is not None:
            failures.extend(activation_failures)
    loaded: list[PreparedPlatformModule] = []
    failed_ids: set[str] = set()
    source_holder: PreparedPlatformModule | None = None
    for module in modules:
        rollback = ModuleRegistrationRollback.capture(services=services)
        try:
            check_required_modules_loaded(module, failed_ids)
            holder = claim_entitlement_source(module, source_holder)
            with rollback.loading_scope(module):
                _call_role_hook(
                    module,
                    role=role,
                    entrypoint=entrypoint,
                    config=config,
                    services=services,
                    import_module=import_module,
                    log=log,
                    installation_id=installation_id,
                )
        except Exception as exc:
            rollback.restore()
            rollback.mark_degraded(module)
            failed_ids.add(module.module_id)
            record = record_module_failure(module, exc, role=role, logger=log)
            if failures is not None:
                failures.append(record)
            continue
        source_holder = holder
        loaded.append(module)
    if loaded:
        log.info(
            "Platform modules active for role %s: %s",
            role,
            describe_platform_modules(tuple(loaded)),
        )
    return tuple(loaded)


def bind_platform_module_services(
    services: Mapping[str, Any] | None, module_id: str, requires: Iterable[str] = ()
) -> dict[str, Any]:
    """``services`` as one module sees them: identity-bound ports bound to that module.

    A service value that offers ``for_platform_module(module_id, requires)`` (Core's
    module secret codec, ADR 0082 §12) is replaced by what that returns; every
    other value is handed over unchanged. ``requires`` are the ids the module
    declares under ``"requires"``.
    """
    bound: dict[str, Any] = {}
    wanted = tuple(str(item) for item in requires)
    for key, value in dict(services or {}).items():
        binder = getattr(value, "for_platform_module", None)
        bound[key] = (
            binder(module_id, wanted) if callable(binder) and not isinstance(value, type) else value
        )
    return bound


def _call_role_hook(
    module: PreparedPlatformModule,
    *,
    role: str,
    entrypoint: str,
    config: Mapping[str, Any],
    services: Mapping[str, Any] | None,
    import_module: Callable[[str], Any],
    log: logging.Logger,
    installation_id: str | None = None,
) -> None:
    imported = import_prepared_platform_module(module, import_module=import_module)
    hook = getattr(imported, entrypoint, None)
    if hook is None:
        log.debug(
            "Platform module '%s' has no '%s' entrypoint; skipped",
            module.module_id,
            entrypoint,
        )
        return
    if not callable(hook):
        raise PlatformModuleImportError(
            f"platform module '{module.module_id}' entrypoint '{entrypoint}' is not callable"
        )
    context: Mapping[str, Any] = MappingProxyType(
        {
            **bind_platform_module_services(services, module.module_id, module.requires),
            "role": role,
            "config": config,
            "platform_module": module.identity(),
            "installation_id": installation_id,
        }
    )
    hook(context)


def import_prepared_platform_module(
    module: PreparedPlatformModule,
    *,
    import_module: Callable[[str], Any] = importlib.import_module,
) -> Any:
    """Import a prepared module; any failure raises ``PlatformModuleImportError`` naming it."""
    try:
        imported = import_module(module.import_name)
    except Exception as exc:
        raise PlatformModuleImportError(
            f"platform module '{module.module_id}' {module.version} failed to import "
            f"'{module.import_name}': {type(exc).__name__}: {exc}"
        ) from exc
    verify_imported_module_origin(module, imported)
    return imported


def verify_imported_module_origin(module: PreparedPlatformModule, imported: Any) -> None:
    """Refuse an imported object that does not come from the module's own ``src/``.

    The name checks at activation keep import names unique; this catches what they
    cannot see, such as a name already in ``sys.modules``. The import package must
    be a regular package or a single-file module: a namespace package has no
    ``__file__`` and is refused. For a dotted import name the top-level package is
    checked as well when it is in ``sys.modules``.
    """
    own_src = (module.path / "src").resolve()
    candidates = [(module.import_name, imported)]
    top_level = module.import_name.split(".", 1)[0]
    if top_level != module.import_name and top_level in sys.modules:
        candidates.append((top_level, sys.modules[top_level]))
    for name, candidate in candidates:
        origin = getattr(candidate, "__file__", None)
        if not isinstance(origin, str) or not origin:
            raise PlatformModuleImportError(
                f"platform module '{module.module_id}' import '{name}' has no __file__; "
                "a platform module import package must be a regular package or module"
            )
        if not Path(origin).resolve().is_relative_to(own_src):
            raise PlatformModuleImportError(
                f"platform module '{module.module_id}' import '{name}' was loaded from "
                f"{origin}, outside the module's own {own_src}"
            )


def describe_platform_modules(modules: tuple[PreparedPlatformModule, ...]) -> str:
    """Return a compact ``id@version`` list for log lines."""
    return ", ".join(f"{module.module_id}@{module.version}" for module in modules) or "none"


def _read_module_list(root: Path, list_path: Path) -> tuple[_ModuleEntry, ...]:
    if list_path.is_symlink() or not list_path.is_file():
        raise PlatformModuleListError(f"{list_path} must be a regular file")
    try:
        payload = json.loads(list_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlatformModuleListError(f"{list_path} cannot be read as JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise PlatformModuleListError(f"{list_path} must contain a JSON object")
    if payload.get("schema_version") != MODULE_LIST_SCHEMA_VERSION:
        raise PlatformModuleListError(
            f"{list_path} has schema_version {payload.get('schema_version')!r}; "
            f"expected {MODULE_LIST_SCHEMA_VERSION}"
        )
    raw_modules = payload.get("modules")
    if not isinstance(raw_modules, list):
        raise PlatformModuleListError(f"{list_path} must contain a 'modules' list")

    entries: list[_ModuleEntry] = []
    seen_ids: set[str] = set()
    seen_imports: set[str] = set()
    seen_paths: set[str] = set()
    for index, raw in enumerate(raw_modules):
        entry = _parse_entry(raw, where=f"{list_path} modules[{index}]")
        top_level = entry.import_name.split(".", 1)[0]
        if entry.module_id in seen_ids:
            raise PlatformModuleListError(f"{list_path} lists module '{entry.module_id}' twice")
        if top_level in seen_imports:
            raise PlatformModuleListError(
                f"{list_path} lists import package '{top_level}' for more than one module"
            )
        if entry.relative_path in seen_paths:
            raise PlatformModuleListError(
                f"{list_path} uses path '{entry.relative_path}' for more than one module"
            )
        seen_ids.add(entry.module_id)
        seen_imports.add(top_level)
        seen_paths.add(entry.relative_path)
        entries.append(entry)
    conflicts = conflicting_module_settings(
        setting for entry in entries for setting in entry.settings
    )
    if conflicts:
        raise PlatformModuleListError(
            f"{list_path} declares module settings in more than one module: "
            + "; ".join(f"{name} ({', '.join(ids)})" for name, ids in conflicts.items())
        )
    overlaps = module_set_setting_conflicts(
        (entry.module_id for entry in entries),
        (setting for entry in entries for setting in entry.settings),
    )
    if overlaps:
        raise PlatformModuleListError(
            f"{list_path} lists modules whose settings overlap: " + "; ".join(overlaps)
        )
    return tuple(entries)


def _parse_entry(raw: object, *, where: str) -> _ModuleEntry:
    if not isinstance(raw, dict):
        raise PlatformModuleListError(f"{where} must be an object")
    missing = sorted(_REQUIRED_ENTRY_KEYS - set(raw))
    unknown = sorted(set(raw) - _REQUIRED_ENTRY_KEYS - OPTIONAL_ENTRY_KEYS)
    if missing or unknown:
        raise PlatformModuleListError(
            f"{where} has missing keys {missing} and unknown keys {unknown}"
        )
    text_fields = ("id", "version", "import", "path", "python_cache_tag", "machine")
    for key in text_fields:
        value = raw[key]
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise PlatformModuleListError(f"{where} field '{key}' must be a non-empty string")
    # Platform modules share the ADR 0013 package identity grammar with extensions.
    if not CANONICAL_EXTENSION_ID_RE.fullmatch(raw["id"]):
        raise PlatformModuleListError(
            f"{where} id '{raw['id']}' is not a canonical lowercase dot-separated "
            "package id (ADR 0013)"
        )
    if not _IMPORT_NAME_PATTERN.match(raw["import"]):
        raise PlatformModuleListError(
            f"{where} import '{raw['import']}' is not a dotted Python module name"
        )
    relative_path = _safe_relative_path(raw["path"], where=f"{where} path")
    files = raw["files_sha256"]
    if not isinstance(files, dict) or not files:
        raise PlatformModuleListError(f"{where} field 'files_sha256' must be a non-empty object")
    digests: dict[str, str] = {}
    for file_path, digest in files.items():
        normalized = _safe_relative_path(file_path, where=f"{where} file '{file_path}'")
        if PurePosixPath(normalized).parts[0] not in _VERIFIED_TREES:
            raise PlatformModuleListError(
                f"{where} file '{file_path}' must live under {', '.join(_VERIFIED_TREES)}/"
            )
        if not isinstance(digest, str) or not _SHA256_PATTERN.match(digest):
            raise PlatformModuleListError(
                f"{where} file '{file_path}' must have a lowercase hex sha256 digest"
            )
        if normalized in digests:
            raise PlatformModuleListError(f"{where} lists file '{normalized}' twice")
        digests[normalized] = digest
    return _ModuleEntry(
        module_id=raw["id"],
        version=raw["version"],
        import_name=raw["import"],
        relative_path=relative_path,
        python_cache_tag=raw["python_cache_tag"],
        machine=raw["machine"],
        files_sha256=MappingProxyType(digests),
        provides=parse_module_capabilities(raw.get("provides", []), where=f"{where} provides"),
        requires=_parse_requires(raw, where=where),
        settings=_parse_settings(raw, where=where),
    )


def _parse_settings(raw: Mapping[str, Any], *, where: str) -> tuple[ModuleSettingDeclaration, ...]:
    try:
        return parse_module_settings(
            raw.get("settings", []), module_id=raw["id"], where=f"{where} settings"
        )
    except ValueError as exc:
        raise PlatformModuleListError(str(exc)) from exc


def _parse_requires(raw: Mapping[str, Any], *, where: str) -> tuple[ModuleRequirement, ...]:
    try:
        return parse_module_requirements(raw.get("requires", []), module_id=raw["id"])
    except ValueError as exc:
        raise PlatformModuleListError(f"{where} requires {exc}") from exc


def _dependency_order(
    entries: Sequence[_ModuleEntry], *, failed: Sequence[str] = ()
) -> tuple[list[_ModuleEntry], dict[str, PlatformModuleDependencyError]]:
    """``entries`` in dependency order, and an error for each module that cannot load."""
    resolution = resolve_module_dependencies(
        {entry.module_id: (entry.version, entry.requires) for entry in entries},
        failed=failed,
    )
    by_id = {entry.module_id: entry for entry in entries}
    errors = {
        module_id: PlatformModuleDependencyError(
            f"platform module '{module_id}' {by_id[module_id].version} {reason}"
        )
        for module_id, reason in resolution.problems.items()
    }
    return [by_id[module_id] for module_id in resolution.order], errors


def check_required_modules_loaded(
    module: PreparedPlatformModule, failed_ids: Iterable[str]
) -> None:
    """Refuse to load ``module`` when a module it requires failed to load in this role.

    Raised inside the role's isolation, so the module is skipped and listed as
    failed with the reason ``requires <id>``.
    """
    failed = set(failed_ids)
    for required_id in module.requires:
        if required_id in failed:
            raise PlatformModuleDependencyError(
                f"platform module '{module.module_id}' {module.version} requires "
                f"{required_id} (which failed to load)"
            )


def parse_module_capabilities(value: object, *, where: str) -> frozenset[str]:
    """Read a ``"provides"`` list: distinct names from :data:`PLATFORM_MODULE_CAPABILITIES`."""
    if not isinstance(value, list):
        raise PlatformModuleListError(f"{where} must be a list of capability names")
    names: set[str] = set()
    for item in value:
        if not isinstance(item, str) or item not in PLATFORM_MODULE_CAPABILITIES:
            raise PlatformModuleListError(
                f"{where} names {item!r}; known capabilities are "
                f"{', '.join(sorted(PLATFORM_MODULE_CAPABILITIES))}"
            )
        if item in names:
            raise PlatformModuleListError(f"{where} names {item!r} twice")
        names.add(item)
    return frozenset(names)


def _safe_relative_path(value: object, *, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise PlatformModuleListError(f"{where} must be a non-empty relative path")
    if "\\" in value or "\x00" in value:
        raise PlatformModuleListError(f"{where} must use forward slashes only")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise PlatformModuleListError(f"{where} must be a normalized relative path inside the root")
    return candidate.as_posix()


def _resolve_module_dir(resolved_root: Path, entry: _ModuleEntry) -> Path:
    candidate = resolved_root / entry.relative_path
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PlatformModuleIntegrityError(
            f"platform module '{entry.module_id}' path '{entry.relative_path}' does not exist"
        ) from exc
    if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
        raise PlatformModuleIntegrityError(
            f"platform module '{entry.module_id}' path '{entry.relative_path}' "
            f"escapes the platform module root {resolved_root}"
        )
    if not resolved.is_dir():
        raise PlatformModuleIntegrityError(
            f"platform module '{entry.module_id}' path '{entry.relative_path}' is not a directory"
        )
    return resolved


def _check_compatibility(entry: _ModuleEntry, *, expected_tag: str, expected_machine: str) -> None:
    if entry.python_cache_tag != expected_tag:
        raise PlatformModuleCompatibilityError(
            f"platform module '{entry.module_id}' {entry.version} was prepared for "
            f"{entry.python_cache_tag}; this interpreter is {expected_tag}"
        )
    if entry.machine != expected_machine:
        raise PlatformModuleCompatibilityError(
            f"platform module '{entry.module_id}' {entry.version} was prepared for machine "
            f"{entry.machine}; this machine is {expected_machine}"
        )


def _verify_tree(module_dir: Path, entry: _ModuleEntry) -> None:
    present: set[str] = set()
    for tree in _VERIFIED_TREES:
        tree_root = module_dir / tree
        if tree_root.is_symlink():
            raise PlatformModuleIntegrityError(
                f"platform module '{entry.module_id}' {tree}/ must not be a symlink"
            )
        if not tree_root.exists():
            continue
        if not tree_root.is_dir():
            raise PlatformModuleIntegrityError(
                f"platform module '{entry.module_id}' {tree}/ is not a directory"
            )
        present.update(_walk_regular_files(module_dir, tree_root, entry))

    listed = set(entry.files_sha256)
    unlisted = sorted(present - listed)
    if unlisted:
        raise PlatformModuleIntegrityError(
            f"platform module '{entry.module_id}' contains files not in the module list: "
            f"{', '.join(unlisted[:5])}"
        )
    missing = sorted(listed - present)
    if missing:
        raise PlatformModuleIntegrityError(
            f"platform module '{entry.module_id}' is missing listed files: {', '.join(missing[:5])}"
        )
    for relative, expected in sorted(entry.files_sha256.items()):
        actual = _sha256_file(module_dir / relative)
        if actual != expected:
            raise PlatformModuleIntegrityError(
                f"platform module '{entry.module_id}' file '{relative}' has sha256 {actual}; "
                f"the module list expects {expected}"
            )


def _walk_regular_files(module_dir: Path, tree_root: Path, entry: _ModuleEntry) -> set[str]:
    files: set[str] = set()
    for current, dirnames, filenames in os.walk(tree_root, followlinks=False):
        current_path = Path(current)
        for name in (*dirnames, *filenames):
            path = current_path / name
            relative = path.relative_to(module_dir).as_posix()
            if path.is_symlink():
                raise PlatformModuleIntegrityError(
                    f"platform module '{entry.module_id}' contains a symlink: {relative}"
                )
            if name.endswith(".pth"):
                raise PlatformModuleIntegrityError(
                    f"platform module '{entry.module_id}' contains a .pth file: {relative}"
                )
        for name in filenames:
            path = current_path / name
            if not path.is_file():
                raise PlatformModuleIntegrityError(
                    f"platform module '{entry.module_id}' contains a non-regular file: "
                    f"{path.relative_to(module_dir).as_posix()}"
                )
            files.add(path.relative_to(module_dir).as_posix())
    return files


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _path_version_lookup(search_path: MutableSequence[str]) -> Callable[[str], str | None]:
    def lookup(name: str) -> str | None:
        for distribution in importlib.metadata.distributions(name=name, path=list(search_path)):
            return distribution.version
        return None

    return lookup


def _check_distributions(
    module_dir: Path,
    entry: _ModuleEntry,
    *,
    staged: dict[str, tuple[str, str]],
    installed_version: Callable[[str], str | None] | None,
) -> None:
    """Check vendored metadata; ``installed_version=None`` skips the image comparison."""
    site_root = module_dir / "site"
    if not site_root.is_dir():
        return
    for dist_info in sorted(site_root.glob("*.dist-info")):
        metadata = importlib.metadata.PathDistribution(dist_info).metadata
        name = metadata.get("Name") if metadata else None
        version = metadata.get("Version") if metadata else None
        if not name or not version:
            raise PlatformModuleIntegrityError(
                f"platform module '{entry.module_id}' has unreadable metadata in "
                f"site/{dist_info.name}"
            )
        normalized = _normalize_distribution_name(name)
        staged_version = staged.get(normalized)
        if staged_version is not None and staged_version[0] != version:
            raise PlatformModuleConflictError(
                f"platform module '{entry.module_id}' ships {name} {version}, but platform "
                f"module '{staged_version[1]}' ships {name} {staged_version[0]}"
            )
        present = installed_version(name) if installed_version is not None else None
        if present is not None and present != version:
            raise PlatformModuleConflictError(
                f"platform module '{entry.module_id}' ships {name} {version}, but {name} "
                f"{present} is already importable"
            )
        staged.setdefault(normalized, (version, entry.module_id))


_EXTENSION_MODULE_SUFFIXES = (".so", ".pyd")
_SOURCE_MODULE_SUFFIXES = (".py", ".pyc", ".pyw")
_METADATA_DIR_SUFFIXES = (".dist-info", ".egg-info", ".data")
_CACHE_DIR = "__pycache__"


def _importable_file_name(filename: str) -> str | None:
    """The top-level import name a file at the root of an import tree provides."""
    if filename.endswith(_EXTENSION_MODULE_SUFFIXES):
        # ``name.cpython-312-x86_64-linux-gnu.so``, ``name.abi3.so``, ``name.so``.
        stem = filename.split(".", 1)[0]
    else:
        suffix = next((s for s in _SOURCE_MODULE_SUFFIXES if filename.endswith(s)), None)
        if suffix is None:
            return None
        stem = filename[: -len(suffix)]
    return stem if stem.isidentifier() else None


def _importable_dir_name(dirname: str) -> str | None:
    if dirname == _CACHE_DIR or dirname.endswith(_METADATA_DIR_SUFFIXES):
        return None
    return dirname if dirname.isidentifier() else None


def _tree_entries(tree_root: Path) -> list[tuple[str, str | None]]:
    """``(entry name, top-level import name or None)`` for the root of one import tree."""
    if not tree_root.is_dir():
        return []
    entries: list[tuple[str, str | None]] = []
    for child in sorted(tree_root.iterdir()):
        if child.is_dir():
            entries.append((child.name, _importable_dir_name(child.name)))
        else:
            entries.append((child.name, _importable_file_name(child.name)))
    return entries


def _distribution_top_level_names(dist_info: Path, entry: _ModuleEntry) -> set[str]:
    """The top-level names a vendored distribution declares.

    ``top_level.txt`` when present, else the first path component of every
    ``RECORD`` row. A distribution with neither cannot be accounted for.
    """
    top_level = dist_info / "top_level.txt"
    if top_level.is_file():
        names: set[str] = set()
        for line in top_level.read_text(encoding="utf-8").splitlines():
            raw = line.strip().replace("\\", "/")
            if not raw:
                continue
            name = raw.split("/", 1)[0].split(".", 1)[0]
            if name.isidentifier():
                names.add(name)
        return names
    record = dist_info / "RECORD"
    if record.is_file():
        names = set()
        rows = csv.reader(record.read_text(encoding="utf-8").splitlines())
        for row in rows:
            if not row or not row[0]:
                continue
            parts = PurePosixPath(row[0].replace("\\", "/")).parts
            if not parts or parts[0] in {".", ".."} or parts[0].endswith(_METADATA_DIR_SUFFIXES):
                continue
            name = (
                _importable_dir_name(parts[0])
                if len(parts) > 1
                else _importable_file_name(parts[0])
            )
            if name:
                names.add(name)
        return names
    raise PlatformModuleIntegrityError(
        f"platform module '{entry.module_id}' vendors site/{dist_info.name} without "
        "top_level.txt or RECORD, so its import names cannot be accounted for"
    )


@dataclass(frozen=True, slots=True)
class _BundledDistribution:
    """A third-party distribution a module vendors in its ``site/``."""

    name: str
    version: str
    #: ``site/``-relative path -> listed sha256 of every file the distribution
    #: owns: its ``*.dist-info`` and the top-level names it declares.
    files: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _ModuleNames:
    entry: _ModuleEntry
    module_dir: Path
    own_name: str
    #: Top-level name in ``site/`` -> the vendored distributions declaring it.
    site: Mapping[str, tuple[_BundledDistribution, ...]]


def _distribution_files(entry: _ModuleEntry, dist_info: Path, names: set[str]) -> Mapping[str, str]:
    files: dict[str, str] = {}
    for relative, digest in entry.files_sha256.items():
        parts = PurePosixPath(relative).parts
        if len(parts) < 2 or parts[0] != "site":
            continue
        top = parts[1]
        provided = _importable_dir_name(top) if len(parts) > 2 else _importable_file_name(top)
        if top == dist_info.name or (provided is not None and provided in names):
            files["/".join(parts[1:])] = digest
    return MappingProxyType(files)


def _module_top_level_names(module_dir: Path, entry: _ModuleEntry) -> _ModuleNames:
    """Every top-level name the module's ``src/`` and ``site/`` make importable.

    Refuses anything in ``src/`` other than the declared import package and any
    name in ``site/`` that no ``*.dist-info`` in that ``site/`` declares.
    """
    own_name = entry.import_name.split(".", 1)[0]
    for entry_name, import_name in _tree_entries(module_dir / "src"):
        if import_name != own_name:
            raise PlatformModuleIntegrityError(
                f"platform module '{entry.module_id}' src/ may contain only its import "
                f"package '{own_name}', but contains '{entry_name}'"
            )

    site_root = module_dir / "site"
    site_names = {name for _entry, name in _tree_entries(site_root) if name}
    declared: dict[str, list[_BundledDistribution]] = {}
    if site_root.is_dir():
        for dist_info in sorted(site_root.glob("*.dist-info")):
            names = _distribution_top_level_names(dist_info, entry)
            metadata = importlib.metadata.PathDistribution(dist_info).metadata
            distribution = _BundledDistribution(
                name=str(metadata.get("Name") or ""),
                version=str(metadata.get("Version") or ""),
                files=_distribution_files(entry, dist_info, names),
            )
            for name in names:
                declared.setdefault(name, []).append(distribution)
    unaccounted = sorted(site_names - set(declared))
    if unaccounted:
        raise PlatformModuleIntegrityError(
            f"platform module '{entry.module_id}' site/ contains {', '.join(unaccounted)} "
            "that no vendored *.dist-info in that site/ declares"
        )
    if own_name in site_names:
        raise PlatformModuleIntegrityError(
            f"platform module '{entry.module_id}' site/ ships its own import package '{own_name}'"
        )
    return _ModuleNames(
        entry=entry,
        module_dir=module_dir,
        own_name=own_name,
        site=MappingProxyType({name: tuple(declared[name]) for name in sorted(site_names)}),
    )


def _bundle_signature(distributions: Sequence[_BundledDistribution]) -> tuple[Any, ...]:
    return tuple(
        sorted(
            (
                _normalize_distribution_name(dist.name),
                dist.version,
                tuple(sorted(dist.files.items())),
            )
            for dist in distributions
        )
    )


def _check_top_level_names(
    verified: Sequence[tuple[_ModuleEntry, Path]],
    search_path: MutableSequence[str],
    installed_version: Callable[[str], str | None] | None,
) -> None:
    """Refuse a module set whose import names could resolve to the wrong code.

    ``installed_version=None`` (``image_checks=False``) skips the comparison with
    what ``search_path`` makes importable; every other rule still applies.

    A module's own import package is unique across modules and never importable
    from Core's paths. A vendored distribution may appear in several modules only
    when every copy has the same version and the same listed file digests; the
    first module's copy then wins by ``sys.path`` order, which is harmless because
    the copies are identical. It may also already be importable from Core's paths
    when the image has that distribution at exactly the same version (Scalable roles
    run images with different locks); Core's copy then wins because module paths
    are appended after Core's.
    """
    modules = [_module_top_level_names(module_dir, entry) for entry, module_dir in verified]
    owners: dict[str, tuple[str, tuple[Any, ...] | None]] = {}
    providers: dict[str, list[_ModuleNames]] = {}
    for module in modules:
        claims: list[tuple[str, tuple[Any, ...] | None]] = [(module.own_name, None)]
        claims += [(name, _bundle_signature(dists)) for name, dists in module.site.items()]
        for name, signature in claims:
            providers.setdefault(name, []).append(module)
            previous = owners.setdefault(name, (module.entry.module_id, signature))
            if previous[0] == module.entry.module_id:
                continue
            if signature is None or previous[1] is None:
                raise PlatformModuleConflictError(
                    f"platform modules '{previous[0]}' and '{module.entry.module_id}' both "
                    f"provide the top-level import name '{name}'"
                )
            if signature != previous[1]:
                raise PlatformModuleConflictError(
                    f"platform modules '{previous[0]}' and '{module.entry.module_id}' both "
                    f"vendor '{name}' but with different distribution versions or contents"
                )
    for module in modules:
        _check_name_is_free(module, module.own_name, search_path, providers, installed_version)
        for name in module.site:
            _check_name_is_free(module, name, search_path, providers, installed_version)


def _installed_top_level_names(
    distribution: str,
    search_path: Sequence[str],
    *,
    packages_distributions: Callable[[], Mapping[str, Sequence[str]]] = (
        importlib.metadata.packages_distributions
    ),
) -> set[str]:
    """The top-level names an installed distribution provides.

    ``top_level.txt`` first, then the first path component of its ``RECORD``
    files. OS-packaged distributions often carry neither; then the interpreter's
    ``packages_distributions()`` mapping is asked. An empty set means the name
    cannot be attributed to the distribution.
    """
    for installed in importlib.metadata.distributions(name=distribution, path=list(search_path)):
        top_level = installed.read_text("top_level.txt")
        if top_level:
            return {
                line.strip().split("/", 1)[0].split(".", 1)[0]
                for line in top_level.splitlines()
                if line.strip()
            }
        names: set[str] = set()
        for file in installed.files or ():
            parts = PurePosixPath(str(file)).parts
            if not parts or parts[0] in {".", ".."}:
                continue
            name = (
                _importable_dir_name(parts[0])
                if len(parts) > 1
                else _importable_file_name(parts[0])
            )
            if name:
                names.add(name)
        if names:
            return names
        break
    wanted = _normalize_distribution_name(distribution)
    try:
        mapping = packages_distributions()
    except Exception:
        return set()
    return {
        name
        for name, owners in mapping.items()
        if any(_normalize_distribution_name(owner) == wanted for owner in owners)
    }


def _check_name_is_free(
    module: _ModuleNames,
    name: str,
    search_path: MutableSequence[str],
    providers: Mapping[str, Sequence[_ModuleNames]],
    installed_version: Callable[[str], str | None] | None,
) -> None:
    module_id = module.entry.module_id
    if name in sys.builtin_module_names or name in sys.stdlib_module_names:
        raise PlatformModuleConflictError(
            f"platform module '{module_id}' import name '{name}' is a standard library or "
            "built-in module"
        )
    if installed_version is None:
        return
    spec = importlib.machinery.PathFinder.find_spec(name, list(search_path))
    if spec is None:
        return
    locations = [
        location
        for location in (*(spec.submodule_search_locations or ()), spec.origin)
        if isinstance(location, str) and location
    ]
    # A second activation in the same process finds the trees of the modules that
    # provide this name (its own, or identical copies of a shared distribution).
    own_roots = [
        provider.module_dir / tree
        for provider in providers.get(name, (module,))
        for tree in (("src",) if name == provider.own_name else ("site",))
    ]
    if locations and all(
        any(Path(location).is_relative_to(root) for root in own_roots) for location in locations
    ):
        return
    where = locations[0] if locations else spec.name
    distributions = module.site.get(name) if name != module.own_name else None
    if not distributions:
        raise PlatformModuleConflictError(
            f"platform module '{module_id}' import name '{name}' is already importable from {where}"
        )
    # A vendored name may already be importable only from the very same
    # distribution at the very same version; Core's copy then wins on sys.path.
    for dist in distributions:
        present = installed_version(dist.name)
        if present is not None and present != dist.version:
            raise PlatformModuleConflictError(
                f"platform module '{module_id}' ships {dist.name} {dist.version}, but "
                f"{dist.name} {present} is already importable"
            )
        if present is None or name not in _installed_top_level_names(dist.name, search_path):
            raise PlatformModuleConflictError(
                f"platform module '{module_id}' import name '{name}' is already importable "
                f"from {where}, which is not {dist.name} {dist.version} installed in this image"
            )
