from __future__ import annotations

import os
import shutil
import stat
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from core.infrastructure.runtime.compact import volume
from core.infrastructure.runtime.compact.volume import (
    CompactVolumeLayout,
    acquire_compact_volume_lock,
    prepare_compact_volume,
)


def _as_role(uid: int, gid: int, groups: tuple[int, ...], action: Callable[[], None]) -> bool:
    pid = os.fork()
    if pid == 0:
        try:
            os.setgroups(list(groups))
            os.setgid(gid)
            os.setuid(uid)
            action()
        except OSError:
            os._exit(1)
        os._exit(0)
    _pid, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status) == 0


@pytest.mark.skipif(os.geteuid() != 0, reason="requires disposable Linux root fixture")
def test_compact_volume_role_permissions_on_new_and_restored_tree() -> None:
    root = Path("/tmp") / f"fs-compact-permissions-{uuid.uuid4().hex}"
    root.mkdir(mode=0o755)
    prepare_compact_volume(root)
    extension_file = root / "extensions" / "release.txt"
    artifact_file = root / "artifacts" / "result.txt"

    assert _as_role(10005, 10005, (), lambda: extension_file.write_text("v1"))
    assert not _as_role(
        10001,
        10001,
        (10100, 10102),
        lambda: extension_file.write_text("tampered"),
    )
    assert _as_role(
        10002,
        10002,
        (10100, 10102),
        lambda: artifact_file.write_text("artifact"),
    )
    assert _as_role(
        10003,
        10003,
        (10100, 10102),
        lambda: (root / "artifacts" / "control.txt").write_text("control"),
    )
    # ADR 0029 decision B: every role may read extensions; only installers write.
    assert _as_role(10004, 10004, (10102,), lambda: extension_file.read_text())
    assert not _as_role(
        10004,
        10004,
        (10102,),
        lambda: (root / "artifacts" / "mcp.txt").write_text("forbidden"),
    )
    assert not _as_role(
        10002,
        10002,
        (10100, 10102),
        lambda: (root / "extensions" / "replacement").symlink_to(extension_file),
    )
    assert not _as_role(
        10001,
        10001,
        (10100, 10102),
        lambda: os.chmod(extension_file, 0o777),
    )

    prepare_compact_volume(root)
    assert extension_file.read_text() == "v1"
    assert artifact_file.read_text() == "artifact"
    shutil.rmtree(root)


@pytest.mark.skipif(os.geteuid() != 0, reason="requires disposable Linux root fixture")
def test_compact_adopts_fresh_provider_owned_volume_with_current_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path("/tmp") / f"fs-compact-provider-volume-{uuid.uuid4().hex}"
    layout = CompactVolumeLayout()
    root.mkdir(mode=0o755)
    os.chown(root, 0, 12345)
    root.chmod(0o2775)
    monkeypatch.setattr(volume, "_runtime_env", {"FS_COMPACT_ADOPT_EMPTY_VOLUME": "1"})

    try:
        lock = acquire_compact_volume_lock(root)
        try:
            prepare_compact_volume(root)
        finally:
            lock.release()

        root_metadata = root.stat()
        extension_metadata = (root / "extensions").stat()
        artifact_metadata = (root / "artifacts").stat()
        assert (root_metadata.st_uid, root_metadata.st_gid) == (
            layout.root_uid,
            layout.root_gid,
        )
        assert stat.S_IMODE(root_metadata.st_mode) == 0o755
        assert (extension_metadata.st_uid, extension_metadata.st_gid) == (
            layout.installer_uid,
            layout.extension_gid,
        )
        assert stat.S_IMODE(extension_metadata.st_mode) == 0o2775
        assert (artifact_metadata.st_uid, artifact_metadata.st_gid) == (
            layout.operator_uid,
            layout.artifact_gid,
        )
        assert stat.S_IMODE(artifact_metadata.st_mode) == 0o2770
    finally:
        shutil.rmtree(root, ignore_errors=True)
