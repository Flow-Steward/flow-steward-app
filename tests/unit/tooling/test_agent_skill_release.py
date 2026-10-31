"""Release archives of the agent skill packages map to one Core commit and repeat exactly.

`infra/tooling/agent-skills/build_release.py` packs each package with the plugin
root at the archive root, adds the archive-only `gemini-extension.json`, and
records the Core commit, tag and version beside the checksums.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
BUILD_SCRIPT = ROOT / "infra" / "tooling" / "agent-skills" / "build_release.py"
SKILLS_DIR = ROOT / "docs" / "agent-skills"
PACKAGES = ("flow-steward-operator", "flow-steward-developer")
CORE = {
    "repository": "Flow-Steward/flow-steward-app",
    "commit": "1" * 40,
    "tag": "v0.0.0-test",
    "version": "0.0.0",
    "commit_time": 1_790_000_000,
}


def _build_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_release", BUILD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _package_tree(package: str) -> dict[str, bytes]:
    root = SKILLS_DIR / package
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _version(package: str) -> str:
    sources = json.loads((SKILLS_DIR / "packages.json").read_text(encoding="utf-8"))
    version: str = sources["packages"][package]["version"]
    return version


@pytest.fixture(scope="module")
def release(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    output = tmp_path_factory.mktemp("release") / "out"
    manifest = _build_module().build(ROOT, CORE, output)
    return output, manifest


def test_building_twice_gives_the_same_bytes(
    release: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    first, _ = release
    _build_module().build(ROOT, CORE, tmp_path / "again")

    names = sorted(path.name for path in first.iterdir())
    assert names == sorted(path.name for path in (tmp_path / "again").iterdir())
    for name in names:
        assert (first / name).read_bytes() == (tmp_path / "again" / name).read_bytes(), name


def test_release_files_and_the_core_mapping(release: tuple[Path, dict[str, Any]]) -> None:
    output, manifest = release
    stems = [f"{package}-{_version(package)}" for package in PACKAGES]

    assert sorted(path.name for path in output.iterdir()) == sorted(
        [f"{stem}.zip" for stem in stems]
        + [f"{stem}.tar.gz" for stem in stems]
        + ["flow-steward-agent-skills-0.0.0.zip", "SHA256SUMS", "release-manifest.json"]
    )
    assert manifest["core"] == {
        key: CORE[key] for key in ("repository", "commit", "tag", "version")
    }
    assert json.loads((output / "release-manifest.json").read_text(encoding="utf-8")) == manifest
    assert [(entry["name"], entry["version"]) for entry in manifest["packages"]] == [
        (package, _version(package)) for package in PACKAGES
    ]


def test_checksums_match_the_archives(release: tuple[Path, dict[str, Any]]) -> None:
    output, manifest = release
    lines = (output / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    recorded = {line.split("  ")[1]: line.split("  ")[0] for line in lines}

    assert len(recorded) == 5
    for name, digest in recorded.items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    described = [entry for package in manifest["packages"] for entry in package["archives"]]
    for entry in [*described, manifest["marketplace"]]:
        assert recorded[entry["file"]] == entry["sha256"]
        assert (output / entry["file"]).stat().st_size == entry["bytes"]


@pytest.mark.parametrize("package", PACKAGES)
def test_package_archives_hold_the_plugin_root_and_the_gemini_manifest(
    release: tuple[Path, dict[str, Any]], package: str
) -> None:
    output, _ = release
    stem = f"{package}-{_version(package)}"
    expected = _package_tree(package)
    gemini = {"name": package, "version": _version(package)}

    with zipfile.ZipFile(output / f"{stem}.zip") as archive:
        zipped = {name: archive.read(name) for name in archive.namelist()}
        assert {info.external_attr >> 16 for info in archive.infolist()} == {0o100644}
    with tarfile.open(output / f"{stem}.tar.gz") as archive:
        members = archive.getmembers()
        tarred = {}
        for member in members:
            handle = archive.extractfile(member)
            assert handle is not None
            tarred[member.name] = handle.read()
        assert {(member.mode, member.uid, member.gid) for member in members} == {(0o644, 0, 0)}

    for files in (zipped, tarred):
        manifest = json.loads(files.pop("gemini-extension.json"))
        assert {key: manifest[key] for key in gemini} == gemini
        assert set(manifest) == {"name", "version", "description"}
        assert files == expected


def test_marketplace_archive_is_a_claude_marketplace_folder(
    release: tuple[Path, dict[str, Any]],
) -> None:
    output, _ = release
    with zipfile.ZipFile(output / "flow-steward-agent-skills-0.0.0.zip") as archive:
        files = {name: archive.read(name) for name in archive.namelist()}

    prefix = "flow-steward-agent-skills/"
    assert (
        files.pop(f"{prefix}.claude-plugin/marketplace.json")
        == (SKILLS_DIR / ".claude-plugin" / "marketplace.json").read_bytes()
    )
    for package in PACKAGES:
        for relative, content in _package_tree(package).items():
            assert files.pop(f"{prefix}{package}/{relative}") == content
    assert files == {}


def test_build_refuses_a_drifted_tree_and_a_used_output(tmp_path: Path) -> None:
    build = _build_module()
    tree = tmp_path / "tree"
    shutil.copytree(SKILLS_DIR, tree / "docs" / "agent-skills")
    icon = tree / "assets" / "frontend" / "public" / "favicon.svg"
    icon.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "assets" / "frontend" / "public" / "favicon.svg", icon)
    used = tmp_path / "used"
    used.mkdir()
    (used / "old.zip").write_bytes(b"x")

    with pytest.raises(RuntimeError, match="is not empty"):
        build.build(tree, CORE, used)

    (tree / "docs/agent-skills/flow-steward-developer/plugin.json").write_text("{}\n")
    with pytest.raises(RuntimeError, match="differ from their canonical sources"):
        build.build(tree, CORE, tmp_path / "fresh")
    assert not (tmp_path / "fresh").exists()


def test_gzip_header_carries_no_build_time(release: tuple[Path, dict[str, Any]]) -> None:
    output, _ = release
    for package in PACKAGES:
        header = (output / f"{package}-{_version(package)}.tar.gz").read_bytes()[:10]
        assert header[4:8] == b"\x00\x00\x00\x00"
        assert header[:2] == b"\x1f\x8b"
