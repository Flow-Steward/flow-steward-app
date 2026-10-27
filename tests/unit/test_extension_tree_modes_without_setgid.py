"""An extension tree on a filesystem that refuses the setgid bit still installs."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

from core.application.extensions.archive_lifecycle_capability import (
    ExtensionArchiveLifecycleCapability,
)
from core.application.extensions.managed_release_service import ManagedReleaseService
from core.application.management.errors import ServiceUnavailableError
from core.infrastructure.extension_runtime.tree_modes import (
    EXTENSION_DIRECTORY_MODE,
    EXTENSION_DIRECTORY_MODE_WITHOUT_SETGID,
    apply_extension_directory_mode,
)


def _refuse_setgid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Behave like Docker Desktop's macOS file sharing: setgid is EPERM, the rest passes."""
    real_chmod = os.chmod

    def chmod(path, mode, *args, **kwargs):
        if mode & stat.S_ISGID:
            raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", chmod)


def test_the_tree_mode_keeps_setgid_where_the_filesystem_allows_it() -> None:
    applied: list[int] = []

    assert apply_extension_directory_mode(applied.append) == EXTENSION_DIRECTORY_MODE
    assert applied == [EXTENSION_DIRECTORY_MODE]


def test_the_tree_mode_drops_only_setgid_where_the_filesystem_refuses_it() -> None:
    applied: list[int] = []

    def set_mode(mode: int) -> None:
        if mode & stat.S_ISGID:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        applied.append(mode)

    assert apply_extension_directory_mode(set_mode) == EXTENSION_DIRECTORY_MODE_WITHOUT_SETGID
    assert applied == [0o775]


def test_a_denied_write_is_not_mistaken_for_a_refused_setgid() -> None:
    def set_mode(_mode: int) -> None:
        raise PermissionError(errno.EACCES, "Permission denied")

    with pytest.raises(PermissionError) as captured:
        apply_extension_directory_mode(set_mode)
    assert captured.value.errno == errno.EACCES


def test_a_release_tree_normalizes_on_a_filesystem_that_refuses_setgid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "handlers").mkdir(parents=True)
    (bundle / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (bundle / "handlers" / "actions.py").write_text("X = 1\n", encoding="utf-8")
    _refuse_setgid(monkeypatch)

    ManagedReleaseService._normalize_managed_release_tree_modes(bundle)

    assert stat.S_IMODE(bundle.stat().st_mode) == 0o775
    assert stat.S_IMODE((bundle / "handlers").stat().st_mode) == 0o775
    assert stat.S_IMODE((bundle / "main.py").stat().st_mode) == 0o664


def test_a_refused_operation_is_named_not_blamed_on_the_deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FS_DEPLOYMENT_PROFILE", "scalable")
    capability = ExtensionArchiveLifecycleCapability(root_provider=lambda: tmp_path)
    refused = PermissionError(
        errno.EPERM, "Operation not permitted", str(tmp_path / ".acme.install-staged-1")
    )

    with pytest.raises(ServiceUnavailableError) as captured:
        capability.translate_managed_root_error(refused, operation="install_update")

    message = str(captured.value)
    assert ".acme.install-staged-1" in message
    assert "Operation not permitted" in message
    assert "unavailable in this deployment" not in message
    assert str(tmp_path) not in message
