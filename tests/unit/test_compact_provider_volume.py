"""Fresh provider volumes may be adopted only through a narrow opt-in contract."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from core.infrastructure.runtime.compact import volume
from core.infrastructure.runtime.compact.volume import (
    CompactVolumeLayout,
    acquire_compact_volume_lock,
    prepare_compact_volume,
)


@pytest.mark.parametrize("provider_mode", [0o2755, 0o2775])
@pytest.mark.parametrize("with_lost_found", [False, True])
def test_empty_provider_volume_is_adopted_only_when_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_mode: int,
    with_lost_found: bool,
) -> None:
    if with_lost_found:
        (tmp_path / "lost+found").mkdir()
    tmp_path.chmod(provider_mode)
    monkeypatch.setattr(volume, "_runtime_env", {"FS_COMPACT_ADOPT_EMPTY_VOLUME": "1"})

    lock = acquire_compact_volume_lock(tmp_path)
    try:
        prepare_compact_volume(tmp_path, layout=CompactVolumeLayout.for_current_user())
    finally:
        lock.release()

    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755
    assert stat.S_IMODE((tmp_path / "extensions").stat().st_mode) == 0o2775


def test_provider_volume_adoption_remains_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o2775)
    monkeypatch.setattr(volume, "_runtime_env", {})

    layout = CompactVolumeLayout.for_current_user()
    with pytest.raises(ValueError, match="root mode is unsafe"):
        prepare_compact_volume(tmp_path, layout=layout)

    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o2775
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("provider_mode", [0o2755, 0o2775])
def test_provider_volume_with_data_is_not_rewritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_mode: int,
) -> None:
    marker = tmp_path / "keep"
    marker.write_text("data", encoding="utf-8")
    tmp_path.chmod(provider_mode)
    monkeypatch.setattr(volume, "_runtime_env", {"FS_COMPACT_ADOPT_EMPTY_VOLUME": "1"})

    layout = CompactVolumeLayout.for_current_user()
    with pytest.raises(ValueError, match="root mode is unsafe"):
        prepare_compact_volume(tmp_path, layout=layout)

    assert marker.read_text(encoding="utf-8") == "data"
    assert stat.S_IMODE(tmp_path.stat().st_mode) == provider_mode
    assert not (tmp_path / "extensions").exists()


def test_provider_volume_with_recovered_lost_found_data_is_not_rewritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lost_found = tmp_path / "lost+found"
    lost_found.mkdir()
    recovered = lost_found / "recovered"
    recovered.write_text("keep", encoding="utf-8")
    tmp_path.chmod(0o2775)
    monkeypatch.setattr(volume, "_runtime_env", {"FS_COMPACT_ADOPT_EMPTY_VOLUME": "1"})

    layout = CompactVolumeLayout.for_current_user()
    with pytest.raises(ValueError, match="root mode is unsafe"):
        prepare_compact_volume(tmp_path, layout=layout)

    assert recovered.read_text(encoding="utf-8") == "keep"
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o2775
    assert not (tmp_path / "extensions").exists()


@pytest.mark.parametrize("entry_type", ["lost-found-file", "lost-found-symlink", "lock-directory"])
def test_provider_volume_rejects_allowlisted_names_with_wrong_types(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_type: str,
) -> None:
    if entry_type == "lost-found-file":
        (tmp_path / "lost+found").write_text("not a directory", encoding="utf-8")
    elif entry_type == "lost-found-symlink":
        (tmp_path / "target").mkdir()
        (tmp_path / "lost+found").symlink_to(tmp_path / "target", target_is_directory=True)
    else:
        (tmp_path / ".flow-steward-runtime.lock").mkdir()
    tmp_path.chmod(0o2775)
    monkeypatch.setattr(volume, "_runtime_env", {"FS_COMPACT_ADOPT_EMPTY_VOLUME": "1"})

    layout = CompactVolumeLayout.for_current_user()
    with pytest.raises(ValueError, match="root mode is unsafe"):
        prepare_compact_volume(tmp_path, layout=layout)

    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o2775
    assert not (tmp_path / "extensions").exists()


def test_world_writable_provider_volume_is_not_adopted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o2777)
    monkeypatch.setattr(volume, "_runtime_env", {"FS_COMPACT_ADOPT_EMPTY_VOLUME": "1"})

    layout = CompactVolumeLayout.for_current_user()
    with pytest.raises(ValueError, match="root mode is unsafe"):
        prepare_compact_volume(tmp_path, layout=layout)

    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o2777


def test_volume_lock_refuses_a_symlink_without_touching_its_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("keep", encoding="utf-8")
    (tmp_path / ".flow-steward-runtime.lock").symlink_to(target)

    with pytest.raises(ValueError, match="safe regular file"):
        acquire_compact_volume_lock(tmp_path)

    assert target.read_text(encoding="utf-8") == "keep"


def test_setgid_mode_is_applied_with_matching_effective_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_gid = os.getegid()
    current_gid = initial_gid
    target_gid = initial_gid + 1
    original_fchmod = os.fchmod
    changes: list[int] = []

    def setegid(gid: int) -> None:
        nonlocal current_gid
        changes.append(gid)
        current_gid = gid

    def without_fsetid(fd: int, mode: int) -> None:
        if mode & stat.S_ISGID and current_gid != target_gid:
            mode &= ~stat.S_ISGID
        original_fchmod(fd, mode)

    monkeypatch.setattr(volume.os, "getegid", lambda: current_gid)
    monkeypatch.setattr(volume.os, "setegid", setegid)
    monkeypatch.setattr(volume.os, "fchmod", without_fsetid)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        volume._fchmod_for_group(fd, 0o2775, target_gid)
    finally:
        os.close(fd)

    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o2775
    assert changes == [target_gid, initial_gid]


def test_effective_group_is_restored_if_chmod_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    initial_gid = os.getegid()
    changes: list[int] = []
    monkeypatch.setattr(volume.os, "setegid", changes.append)
    monkeypatch.setattr(volume.os, "fchmod", lambda *_: (_ for _ in ()).throw(OSError()))

    with pytest.raises(OSError):
        volume._fchmod_for_group(-1, 0o2775, initial_gid + 1)

    assert changes == [initial_gid + 1, initial_gid]
