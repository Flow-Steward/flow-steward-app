"""Compact capture must not replay source permissions onto host-backed staging."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import subprocess
from pathlib import Path

import pytest

from core.application.clone.contracts import CloneDataClass
from core.infrastructure.clone.installation_gateway import DockerCloneVolumeTransfer

_ITEMS = (
    (CloneDataClass.LOCAL_ARTIFACTS, "artifacts"),
    (CloneDataClass.EXTENSIONS, "extensions"),
    (CloneDataClass.DURABLE_STATE, "state"),
)
_PAYLOAD = b"captured source bytes"


class _Docker:
    """Replace only Docker I/O; Python staging and cleanup remain real."""

    def __init__(self, protected: Path, items, *, fail_second=False, content_variant=None):
        self.protected = protected
        self.items = items
        self.fail_second = fail_second
        self.content_variant = content_variant
        self.copies: list[Path] = []
        self.removed: list[str] = []
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command, **kwargs):
        argv = tuple(command)
        self.commands.append(argv)
        stdout = ""
        if argv[:3] == ("docker", "volume", "ls"):
            stdout = "owned_compact_data\n"
        elif argv[:2] == ("docker", "create"):
            stdout = "owned-transfer\n"
        elif argv[:2] == ("docker", "rm"):
            self.removed.append(argv[-1])
        elif "core.infrastructure.clone.compact_volume_capture" in argv:
            requested = json.loads(kwargs["input"])
            assert requested == [
                {"data_class": data_class.value, "archive_root": root}
                for data_class, root in self.items
            ]
            if "--validate-only" not in argv:
                entries = []
                for data_class, root in self.items:
                    base = {"data_class": data_class.value, "uid": 12345, "gid": 12346}
                    entries.extend(
                        (
                            dict(base, path=root, entry_type="directory", mode=0o2775, size=0),
                            dict(
                                base,
                                path=f"{root}/payload",
                                entry_type="file",
                                mode=0o400,
                                size=len(_PAYLOAD),
                                sha256=hashlib.sha256(_PAYLOAD).hexdigest(),
                            ),
                            dict(
                                base,
                                path=f"{root}/link",
                                entry_type="symlink",
                                mode=0o777,
                                size=0,
                                link_target="payload",
                            ),
                        )
                    )
                    if self.content_variant == "nested":
                        entries.extend(
                            (
                                dict(
                                    base,
                                    path=f"{root}/nested",
                                    entry_type="directory",
                                    mode=0o2750,
                                    size=0,
                                ),
                                dict(
                                    base,
                                    path=f"{root}/nested/deep",
                                    entry_type="file",
                                    mode=0o400,
                                    size=4,
                                    sha256=hashlib.sha256(b"deep").hexdigest(),
                                ),
                                dict(
                                    base,
                                    path=f"{root}/directory-link",
                                    entry_type="symlink",
                                    mode=0o777,
                                    size=0,
                                    link_target="nested",
                                ),
                            )
                        )
                stdout = json.dumps(entries)
        elif argv[:2] == ("docker", "cp"):
            target = Path(argv[-1])
            assert target.parent.parent == Path("/tmp")
            assert target.parent.name.startswith("fs-clone-capture-")
            assert target.parent.stat().st_uid == os.getuid()
            assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
            self.copies.append(target)
            if target.is_relative_to(self.protected):
                return subprocess.CompletedProcess(
                    argv, 1, "", "fchmodat2: operation not permitted"
                )
            if self.fail_second and len(self.copies) == 2:
                return subprocess.CompletedProcess(argv, 1, "", "synthetic second-root failure")
            target.mkdir(parents=True)
            (target / "payload").write_bytes(_PAYLOAD)
            (target / "payload").chmod(0o400)
            (target / "link").symlink_to("payload")
            if self.content_variant == "nested":
                nested = target / "nested"
                nested.mkdir()
                (nested / "deep").write_bytes(b"deep")
                (nested / "deep").chmod(0o400)
                nested.chmod(0o2750)
                (target / "directory-link").symlink_to("nested", target_is_directory=True)
            elif self.content_variant == "fifo":
                os.mkfifo(target / "special")
            elif self.content_variant == "socket":
                with socket.socket(socket.AF_UNIX) as special:
                    special.bind(str(target / "special"))
            target.chmod(0o2775)
        return subprocess.CompletedProcess(argv, 0, stdout, "")


@pytest.mark.parametrize("state_only", [False, True])
def test_compact_capture_uses_private_content_transport_and_original_metadata(tmp_path, state_only):
    (tmp_path / "protected").mkdir()
    destination = tmp_path / "protected/source"
    items = (_ITEMS[-1],) if state_only else _ITEMS
    docker = _Docker(tmp_path / "protected", items)
    transfer = DockerCloneVolumeTransfer(project_name="owned", tool_image="pinned", runner=docker)

    entries = transfer.capture_compact_filesystems(
        destination, items, durable_state_only=state_only
    )

    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    for _, root in items:
        captured = destination / root
        assert stat.S_IMODE(captured.stat().st_mode) == 0o700
        assert stat.S_IMODE((captured / "payload").stat().st_mode) == 0o600
        assert (captured / "payload").read_bytes() == _PAYLOAD
        assert (captured / "link").is_symlink()
        assert (captured / "link").readlink() == Path("payload")
        original = next(entry for entry in entries if entry.path == root)
        assert (original.uid, original.gid, original.mode) == (12345, 12346, 0o2775)
        original_file = next(entry for entry in entries if entry.path == f"{root}/payload")
        assert (original_file.uid, original_file.gid, original_file.mode) == (12345, 12346, 0o400)
    assert docker.copies
    assert all(not path.exists() for path in docker.copies)
    assert docker.removed == (["owned-transfer"] * (2 if state_only else 1))
    mounts = [
        value for command in docker.commands for value in command if "volume-subpath=" in value
    ]
    assert bool(mounts) is state_only
    if state_only:
        assert all("volume-subpath=state/.flow-steward" in mount for mount in mounts)


@pytest.mark.parametrize("destination_exists", [False, True])
def test_compact_capture_second_root_failure_removes_products_and_scratch(
    tmp_path, destination_exists
):
    (tmp_path / "protected").mkdir()
    destination = tmp_path / "protected/source"
    if destination_exists:
        destination.mkdir(parents=True, mode=0o700)
        (destination / "retained").write_bytes(b"unrelated")
    docker = _Docker(tmp_path / "protected", _ITEMS, fail_second=True)
    transfer = DockerCloneVolumeTransfer(project_name="owned", tool_image="pinned", runner=docker)

    with pytest.raises(RuntimeError, match="Clone Docker operation failed"):
        transfer.capture_compact_filesystems(destination, _ITEMS)

    assert len(docker.copies) == 2
    assert all(not path.exists() for path in docker.copies)
    assert all(not (destination / root).exists() for _, root in _ITEMS)
    assert docker.removed == ["owned-transfer"]
    if destination_exists:
        assert (destination / "retained").read_bytes() == b"unrelated"
    else:
        assert not destination.exists()


def test_compact_capture_preserves_nested_bytes_without_following_directory_symlink(tmp_path):
    (tmp_path / "protected").mkdir()
    destination = tmp_path / "protected/source"
    items = (_ITEMS[0],)
    docker = _Docker(tmp_path / "protected", items, content_variant="nested")
    transfer = DockerCloneVolumeTransfer(project_name="owned", tool_image="pinned", runner=docker)

    entries = transfer.capture_compact_filesystems(destination, items)

    nested = destination / "artifacts/nested"
    assert (nested / "deep").read_bytes() == b"deep"
    assert stat.S_IMODE(nested.stat().st_mode) == 0o700
    assert stat.S_IMODE((nested / "deep").stat().st_mode) == 0o600
    link = destination / "artifacts/directory-link"
    assert link.is_symlink()
    assert link.readlink() == Path("nested")
    original = next(entry for entry in entries if entry.path == "artifacts/nested")
    assert (original.uid, original.gid, original.mode) == (12345, 12346, 0o2750)
    assert all(not path.exists() for path in docker.copies)
    assert docker.removed == ["owned-transfer"]


@pytest.mark.parametrize("special", ["fifo", "socket"])
def test_compact_capture_rejects_special_transport_entries_and_cleans_products(tmp_path, special):
    (tmp_path / "protected").mkdir()
    destination = tmp_path / "protected/source"
    docker = _Docker(tmp_path / "protected", (_ITEMS[0],), content_variant=special)
    transfer = DockerCloneVolumeTransfer(project_name="owned", tool_image="pinned", runner=docker)

    with pytest.raises(ValueError):
        transfer.capture_compact_filesystems(destination, (_ITEMS[0],))

    assert not destination.exists()
    assert docker.copies
    assert all(not path.exists() for path in docker.copies)
    assert docker.removed == ["owned-transfer"]


def test_compact_capture_local_read_failure_cleans_prior_root_and_scratch(tmp_path, monkeypatch):
    (tmp_path / "protected").mkdir()
    destination = tmp_path / "protected/source"
    destination.mkdir(mode=0o700)
    (destination / "retained").write_bytes(b"unrelated")
    docker = _Docker(tmp_path / "protected", _ITEMS)
    transfer = DockerCloneVolumeTransfer(project_name="owned", tool_image="pinned", runner=docker)
    original_read = os.read
    failures = []

    def fail_second_root_read(descriptor, size):
        if (destination / "extensions/payload").exists():
            assert (destination / "artifacts/payload").read_bytes() == _PAYLOAD
            failures.append(True)
            raise OSError("synthetic local read failure")
        return original_read(descriptor, size)

    monkeypatch.setattr(os, "read", fail_second_root_read)

    with pytest.raises(OSError, match="synthetic local read failure"):
        transfer.capture_compact_filesystems(destination, _ITEMS)

    assert failures == [True]
    assert len(docker.copies) >= 2
    assert all(not path.exists() for path in docker.copies)
    assert all(not (destination / root).exists() for _, root in _ITEMS)
    assert (destination / "retained").read_bytes() == b"unrelated"
    assert docker.removed == ["owned-transfer"]
