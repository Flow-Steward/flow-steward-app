from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = (
    ROOT / "infra/docker/docker-compose.yaml",
    ROOT / "infra/docker/docker-compose.prod.yaml",
)


def _compose_project_directory(tmp_path: Path) -> Path:
    """A task-owned project directory mirroring the repository layout.

    Relative Compose paths resolve against the project directory, so the development
    file's ``env_file: ../../.env`` reads the explicit test env file written here
    instead of depending on a host ``.env`` (absent from the core-only test tree).
    """
    project_root = tmp_path / "compose-project"
    project_directory = project_root / "infra/docker"
    project_directory.mkdir(parents=True)
    (project_root / ".env").write_text("FS_RUNTIME_ENVIRONMENT=development\n", encoding="utf-8")
    return project_directory


def _compose_env(tmp_path: Path) -> dict[str, str]:
    candidate_env = tmp_path / "candidate-production.env"
    candidate_env.write_text("FS_RUNTIME_ENVIRONMENT=production\n", encoding="utf-8")
    candidate_state = tmp_path / "candidate-state"
    candidate_state.mkdir()
    revision = "a" * 40

    # Every variable the two Compose files require (`${NAME:?...}`), with test values.
    return {
        **os.environ,
        "FS_APP_IMAGE": "flow-steward-app:test",
        "FS_APP_IMAGE_DIGEST": f"sha256:{'1' * 64}",
        "FS_BUILD_SHA": revision,
        "FS_CANDIDATE_PROD_ENV": str(candidate_env),
        "FS_CANDIDATE_STATE_DIR": str(candidate_state),
        "FS_CLI_IMAGE": "flow-steward-cli:test",
        "FS_CONTROL_IMAGE": "flow-steward-control:test",
        "FS_CONTROL_IMAGE_DIGEST": f"sha256:{'3' * 64}",
        "FS_METRICS_TOKEN": "test-metrics-token",
        "FS_WORKER_IMAGE": "flow-steward-worker:test",
        "FS_WORKER_IMAGE_DIGEST": f"sha256:{'4' * 64}",
        "FS_WORKER_MCP_BEARER_TOKEN": "test-worker-token",
        "MCP_IMAGE": "flow-steward-mcp:test",
        "MCP_IMAGE_DIGEST": f"sha256:{'2' * 64}",
        "MCP_IMAGE_REVISION": revision,
        "POSTGRES_IMAGE": "fs-postgres-extended:test",
        "POSTGRES_DB": "flow_steward",
        "POSTGRES_PASSWORD": "test-postgres-password",
        "POSTGRES_USER": "flow_steward",
        "SECURITY_PII_SALT": "test-pii-salt",
    }


def _render_compose(compose_file: Path, tmp_path: Path) -> dict[str, object]:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is required to render the Compose contract")

    completed = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "--project-directory",
            str(_compose_project_directory(tmp_path)),
            "--profile",
            "*",
            "config",
            "--format",
            "json",
        ],
        cwd=tmp_path,
        env=_compose_env(tmp_path),
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict)
    return payload


@pytest.mark.parametrize("compose_file", COMPOSE_FILES, ids=lambda path: path.name)
def test_rendered_compose_uses_only_postgres_search_topology(
    compose_file: Path,
    tmp_path: Path,
) -> None:
    payload = _render_compose(compose_file, tmp_path)
    serialized = json.dumps(payload, sort_keys=True).lower()
    services = payload["services"]

    retired_graph_backend = "neo" + "4j"
    assert retired_graph_backend not in serialized
    assert "pg-fs" in services
    assert "memory-postgres-extensions" in services
    assert services["fs-app"]["environment"]["FS_MEMORY_VECTOR_BACKEND"] == ("postgres_pgvector")
    assert services["fs-app"]["environment"]["FS_CONTEXT_GRAPH_BACKEND"] == ("postgres_age")


def _project_resources(project: str) -> dict[str, list[str]]:
    commands: Mapping[str, list[str]] = {
        "containers": [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{.Names}}",
        ],
        "networks": [
            "docker",
            "network",
            "ls",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{.Name}}",
        ],
        "volumes": [
            "docker",
            "volume",
            "ls",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{.Name}}",
        ],
    }
    return {
        kind: subprocess.run(command, check=True, capture_output=True, text=True)
        .stdout.strip()
        .splitlines()
        for kind, command in commands.items()
    }


@pytest.mark.integration
def test_production_postgres_topology_starts_and_tears_down_cleanly(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is required for the focused startup proof")

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    image = f"fs-postgres-extended:{version}-pgvector-0.8.5"
    inspected = subprocess.run(
        ["docker", "image", "inspect", image],
        check=False,
        capture_output=True,
        text=True,
    )
    if inspected.returncode != 0:
        pytest.skip(f"prepared PostgreSQL image is unavailable: {image}")

    project = f"fs-pgonly-{uuid4().hex[:10]}"
    compose_file = ROOT / "infra/docker/docker-compose.prod.yaml"
    env = _compose_env(tmp_path)
    env["POSTGRES_IMAGE"] = image
    prefix = ["docker", "compose", "-p", project, "-f", str(compose_file)]
    started_at = time.monotonic()
    resources: dict[str, list[str]] = {}
    elapsed_seconds = 0.0
    try:
        subprocess.run(
            [*prefix, "up", "-d", "--no-build", "--wait", "pg-fs"],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
        resources = _project_resources(project)
        assert resources["containers"] == [f"{project}-pg-fs-1"]
        retired_graph_backend = "neo" + "4j"
        assert retired_graph_backend not in "\n".join(
            name.lower() for names in resources.values() for name in names
        )
        elapsed_seconds = time.monotonic() - started_at
        assert elapsed_seconds < 180
    finally:
        subprocess.run(
            [*prefix, "down", "--volumes", "--remove-orphans", "--timeout", "10"],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )

    post_teardown = _project_resources(project)
    assert post_teardown == {
        "containers": [],
        "networks": [],
        "volumes": [],
    }
    print(
        "postgres_only_startup_evidence="
        + json.dumps(
            {
                "project": project,
                "image": image,
                "elapsed_seconds": round(elapsed_seconds, 3),
                "started_resources": resources,
                "post_teardown": post_teardown,
            },
            sort_keys=True,
        )
    )
