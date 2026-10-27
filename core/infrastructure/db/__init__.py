"""Data infrastructure package with lazy submodule loading."""

from __future__ import annotations

import importlib
import sys
import types
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

CORE_INFRASTRUCTURE_DB_PSYCOPG_RUNTIME_IS_MESSAGE = (
    "core.infrastructure.db.psycopg_runtime is required for core.infrastructure.db.platform_logger"
)


def _build_platform_logger_stub() -> types.ModuleType:
    module = types.ModuleType(f"{__name__}.platform_logger")

    class PlatformLogger:  # pragma: no cover - test fallback only
        def __init__(self, *_args, **_kwargs) -> None:
            raise RuntimeError(CORE_INFRASTRUCTURE_DB_PSYCOPG_RUNTIME_IS_MESSAGE)

        @classmethod
        def get_shared_instance(cls, *_args, **_kwargs):
            raise RuntimeError(CORE_INFRASTRUCTURE_DB_PSYCOPG_RUNTIME_IS_MESSAGE)

    class PlatformLoggerCallback:  # pragma: no cover - test fallback only
        pass

    def resolve_observability_dsn(explicit_dsn: object | None = None) -> str:
        candidates = [
            str(explicit_dsn or ""),
            _runtime_env.get("FS_OBSERVABILITY_DSN", ""),
            _runtime_env.get("FS_DB_DSN", ""),
            _runtime_env.get("DATABASE_URL", ""),
            _runtime_env.get("POSTGRES_DSN", ""),
        ]
        for value in candidates:
            if value and value.strip():
                return value.strip()
        return ""

    def get_shared_observability_logger(dsn: object | None = None):
        return PlatformLogger.get_shared_instance(dsn)

    def _connect_unavailable(*_args, **_kwargs):
        raise RuntimeError(CORE_INFRASTRUCTURE_DB_PSYCOPG_RUNTIME_IS_MESSAGE)

    module_any: Any = module
    module_any.PlatformLogger = PlatformLogger
    module_any.PlatformLoggerCallback = PlatformLoggerCallback
    module_any.get_shared_observability_logger = get_shared_observability_logger
    module_any.resolve_observability_dsn = resolve_observability_dsn
    module_any.AESGCM = None
    module_any.pooling_enabled = lambda: False
    module_any.pool_bounds = lambda: (1, 1)
    module_any.get_threaded_pool = lambda *_args, **_kwargs: None
    runtime_stub = types.SimpleNamespace(connect=_connect_unavailable)
    module_any.psycopg_runtime = runtime_stub
    return module


def _load_submodule(name: str):
    canonical_name = name
    full_name = f"{__name__}.{canonical_name}"
    cached = sys.modules.get(full_name)
    if cached is not None:
        if name != canonical_name:
            sys.modules[f"{__name__}.{name}"] = cached
            setattr(sys.modules[__name__], name, cached)
        return cached
    try:
        module = importlib.import_module(full_name)
    except ModuleNotFoundError as exc:
        if canonical_name == "platform_logger" and (
            str(getattr(exc, "name", "") or "")
            in {"psycopg", "core.infrastructure.db.psycopg_runtime"}
            or "psycopg_runtime" in str(exc)
        ):
            module = _build_platform_logger_stub()
            sys.modules[full_name] = module
        else:
            raise
    setattr(sys.modules[__name__], canonical_name, module)
    if name != canonical_name:
        sys.modules[f"{__name__}.{name}"] = module
        setattr(sys.modules[__name__], name, module)
    return module


def __getattr__(name: str):
    if not name or name.startswith("_"):
        raise AttributeError(name)
    try:
        return _load_submodule(name)
    except ModuleNotFoundError as exc:
        raise AttributeError(name) from exc
