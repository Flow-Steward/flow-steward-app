"""Docker management services must keep handoff probes on the Docker host."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "compose_file",
    ("docker-compose.yaml", "docker-compose.prod.yaml", "docker-compose.compact.yaml"),
)
def test_rendered_management_service_keeps_docker_host_probe_context(compose_file: str) -> None:
    image = "registry.invalid/synthetic@sha256:" + "a" * 64
    environment = {
        **os.environ,
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
    cli = json.loads(rendered.stdout)["services"]["fs-cli"]
    hosts = cli.get("extra_hosts") or {}
    if isinstance(hosts, dict):
        mapped = hosts.get("host.docker.internal")
        has_host_gateway = mapped == "host-gateway" or mapped == ["host-gateway"]
    else:
        has_host_gateway = any(
            item in {"host.docker.internal=host-gateway", "host.docker.internal:host-gateway"}
            for item in hosts
        )
    assert (cli["environment"].get("FS_INSTALLER_USE_HOST_PYTHON"), has_host_gateway) == ("0", True)
