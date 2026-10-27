# ruff: noqa: E402, I001, RUF100
"""Compatibility facade for split contract validation modules."""

from __future__ import annotations

import sys as _sys
import types as _types

_SPLIT_MODULE_NAMES = (
    "core.application.registry.contract_validation_common",
    "core.application.registry.contract_validation_workflow",
    "core.application.registry.contract_validation_invoke",
    "core.application.registry.contract_validation_entities",
    "core.application.registry.contract_validation_workflow_readiness",
)

from .contract_validation_common import *  # noqa: F403,F401  # NOSONAR
from .contract_validation_workflow import *  # noqa: F403,F401  # NOSONAR
from .contract_validation_invoke import *  # noqa: F403,F401  # NOSONAR
from .contract_validation_entities import *  # noqa: F403,F401  # NOSONAR
from .contract_validation_workflow_readiness import *  # noqa: F403,F401  # NOSONAR
from .contract_validation_workflow_readiness import (
    validate_workflow_readiness as validate_workflow_readiness,
)


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
    for module in modules:
        module.__dict__.update(merged)
    globals().update(merged)


class _ContractValidationFacadeModule(_types.ModuleType):
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
_sys.modules[__name__].__class__ = _ContractValidationFacadeModule

__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
