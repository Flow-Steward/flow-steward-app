"""Deterministic Clone archive assembly and confined verified staging."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import shutil
import stat
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from pydantic import ValidationError

from core.application.clone.contracts import (
    ARCHIVE_ROOT_BY_DATA_CLASS,
    MAX_CLONE_AGGREGATE_BYTES,
    MAX_CLONE_ENCRYPTED_BYTES,
    MAX_CLONE_ENTRIES,
    MAX_CLONE_ENTRY_BYTES,
    CloneEntry,
    CloneManifest,
    canonical_clone_path,
)
from core.application.lifecycle.contracts import DeploymentConfig
from core.application.lifecycle.errors import LifecycleError
from core.infrastructure.clone.envelope import (
    PassphraseSource,
    decrypt_clone_stream,
    encrypt_clone_stream,
)

_MANIFEST_NAME = "manifest.json"
_MAX_MANIFEST_BYTES = 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class VerifiedCloneArchive:
    """A fully authenticated and validated private staging tree."""

    manifest: CloneManifest
    root: Path
    configuration: DeploymentConfig | None = None
    artifact_size: int = 0
    artifact_sha256: str = ""

    def cleanup(self) -> None:
        shutil.rmtree(self.root)
        if self.root.exists() or self.root.is_symlink():
            raise RuntimeError("Clone plaintext staging cleanup did not complete")


@dataclass(frozen=True)
class TrustedPublicationMetadata:
    """Authenticated source metadata whose private staging identity is transport-local."""

    entries: tuple[CloneEntry, ...]

    def trusts(self, entry: CloneEntry) -> bool:
        return entry in self.entries


class CloneArchiveGateway:
    """Build and open manifest-first archives without generic extraction."""

    def write_archive(
        self,
        source_root: Path,
        manifest: CloneManifest,
        destination: BinaryIO,
        *,
        passphrase_source: PassphraseSource,
        trusted_publication_metadata: TrustedPublicationMetadata | None = None,
    ) -> None:
        root = _verified_source_root(source_root)
        with tempfile.TemporaryFile(mode="w+b") as plaintext:
            with tarfile.open(
                fileobj=plaintext,
                mode="w",
                format=tarfile.PAX_FORMAT,
            ) as archive:
                manifest_payload = _canonical_manifest(manifest)
                _add_bytes(archive, _MANIFEST_NAME, manifest_payload, 0o600)
                for entry in manifest.entries:
                    trusted_publication = bool(
                        trusted_publication_metadata and trusted_publication_metadata.trusts(entry)
                    )
                    _add_source_entry(
                        archive,
                        root,
                        entry,
                        trusted_publication=trusted_publication,
                    )
            plaintext.seek(0)
            encrypt_clone_stream(
                plaintext,
                destination,
                passphrase_source=passphrase_source,
            )

    def stage_verified(
        self,
        source: BinaryIO,
        *,
        staging_parent: Path,
        passphrase_source: PassphraseSource,
    ) -> VerifiedCloneArchive:
        parent = _verified_staging_parent(staging_parent)
        with tempfile.TemporaryFile(mode="w+b", dir=parent) as plaintext:
            decrypt_clone_stream(
                source,
                plaintext,
                passphrase_source=passphrase_source,
            )
            plaintext.seek(0)
            try:
                with tarfile.open(fileobj=plaintext, mode="r:") as archive:
                    manifest, members = _validated_members(archive)
                    if shutil.disk_usage(parent).free < manifest.aggregate_size:
                        raise LifecycleError.stable_failure(
                            code="clone_restore_staging_space_insufficient",
                            cause=(
                                "Clone staging space is insufficient for the authenticated payload."
                            ),
                            remediation=("Free space on the private staging filesystem and retry."),
                            step="restore_preflight",
                        )
                    staging = Path(tempfile.mkdtemp(prefix="clone-", dir=parent))
                    staging.chmod(0o700)
                    try:
                        _extract_verified(archive, manifest, members, staging)
                    except Exception:
                        shutil.rmtree(staging, ignore_errors=True)
                        raise
            except (OSError, tarfile.TarError) as exc:
                raise RuntimeError("Clone payload is not a valid uncompressed tar archive") from exc
        return VerifiedCloneArchive(manifest=manifest, root=staging)


class CloneRestoreArchiveGateway:
    """Open one operator-selected archive without following symbolic links."""

    def __init__(
        self,
        *,
        staging_parent: Path,
        archive: CloneArchiveGateway | None = None,
    ) -> None:
        self._staging_parent = Path(staging_parent)
        self._archive = archive or CloneArchiveGateway()

    def artifact_size(self, path: str) -> int:
        descriptor = self._open_regular(path)
        try:
            return os.fstat(descriptor).st_size
        finally:
            os.close(descriptor)

    def artifact_sha256(self, path: str) -> str:
        descriptor = self._open_regular(path)
        size = os.fstat(descriptor).st_size
        if not 1 <= size <= MAX_CLONE_ENCRYPTED_BYTES:
            os.close(descriptor)
            raise ValueError("Clone archive size is outside the supported bounds")
        with os.fdopen(descriptor, "rb") as source:
            return _sha256_stream(source)

    def free_bytes(self) -> int:
        parent = _verified_staging_parent(self._staging_parent)
        return shutil.disk_usage(parent).free

    def stage_verified(
        self,
        path: str,
        *,
        passphrase_source: PassphraseSource,
    ) -> VerifiedCloneArchive:
        parent = _verified_staging_parent(self._staging_parent)
        descriptor = self._open_regular(path)
        size = os.fstat(descriptor).st_size
        if not 1 <= size <= MAX_CLONE_ENCRYPTED_BYTES:
            os.close(descriptor)
            raise ValueError("Clone archive size is outside the supported bounds")
        if shutil.disk_usage(parent).free < size:
            os.close(descriptor)
            raise ValueError("Clone staging space is insufficient for the encrypted archive")
        digest = hashlib.sha256()
        copied = 0
        with (
            os.fdopen(descriptor, "rb") as source,
            tempfile.TemporaryFile(mode="w+b", dir=parent) as authenticated,
        ):
            while chunk := source.read(_COPY_CHUNK_BYTES):
                copied += len(chunk)
                if copied > size:
                    raise ValueError("Clone archive changed while it was being staged")
                digest.update(chunk)
                authenticated.write(chunk)
            if copied != size:
                raise ValueError("Clone archive changed while it was being staged")
            authenticated.flush()
            authenticated.seek(0)
            verified = self._archive.stage_verified(
                authenticated,
                staging_parent=parent,
                passphrase_source=passphrase_source,
            )
            try:
                configuration = DeploymentConfig.model_validate_json(
                    (verified.root / "evidence/configuration.json").read_text(encoding="utf-8")
                )
            except BaseException:
                verified.cleanup()
                raise
        return VerifiedCloneArchive(
            manifest=verified.manifest,
            root=verified.root,
            configuration=configuration,
            artifact_size=size,
            artifact_sha256=digest.hexdigest(),
        )

    def discard_staged(self, staged_root: str) -> None:
        """Remove only a real direct child of the configured staging directory."""
        parent = _verified_staging_parent(self._staging_parent)
        selected = Path(staged_root)
        if selected.is_symlink() or not selected.is_dir():
            raise ValueError("Clone staging root must be a real directory")
        resolved = selected.resolve(strict=True)
        if resolved.parent != parent or not resolved.name.startswith("clone-"):
            raise ValueError("Clone staging root is outside the configured staging directory")
        shutil.rmtree(resolved)

    @staticmethod
    def _open_regular(path: str) -> int:
        selected = Path(path)
        if selected.is_symlink():
            raise ValueError("Clone archive must not be a symbolic link")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(selected, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise ValueError("Clone archive must be a regular file")
        return descriptor


def _canonical_manifest(manifest: CloneManifest) -> bytes:
    payload = json.dumps(
        manifest.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > _MAX_MANIFEST_BYTES:
        raise RuntimeError("Clone manifest exceeds the supported size")
    return payload


def _add_bytes(archive: tarfile.TarFile, name: str, payload: bytes, mode: int) -> None:
    info = _tar_info(name, mode=mode, size=len(payload))
    from io import BytesIO

    archive.addfile(info, BytesIO(payload))


def _tar_info(
    name: str,
    *,
    mode: int,
    size: int = 0,
    uid: int = 0,
    gid: int = 0,
) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.mode = mode
    info.size = size
    info.mtime = 0
    info.uid = uid
    info.gid = gid
    info.uname = ""
    info.gname = ""
    return info


def _verified_source_root(source_root: Path) -> Path:
    root = Path(source_root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Clone source root must be a real directory")
    return root.resolve(strict=True)


def _add_source_entry(
    archive: tarfile.TarFile,
    root: Path,
    entry: CloneEntry,
    *,
    trusted_publication: bool = False,
) -> None:
    source = _confined_source_path(root, entry)
    metadata = source.lstat()
    mode = stat.S_IMODE(metadata.st_mode)
    if not trusted_publication and mode != entry.mode:
        raise ValueError(f"Clone source mode changed for {entry.path}")
    if not trusted_publication and (metadata.st_uid != entry.uid or metadata.st_gid != entry.gid):
        raise ValueError(f"Clone source ownership changed for {entry.path}")
    info = _tar_info(
        entry.path,
        mode=entry.mode,
        size=entry.size,
        uid=entry.uid,
        gid=entry.gid,
    )
    if entry.entry_type == "file":
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"Clone source is not a regular file: {entry.path}")
        if metadata.st_size != entry.size:
            raise ValueError(f"Clone source size changed for {entry.path}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(source, flags)
        except OSError as exc:
            raise ValueError(f"Clone source file changed for {entry.path}") from exc
        with os.fdopen(descriptor, "rb") as source_file:
            opened = os.fstat(source_file.fileno())
            inspected_identity = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_size,
                metadata.st_uid,
                metadata.st_gid,
            )
            opened_identity = (
                opened.st_dev,
                opened.st_ino,
                opened.st_mode,
                opened.st_size,
                opened.st_uid,
                opened.st_gid,
            )
            if inspected_identity != opened_identity or not stat.S_ISREG(opened.st_mode):
                raise ValueError(f"Clone source file changed for {entry.path}")
            if _sha256_stream(source_file) != entry.sha256:
                raise ValueError(f"Clone source checksum changed for {entry.path}")
            source_file.seek(0)
            archive.addfile(info, source_file)
        return
    if entry.entry_type == "directory":
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"Clone source is not a directory: {entry.path}")
        info.type = tarfile.DIRTYPE
        archive.addfile(info)
        return
    if not stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"Clone source is not a symbolic link: {entry.path}")
    target = os.readlink(source)
    if target != entry.link_target:
        raise ValueError(f"Clone source link target changed for {entry.path}")
    _require_symlink_confined(entry)
    resolved = source.resolve(strict=True)
    class_root = (root / ARCHIVE_ROOT_BY_DATA_CLASS[entry.data_class]).resolve(strict=True)
    if not resolved.is_relative_to(class_root):
        raise ValueError(f"Clone source symlink escapes its class root: {entry.path}")
    info.type = tarfile.SYMTYPE
    info.size = 0
    info.linkname = target
    archive.addfile(info)


def _confined_source_path(root: Path, entry: CloneEntry) -> Path:
    candidate = root / entry.path
    current = root
    for part in PurePosixPath(entry.path).parts[:-1]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Clone source path traverses a symlink: {entry.path}")
    if not candidate.exists() and not candidate.is_symlink():
        raise ValueError(f"Clone source entry is missing: {entry.path}")
    if entry.entry_type != "symlink":
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ValueError(f"Clone source path escapes its root: {entry.path}")
    return candidate


def _sha256_stream(source: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := source.read(_COPY_CHUNK_BYTES):
        digest.update(chunk)
    return digest.hexdigest()


def _validated_members(
    archive: tarfile.TarFile,
) -> tuple[CloneManifest, dict[str, tarfile.TarInfo]]:
    first = archive.next()
    if first is None or first.name != _MANIFEST_NAME or not first.isreg():
        raise RuntimeError("Clone manifest must be the first regular archive entry")
    if not 1 <= first.size <= _MAX_MANIFEST_BYTES:
        raise RuntimeError("Clone manifest size is outside the supported bounds")
    manifest_source = archive.extractfile(first)
    if manifest_source is None:
        raise RuntimeError("Clone manifest cannot be read")
    manifest_payload = manifest_source.read(_MAX_MANIFEST_BYTES + 1)
    if len(manifest_payload) != first.size:
        raise RuntimeError("Clone manifest is truncated")
    try:
        manifest = CloneManifest.model_validate_json(manifest_payload)
    except ValidationError as exc:
        raise RuntimeError("Clone manifest validation failed") from exc
    if _canonical_manifest(manifest) != manifest_payload:
        raise RuntimeError("Clone manifest is not canonical JSON")

    members: dict[str, tarfile.TarInfo] = {}
    total_size = 0
    index = 0
    while (member := archive.next()) is not None:
        index += 1
        if index > MAX_CLONE_ENTRIES:
            raise RuntimeError("Clone archive entry count exceeds the supported bound")
        try:
            name = canonical_clone_path(member.name)
        except ValueError as exc:
            raise RuntimeError("Clone archive contains an unsafe path") from exc
        if name in members:
            raise RuntimeError("Clone archive contains duplicate normalized paths")
        if member.islnk():
            raise RuntimeError("Clone archive hard links are forbidden")
        if not (member.isreg() or member.isdir() or member.issym()):
            raise RuntimeError("Clone archive contains an unsupported entry type")
        if member.mode < 1 or member.mode > 0o3777:
            raise RuntimeError("Clone archive entry mode is outside the supported bounds")
        if member.size < 0 or member.size > MAX_CLONE_ENTRY_BYTES:
            raise RuntimeError("Clone archive entry size is outside the supported bound")
        total_size += member.size
        if total_size > MAX_CLONE_AGGREGATE_BYTES:
            raise RuntimeError("Clone archive aggregate size exceeds the supported bound")
        members[name] = member

    expected = {entry.path: entry for entry in manifest.entries}
    if set(members) != set(expected):
        raise RuntimeError("Clone archive entries do not exactly match the manifest")
    if total_size != manifest.aggregate_size:
        raise RuntimeError("Clone archive aggregate size does not match the manifest")
    for path, entry in expected.items():
        _validate_member(members[path], entry)
    _reject_symlink_descendants(manifest.entries)
    return manifest, members


def _validate_member(member: tarfile.TarInfo, entry: CloneEntry) -> None:
    if member.isreg():
        actual_type = "file"
    elif member.isdir():
        actual_type = "directory"
    else:
        actual_type = "symlink"
    if actual_type != entry.entry_type:
        raise RuntimeError(f"Clone archive entry type mismatch for {entry.path}")
    if (
        member.size != entry.size
        or member.mode != entry.mode
        or member.uid != entry.uid
        or member.gid != entry.gid
    ):
        raise RuntimeError(f"Clone archive entry evidence mismatch for {entry.path}")
    if entry.entry_type == "symlink":
        if member.linkname != entry.link_target:
            raise RuntimeError(f"Clone archive symlink target mismatch for {entry.path}")
        _require_symlink_confined(entry)


def _require_symlink_confined(entry: CloneEntry) -> None:
    assert entry.link_target is not None
    if PurePosixPath(entry.link_target).is_absolute() or "\x00" in entry.link_target:
        raise RuntimeError(f"Clone symlink escapes its class root: {entry.path}")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(entry.path), entry.link_target))
    root = ARCHIVE_ROOT_BY_DATA_CLASS[entry.data_class]
    if resolved != root and not resolved.startswith(f"{root}/"):
        raise RuntimeError(f"Clone symlink escapes its class root: {entry.path}")


def _reject_symlink_descendants(entries: tuple[CloneEntry, ...]) -> None:
    links = [entry.path for entry in entries if entry.entry_type == "symlink"]
    for link in links:
        if any(entry.path.startswith(f"{link}/") for entry in entries):
            raise RuntimeError("Clone archive places an entry beneath a symbolic link")


def _verified_staging_parent(staging_parent: Path) -> Path:
    parent = Path(staging_parent)
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.is_symlink() or not parent.is_dir():
        raise RuntimeError("Clone staging parent must be a real directory")
    return parent.resolve(strict=True)


def _extract_verified(
    archive: tarfile.TarFile,
    manifest: CloneManifest,
    members: dict[str, tarfile.TarInfo],
    staging: Path,
) -> None:
    entries = {entry.path: entry for entry in manifest.entries}
    directories = [entry for entry in manifest.entries if entry.entry_type == "directory"]
    for entry in manifest.entries:
        if entry.entry_type != "file":
            continue
        target = _staging_target(staging, entry.path)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        member_source = archive.extractfile(members[entry.path])
        if member_source is None:
            raise RuntimeError(f"Clone archive file cannot be read: {entry.path}")
        digest = hashlib.sha256()
        written = 0
        with target.open("xb") as destination:
            os.fchmod(destination.fileno(), 0o600)
            while chunk := member_source.read(_COPY_CHUNK_BYTES):
                written += len(chunk)
                if written > entry.size:
                    raise RuntimeError(
                        f"Clone archive file exceeds its declared size: {entry.path}"
                    )
                digest.update(chunk)
                destination.write(chunk)
            if written != entry.size or digest.hexdigest() != entry.sha256:
                raise RuntimeError(f"Clone archive checksum or size mismatch for {entry.path}")
            os.fchmod(destination.fileno(), entry.mode & 0o600)

    for entry in manifest.entries:
        if entry.entry_type != "symlink":
            continue
        target = _staging_target(staging, entry.path)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        assert entry.link_target is not None
        os.symlink(entry.link_target, target)

    for entry in sorted(directories, key=lambda item: item.path.count("/"), reverse=True):
        target = _staging_target(staging, entry.path)
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.chmod(0o700)
    del entries


def _staging_target(staging: Path, relative: str) -> Path:
    target = staging.joinpath(*PurePosixPath(relative).parts)
    if not target.absolute().is_relative_to(staging.absolute()):
        raise RuntimeError("Clone staging path escapes its private root")
    return target


__all__ = [
    "CloneArchiveGateway",
    "CloneRestoreArchiveGateway",
    "VerifiedCloneArchive",
]
