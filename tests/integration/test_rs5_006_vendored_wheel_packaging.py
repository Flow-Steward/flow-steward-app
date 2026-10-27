"""The app runtime materializes a bundle's vendored wheels, and verifies them.

This is the real path a deployment takes: the `app-runtime` image, with the
extensions directory mounted at `/app/extensions`, running the same
`bundled_dependencies` command the `extension-dependencies` compose service runs.
The target and the command are read from the compose file so a rename shows up as
a failing assertion here rather than as a docker error — the earlier version of
this test asked for a `runtime` stage that had not existed since the image was
split per role, and was RED on every run.

The bundle is synthetic and built under `tmp_path`, so this exercises whatever
ships, including nothing.
"""

from __future__ import annotations

import os
import secrets
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.support.vendored_wheels import DEPENDENCY_WHEEL_NAME, write_vendored_wheel_bundle

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "infra" / "docker" / "docker-compose.yaml"
#: The compose service whose whole job is materializing extension dependencies.
DEPENDENCY_SERVICE = "extension-dependencies"
PYTHON_RUNTIME_IMAGE = (
    "python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a"
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _dependency_service() -> dict:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    service = (compose.get("services") or {}).get(DEPENDENCY_SERVICE)
    assert isinstance(service, dict), (
        f"{DEPENDENCY_SERVICE} is no longer a compose service; "
        "the deployment no longer materializes extension dependencies this way"
    )
    return service


def _build_target() -> str:
    target = str((_dependency_service().get("build") or {}).get("target") or "").strip()
    assert target, f"{DEPENDENCY_SERVICE} declares no build target"
    return target


def _run_as_user() -> list[str]:
    """Run as the host user, the way the compose service does.

    The compose service declares `user: "${FS_HOST_UID}:${FS_HOST_GID}"` because
    the extensions directory is a host bind mount. Running as the image's `fs`
    user instead works on Docker Desktop, where the VM maps ownership, and fails
    on Linux CI where a `tmp_path` owned by the host user is not writable by `fs`
    — green locally, red on the machine that matters.
    """
    declared = str(_dependency_service().get("user") or "").strip()
    assert declared, f"{DEPENDENCY_SERVICE} no longer declares a user"
    return ["--user", f"{os.getuid()}:{os.getgid()}"]


def _materialize_command() -> list[str]:
    command = _dependency_service().get("command")
    assert isinstance(command, list), f"{DEPENDENCY_SERVICE} declares no command"
    assert command, f"{DEPENDENCY_SERVICE} declares no command"
    return [str(part) for part in command]


def test_installed_extension_dependencies_materialize_at_runtime_from_clean_artifacts() -> None:
    """The image ships no extension, so nothing is materialized while it builds.

    Dependencies of the extensions installed on a stand are materialized at runtime
    by the dependency service, and a checkout's own .fs-python sites never enter a
    build context.
    """
    dockerfile = (ROOT / "infra" / "docker" / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "infra" / "docker" / "docker-compose.yaml").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "core.infrastructure.extension_runtime.bundled_dependencies" not in dockerfile
    assert "core.infrastructure.extension_runtime.bundled_dependencies" in compose
    assert "**/.fs-python/" in dockerignore


def test_the_dependency_service_targets_a_stage_that_exists() -> None:
    """A renamed stage must fail here, with what is wrong, not inside docker."""
    dockerfile = (ROOT / "infra" / "docker" / "Dockerfile").read_text(encoding="utf-8")
    stages = {
        line.split(" AS ")[-1].strip()
        for line in dockerfile.splitlines()
        if line.startswith("FROM ") and " AS " in line
    }

    assert _build_target() in stages, f"{_build_target()} is not a stage in the Dockerfile"


def _native_platform() -> str:
    """Return the Docker daemon's own architecture.

    Building the other one here means emulation, and the frontend build segfaults
    under QEMU (`npm run build` exits 139) — a parameter that is permanently RED
    for a reason that has nothing to do with what is under test. The other
    architecture is covered where it is native: a CI runner of that architecture.
    """
    completed = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Arch}}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    arch = (completed.stdout or "").strip()
    assert completed.returncode == 0, completed.stderr or "docker daemon is unavailable"
    assert arch, completed.stderr or "docker daemon is unavailable"
    return f"linux/{arch}"


def _runtime_permission_probe(bundle_name: str) -> str:
    return (
        "from contextlib import suppress\n"
        "from pathlib import Path\n"
        "targets = [\n"
        f" Path('/app/extensions/{bundle_name}/extension.yaml'),\n"
        "]\n"
        "denied = []\n"
        "for target in targets:\n"
        " try:\n"
        "  target.chmod(0o644)\n"
        " except OSError:\n"
        "  denied.append((str(target), 'chmod'))\n"
        " try:\n"
        "  target.write_text('attacker-controlled')\n"
        " except OSError:\n"
        "  denied.append((str(target), 'write'))\n"
        " replacement = target.with_name(target.name + '.attacker')\n"
        " try:\n"
        "  replacement.write_text('attacker-controlled')\n"
        "  replacement.replace(target)\n"
        " except OSError:\n"
        "  denied.append((str(target), 'replace'))\n"
        " finally:\n"
        "  with suppress(OSError):\n"
        "   replacement.unlink()\n"
        "assert len(denied) == len(targets) * 3, denied\n"
    )


def test_runtime_read_only_mounts_reject_hostile_bundle_mutation(tmp_path: Path) -> None:
    extensions_root = tmp_path / "extensions"
    bundle = write_vendored_wheel_bundle(extensions_root)
    attempted = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "1000:1000",
            "--volume",
            f"{extensions_root}:/app/extensions:ro",
            "--entrypoint",
            "python",
            PYTHON_RUNTIME_IMAGE,
            "-c",
            _runtime_permission_probe(bundle.name),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert attempted.returncode == 0, attempted.stderr


def test_the_app_runtime_materializes_and_imports_a_vendored_wheel(tmp_path: Path) -> None:
    platform = _native_platform()
    architecture = platform.replace("/", "-")
    image = f"fs-vendored-wheels-{architecture}-{secrets.token_hex(6)}:acceptance"
    extensions_root = tmp_path / "extensions"
    bundle = write_vendored_wheel_bundle(extensions_root)
    runtime_permission_probe = _runtime_permission_probe(bundle.name)
    try:
        built = subprocess.run(
            [
                "docker",
                "buildx",
                "build",
                "--platform",
                platform,
                "--target",
                _build_target(),
                "--load",
                "--tag",
                image,
                "--file",
                "infra/docker/Dockerfile",
                ".",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=3600,
            check=False,
        )
        assert built.returncode == 0, built.stderr

        materialized = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                platform,
                *_run_as_user(),
                "--volume",
                f"{extensions_root}:/app/extensions",
                "--entrypoint",
                _materialize_command()[0],
                image,
                *_materialize_command()[1:],
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        assert materialized.returncode == 0, materialized.stderr

        imported = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                platform,
                *_run_as_user(),
                "--volume",
                f"{extensions_root}:/app/extensions:ro",
                "--entrypoint",
                "python",
                image,
                "-c",
                (
                    "from pathlib import Path; "
                    "from core.infrastructure.extension_runtime.extension_runtime import "
                    "run_extension_health, scan_extensions; "
                    "rows=scan_extensions(Path('/app/extensions')); "
                    "assert len(rows) == 1, rows; "
                    "health=run_extension_health(rows[0]); "
                    f"assert '{DEPENDENCY_WHEEL_NAME}' in health['detail'], health; "
                    f"exec({runtime_permission_probe!r})"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        assert imported.returncode == 0, imported.stderr

        # A wheel whose bytes do not match the declared hash must be refused.
        manifest_path = bundle / "extension.yaml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest["python_requirements"][0]["hashes"][0] = "sha256:" + ("0" * 64)
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        corrupt = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                platform,
                *_run_as_user(),
                "--volume",
                f"{extensions_root}:/app/extensions",
                "--entrypoint",
                _materialize_command()[0],
                image,
                *_materialize_command()[1:],
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        assert corrupt.returncode != 0, corrupt.stdout
    finally:
        subprocess.run(
            ["docker", "image", "rm", "-f", image],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
