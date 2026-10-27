"""Public schema-driven Click application entry point."""

from __future__ import annotations

from typing import Any

from core.entrypoints.cli.platform.click_adapter import build_click_app
from core.entrypoints.cli.platform.service_registry import ServiceRegistry


class _LazyCLIRuntime:
    """Defer concrete product/lifecycle composition until a command executes."""

    _runtime: Any = None

    def __getattr__(self, name: str) -> Any:
        if self._runtime is None:
            from core.entrypoints.cli.runtime import CLIRuntime

            self._runtime = CLIRuntime()
        return getattr(self._runtime, name)


_runtime = _LazyCLIRuntime()
click_app = build_click_app(ServiceRegistry(_runtime), _runtime)


if __name__ == "__main__":
    click_app(prog_name="flow-steward")
