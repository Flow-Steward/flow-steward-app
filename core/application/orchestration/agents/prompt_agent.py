# ruff: noqa: E402, F405, I001, RUF100
# mypy: ignore-errors
"""Compatibility facade for split PromptAgent implementation modules."""

from __future__ import annotations

import sys as _sys
import types as _types

_SPLIT_MODULE_NAMES = (
    "core.application.orchestration.agents.prompt_agent_common",
    "core.application.orchestration.agents.prompt_agent_execute",
    "core.application.orchestration.agents.prompt_agent_trace",
    "core.application.orchestration.agents.prompt_agent_tools",
    "core.application.orchestration.agents.prompt_agent_artifacts",
    "core.application.orchestration.agents.prompt_agent_quality",
    "core.application.orchestration.agents.prompt_agent_phase_result",
)

from .prompt_agent_common import *  # noqa: F403,F401  # NOSONAR
from .prompt_agent_execute import *  # noqa: F403,F401  # NOSONAR
from .prompt_agent_trace import *  # noqa: F403,F401  # NOSONAR
from .prompt_agent_tools import *  # noqa: F403,F401  # NOSONAR
from .prompt_agent_artifacts import *  # noqa: F403,F401  # NOSONAR
from .prompt_agent_quality import *  # noqa: F403,F401  # NOSONAR
from .prompt_agent_phase_result import *  # noqa: F403,F401  # NOSONAR


class PromptAgent(
    PromptAgentPhaseResultMixin,
    PromptAgentQualityMixin,
    PromptAgentArtifactMixin,
    PromptAgentToolMixin,
    PromptAgentTraceMixin,
    PromptAgentExecutionMixin,
    PromptAgentCommonMixin,
    BaseAgent,
):
    """Generic LLM agent driven entirely by YAML manifest data."""


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
    merged["PromptAgent"] = globals()["PromptAgent"]
    for module in modules:
        module.__dict__.update(merged)
    globals().update(merged)


class _PromptAgentFacadeModule(_types.ModuleType):
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
_sys.modules[__name__].__class__ = _PromptAgentFacadeModule

__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
