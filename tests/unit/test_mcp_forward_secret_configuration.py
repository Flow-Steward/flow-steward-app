"""ADR 0047 decisions 13 and 19: delivery of FS_MCP_FORWARD_SECRET (checklist S3-S5).

fs-mcp proves to fs-app that an OAuth access token arrives as the MCP's own back end by
sending this secret in X-Flow-Steward-Mcp-Forward. Only those two processes may hold it,
on both profiles; nothing makes it required, so an installation without it starts with
OAuth unavailable and PATs unaffected.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
from core.infrastructure.runtime.compact import process_spec

ROOT = Path(__file__).resolve().parents[2]
NAME = "FS_MCP_FORWARD_SECRET"
_FORWARD = "compact-forward-secret-value-0123456789abcdef"  # pragma: allowlist secret


def _specs(parent: dict[str, str]) -> dict[str, process_spec.ProcessSpec]:
    settings = CompactRuntimeSettings.from_environment(
        {"FS_DEPLOYMENT_PROFILE": "compact", "FS_WORKER_PROCESSES": "2"}
    )
    specs = process_spec.build_process_specs(
        settings,
        parent_environment={
            "PATH": "/usr/bin",
            "FS_WORKER_MCP_BEARER_TOKEN": "worker-token",
            "FS_PUBLIC_BASE_URL": "https://fs.example.test",
            **parent,
        },
        public_port=8080,
        observation_fd=7,
    )
    return {spec.role: spec for spec in specs}


# --- S4: Compact -------------------------------------------------------------------


def test_compact_forward_secret_reaches_only_the_app_and_mcp_roles() -> None:
    specs = _specs({NAME: _FORWARD})

    holders = sorted(role for role, spec in specs.items() if NAME in spec.environment)

    assert holders == ["app", "mcp"]
    assert specs["app"].environment[NAME] == specs["mcp"].environment[NAME] == _FORWARD


def test_compact_app_sees_the_issuer_and_resource_the_mcp_role_advertises() -> None:
    """The authorization server's readiness compares its own issuer/resource with the
    MCP server's; on Compact both are derived from the one public origin."""
    specs = _specs({NAME: _FORWARD})

    for name in ("FS_MCP_ISSUER_URL", "FS_MCP_RESOURCE_URL"):
        assert specs["app"].environment[name] == specs["mcp"].environment[name]
    assert specs["app"].environment["FS_MCP_RESOURCE_URL"] == "https://fs.example.test/mcp"


def test_compact_without_a_forward_secret_starts_without_one() -> None:
    """An older supervisor (or a test harness) that sets none: no role gets an empty
    value; OAuth reports forward_secret_missing and PATs keep working."""
    specs = _specs({})

    assert all(NAME not in spec.environment for spec in specs.values())


def test_compact_forward_secret_is_minted_fresh_for_every_start() -> None:
    first, second = process_spec.mint_mcp_forward_secret(), process_spec.mint_mcp_forward_secret()

    assert first != second
    assert len(first) >= 32 and len(second) >= 32


def test_compact_supervisor_mints_the_forward_secret_on_start() -> None:
    """Pins the wiring, not just the helper: the supervisor's start path sets it."""
    source = (ROOT / "core/entrypoints/cli/compact_runtime.py").read_text(encoding="utf-8")

    assert "process_environment[MCP_FORWARD_SECRET_ENV] = mint_mcp_forward_secret()" in source


def test_compact_forward_secret_is_never_persisted_or_read_from_env_file() -> None:
    """It is in no role allowlist, so the runtime's self-minted durable secrets (which
    are written to the volume) never include it, and the Compact Compose overlay does
    not deliver it from .env.prod."""
    from core.infrastructure.runtime.compact.runtime_secrets import self_minted_secret_names

    assert NAME not in process_spec.delivered_environment_names()
    assert NAME not in self_minted_secret_names()
    compact_compose = (ROOT / "infra/docker/docker-compose.compact.yaml").read_text(
        encoding="utf-8"
    )
    assert NAME not in compact_compose


# --- S5: Scalable Compose --------------------------------------------------------------


@pytest.mark.parametrize("compose_file", ["docker-compose.yaml", "docker-compose.prod.yaml"])
def test_scalable_compose_gives_the_forward_secret_to_app_and_mcp_only(compose_file: str) -> None:
    services = yaml.safe_load((ROOT / "infra/docker" / compose_file).read_text(encoding="utf-8"))[
        "services"
    ]

    holders = sorted(
        name for name, service in services.items() if NAME in (service.get("environment") or {})
    )

    assert holders == ["fs-app", "fs-mcp"]
    for name in holders:
        assert services[name]["environment"][NAME] == "${FS_MCP_FORWARD_SECRET:-}"


def test_scalable_production_app_sees_any_issuer_or_resource_override() -> None:
    """An override visible to fs-mcp is visible to fs-app too, so a mismatch becomes the
    issuer_mismatch blocker instead of an authorization server and resource server that
    silently disagree."""
    services = yaml.safe_load(
        (ROOT / "infra/docker/docker-compose.prod.yaml").read_text(encoding="utf-8")
    )["services"]

    for name in ("FS_MCP_ISSUER_URL", "FS_MCP_RESOURCE_URL"):
        assert services["fs-app"]["environment"][name] == f"${{{name}:-}}"
        assert services["fs-mcp"]["environment"][name] == f"${{{name}:-}}"


def test_no_compose_file_requires_the_forward_secret() -> None:
    """Nothing is `:?`-required: an install or update without it must still start."""
    for path in sorted((ROOT / "infra/docker").rglob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"FS_MCP_FORWARD_SECRET\s*:\?", text), path


# --- S3: registry --------------------------------------------------------------------


def test_forward_secret_is_a_registered_target_owned_secret() -> None:
    from core.infrastructure.config.env_registry import CloneEnvOwner, env_spec_map

    spec = env_spec_map()[NAME]

    assert spec.secret is True
    assert spec.installer_managed is False
    assert spec.doctor_required is False
    assert spec.default == ""
    assert spec.clone_owner is CloneEnvOwner.TARGET
