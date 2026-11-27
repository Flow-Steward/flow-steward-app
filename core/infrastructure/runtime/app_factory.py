"""Internal app bootstrap seams for Flow Steward core and optional private modules."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

if TYPE_CHECKING:
    from fastapi import FastAPI

    from core.infrastructure.platform_modules.loader import PreparedPlatformModule


def validate_runtime_service_instance(
    *, seam: str, dependency: str, service: Any, required_methods: tuple[str, ...]
) -> Any:
    missing = [
        method for method in required_methods if not callable(getattr(service, method, None))
    ]
    if missing:
        raise RuntimeError(
            f"required runtime service seam '{seam}' dependency '{dependency}' "
            f"is missing methods: {', '.join(missing)}"
        )
    return service


@dataclass(slots=True)
class RuntimeContainer:
    """Internal runtime bundle shared across core and optional private bootstrap."""

    app: FastAPI
    config: Mapping[str, Any]
    auth_helpers: dict[str, Callable[..., Any]] = field(default_factory=dict)
    service_factories: dict[str, Callable[..., Any]] = field(default_factory=dict)
    registries: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    #: ADR 0082 ports a platform module's ``register`` reads as attributes. The
    #: composition root sets them before modules load; ``None`` in a container built
    #: without them. Feature name -> base URL of the module API serving it:
    feature_endpoints: Any = None
    #: Read and replace the assigned approvers of Approval steps.
    approval_assignments: Any = None
    #: Read the approval decision journal.
    approval_journal: Any = None
    #: Read and replace the project limits of people (scoped roles).
    scoped_roles: Any = None
    #: The installation id Core resolved for this deployment, read only; ``None``
    #: when the deployment projects none (a plain local run).
    installation_id: str | None = None
    #: List, create, enable and disable accounts (installation administrator only).
    accounts: Any = None
    #: Plug sign-in methods, an identity resolver or a trusted header source into
    #: external sign-in (ADR 0083).
    external_sign_in: Any = None
    #: Register secret backends that keep stored-secret values outside the database
    #: (ADR 0066 amendment 2026-11-26).
    secret_backends: Any = None


def validate_runtime_service_ports(
    container: RuntimeContainer,
    contracts: Sequence[Any],
) -> dict[str, Any]:
    """Resolve concrete services and fail closed when a required port is absent."""
    resolved: dict[str, Any] = {}
    for contract in contracts:
        if isinstance(contract, tuple):
            seam, factory_name, required_methods = contract
        else:
            seam = str(contract.consumer)
            factory_name = str(contract.dependency)
            required_methods = tuple(contract.required_methods)
        factory = container.service_factories.get(factory_name)
        if not callable(factory):
            raise RuntimeError(
                f"required runtime service seam '{seam}' has no factory '{factory_name}'"
            )
        service = resolved.setdefault(factory_name, factory())
        validate_runtime_service_instance(
            seam=seam,
            dependency=factory_name,
            service=service,
            required_methods=required_methods,
        )
    return resolved


def build_runtime_container(
    *,
    app: FastAPI,
    config: Mapping[str, Any],
    auth_helpers: Mapping[str, Callable[..., Any]] | None = None,
    service_factories: Mapping[str, Callable[..., Any]] | None = None,
    registries: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> RuntimeContainer:
    """Create the internal runtime container used during bootstrap."""

    return RuntimeContainer(
        app=app,
        config=config,
        auth_helpers=dict(auth_helpers or {}),
        service_factories=dict(service_factories or {}),
        registries=dict(registries or {}),
        metadata=dict(metadata or {}),
    )


def _configured_optional_module_names(module_names: Any = None) -> tuple[str, ...]:
    if module_names is None:
        raw_names: Any = _runtime_env.get("FS_OPTIONAL_PRIVATE_BOOTSTRAP_MODULES", "")
    else:
        raw_names = module_names

    candidates = raw_names.split(",") if isinstance(raw_names, str) else list(raw_names or ())

    names: list[str] = []
    for candidate in candidates:
        name = str(candidate or "").strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


class ModuleViewProvider(Protocol):
    """Builds the container each loaded module's ``register`` receives.

    The loader owns importing and calling entrypoints; the provider owns what a
    module may see. ``module`` is the prepared platform module being registered,
    or ``None`` for a module listed by ``FS_OPTIONAL_PRIVATE_BOOTSTRAP_MODULES``.
    """

    def module_view(
        self, module: PreparedPlatformModule | None, container: RuntimeContainer
    ) -> AbstractContextManager[RuntimeContainer]:
        """Yield the view for one ``register`` call and reconcile it on a clean exit."""
        ...

    def prepared_modules_loaded(self, container: RuntimeContainer) -> None:
        """Called once after every prepared module registered, before optional ones."""
        ...


class _SharedContainerView:
    """Default provider: every module registers against Core's own container."""

    def module_view(
        self, module: PreparedPlatformModule | None, container: RuntimeContainer
    ) -> AbstractContextManager[RuntimeContainer]:
        del module
        return nullcontext(container)

    def prepared_modules_loaded(self, container: RuntimeContainer) -> None:
        del container


@contextmanager
def _module_view_on_app(
    app: FastAPI, container: RuntimeContainer, module_view: RuntimeContainer
) -> Iterator[None]:
    """Point ``app.state.runtime_container`` at the module's view while it registers.

    This keeps ``register`` consistent with the view it was handed; it is not
    isolation. After registration ``app.state.runtime_container`` is Core's own
    container again, and in-process module code can reach Core objects through
    ``app`` at any time. The view binding is an interface contract.
    """
    state = getattr(app, "state", None)
    if state is None or getattr(state, "runtime_container", None) is not container:
        yield
        return
    state.runtime_container = module_view
    try:
        yield
    finally:
        state.runtime_container = container


def _call_entrypoint(
    app: FastAPI,
    container: RuntimeContainer,
    register: Callable[..., Any],
    *,
    module: PreparedPlatformModule | None,
    views: ModuleViewProvider,
) -> None:
    with (
        views.module_view(module, container) as module_view,
        _module_view_on_app(app, container, module_view),
    ):
        register(app, module_view)


def _import_prepared_module(module: PreparedPlatformModule, entrypoint: str) -> Callable[..., Any]:
    from core.infrastructure.platform_modules.loader import verify_imported_module_origin

    try:
        imported = importlib.import_module(module.import_name)
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"Required platform module '{module.module_id}' cannot import "
            f"'{module.import_name}': {exc}"
        ) from exc
    # The imported object must come from the module's own src/, so code shipped by
    # another module cannot register under this module's identity.
    verify_imported_module_origin(module, imported)
    register = getattr(imported, entrypoint, None)
    if not callable(register):
        raise RuntimeError(
            f"Required platform module '{module.module_id}' ('{module.import_name}') "
            f"is missing callable entrypoint '{entrypoint}'"
        )
    return register


def load_optional_private_modules(
    app: FastAPI,
    container: RuntimeContainer,
    *,
    logger: logging.Logger | None = None,
    module_names: Any = None,
    prepared_modules: Sequence[PreparedPlatformModule] = (),
    entrypoint: str = "register",
    views: ModuleViewProvider | None = None,
) -> tuple[str, ...]:
    """Load prepared platform modules, then optional private bootstrap modules.

    ``prepared_modules`` are verified prepared platform modules and load first,
    in order. A prepared module that fails to load (its import fails, its package
    or entrypoint is missing, it comes from outside its own ``src/``, or its
    ``register`` raises) is skipped and the process keeps running: everything it
    registered is rolled back first (see :class:`ModuleRegistrationRollback`), the
    error is logged with its id and version, and the failure is recorded under
    ``metadata["platform_module_failures"]``. Its features stay off, because its
    entitlement source and feature endpoints never stay registered, and the
    entitlement service is marked degraded so a "not entitled" answer that only
    the failed module could have turned is reported as unknown. The returned
    names are those of the modules that loaded. Modules listed by
    ``FS_OPTIONAL_PRIVATE_BOOTSTRAP_MODULES`` load afterwards: a missing optional
    package is a normal Community install; an import failure inside an installed
    one still surfaces.

    ``views`` decides the container each ``register`` call receives; without it
    every module registers against ``container`` itself. While a module registers
    against another view, ``app.state.runtime_container`` exposes that view.

    Every prepared module must be imported from its own ``src/``; an object
    loaded from anywhere else is refused before its entrypoint is called.
    """
    from core.infrastructure.platform_modules.loader import (
        PLATFORM_MODULE_FAILURES_KEY,
        ModuleRegistrationRollback,
        check_required_modules_loaded,
        claim_entitlement_source,
        record_module_failure,
    )

    provider: ModuleViewProvider = views or _SharedContainerView()
    loaded: list[str] = []
    failures: list[dict[str, str]] = []
    failed_ids: set[str] = set()
    source_holder: PreparedPlatformModule | None = None
    for module in prepared_modules:
        rollback = ModuleRegistrationRollback.capture(container, app=app)
        try:
            # A module whose required module failed to load is skipped as well.
            check_required_modules_loaded(module, failed_ids)
            holder = claim_entitlement_source(module, source_holder)
            register = _import_prepared_module(module, entrypoint)
            with rollback.loading_scope(module):
                _call_entrypoint(app, container, register, module=module, views=provider)
        except Exception as exc:
            rollback.restore()
            rollback.mark_degraded(module)
            failed_ids.add(module.module_id)
            failures.append(record_module_failure(module, exc, logger=logger))
            continue
        source_holder = holder
        loaded.append(module.import_name)
    container.metadata[PLATFORM_MODULE_FAILURES_KEY] = failures
    provider.prepared_modules_loaded(container)

    for module_name in _configured_optional_module_names(module_names):
        if module_name in loaded:
            continue
        module_root = module_name.split(".", 1)[0]

        try:
            imported = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name in {module_name, module_root}:
                continue
            raise

        register = getattr(imported, entrypoint, None)
        if not callable(register):
            if logger is not None:
                logger.warning(
                    "Optional private bootstrap '%s' is missing callable entrypoint '%s'",
                    module_name,
                    entrypoint,
                )
            continue

        # No platform module identity, so it declares no capability: Core's
        # entitlement service refuses a source it tries to register.
        with ModuleRegistrationRollback.capture(container, app=app).loading_scope(None):
            _call_entrypoint(app, container, register, module=None, views=provider)
        loaded.append(module_name)
    return tuple(loaded)
