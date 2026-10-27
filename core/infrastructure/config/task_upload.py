"""Operator-owned task upload admission settings."""

from __future__ import annotations

from dataclasses import dataclass

from core.infrastructure.config.env_registry import registry_default_value
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env


def _bounded_int(name: str, *, fallback: int, minimum: int, maximum: int) -> int:
    try:
        default = int(registry_default_value(name))
    except (KeyError, TypeError, ValueError):
        default = fallback
    try:
        value = int(_runtime_env.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class TaskUploadAdmissionSettings:
    total_capacity: int
    per_account_capacity: int
    per_principal_capacity: int
    body_read_timeout_seconds: int


def task_upload_admission_settings() -> TaskUploadAdmissionSettings:
    total = _bounded_int(
        "FS_TASK_UPLOAD_MAX_CONCURRENT_PREPARATIONS", fallback=4, minimum=1, maximum=64
    )
    account = _bounded_int(
        "FS_TASK_UPLOAD_MAX_CONCURRENT_PREPARATIONS_PER_ACCOUNT",
        fallback=2,
        minimum=1,
        maximum=64,
    )
    principal = _bounded_int(
        "FS_TASK_UPLOAD_MAX_CONCURRENT_PREPARATIONS_PER_PRINCIPAL",
        fallback=2,
        minimum=1,
        maximum=64,
    )
    return TaskUploadAdmissionSettings(
        total_capacity=total,
        per_account_capacity=min(total, account),
        per_principal_capacity=min(total, account, principal),
        body_read_timeout_seconds=_bounded_int(
            "FS_TASK_UPLOAD_BODY_READ_TIMEOUT_SECONDS",
            fallback=120,
            minimum=5,
            maximum=3600,
        ),
    )


TASK_UPLOAD_ADMISSION_SETTINGS = task_upload_admission_settings()
