from __future__ import annotations

from pathlib import Path

import yaml


def test_production_compose_requires_and_propagates_release_provenance() -> None:
    compose = yaml.safe_load(Path("infra/docker/docker-compose.prod.yaml").read_text())
    services = compose["services"]

    role_digests = {
        "fs-app": "FS_APP_IMAGE_DIGEST",
        "fs-worker": "FS_WORKER_IMAGE_DIGEST",
        "control-plane": "FS_CONTROL_IMAGE_DIGEST",
    }
    for name, digest_key in role_digests.items():
        environment = services[name]["environment"]
        assert "FS_BUILD_SHA" not in environment
        assert not any(key.endswith("_IMAGE_REVISION") for key in environment)
        assert environment[digest_key].startswith(f"${{{digest_key}:?")

    assert services["fs-cli"]["environment"]["FS_BUILD_SHA"].startswith("${FS_BUILD_SHA:?")

    mcp_environment = services["fs-mcp"]["environment"]
    assert mcp_environment["FS_DEPLOYMENT_REVISION"].startswith("${MCP_IMAGE_REVISION:?")
    assert mcp_environment["FS_IMAGE_DIGEST"].startswith("${MCP_IMAGE_DIGEST:?")

    frontend_environment = services.get("fs-frontend", {}).get("environment", {})
    assert not any(key.endswith("_IMAGE_REVISION") for key in frontend_environment)


def test_app_image_targets_embed_the_same_revision_as_env_and_label() -> None:
    dockerfile = Path("infra/docker/Dockerfile").read_text()
    stage_boundaries = (
        (
            "FROM runtime-dependencies AS app-runtime",
            "FROM worker-system-tooling AS worker-runtime",
        ),
        (
            "FROM worker-system-tooling AS worker-runtime",
            "FROM control-dependencies AS control-runtime",
        ),
        ("FROM control-dependencies AS control-runtime", "FROM audit-tooling AS audit"),
        (
            "FROM frontend-dependencies AS frontend-development",
            "FROM runtime-dependencies AS worker-system-tooling",
        ),
    )

    for start, end in stage_boundaries:
        stage = dockerfile.split(start, 1)[1].split(end, 1)[0]
        assert "ARG BUILD_SHA=unknown" in stage
        assert 'LABEL org.opencontainers.image.revision="${BUILD_SHA}"' in stage
        assert "ENV FS_BUILD_SHA=${BUILD_SHA}" in stage


def test_development_compose_passes_revision_to_every_app_image_target() -> None:
    compose = yaml.safe_load(Path("infra/docker/docker-compose.yaml").read_text())
    services = compose["services"]
    expected_targets = {
        "fs-app": "app-runtime",
        "fs-frontend": "frontend-development",
        "fs-test-runner": "audit",
        "fs-audit": "audit",
        "fs-worker": "worker-runtime",
        "control-plane": "control-runtime",
    }

    for service_name, target in expected_targets.items():
        build = services[service_name]["build"]
        assert build["target"] == target
        assert build["args"]["BUILD_SHA"] == "${FS_BUILD_SHA:-unknown}"
