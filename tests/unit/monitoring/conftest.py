from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

INSTALLATION_ID = "4ba8a97c-722c-4ab7-9832-4129821ca88a"


@pytest.fixture
def now():
    return datetime(2026, 10, 1, 12, tzinfo=UTC)


@pytest.fixture
def context():
    from core.application.monitoring.contracts import RuntimeInstallationContext

    return RuntimeInstallationContext(
        installation_id=INSTALLATION_ID, deployment_profile="scalable"
    )


@pytest.fixture
def report_body(now):
    def build(**changes):
        envelope = {
            "schema_version": 1,
            "installation_id": INSTALLATION_ID,
            "deployment_profile": "scalable",
            "service_type": "worker",
            "instance_id": "9ee9238d-23e9-4f78-847d-d5652e5b521b",
            "incarnation_id": "9ee9238d-23e9-4f78-847d-d5652e5b521b",
            "sequence": 1,
            "observed_at": now.isoformat(),
            "source": "self",
            "payload": {
                "type": "runtime_status",
                "version": 1,
                "lifecycle": "running",
                "readiness": {"state": "not_observed"},
                "health": {"state": "not_observed"},
                "consumer_state": "registered",
            },
        }
        envelope.update(changes)
        return json.dumps(envelope).encode()

    return build
