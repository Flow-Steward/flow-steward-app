"""Lifecycle-only Click entrypoint shipped in the retained fs-cli image."""

from __future__ import annotations

from typing import Any

from core.entrypoints.cli.platform.click_adapter import build_click_app
from core.entrypoints.cli.platform.service_registry import ServiceRegistry

LIFECYCLE_COMMAND_DOMAINS = frozenset(
    {
        "backup",
        "clone",
        "config",
        "database",
        "environment",
        "maintenance",
        "readiness",
        "release",
        "stack",
    }
)


class _LazyLifecycleRuntime:
    _runtime: Any = None

    def __getattr__(self, name: str) -> Any:
        if self._runtime is None:
            from core.entrypoints.cli.lifecycle_runtime import LifecycleCLIRuntime

            self._runtime = LifecycleCLIRuntime()
        return getattr(self._runtime, name)


_runtime = _LazyLifecycleRuntime()
click_app = build_click_app(
    ServiceRegistry(_runtime, allowed_domains=LIFECYCLE_COMMAND_DOMAINS),
    _runtime,
)


if __name__ == "__main__":
    click_app(prog_name="flow-steward")
