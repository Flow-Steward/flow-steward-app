from __future__ import annotations

import secrets
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(scope="module")
def built_images() -> Iterator[dict[str, str]]:
    token = secrets.token_hex(8)
    images = {
        target: f"fs-extension-boundary-{token}-{target}:acceptance"
        for target in ("cli", "runtime", "audit")
    }
    try:
        for target, image in images.items():
            completed = subprocess.run(
                [
                    "docker",
                    "build",
                    "--target",
                    target,
                    "--build-arg",
                    f"FS_BUILD_REVISION={token}",
                    "--build-arg",
                    f"BUILD_SHA={token}",
                    "--tag",
                    image,
                    "--file",
                    "infra/docker/Dockerfile",
                    ".",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=1_800,
                check=False,
            )
            assert completed.returncode == 0, completed.stderr
        yield images
    finally:
        for image in images.values():
            subprocess.run(
                ["docker", "image", "rm", image],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )


def _python_import(image: str, module: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            image,
            "-c",
            f"import {module}",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize("target", ("cli", "runtime"))
@pytest.mark.parametrize("forbidden", ("pytest", "playwright", "semgrep"))
def test_lean_image_omits_test_and_audit_packages(
    built_images: dict[str, str],
    target: str,
    forbidden: str,
) -> None:
    assert _python_import(built_images[target], forbidden).returncode != 0


def test_development_tool_image_contains_required_tooling(
    built_images: dict[str, str],
) -> None:
    assert _python_import(built_images["audit"], "pytest").returncode == 0
    assert _python_import(built_images["audit"], "playwright").returncode == 0
    semgrep = subprocess.run(
        ["docker", "run", "--rm", built_images["audit"], "semgrep", "--version"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert semgrep.returncode == 0, semgrep.stderr


def test_compose_keeps_test_tooling_development_only() -> None:
    development = yaml.safe_load(
        (ROOT / "infra/docker/docker-compose.yaml").read_text(encoding="utf-8")
    )["services"]
    production = yaml.safe_load(
        (ROOT / "infra/docker/docker-compose.prod.yaml").read_text(encoding="utf-8")
    )["services"]

    assert development["fs-test-runner"]["profiles"] == ["test"]
    assert development["fs-audit"]["profiles"] == ["audit"]
    assert {"fs-test-runner", "fs-audit"}.isdisjoint(production)
