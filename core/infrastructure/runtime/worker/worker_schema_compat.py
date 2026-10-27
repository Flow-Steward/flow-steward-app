"""Worker startup database schema compatibility checks."""

from __future__ import annotations

import logging
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

logger = logging.getLogger("platform.worker")
_SCHEMA_COMPAT_MAX_ATTEMPTS_ENV = "FS_SCHEMA_COMPAT_MAX_ATTEMPTS"
_SCHEMA_COMPAT_RETRY_DELAY_SECONDS_ENV = "FS_SCHEMA_COMPAT_RETRY_DELAY_SECONDS"
_DEFAULT_SCHEMA_COMPAT_MAX_ATTEMPTS = 12
_DEFAULT_SCHEMA_COMPAT_RETRY_DELAY_SECONDS = 2.0
_MIN_SCHEMA_COMPAT_MAX_ATTEMPTS = 1
_MIN_SCHEMA_COMPAT_RETRY_DELAY_SECONDS = 0.5


@dataclass(frozen=True)
class WorkerSchemaDependencies:
    logger: Any
    retry_settings: Callable[[], tuple[int, float]]
    run_check: Callable[[], None]
    retry_delay: Callable[..., float | None]


def _schema_compat_retry_settings() -> tuple[int, float]:
    raw_attempts = _runtime_env.get(
        _SCHEMA_COMPAT_MAX_ATTEMPTS_ENV,
        str(_DEFAULT_SCHEMA_COMPAT_MAX_ATTEMPTS),
    )
    raw_delay_seconds = _runtime_env.get(
        _SCHEMA_COMPAT_RETRY_DELAY_SECONDS_ENV,
        str(_DEFAULT_SCHEMA_COMPAT_RETRY_DELAY_SECONDS),
    )

    attempts = max(
        _MIN_SCHEMA_COMPAT_MAX_ATTEMPTS,
        int(raw_attempts or str(_DEFAULT_SCHEMA_COMPAT_MAX_ATTEMPTS)),
    )
    delay_seconds = max(
        _MIN_SCHEMA_COMPAT_RETRY_DELAY_SECONDS,
        float(raw_delay_seconds or str(_DEFAULT_SCHEMA_COMPAT_RETRY_DELAY_SECONDS)),
    )
    return attempts, delay_seconds


def _iter_exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        next_exc = current.__cause__ or current.__context__
        current = next_exc if isinstance(next_exc, BaseException) else None
    return chain


def _is_transient_schema_compat_error(exc: BaseException) -> bool:
    transient_markers = (
        "failed to resolve host",
        "name or service not known",
        "temporary failure in name resolution",
        "connection refused",
        "could not connect",
        "server closed the connection unexpectedly",
    )
    for item in _iter_exception_chain(exc):
        if isinstance(item, socket.gaierror | ConnectionError | TimeoutError | OSError):
            return True
        text = str(item).strip().lower()
        if any(marker in text for marker in transient_markers):
            return True
    return False


def _run_schema_compatibility_check() -> None:
    from core.infrastructure.config.runtime_config import CONFIG
    from core.infrastructure.db.platform_logger import resolve_observability_dsn
    from core.infrastructure.db.schema_contract import (
        enforce_runtime_schema_contract_dsn,
    )

    dsn = resolve_observability_dsn(
        str(CONFIG.get("fs_observability_dsn") or CONFIG.get("fs_db_dsn") or "")
    )
    enforce_runtime_schema_contract_dsn(dsn, component="worker")


def _schema_compat_retry_delay(
    exc: BaseException,
    *,
    attempt: int,
    attempts: int,
    delay_seconds: float,
) -> float | None:
    if attempt >= attempts or not _is_transient_schema_compat_error(exc):
        return None
    return delay_seconds


def _enforce_schema_compatibility(
    dependencies: WorkerSchemaDependencies | None = None,
) -> None:
    """Fail fast when DB schema is older than worker runtime expectations."""
    deps = dependencies or WorkerSchemaDependencies(
        logger=logger,
        retry_settings=_schema_compat_retry_settings,
        run_check=_run_schema_compatibility_check,
        retry_delay=_schema_compat_retry_delay,
    )
    attempts, delay_seconds = deps.retry_settings()
    for attempt in range(1, attempts + 1):
        try:
            deps.run_check()
            return
        except Exception as exc:
            retry_delay = deps.retry_delay(
                exc,
                attempt=attempt,
                attempts=attempts,
                delay_seconds=delay_seconds,
            )
            if retry_delay is None:
                deps.logger.error("Worker schema compatibility check failed: %s", exc)
                raise
            deps.logger.info(
                "Worker schema compatibility check attempt %d/%d failed: %s; retrying in %.1fs",
                attempt,
                attempts,
                exc,
                retry_delay,
            )
            time.sleep(retry_delay)
