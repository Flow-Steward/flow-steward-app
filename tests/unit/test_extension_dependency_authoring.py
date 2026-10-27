from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml

from core.application.tooling.extensions import deps

_LOCKED_REQUIREMENTS = [
    {
        "name": "demo",
        "version": "1.0.0",
        "hashes": [f"sha256:{'a' * 64}"],
    }
]


def _request(tmp_path: Path) -> tuple[deps.ExtensionDependencyLockRequest, Path, str]:
    extensions_root = tmp_path / "extensions"
    bundle_root = extensions_root / "acme_atomic_deps"
    bundle_root.mkdir(parents=True)
    manifest_path = bundle_root / "extension.yaml"
    original_manifest = "extension_id: acme.atomic-deps\npython_requirements: []\n"
    manifest_path.write_text(original_manifest, encoding="utf-8")
    manifest_path.chmod(0o640)
    requirements_path = bundle_root / "requirements.in"
    requirements_path.write_text("demo==1.0.0\n", encoding="utf-8")
    request = deps.ExtensionDependencyLockRequest(
        extension_id="acme.atomic-deps",
        root=extensions_root,
    )
    return request, manifest_path, original_manifest


def test_dependency_lock_publish_failure_preserves_complete_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, manifest_path, original_manifest = _request(tmp_path)
    monkeypatch.setattr(deps, "lock_direct_requirements", lambda **_kwargs: _LOCKED_REQUIREMENTS)

    def fail_publish(_source: object, _destination: object) -> None:
        raise OSError("injected manifest publish failure")

    monkeypatch.setattr(os, "replace", fail_publish)

    with pytest.raises(OSError, match="injected manifest publish failure"):
        deps.lock_extension_dependencies(request)

    assert manifest_path.read_text(encoding="utf-8") == original_manifest
    assert list(manifest_path.parent.glob(".extension.yaml.*.tmp")) == []


def test_dependency_lock_atomically_replaces_fsynced_manifest_in_same_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, manifest_path, _original_manifest = _request(tmp_path)
    monkeypatch.setattr(deps, "lock_direct_requirements", lambda **_kwargs: _LOCKED_REQUIREMENTS)
    real_replace = os.replace
    real_fsync = os.fsync
    replace_calls: list[tuple[Path, Path]] = []
    events: list[str] = []

    def tracking_replace(source: object, destination: object) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        assert source_path.parent == manifest_path.parent
        assert source_path.is_file()
        events.append("replace")
        replace_calls.append((source_path, destination_path))
        real_replace(source, destination)

    def tracking_fsync(fd: int) -> None:
        descriptor_mode = os.fstat(fd).st_mode
        if stat.S_ISREG(descriptor_mode):
            events.append("fsync_file")
        elif stat.S_ISDIR(descriptor_mode):
            events.append("fsync_directory")
        else:
            pytest.fail(f"unexpected fsync descriptor type: {descriptor_mode:o}")
        real_fsync(fd)

    monkeypatch.setattr(os, "replace", tracking_replace)
    monkeypatch.setattr(os, "fsync", tracking_fsync)

    result = deps.lock_extension_dependencies(request)

    assert result.status == "locked"
    assert result.exit_code == 0
    assert len(replace_calls) == 1
    assert replace_calls[0][1] == manifest_path
    assert events == ["fsync_file", "replace", "fsync_directory"]
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["python_requirements"] == _LOCKED_REQUIREMENTS
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o640
    assert list(manifest_path.parent.glob(".extension.yaml.*.tmp")) == []
