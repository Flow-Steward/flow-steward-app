"""Thin CLI bindings for shared core readiness."""

from __future__ import annotations

from core.application.management.errors import ServiceUnavailableError
from core.application.readiness.contracts import ReadinessPrerequisiteMissing
from core.entrypoints.cli.platform.service_bindings.common import RuntimeServices, ServiceFactory


def _run_probe(operation) -> object:
    try:
        return operation()
    except ReadinessPrerequisiteMissing:
        # The service turns this into a finding; naming what is missing is the
        # command's purpose, so it must not be flattened into internal_error.
        raise
    except ServiceUnavailableError:
        raise
    except Exception as exc:
        raise ServiceUnavailableError(f"Readiness probe failed ({exc.__class__.__name__})") from exc


def readiness_status(runtime: RuntimeServices, _value: object, **_kwargs: object) -> object:
    return _run_probe(runtime.core_readiness_service.status)


def readiness_doctor(runtime: RuntimeServices, _value: object, **_kwargs: object) -> object:
    return _run_probe(runtime.core_readiness_service.check)


READINESS_SERVICE_FACTORIES: dict[str, ServiceFactory] = {
    "readiness.status": readiness_status,
    "readiness.doctor": readiness_doctor,
}
