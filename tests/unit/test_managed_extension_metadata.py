from __future__ import annotations

import os
import stat
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from core.application.extensions.managed_release_service import ManagedReleaseService


@pytest.mark.parametrize("operation", ["install", "update"])
def test_archive_metadata_is_created_under_setgid_directory(tmp_path, monkeypatch, operation):
    """copytree clears setgid; restore it before creating installer-owned metadata."""
    from core.application.extensions.archive_new_install_service import (
        ExtensionArchiveNewInstallService,
    )
    from core.application.extensions.archive_update_service import ExtensionArchiveUpdateService

    source = tmp_path / "upload"
    source.mkdir(mode=0o755)
    source.chmod(0o755)
    (source / "extension.yaml").write_text("extension_id: example\n")
    shared_root = tmp_path / "extensions"
    shared_root.mkdir()
    shared_root.chmod(0o2775)
    staged = shared_root / ".example.staged"
    metadata = staged / ".fs-extension-install.yaml"
    context = MagicMock()
    managed = ManagedReleaseService(context=context)
    request = SimpleNamespace(
        bundle_dir=source,
        compiled={"python_requirements": []},
        metadata_filename=metadata.name,
    )
    state = SimpleNamespace(staged_dir=staged, target_dir=shared_root / "example")
    original_write = Path.write_text

    class MetadataObserved(Exception):
        pass

    def observe_creation(path, *args, **kwargs):
        if path == metadata:
            # Observe the actual directory at the real file-creation boundary,
            # not a call to the normalizer or a desired final mode.
            assert path.parent.stat().st_mode & stat.S_ISGID, (
                "Metadata would inherit the installer primary group instead of the shared writer group"
            )
            original_write(path, *args, **kwargs)
            raise MetadataObserved
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", observe_creation)
    dependencies = {
        "context": context,
        "projection": MagicMock(),
        "managed_release": managed,
        "cleanup": MagicMock(),
        "state": MagicMock(),
    }
    if operation == "install":
        install_service = ExtensionArchiveNewInstallService(**dependencies, get_logger=lambda: None)
        stage = partial(install_service._prepare_managed_release, request=request, state=state)
    else:
        update_service = ExtensionArchiveUpdateService(**dependencies)
        prepared = SimpleNamespace(
            request=request,
            staged_dir=staged,
            target_dir=state.target_dir,
        )
        stage = partial(update_service._stage_candidate_release, prepared, MagicMock())
    with pytest.raises(MetadataObserved):
        stage()
    assert yaml.safe_load(metadata.read_text())["python_dependencies"]["mode"] == "none"


def test_install_id_replacement_preserves_metadata_group(tmp_path, monkeypatch):
    bundle = tmp_path / "release"
    bundle.mkdir()
    metadata = bundle / ".fs-extension-install.yaml"
    metadata.write_text("python_dependencies: {}\n", encoding="utf-8")
    metadata.chmod(0o664)
    original_lstat = Path.lstat
    ownership = []

    def shared_metadata_stat(path):
        value = original_lstat(path)
        if path == metadata:
            fields = list(value)
            fields[5] = 10103
            return os.stat_result(fields)
        return value

    def set_descriptor_group(descriptor, uid, gid):
        ownership.append((uid, gid))
        assert stat.S_ISREG(os.fstat(descriptor).st_mode)

    # The gate is unprivileged: model only the kernel ownership boundary, keeping
    # real writes, fsync, replacement and metadata rendering in the test.
    monkeypatch.setattr(Path, "lstat", shared_metadata_stat)
    monkeypatch.setattr(os, "fchown", set_descriptor_group)
    ManagedReleaseService._record_install_id_in_bundle_metadata(
        bundle_root=bundle,
        metadata_filename=metadata.name,
        install_id="accepted-install",
    )

    assert ownership == [(-1, 10103)]
    assert yaml.safe_load(metadata.read_text()) == {
        "python_dependencies": {},
        "install_id": "accepted-install",
    }
    assert stat.S_IMODE(metadata.stat().st_mode) == 0o664
    assert list(tmp_path.iterdir()) == [bundle]


def test_install_id_group_failure_keeps_existing_metadata(tmp_path, monkeypatch):
    bundle = tmp_path / "release"
    bundle.mkdir()
    metadata = bundle / ".fs-extension-install.yaml"
    original = "python_dependencies: {}\n"
    metadata.write_text(original, encoding="utf-8")

    def deny_group_change(*args):
        raise PermissionError("group change denied")

    monkeypatch.setattr(os, "fchown", deny_group_change)
    with pytest.raises(PermissionError, match="group change denied"):
        ManagedReleaseService._record_install_id_in_bundle_metadata(
            bundle_root=bundle,
            metadata_filename=metadata.name,
            install_id="not-published",
        )
    assert metadata.read_text() == original
    assert list(tmp_path.iterdir()) == [bundle]
