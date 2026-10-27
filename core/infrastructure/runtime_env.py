"""Canonical process-environment access boundary.

Runtime code imports ``runtime_env`` instead of reading ``os.environ`` directly.
This module is deliberately outside the configuration package so importing it
cannot initialize application configuration or load values from a dotenv file.
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from os import environ as _process_environment


class RuntimeEnvironment(MutableMapping[str, str]):
    """A transparent, typed view over the current process environment."""

    def __getitem__(self, key: str) -> str:
        return _process_environment[key]

    def __setitem__(self, key: str, value: str) -> None:
        _process_environment[str(key)] = str(value)

    def __delitem__(self, key: str) -> None:
        del _process_environment[key]

    def __iter__(self) -> Iterator[str]:
        return iter(_process_environment)

    def __len__(self) -> int:
        return len(_process_environment)

    def getenv(self, key: str, default: str | None = None) -> str | None:
        """Mirror ``os.getenv`` for incremental caller migration."""
        return _process_environment.get(key, default)

    def copy(self) -> dict[str, str]:
        """Return an independent subprocess-safe environment dictionary."""
        return dict(_process_environment)


runtime_env = RuntimeEnvironment()

__all__ = ["RuntimeEnvironment", "runtime_env"]
