"""Internal app bootstrap seams for Flow Steward core and optional private modules."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

if TYPE_CHECKING:
    from fastapi import FastAPI


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


def load_optional_private_modules(
    app: FastAPI,
    container: RuntimeContainer,
    *,
    logger: logging.Logger | None = None,
    module_names: Any = None,
    entrypoint: str = "register",
) -> tuple[str, ...]:
    """Load optional private bootstrap modules when present.

    Missing private packages are treated as a normal Community install.
    Import failures inside an installed private package still surface so a
    broken optional module is not silently ignored.
    """
    loaded: list[str] = []
    for module_name in _configured_optional_module_names(module_names):
        module_root = module_name.split(".", 1)[0]

        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name in {module_name, module_root}:
                continue
            raise

        register = getattr(module, entrypoint, None)
        if not callable(register):
            if logger is not None:
                logger.warning(
                    "Optional private bootstrap '%s' is missing callable entrypoint '%s'",
                    module_name,
                    entrypoint,
                )
            continue

        register(app, container)
        loaded.append(module_name)
    return tuple(loaded)
