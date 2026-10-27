"""DEV-HOOK-01: rendered scalable roles receive the same installation authority."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "compose_file",
    ("docker-compose.yaml", "docker-compose.prod.yaml"),
)
@pytest.mark.parametrize(
    ("markers", "expected"),
    [
        ({}, {"ENVIRONMENT": "production", "FS_STACK_ENVIRONMENT": "production"}),
        (
            {"ENVIRONMENT": "development"},
            {"ENVIRONMENT": "development", "FS_STACK_ENVIRONMENT": "development"},
        ),
        (
            {"FS_STACK_ENVIRONMENT": "local_development"},
            {"ENVIRONMENT": "local_development", "FS_STACK_ENVIRONMENT": "local_development"},
        ),
        (
            {"ENVIRONMENT": "development", "FS_STACK_ENVIRONMENT": "development"},
            {"ENVIRONMENT": "development", "FS_STACK_ENVIRONMENT": "development"},
        ),
        (
            {"ENVIRONMENT": "development", "FS_STACK_ENVIRONMENT": "production"},
            {"ENVIRONMENT": "development", "FS_STACK_ENVIRONMENT": "production"},
        ),
        (
            {"ENVIRONMENT": "unknown", "FS_STACK_ENVIRONMENT": "development"},
            {"ENVIRONMENT": "unknown", "FS_STACK_ENVIRONMENT": "development"},
        ),
        (
            {"ENVIRONMENT": "", "FS_STACK_ENVIRONMENT": "development"},
            {"ENVIRONMENT": "", "FS_STACK_ENVIRONMENT": "development"},
        ),
    ],
)
def test_rendered_scalable_roles_keep_canonical_environment_markers(
    compose_file: str, markers: dict[str, str], expected: dict[str, str]
) -> None:
    image = "registry.invalid/synthetic@sha256:" + "a" * 64
    environment = {
        **{
            name: value
            for name, value in os.environ.items()
            if name not in {"ENVIRONMENT", "FS_STACK_ENVIRONMENT"}
        },
        **markers,
        # A host shell choice must not misidentify a Docker management process.
        "FS_INSTALLER_USE_HOST_PYTHON": "1",
        "FS_APP_IMAGE": image,
        "FS_WORKER_IMAGE": image,
        "FS_CONTROL_IMAGE": image,
        "FS_CLI_IMAGE": image,
        "MCP_IMAGE": image,
        "POSTGRES_IMAGE": image,
        "FS_COMPACT_IMAGE": image,
        "FS_APP_IMAGE_DIGEST": "sha256:" + "a" * 64,
        "FS_WORKER_IMAGE_DIGEST": "sha256:" + "a" * 64,
        "FS_CONTROL_IMAGE_DIGEST": "sha256:" + "a" * 64,
        "MCP_IMAGE_DIGEST": "sha256:" + "a" * 64,
        "FS_COMPACT_IMAGE_INDEX_DIGEST": "sha256:" + "a" * 64,
        "FS_COMPACT_IMAGE_MANIFEST_DIGEST": "sha256:" + "a" * 64,
        "FS_COMPACT_IMAGE_CONFIG_DIGEST": "sha256:" + "a" * 64,
        "FS_COMPACT_IMAGE_PLATFORM": "linux/amd64",
        "FS_COMPACT_IMAGE_REVISION": "a" * 40,
        "FS_BUILD_SHA": "a" * 40,
        "MCP_IMAGE_REVISION": "a" * 40,
        "FS_WORKER_PROCESSES": "1",
        "FS_CANDIDATE_STATE_DIR": "/tmp/synthetic-state",
        "FS_CANDIDATE_PROD_ENV": "/tmp/synthetic.env.prod",
        "FS_WORKER_MCP_BEARER_TOKEN": "synthetic-test-value",
        "FS_METRICS_TOKEN": "synthetic-test-value",
        "SECURITY_PII_SALT": "synthetic-test-value",
    }
    rendered = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            os.devnull,
            "-f",
            str(ROOT / "infra/docker" / compose_file),
            "--profile",
            "cli",
            "config",
            "--no-env-resolution",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert rendered.returncode == 0, rendered.stderr
    services = json.loads(rendered.stdout)["services"]
    for role in ("fs-app", "fs-mcp", "fs-cli"):
        actual = services[role]["environment"]
        assert {marker: actual.get(marker) for marker in expected} == expected, role
