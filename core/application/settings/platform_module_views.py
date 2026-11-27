"""The container view each loaded platform module registers against (ADR 0082).

Core's module loader imports a module and calls its ``register`` entrypoint; this
policy decides what that call sees. A prepared platform module receives a view
bound to the identity under which Core loaded it: ``metadata["platform_module"]``,
a route rule registrar confined to its own API tree, and ``external_sign_in`` whose
secret codec is bound to the module (ADR 0082 §12: it decrypts only values the
module, or a module it requires, encrypted); the pages it registers are stamped
with its id.

A module listed by ``FS_OPTIONAL_PRIVATE_BOOTSTRAP_MODULES`` is the pre-existing
path that platform modules replace; it keeps its previous behaviour and registers
against Core's own container. It has no verified identity, so only two things
differ: ``registries["module_route_permissions"]`` is absent while it registers,
and it cannot register a host-rendered page without a compiled component. The
page seal still applies after loading.

Platform modules are trusted in-process code. Their view is an interface
contract, not isolation: a module receives ``app``, the inner objects of the view
are Core's own objects, and after registration ``app.state.runtime_container`` is
Core's container. What the view does enforce is that a platform module may add
top-level keys to ``registries``, ``service_factories``, ``auth_helpers`` and
``metadata`` but never replace or delete a key that existed before it ran.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, ClassVar, Protocol, TypeVar

from core.application.auth.route_permissions import (
    ModuleRoutePermissionRegistry,
    ModuleRouteRuleRegistrar,
)
from core.application.reports.ui_page_registry import page_registration_scope

#: Registry key of the route permission registration point (ADR 0082, section 2).
MODULE_ROUTE_PERMISSIONS_KEY = "module_route_permissions"
#: Metadata key of the identity under which Core loaded a platform module.
PLATFORM_MODULE_METADATA_KEY = "platform_module"
#: Metadata key of what each loaded platform module registered, for diagnostics.
PLATFORM_MODULE_REGISTRATIONS_KEY = "platform_module_registrations"
#: The container dictionaries a module's view copies at the top level.
_VIEW_DICTS = ("registries", "service_factories", "auth_helpers", "metadata")
#: Per dictionary, the identity-bound key that differs in every view and never
#: flows back into Core's container.
_BOUND_KEYS = {
    "registries": MODULE_ROUTE_PERMISSIONS_KEY,
    "metadata": PLATFORM_MODULE_METADATA_KEY,
}


class ModuleContainerViolationError(RuntimeError):
    """A module replaced or removed a top-level container entry it did not add."""


class LoadedModule(Protocol):
    """A prepared platform module as the loader identifies it."""

    @property
    def module_id(self) -> str: ...

    @property
    def version(self) -> str: ...

    def identity(self) -> Mapping[str, str]: ...


class ModuleContainer(Protocol):
    """The dataclass runtime container shared by Core and loaded modules."""

    __dataclass_fields__: ClassVar[dict[str, Any]]
    auth_helpers: dict[str, Any]
    service_factories: dict[str, Any]
    registries: dict[str, Any]
    metadata: dict[str, Any]


ContainerT = TypeVar("ContainerT", bound=ModuleContainer)


def _module_container(
    container: ContainerT,
    *,
    identity: Mapping[str, str] | None,
    route_registrar: ModuleRouteRuleRegistrar | None,
    requires: tuple[str, ...] = (),
) -> ContainerT:
    """The container one module's ``register`` receives.

    Top-level copies of the four container dictionaries; the values inside them
    (helpers, factories and every registry object, including the page and
    navigation dictionaries) stay shared with Core by design. Only the
    identity-bound entries differ. The shared route permission registry is
    replaced by a registrar bound to ``identity``, or left out when the module has
    no verified identity, so an attempt to register a rule fails loudly.
    """
    registries = dict(container.registries)
    registries.pop(MODULE_ROUTE_PERMISSIONS_KEY, None)
    if route_registrar is not None:
        registries[MODULE_ROUTE_PERMISSIONS_KEY] = route_registrar
    metadata = dict(container.metadata)
    metadata.pop(PLATFORM_MODULE_METADATA_KEY, None)
    if identity is not None:
        metadata[PLATFORM_MODULE_METADATA_KEY] = identity
    bound: dict[str, Any] = {}
    port = getattr(container, "external_sign_in", None)
    binder = getattr(port, "for_platform_module", None)
    if identity is not None and callable(binder):
        bound["external_sign_in"] = binder(str(identity.get("id") or ""), requires)
    return dataclasses.replace(
        container,
        auth_helpers=dict(container.auth_helpers),
        service_factories=dict(container.service_factories),
        registries=registries,
        metadata=metadata,
        **bound,
    )


def _snapshot(view: ModuleContainer) -> dict[str, dict[str, Any]]:
    return {name: dict(getattr(view, name)) for name in _VIEW_DICTS}


def _merge_module_container(
    container: ModuleContainer,
    module_view: ModuleContainer,
    before: Mapping[str, Mapping[str, Any]],
    *,
    who: str,
) -> None:
    """Copy the top-level keys a module added back into Core's container.

    ``before`` is the view as the module received it. Replacing or deleting any of
    those keys raises :class:`ModuleContainerViolationError` naming the key and the
    module; nothing is merged then. The identity-bound entries are never compared
    and never copied back.
    """
    additions: list[tuple[dict[str, Any], str, Any]] = []
    for name in _VIEW_DICTS:
        bound_key = _BOUND_KEYS.get(name)
        original = before[name]
        current: dict[str, Any] = getattr(module_view, name)
        shared: dict[str, Any] = getattr(container, name)
        for key, value in original.items():
            if key == bound_key:
                continue
            if key not in current:
                raise ModuleContainerViolationError(
                    f"{who} removed existing {name} entry {key!r}; modules may only add entries"
                )
            if current[key] is not value:
                raise ModuleContainerViolationError(
                    f"{who} replaced existing {name} entry {key!r}; modules may only add entries"
                )
        for key, value in current.items():
            if key == bound_key or key in original:
                continue
            if key in shared:
                raise ModuleContainerViolationError(
                    f"{who} added {name} entry {key!r}, which Core's container already has"
                )
            additions.append((shared, key, value))
    for shared, key, value in additions:
        shared[key] = value


@contextmanager
def _environment_module_container(container: ContainerT) -> Iterator[ContainerT]:
    """Core's own container for an environment-listed module, as before ADR 0082.

    The route permission registry is taken out while the module registers and put
    back afterwards, also when the module raised or set the key itself.
    """
    registries = container.registries
    had_routes = MODULE_ROUTE_PERMISSIONS_KEY in registries
    shared_routes = registries.pop(MODULE_ROUTE_PERMISSIONS_KEY, None)
    try:
        with page_registration_scope(module_id=None):
            yield container
    finally:
        if had_routes:
            registries[MODULE_ROUTE_PERMISSIONS_KEY] = shared_routes
        else:
            registries.pop(MODULE_ROUTE_PERMISSIONS_KEY, None)


class PlatformModuleViews:
    """Identity-bound container views for one loader run.

    Implements the loader's view provider structurally. What each prepared module
    registered is recorded under ``metadata["platform_module_registrations"]``
    once every prepared module has loaded.
    """

    def __init__(self) -> None:
        self._registrations: list[dict[str, Any]] = []

    @contextmanager
    def module_view(
        self, module: LoadedModule | None, container: ContainerT
    ) -> Iterator[ContainerT]:
        if module is None:
            with _environment_module_container(container) as same_container:
                yield same_container
            return
        shared_routes = container.registries.get(MODULE_ROUTE_PERMISSIONS_KEY)
        has_routes = isinstance(shared_routes, ModuleRoutePermissionRegistry)
        view = _module_container(
            container,
            identity=module.identity(),
            route_registrar=(
                ModuleRouteRuleRegistrar(shared_routes, module.module_id) if has_routes else None
            ),
            requires=tuple(str(item) for item in getattr(module, "requires", ()) or ()),
        )
        before = _snapshot(view)
        with page_registration_scope(module_id=module.module_id) as scope:
            yield view
        _merge_module_container(
            container, view, before, who=f"platform module '{module.module_id}'"
        )
        self._registrations.append(
            {
                "id": module.module_id,
                "version": module.version,
                "pages": list(scope.page_keys),
                "route_rules": shared_routes.rule_count(module.module_id) if has_routes else 0,
            }
        )

    def prepared_modules_loaded(self, container: ModuleContainer) -> None:
        container.metadata[PLATFORM_MODULE_REGISTRATIONS_KEY] = list(self._registrations)


__all__ = [
    "MODULE_ROUTE_PERMISSIONS_KEY",
    "PLATFORM_MODULE_METADATA_KEY",
    "PLATFORM_MODULE_REGISTRATIONS_KEY",
    "ModuleContainerViolationError",
    "PlatformModuleViews",
]
