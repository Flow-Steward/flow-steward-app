"""Roll back what a platform module registered when its loading fails (ADR 0082).

:class:`ModuleRegistrationRollback` captures the application, container, role
service and process-wide Core registry state a module can add to just before the
module is imported, and puts it back when the module fails.
:func:`mark_failed_modules_degraded` marks Core's services degraded for modules
refused before import. :mod:`core.infrastructure.platform_modules.loader` uses
both and re-exports them.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Any

from core.infrastructure.platform_modules.errors import PlatformModuleError

if TYPE_CHECKING:
    from core.infrastructure.platform_modules.loader import PreparedPlatformModule


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
    "core.application.auth.invitation_policies",
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
