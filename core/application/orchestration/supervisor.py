# ruff: noqa: E402, F405, I001, RUF100
# mypy: ignore-errors
"""Compatibility facade for split orchestrator supervisor modules."""

from __future__ import annotations

import sys as _sys
import types as _types

_SPLIT_MODULE_NAMES = (
    "core.application.orchestration.supervisor_common",
    "core.application.orchestration.supervisor_routing",
)

from .supervisor_common import *  # noqa: F403,F401  # NOSONAR
from .supervisor_routing import *  # noqa: F403,F401  # NOSONAR


class OrchestratorSupervisor(
    OrchestratorSupervisorRoutingMixin,
    OrchestratorSupervisorCommonMixin,
):
    """Validates and hydrates the workflow a run is bound to."""


def _sync_split_module_globals() -> None:
    merged: dict[str, object] = {}
    modules = [
        module
        for module_name in _SPLIT_MODULE_NAMES
        if (module := _sys.modules.get(module_name)) is not None
    ]
    for module in modules:
        merged.update(
            {
                name: value
                for name, value in vars(module).items()
                if not (name.startswith("__") and name.endswith("__"))
            }
        )
    merged["OrchestratorSupervisor"] = globals()["OrchestratorSupervisor"]
    for module in modules:
        module.__dict__.update(merged)
    globals().update(merged)


class _SupervisorFacadeModule(_types.ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for module_name in _SPLIT_MODULE_NAMES:
            module = _sys.modules.get(module_name)
            if module is not None:
                module.__dict__[name] = value

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        for module_name in _SPLIT_MODULE_NAMES:
            module = _sys.modules.get(module_name)
            if module is not None:
                module.__dict__.pop(name, None)


_sync_split_module_globals()
_sys.modules[__name__].__class__ = _SupervisorFacadeModule

__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
