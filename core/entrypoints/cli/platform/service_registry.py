"""Closed, import-lazy service dispatch for public CLI contracts."""

from __future__ import annotations

from collections.abc import Callable, Iterator, MutableMapping
from importlib import import_module
from typing import Any

from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS

ServiceFactory = Callable[..., object]
ChoiceProvider = Callable[..., object]
PlanBuilder = Callable[..., object]

_BINDINGS_PACKAGE = "core.entrypoints.cli.platform.service_bindings"
_BINDING_NAMES = {
    "backup": "BACKUP",
    "clone": "CLONE",
    "config": "CONFIG",
    "database": "DATABASE",
    "environment": "ENVIRONMENT",
    "extensions": "EXTENSION",
    "jobs": "JOB",
    "localization": "LOCALIZATION",
    "maintenance": "MAINTENANCE",
    "projects": "PROJECT",
    "quality": "QUALITY",
    "readiness": "READINESS",
    "release": "RELEASE",
    "reports": "REPORT",
    "stack": "STACK",
    "testing": "TESTING",
    "users": "USER",
    "workflows": "WORKFLOW",
}


def _mapping_entry(key: str, suffix: str) -> Callable[..., object]:
    prefix = key.split(".", 1)[0]
    binding_name = _BINDING_NAMES[prefix]
    module = import_module(f"{_BINDINGS_PACKAGE}.{prefix}")
    mapping = getattr(module, f"{binding_name}_{suffix}")
    return mapping[key]


def _choice_provider(key: str) -> Callable[..., object]:
    if key == "stack.log-services":
        module = import_module(f"{_BINDINGS_PACKAGE}.stack")
        return module.stack_log_services
    module = import_module(f"{_BINDINGS_PACKAGE}.common")
    if key == "accounts.available":
        return module.accounts_available
    if key == "projects.available":
        return module.projects_available
    raise KeyError(key)


class _LazyCallableMap(MutableMapping[str, Callable[..., object]]):
    """Expose a closed mapping without importing every concrete service module."""

    def __init__(
        self,
        keys: tuple[str, ...],
        resolver: Callable[[str], Callable[..., object]],
    ) -> None:
        self._keys = keys
        self._resolver = resolver
        self._overrides: dict[str, Callable[..., object]] = {}

    def __getitem__(self, key: str) -> Callable[..., object]:
        if key in self._overrides:
            return self._overrides[key]
        if key not in self._keys:
            raise KeyError(key)
        return self._resolver(key)

    def __setitem__(self, key: str, value: Callable[..., object]) -> None:
        self._overrides[key] = value

    def __delitem__(self, key: str) -> None:
        if key not in self._overrides:
            raise KeyError(key)
        del self._overrides[key]

    def __iter__(self) -> Iterator[str]:
        return iter(dict.fromkeys((*self._keys, *self._overrides)))

    def __len__(self) -> int:
        return len(tuple(iter(self)))


SERVICE_FACTORIES = _LazyCallableMap(
    tuple(spec.service_ref for spec in PUBLIC_COMMAND_SPECS),
    lambda key: _mapping_entry(key, "SERVICE_FACTORIES"),
)
CHOICE_PROVIDERS = _LazyCallableMap(
    ("accounts.available", "projects.available", "stack.log-services"),
    _choice_provider,
)
PLAN_BUILDERS = _LazyCallableMap(
    tuple(
        dict.fromkeys(
            spec.plan_builder_ref
            for spec in PUBLIC_COMMAND_SPECS
            if spec.plan_builder_ref is not None
        )
    ),
    lambda key: _mapping_entry(key, "PLAN_BUILDERS"),
)


class ServiceRegistry:
    """Resolve only explicit first-party service and choice-provider callables."""

    def __init__(self, runtime: Any, *, allowed_domains: frozenset[str] | None = None):
        self.runtime = runtime
        self._allowed_domains = allowed_domains

    def _assert_allowed(self, key: str) -> None:
        if self._allowed_domains is None:
            return
        domain = str(key).split(".", 1)[0]
        if domain not in self._allowed_domains:
            raise KeyError(f"Command domain is not available in this runtime: {domain}")

    def service(self, key: str) -> ServiceFactory:
        self._assert_allowed(key)
        return SERVICE_FACTORIES[key]

    def choice_provider(self, key: str) -> ChoiceProvider:
        self._assert_allowed(key)
        return CHOICE_PROVIDERS[key]

    def plan_builder(self, key: str) -> PlanBuilder:
        self._assert_allowed(key)
        return PLAN_BUILDERS[key]

    async def build_plan(self, key: str, value: object) -> object:
        return await self.plan_builder(key)(self.runtime, value)

    async def invoke(
        self,
        key: str,
        value: object,
        *,
        progress_sink: Callable[[Any], None] | None = None,
        technical_sink: Callable[[str], None] | None = None,
        cancellation: Any = None,
    ) -> object:
        from core.entrypoints.cli.platform.service_bindings.common import (
            await_maybe,
            bind_cancellation_token,
        )

        with bind_cancellation_token(cancellation):
            return await await_maybe(
                self.service(key)(
                    self.runtime,
                    value,
                    progress_sink=progress_sink,
                    technical_sink=technical_sink,
                )
            )

    async def choices(self, key: str) -> tuple[Any, ...]:
        result = await self.choice_provider(key)(self.runtime)
        return tuple(result)
