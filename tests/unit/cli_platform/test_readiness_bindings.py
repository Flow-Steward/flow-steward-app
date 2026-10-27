from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.application.management.errors import ServiceUnavailableError
from core.entrypoints.cli.platform.service_bindings.readiness import readiness_doctor


def test_readiness_binding_maps_probe_crash_to_typed_dependency_failure() -> None:
    class BrokenReadiness:
        def check(self):
            raise RuntimeError("database authentication rejected")

    runtime = SimpleNamespace(core_readiness_service=BrokenReadiness())

    with pytest.raises(ServiceUnavailableError, match=r"Readiness probe failed \(RuntimeError\)"):
        readiness_doctor(runtime, object())
