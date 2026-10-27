from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from collections import namedtuple
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.application.clone.contracts import (
    MAX_CLONE_ENCRYPTED_BYTES,
    CloneDataClass,
    CloneEntry,
    CloneManifest,
)
from core.application.release.contracts import ReleaseIdentity, ReleaseImageIdentity
from core.application.release.images import required_product_roles
from core.infrastructure.clone import archive_gateway
from core.infrastructure.clone.archive_gateway import (
    CloneArchiveGateway,
    CloneRestoreArchiveGateway,
)
from core.infrastructure.clone.envelope import encrypt_clone_stream

_PASSPHRASE = b"archive gateway test passphrase"
_COMMIT = "a" * 40
_INDEX = "sha256:" + "1" * 64
_IMAGE_MANIFEST = "sha256:" + "2" * 64
_CONFIG = "sha256:" + "3" * 64


def _release() -> ReleaseIdentity:
    registry = "ghcr.io/example"
    platform = "linux/arm64"
    return ReleaseIdentity(
        version="1.2.3",
        tag="v1.2.3",
        commit_sha=_COMMIT,
        registry=registry,
        profile="scalable",
        platform=platform,
        images=tuple(
            ReleaseImageIdentity(
                role=role.role,
                registry_ref=f"{registry}/{role.repository}@{_IMAGE_MANIFEST}",
                index_digest=_INDEX,
                manifest_digest=_IMAGE_MANIFEST,
                config_digest=_CONFIG,
                revision=_COMMIT,
                platform=platform,
            )
            for role in required_product_roles("scalable")
        ),
    )


def _entry(
    path: str = "extensions/acme/data.txt",
    payload: bytes = b"cloned state",
    **updates: object,
) -> CloneEntry:
    values: dict[str, object] = {
        "data_class": CloneDataClass.EXTENSIONS,
        "path": path,
        "entry_type": "file",
        "size": len(payload),
        "mode": 0o640,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    values.update(updates)
    return CloneEntry.model_validate(values)


def _manifest(*entries: CloneEntry) -> CloneManifest:
    return CloneManifest(
        created_at=datetime(2026, 9, 26, tzinfo=UTC),
        release=_release(),
        deployment_profile="scalable",
        data_placement="host_directories",
        file_storage="local_fs",
        source_installation_id="source-installation",
        entries=entries,
        entry_count=len(entries),
        aggregate_size=sum(entry.size for entry in entries),
    )


def _canonical_manifest(manifest: CloneManifest) -> bytes:
    return json.dumps(
        manifest.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _encrypted_tar(
    manifest: CloneManifest,
    members: list[tuple[tarfile.TarInfo, bytes]],
    *,
    manifest_last: bool = False,
) -> io.BytesIO:
    plaintext = io.BytesIO()
    with tarfile.open(fileobj=plaintext, mode="w", format=tarfile.PAX_FORMAT) as archive:
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_payload = _canonical_manifest(manifest)
        manifest_info.size = len(manifest_payload)
        manifest_info.mode = 0o600
        if not manifest_last:
            archive.addfile(manifest_info, io.BytesIO(manifest_payload))
        for info, payload in members:
            archive.addfile(info, io.BytesIO(payload) if info.isreg() else None)
        if manifest_last:
            archive.addfile(manifest_info, io.BytesIO(manifest_payload))
    plaintext.seek(0)
    encrypted = io.BytesIO()
    encrypt_clone_stream(
        plaintext,
        encrypted,
        passphrase_source=lambda: _PASSPHRASE,
    )
    encrypted.seek(0)
    return encrypted


def _regular(name: str, payload: bytes = b"cloned state", mode: int = 0o640) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = mode
    return info


def test_archive_gateway_round_trips_deterministically_into_private_staging(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    payload_path = source / "extensions/acme/data.txt"
    payload_path.parent.mkdir(parents=True)
    payload_path.write_bytes(b"cloned state")
    payload_path.chmod(0o640)
    manifest = _manifest(_entry(uid=os.getuid(), gid=os.getgid()))
    first = io.BytesIO()
    second = io.BytesIO()
    gateway = CloneArchiveGateway()

    gateway.write_archive(
        source,
        manifest,
        first,
        passphrase_source=lambda: _PASSPHRASE,
    )
    gateway.write_archive(
        source,
        manifest,
        second,
        passphrase_source=lambda: _PASSPHRASE,
    )
    assert first.getvalue() != second.getvalue()  # envelope salts/nonces stay random

    verified = gateway.stage_verified(
        io.BytesIO(first.getvalue()),
        staging_parent=tmp_path / "staging",
        passphrase_source=lambda: _PASSPHRASE,
    )

    assert verified.manifest == manifest
    assert (verified.root / "extensions/acme/data.txt").read_bytes() == b"cloned state"
    assert verified.root.stat().st_mode & 0o777 == 0o700
    assert (verified.root / "extensions/acme/data.txt").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mode", (0o1777, 0o3770))
def test_archive_gateway_authenticates_sticky_broker_directory_in_private_staging(
    tmp_path: Path, mode: int
) -> None:
    source = tmp_path / "source"
    broker = source / "rabbitmq"
    broker.mkdir(parents=True)
    broker.chmod(mode)
    directory = _entry(
        path="rabbitmq",
        data_class=CloneDataClass.RABBITMQ,
        entry_type="directory",
        size=0,
        mode=mode,
        sha256=None,
        uid=os.getuid(),
        gid=os.getgid(),
    )
    archive = io.BytesIO()
    gateway = CloneArchiveGateway()
    gateway.write_archive(
        source, _manifest(directory), archive, passphrase_source=lambda: _PASSPHRASE
    )
    archive.seek(0)
    verified = gateway.stage_verified(
        archive,
        staging_parent=tmp_path / "staging",
        passphrase_source=lambda: _PASSPHRASE,
    )

    assert verified.manifest.entries[0].mode == mode
    assert (verified.root / "rabbitmq").stat().st_mode & 0o7777 == 0o700
    verified.cleanup()


def test_archive_gateway_restores_file_and_symlink_ownership_without_following_link(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    payload_path = source / "extensions/acme/data.txt"
    link_path = source / "extensions/acme/data-link"
    payload_path.parent.mkdir(parents=True)
    payload_path.write_bytes(b"cloned state")
    payload_path.chmod(0o640)
    os.symlink("data.txt", link_path)
    restored_uid = os.getuid()
    restored_gid = os.getgid()
    file_entry = _entry(uid=restored_uid, gid=restored_gid)
    link_entry = CloneEntry(
        data_class=CloneDataClass.EXTENSIONS,
        path="extensions/acme/data-link",
        entry_type="symlink",
        size=0,
        mode=0o777,
        uid=restored_uid,
        gid=restored_gid,
        link_target="data.txt",
    )
    encrypted = io.BytesIO()
    gateway = CloneArchiveGateway()

    gateway.write_archive(
        source,
        _manifest(link_entry, file_entry),
        encrypted,
        passphrase_source=lambda: _PASSPHRASE,
    )
    encrypted.seek(0)
    verified = gateway.stage_verified(
        encrypted,
        staging_parent=tmp_path / "staging",
        passphrase_source=lambda: _PASSPHRASE,
    )

    assert (verified.root / file_entry.path).stat().st_uid == restored_uid
    assert (verified.root / file_entry.path).stat().st_gid == restored_gid
    assert (verified.root / link_entry.path).lstat().st_uid == restored_uid
    assert (verified.root / link_entry.path).lstat().st_gid == restored_gid


def test_archive_gateway_keeps_differing_manifest_ownership_as_evidence_in_private_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restored_uid = os.getuid() + 100
    restored_gid = os.getgid() + 100
    file_entry = _entry(uid=restored_uid, gid=restored_gid)
    link_entry = CloneEntry(
        data_class=CloneDataClass.EXTENSIONS,
        path="extensions/acme/data-link",
        entry_type="symlink",
        size=0,
        mode=0o777,
        uid=restored_uid,
        gid=restored_gid,
        link_target="data.txt",
    )
    file_info = _regular(file_entry.path)
    file_info.uid = restored_uid
    file_info.gid = restored_gid
    link_info = tarfile.TarInfo(link_entry.path)
    link_info.type = tarfile.SYMTYPE
    link_info.mode = link_entry.mode
    link_info.uid = restored_uid
    link_info.gid = restored_gid
    link_info.linkname = link_entry.link_target or ""
    applied: list[tuple[str, int, int, bool]] = []

    def record_chown(path, uid, gid, *, follow_symlinks=True):
        applied.append((Path(path).name, uid, gid, follow_symlinks))

    def record_fchown(_descriptor, uid, gid):
        applied.append(("descriptor", uid, gid, False))

    monkeypatch.setattr(archive_gateway.os, "chown", record_chown)
    monkeypatch.setattr(archive_gateway.os, "fchown", record_fchown)

    verified = CloneArchiveGateway().stage_verified(
        _encrypted_tar(
            _manifest(link_entry, file_entry),
            [(link_info, b""), (file_info, b"cloned state")],
        ),
        staging_parent=tmp_path / "staging",
        passphrase_source=lambda: _PASSPHRASE,
    )

    assert applied == []
    assert (verified.root / file_entry.path).stat().st_uid == os.geteuid()
    assert (verified.root / file_entry.path).stat().st_gid == os.getegid()
    assert (verified.root / link_entry.path).lstat().st_uid == os.geteuid()
    assert (verified.root / link_entry.path).lstat().st_gid == os.getegid()


def test_archive_gateway_rejects_insufficient_staging_space_before_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(_entry())
    staging_parent = tmp_path / "staging"
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(archive_gateway.shutil, "disk_usage", lambda _path: usage(10, 10, 0))
    encrypted = _encrypted_tar(
        manifest,
        [(_regular("extensions/acme/data.txt"), b"cloned state")],
    )
    gateway = CloneArchiveGateway()

    with pytest.raises(Exception, match="space"):
        gateway.stage_verified(
            encrypted,
            staging_parent=staging_parent,
            passphrase_source=lambda: _PASSPHRASE,
        )

    assert not list(staging_parent.glob("clone-*"))


def test_restore_archive_gateway_discards_only_owned_staging_child(
    tmp_path: Path,
) -> None:
    staging_parent = tmp_path / "staging"
    staged = staging_parent / "clone-owned"
    staged.mkdir(parents=True)
    (staged / "secret.txt").write_text("plaintext", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    gateway = CloneRestoreArchiveGateway(staging_parent=staging_parent)

    gateway.discard_staged(str(staged))

    assert not staged.exists()
    with pytest.raises(ValueError, match="outside"):
        gateway.discard_staged(str(outside))
    assert outside.is_dir()


def test_restore_archive_rejects_oversized_ciphertext_before_passphrase_or_staging(
    tmp_path: Path,
) -> None:
    source = tmp_path / "oversized.fsclone"
    source.touch()
    with source.open("r+b") as output:
        output.truncate(MAX_CLONE_ENCRYPTED_BYTES + 1)
    passphrase_requested = False

    def passphrase() -> bytes:
        nonlocal passphrase_requested
        passphrase_requested = True
        return _PASSPHRASE

    staging_parent = tmp_path / "staging"
    gateway = CloneRestoreArchiveGateway(staging_parent=staging_parent)

    with pytest.raises(ValueError, match="size"):
        gateway.stage_verified(str(source), passphrase_source=passphrase)

    assert passphrase_requested is False
    assert not list(staging_parent.glob("clone-*"))


@pytest.mark.parametrize("variant", ["extra", "missing", "checksum", "mode", "manifest-last"])
def test_archive_gateway_rejects_manifest_payload_mismatch_without_publishing_staging(
    tmp_path: Path,
    variant: str,
) -> None:
    expected = _entry()
    manifest = _manifest(expected)
    payload = b"cloned state"
    members: list[tuple[tarfile.TarInfo, bytes]] = [(_regular(expected.path, payload), payload)]
    manifest_last = False
    if variant == "extra":
        members.append((_regular("extensions/acme/extra.txt", b"extra"), b"extra"))
    elif variant == "missing":
        members.clear()
    elif variant == "checksum":
        members[0] = (_regular(expected.path, b"cloned statf"), b"cloned statf")
    elif variant == "mode":
        members[0] = (_regular(expected.path, payload, 0o600), payload)
    else:
        manifest_last = True

    staging_parent = tmp_path / "staging"
    encrypted = _encrypted_tar(manifest, members, manifest_last=manifest_last)
    gateway = CloneArchiveGateway()
    with pytest.raises(RuntimeError):
        gateway.stage_verified(
            encrypted,
            staging_parent=staging_parent,
            passphrase_source=lambda: _PASSPHRASE,
        )

    assert not list(staging_parent.glob("clone-*"))


@pytest.mark.parametrize(
    "unsafe_name",
    (
        "/absolute",
        "../parent",
        "extensions/acme/nul\x00name",
        "extensions/" + "x" * 505,
        "extensions//acme/data.txt",
    ),
)
def test_archive_gateway_rejects_unsafe_archive_paths(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    manifest = _manifest(_entry())
    encrypted = _encrypted_tar(manifest, [(_regular(unsafe_name), b"cloned state")])
    gateway = CloneArchiveGateway()
    with pytest.raises((RuntimeError, ValueError, tarfile.TarError)):
        gateway.stage_verified(
            encrypted,
            staging_parent=tmp_path / "staging",
            passphrase_source=lambda: _PASSPHRASE,
        )


@pytest.mark.parametrize("kind", ["hardlink", "fifo", "device"])
def test_archive_gateway_rejects_links_and_unsupported_types(
    tmp_path: Path,
    kind: str,
) -> None:
    manifest = _manifest(_entry())
    info = tarfile.TarInfo("extensions/acme/data.txt")
    if kind == "hardlink":
        info.type = tarfile.LNKTYPE
        info.linkname = "extensions/acme/other.txt"
    elif kind == "fifo":
        info.type = tarfile.FIFOTYPE
    else:
        info.type = tarfile.CHRTYPE

    encrypted = _encrypted_tar(manifest, [(info, b"")])
    gateway = CloneArchiveGateway()
    with pytest.raises(RuntimeError, match=r"type|hard link"):
        gateway.stage_verified(
            encrypted,
            staging_parent=tmp_path / "staging",
            passphrase_source=lambda: _PASSPHRASE,
        )


def test_archive_gateway_rejects_escaping_symlink_and_descendant(
    tmp_path: Path,
) -> None:
    link = CloneEntry(
        data_class=CloneDataClass.EXTENSIONS,
        path="extensions/acme/link",
        entry_type="symlink",
        size=0,
        mode=0o777,
        link_target="../../outside",
    )
    manifest = _manifest(link)
    info = tarfile.TarInfo(link.path)
    info.type = tarfile.SYMTYPE
    info.mode = 0o777
    info.linkname = link.link_target or ""

    encrypted = _encrypted_tar(manifest, [(info, b"")])
    gateway = CloneArchiveGateway()
    with pytest.raises(RuntimeError, match=r"symlink|escape"):
        gateway.stage_verified(
            encrypted,
            staging_parent=tmp_path / "staging",
            passphrase_source=lambda: _PASSPHRASE,
        )


def test_archive_gateway_rejects_entry_beneath_confined_symlink(tmp_path: Path) -> None:
    link = CloneEntry(
        data_class=CloneDataClass.EXTENSIONS,
        path="extensions/acme/link",
        entry_type="symlink",
        size=0,
        mode=0o777,
        link_target="real",
    )
    descendant = _entry(path="extensions/acme/link/data.txt")
    manifest = _manifest(link, descendant)
    link_info = tarfile.TarInfo(link.path)
    link_info.type = tarfile.SYMTYPE
    link_info.mode = link.mode
    link_info.linkname = link.link_target or ""

    encrypted = _encrypted_tar(
        manifest,
        [(link_info, b""), (_regular(descendant.path), b"cloned state")],
    )
    gateway = CloneArchiveGateway()
    with pytest.raises(RuntimeError, match="beneath a symbolic link"):
        gateway.stage_verified(
            encrypted,
            staging_parent=tmp_path / "staging",
            passphrase_source=lambda: _PASSPHRASE,
        )


def test_archive_gateway_rejects_entry_and_aggregate_bounds_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(_entry())
    monkeypatch.setattr(archive_gateway, "MAX_CLONE_ENTRY_BYTES", 4)
    monkeypatch.setattr(archive_gateway, "MAX_CLONE_AGGREGATE_BYTES", 4)
    encrypted = _encrypted_tar(
        manifest,
        [(_regular("extensions/acme/data.txt"), b"cloned state")],
    )
    gateway = CloneArchiveGateway()

    with pytest.raises(RuntimeError, match=r"size|bound|truncated"):
        gateway.stage_verified(
            encrypted,
            staging_parent=tmp_path / "staging",
            passphrase_source=lambda: _PASSPHRASE,
        )


def test_archive_gateway_enforces_aggregate_bound_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"cloned state"
    first = _entry("extensions/acme/a.txt", payload)
    second = _entry("extensions/acme/b.txt", payload)
    manifest = _manifest(first, second)
    monkeypatch.setattr(archive_gateway, "MAX_CLONE_ENTRY_BYTES", len(payload))
    monkeypatch.setattr(archive_gateway, "MAX_CLONE_AGGREGATE_BYTES", len(payload) + 1)
    encrypted = _encrypted_tar(
        manifest,
        [(_regular(first.path), payload), (_regular(second.path), payload)],
    )
    gateway = CloneArchiveGateway()

    with pytest.raises(RuntimeError, match="aggregate"):
        gateway.stage_verified(
            encrypted,
            staging_parent=tmp_path / "staging",
            passphrase_source=lambda: _PASSPHRASE,
        )


def test_archive_gateway_rejects_duplicate_tar_headers(tmp_path: Path) -> None:
    entry = _entry()
    manifest = _manifest(entry)
    member = (_regular(entry.path), b"cloned state")
    encrypted = _encrypted_tar(manifest, [member, member])
    gateway = CloneArchiveGateway()

    with pytest.raises(RuntimeError, match="duplicate"):
        gateway.stage_verified(
            encrypted,
            staging_parent=tmp_path / "staging",
            passphrase_source=lambda: _PASSPHRASE,
        )


def test_archive_writer_rejects_source_symlink_escape(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "extensions/acme").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"cloned state")
    os.symlink(outside, source / "extensions/acme/data.txt")
    manifest = _manifest(_entry())
    destination = io.BytesIO()
    gateway = CloneArchiveGateway()

    with pytest.raises(ValueError, match=r"escape|symlink"):
        gateway.write_archive(
            source,
            manifest,
            destination,
            passphrase_source=lambda: _PASSPHRASE,
        )


def test_archive_writer_rejects_ordinary_source_ownership_mismatch(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    payload = source / "extensions/acme/data.txt"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"cloned state")
    payload.chmod(0o640)
    entry = _entry(uid=os.getuid() + 1, gid=os.getgid())
    manifest = _manifest(entry)
    destination = io.BytesIO()
    gateway = CloneArchiveGateway()

    with pytest.raises(ValueError, match="ownership changed"):
        gateway.write_archive(
            source,
            manifest,
            destination,
            passphrase_source=lambda: _PASSPHRASE,
        )


def test_archive_writer_rejects_regular_file_swapped_after_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    payload_path = source / "extensions/acme/data.txt"
    payload_path.parent.mkdir(parents=True)
    payload_path.write_bytes(b"cloned state")
    payload_path.chmod(0o640)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"cloned state")
    replacement.chmod(0o640)
    original_open = os.open
    swapped = False

    def swap_before_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == payload_path and not swapped:
            swapped = True
            payload_path.unlink()
            os.symlink(replacement, payload_path)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(archive_gateway.os, "open", swap_before_open)
    manifest = _manifest(_entry(uid=os.getuid(), gid=os.getgid()))
    destination = io.BytesIO()
    gateway = CloneArchiveGateway()

    with pytest.raises((OSError, ValueError), match=r"symbolic|changed|loop"):
        gateway.write_archive(
            source,
            manifest,
            destination,
            passphrase_source=lambda: _PASSPHRASE,
        )
