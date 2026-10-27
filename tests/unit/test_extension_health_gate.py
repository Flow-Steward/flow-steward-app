from __future__ import annotations

import pytest

from core.application.extensions.health_gate import require_extension_health_ready
from core.application.management.errors import ServiceValidationError


def test_extension_health_gate_accepts_ready_runtime() -> None:
    require_extension_health_ready(
        {
            "extension_id": "acme.ready",
            "healthcheck": {"command": ["python3", "health.py"]},
        },
        run_health=lambda _extension: {"status": "healthy"},
    )


def test_extension_health_gate_blocks_activation_with_bounded_reason() -> None:
    with pytest.raises(ServiceValidationError) as raised:
        require_extension_health_ready(
            {
                "extension_id": "acme.broken",
                "healthcheck": {"command": ["python3", "health.py"]},
            },
            run_health=lambda _extension: {
                "status": "failed",
                "detail": "dependency_unavailable: paramiko",
            },
        )

    assert raised.value.payload == {
        "reason_code": "extension_health_failed",
        "extension_id": "acme.broken",
    }
    assert "dependency_unavailable: paramiko" in str(raised.value)
