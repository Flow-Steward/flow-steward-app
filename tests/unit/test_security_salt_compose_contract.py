"""Every service that runs PII/security code must receive the production salt.

RC7-001 / RA-001: the security-posture loop was silently dead in production.
The audited stack had SECURITY_PII_SALT in fs-app and fs-worker only - and even
those two got it from a temporary audit override, not from shipped
configuration. The production compose declared it for no service at all, so a
clean production install starts services whose first posture iteration raises
"SECURITY_PII_SALT must be set to a unique secret value in production".

This enumerates the services rather than trusting a shared env_file: the salt is
a durable installer-owned secret, and a service silently missing it is exactly
the failure this row exists for.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

COMPOSE = Path("infra/docker/docker-compose.prod.yaml")

# Services that import or execute security/PII code. The retained CLI and thin
# API-backed MCP adapter deliberately do not run the posture loop.
SERVICES_RUNNING_SECURITY_CODE = ("fs-app", "fs-worker", "control-plane")


def _services() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]


@pytest.mark.parametrize("service", SERVICES_RUNNING_SECURITY_CODE)
def test_the_salt_is_declared_for_every_service_that_needs_it(service: str) -> None:
    environment = _services()[service].get("environment") or {}

    assert "SECURITY_PII_SALT" in environment, (
        f"{service} runs security/PII code but production compose never gives it the salt"
    )


@pytest.mark.parametrize("service", SERVICES_RUNNING_SECURITY_CODE)
def test_a_missing_salt_stops_the_stack_instead_of_starting_a_dead_service(
    service: str,
) -> None:
    """`:?` at compose time, not a service that starts and fails forever.

    Defaulting it would reintroduce the defect in a quieter form: the service
    would start, the posture loop would fail every iteration, and readiness
    would stay green.
    """
    value = str((_services()[service].get("environment") or {})["SECURITY_PII_SALT"])

    assert value.startswith("${SECURITY_PII_SALT:?"), (
        f"{service} accepts a missing or defaulted salt: {value!r}"
    )


def test_the_salt_stays_an_installer_managed_durable_secret() -> None:
    """The compose contract is only honest if something provisions the value."""
    import json

    manifest = json.loads(
        Path("core/infrastructure/config/flow_steward_env_manifest.json").read_text()
    )
    rows = manifest.get("variables", manifest)
    rows = rows if isinstance(rows, list) else rows.get("entries", [])
    spec = next(row for row in rows if row["name"] == "SECURITY_PII_SALT")

    assert spec["installer_managed"] is True
    assert spec["secret"] is True
    assert spec["preserve_existing"] is True, "rotating it silently would break existing pseudonyms"
    assert "production" in spec["environments"]


def test_thin_mcp_does_not_receive_the_product_pii_salt() -> None:
    environment = _services()["fs-mcp"].get("environment") or {}

    assert "SECURITY_PII_SALT" not in environment
