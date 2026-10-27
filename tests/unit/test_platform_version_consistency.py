"""Canonical platform version contracts for release-owned surfaces."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLATFORM_FILES = (
    Path("VERSION"),
    Path("assets/frontend/package.json"),
    Path("assets/frontend/package-lock.json"),
    Path("core/infrastructure/config/flow_steward_env_manifest.json"),
    Path("infra/docker/docker-compose.yaml"),
    Path("infra/docker/docker-compose.compact.yaml"),
    Path("infra/docker/docker-compose.prod.yaml"),
    Path(".env.example.prod"),
)
RELEASE_REPOSITORY = "Flow-Steward/flow-steward-app"


def _manifest_default(repo_root: Path, name: str) -> str:
    payload = json.loads(
        (repo_root / "core/infrastructure/config/flow_steward_env_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    matches = [item for item in payload["variables"] if item["name"] == name]
    assert len(matches) == 1
    return str(matches[0].get("default") or "")


def _env_default(repo_root: Path, name: str) -> str:
    prefix = f"{name}="
    matches = [
        line.removeprefix(prefix)
        for line in (repo_root / ".env.example.prod").read_text(encoding="utf-8").splitlines()
        if line.startswith(prefix)
    ]
    assert len(matches) == 1
    return matches[0]


def _env_default_or_absent(repo_root: Path, name: str) -> str | None:
    prefix = f"{name}="
    matches = [
        line.removeprefix(prefix)
        for line in (repo_root / ".env.example.prod").read_text(encoding="utf-8").splitlines()
        if line.startswith(prefix)
    ]
    assert len(matches) <= 1
    return matches[0] if matches else None


def _compose_default(
    repo_root: Path, name: str, compose_file: str = "infra/docker/docker-compose.yaml"
) -> str:
    source = (repo_root / compose_file).read_text(encoding="utf-8")
    matches = re.findall(rf"\$\{{{re.escape(name)}:-([^}}]+)}}", source)
    assert matches
    assert len(set(matches)) == 1
    return matches[0]


def _assert_platform_release_surfaces(repo_root: Path, version: str) -> None:
    package = json.loads((repo_root / "assets/frontend/package.json").read_text())
    package_lock = json.loads((repo_root / "assets/frontend/package-lock.json").read_text())
    assert package["version"] == version
    assert package_lock["version"] == version
    assert package_lock["packages"][""]["version"] == version
    assert _compose_default(repo_root, "FS_CLI_IMAGE") == f"flow-steward-cli:{version}"
    assert _manifest_default(repo_root, "FS_APP_IMAGE") == f"flow-steward-app:{version}"
    assert _manifest_default(repo_root, "FS_WORKER_IMAGE") == f"flow-steward-worker:{version}"
    assert _manifest_default(repo_root, "FS_CONTROL_IMAGE") == f"flow-steward-control:{version}"
    assert _manifest_default(repo_root, "MCP_IMAGE") == f"flow-steward-mcp:{version}"
    # Production image refs are resolved from the verified official release and
    # written as digests by the installer. The checked-in production example must
    # never suggest mutable local tags as an alternative source of truth.
    assert _env_default(repo_root, "FS_APP_IMAGE") == ""
    assert _env_default(repo_root, "FS_WORKER_IMAGE") == ""
    assert _env_default(repo_root, "FS_CONTROL_IMAGE") == ""
    assert _env_default_or_absent(repo_root, "MCP_IMAGE") in {None, ""}


def test_platform_release_surfaces_follow_canonical_version() -> None:
    from core.entrypoints.cli.platform.click_adapter import _read_version
    from core.infrastructure.config.version import APP_VERSION

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert version == APP_VERSION
    assert _read_version() == version
    _assert_platform_release_surfaces(ROOT, version)


def test_release_repository_default_matches_all_production_surfaces() -> None:
    from core.application.release.catalog import DEFAULT_RELEASE_REPOSITORY

    assert DEFAULT_RELEASE_REPOSITORY == RELEASE_REPOSITORY
    assert _manifest_default(ROOT, "FS_RELEASE_REPOSITORY") == RELEASE_REPOSITORY
    assert _env_default(ROOT, "FS_RELEASE_REPOSITORY") == RELEASE_REPOSITORY
    for compose_file in (
        "infra/docker/docker-compose.compact.yaml",
        "infra/docker/docker-compose.prod.yaml",
    ):
        assert _compose_default(ROOT, "FS_RELEASE_REPOSITORY", compose_file) == RELEASE_REPOSITORY


def _any_extension_manifest(repo_root: Path) -> Path | None:
    """Return some shipped bundle manifest, or None when no bundle ships.

    Which bundle is irrelevant: the rule under test is that an extension's version
    is its own, so naming one would make this a statement about that bundle.
    """
    return next(iter(sorted((repo_root / "extensions").glob("*/extension.yaml"))), None)


def test_independently_versioned_postgres_and_extensions_are_not_platform_tags() -> None:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    postgres_image = _manifest_default(ROOT, "POSTGRES_IMAGE")
    assert postgres_image != f"flow-steward:{version}"
    assert "pgvector-0.8.5" in postgres_image
    manifest = _any_extension_manifest(ROOT)
    if manifest is not None:
        assert not re.search(
            rf"(?m)^version:\s*{re.escape(version)}\s*$",
            manifest.read_text(encoding="utf-8"),
        )


def _prepare_fixture_version(repo_root: Path, version: str) -> None:
    (repo_root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    for relative in ("assets/frontend/package.json", "assets/frontend/package-lock.json"):
        path = repo_root / relative
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["version"] = version
        if relative.endswith("package-lock.json"):
            payload["packages"][""]["version"] = version
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    manifest_path = repo_root / "core/infrastructure/config/flow_steward_env_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    defaults = {
        "FS_APP_IMAGE": f"flow-steward-app:{version}",
        "FS_WORKER_IMAGE": f"flow-steward-worker:{version}",
        "FS_CONTROL_IMAGE": f"flow-steward-control:{version}",
        "MCP_IMAGE": f"flow-steward-mcp:{version}",
    }
    for variable in manifest["variables"]:
        if variable["name"] in defaults:
            variable["default"] = defaults[variable["name"]]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    compose_path = repo_root / "infra/docker/docker-compose.yaml"
    compose = compose_path.read_text(encoding="utf-8")
    compose = re.sub(
        r"\$\{FS_CLI_IMAGE:-flow-steward-cli:[^}]+}",
        f"${{FS_CLI_IMAGE:-flow-steward-cli:{version}}}",
        compose,
    )
    compose_path.write_text(compose, encoding="utf-8")


@pytest.fixture
def candidate_fixture(tmp_path: Path) -> Path:
    for relative in PLATFORM_FILES:
        source = ROOT / relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return tmp_path


def test_candidate_version_rehearsal_updates_only_platform_surfaces(
    candidate_fixture: Path,
) -> None:
    postgres_before = _manifest_default(candidate_fixture, "POSTGRES_IMAGE")
    manifest = _any_extension_manifest(ROOT)
    extension_source = manifest.read_text(encoding="utf-8") if manifest else None

    _prepare_fixture_version(candidate_fixture, "0.2.0")

    _assert_platform_release_surfaces(candidate_fixture, "0.2.0")
    assert _manifest_default(candidate_fixture, "POSTGRES_IMAGE") == postgres_before
    if manifest is not None:
        assert manifest.read_text(encoding="utf-8") == extension_source
