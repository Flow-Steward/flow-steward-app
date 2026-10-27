"""Closed, secret-free contracts for encrypted Clone archives."""

from __future__ import annotations

import base64
import re
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.application.clone.state_policy import CLONE_SOURCE_EXCLUDED_STATE_NAMES
from core.application.release.contracts import ReleaseIdentity

_SCRYPT_MIN_N = 2**14
_SCRYPT_MAX_N = 2**18
_SCRYPT_MAX_MEMORY_BYTES = 256 * 1024 * 1024
_MIN_SALT_BYTES = 16
_MAX_SALT_BYTES = 64
MAX_CLONE_PATH_BYTES = 512
MAX_CLONE_ENTRY_BYTES = 8 * 1024**3
MAX_CLONE_ENTRIES = 100_000
MAX_CLONE_AGGREGATE_BYTES = 64 * 1024**3
# Tar uses at most two 512-byte records per bounded entry in this format. The
# remaining allowance covers the manifest, tar terminator, Clone header, and GCM tag.
MAX_CLONE_ENCRYPTED_BYTES = MAX_CLONE_AGGREGATE_BYTES + MAX_CLONE_ENTRIES * 1024 + 2 * 1024**2
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_GENERATED_PATH_PARTS = frozenset(
    {"node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache"}
)
_EPHEMERAL_STATE_PARTS = CLONE_SOURCE_EXCLUDED_STATE_NAMES | frozenset(
    {"journals", "recovery", "tmp", "cache", "caches"}
)


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CloneKdfHeader(_ClosedModel):
    """Authenticated, bounded parameters for one archive key derivation."""

    schema_version: Literal[1] = 1
    name: Literal["scrypt"] = "scrypt"
    salt: str = Field(min_length=24, max_length=88)
    n: int = Field(default=_SCRYPT_MIN_N, ge=_SCRYPT_MIN_N, le=_SCRYPT_MAX_N)
    r: int = Field(default=8, ge=1, le=32)
    p: int = Field(default=1, ge=1, le=16)
    length: Literal[32] = 32

    @model_validator(mode="after")
    def validate_parameters(self) -> Self:
        if self.n & (self.n - 1):
            raise ValueError("scrypt n must be a power of two")
        try:
            salt = base64.b64decode(self.salt, altchars=b"-_", validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("scrypt salt must be canonical URL-safe base64") from exc
        if not _MIN_SALT_BYTES <= len(salt) <= _MAX_SALT_BYTES:
            raise ValueError("scrypt salt length is outside the supported bounds")
        if base64.urlsafe_b64encode(salt).decode("ascii") != self.salt:
            raise ValueError("scrypt salt must be canonical URL-safe base64")
        if 128 * self.n * self.r > _SCRYPT_MAX_MEMORY_BYTES:
            raise ValueError("scrypt parameters exceed the supported memory bound")
        return self

    def salt_bytes(self) -> bytes:
        return base64.urlsafe_b64decode(self.salt.encode("ascii"))


class CloneEnvelopeHeader(_ClosedModel):
    """Public framing metadata authenticated as AES-GCM associated data."""

    schema_version: Literal[1] = 1
    format: Literal["flow-steward-clone"] = "flow-steward-clone"
    cipher: Literal["aes-256-gcm-stream"] = "aes-256-gcm-stream"
    kdf: CloneKdfHeader


class CloneDataClass(StrEnum):
    """Logical source-owned product state accepted by Clone."""

    POSTGRESQL = "postgresql"
    RABBITMQ = "rabbitmq"
    BUNDLED_STORAGE = "bundled_storage"
    LOCAL_ARTIFACTS = "local_artifacts"
    EXTENSIONS = "extensions"
    DURABLE_STATE = "durable_state"
    SOURCE_SECRETS = "source_secrets"  # pragma: allowlist secret - data-class name only
    RELEASE_EVIDENCE = "release_evidence"


ARCHIVE_ROOT_BY_DATA_CLASS: dict[CloneDataClass, str] = {
    CloneDataClass.POSTGRESQL: "postgresql",
    CloneDataClass.RABBITMQ: "rabbitmq",
    CloneDataClass.BUNDLED_STORAGE: "bundled-storage",
    CloneDataClass.LOCAL_ARTIFACTS: "artifacts",
    CloneDataClass.EXTENSIONS: "extensions",
    CloneDataClass.DURABLE_STATE: "state",
    CloneDataClass.SOURCE_SECRETS: "secrets",  # pragma: allowlist secret - archive path only
    CloneDataClass.RELEASE_EVIDENCE: "evidence",
}


def canonical_clone_path(value: str) -> str:
    """Return a canonical relative POSIX path or reject it."""
    if not value or "\x00" in value or "\\" in value:
        raise ValueError("Clone path contains an unsupported character")
    if len(value.encode("utf-8")) > MAX_CLONE_PATH_BYTES:
        raise ValueError("Clone path exceeds the supported byte length")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("Clone path must use canonical Unicode normalization")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("Clone path must be canonical and relative")
    if path.as_posix() != value:
        raise ValueError("Clone path must be canonical and relative")
    return value


class CloneEntry(_ClosedModel):
    """One authenticated archive entry owned by exactly one logical class."""

    data_class: CloneDataClass
    path: str = Field(min_length=1, max_length=MAX_CLONE_PATH_BYTES)
    entry_type: Literal["file", "directory", "symlink"]
    size: int = Field(ge=0, le=MAX_CLONE_ENTRY_BYTES)
    mode: int = Field(ge=1, le=0o3777)
    uid: int = Field(default=0, ge=0, le=2**31 - 1)
    gid: int = Field(default=0, ge=0, le=2**31 - 1)
    sha256: str | None = None
    link_target: str | None = Field(default=None, max_length=MAX_CLONE_PATH_BYTES)

    @model_validator(mode="after")
    def validate_entry(self) -> Self:
        canonical_clone_path(self.path)
        archive_root = ARCHIVE_ROOT_BY_DATA_CLASS[self.data_class]
        if self.path != archive_root and not self.path.startswith(f"{archive_root}/"):
            raise ValueError("Clone entry path does not match its ownership class")
        parts = set(PurePosixPath(self.path).parts)
        if parts & _GENERATED_PATH_PARTS:
            raise ValueError("Clone entry contains an excluded generated dependency")
        if self.data_class is CloneDataClass.EXTENSIONS and ".git" in parts:
            raise ValueError("Clone entry contains excluded extension Git metadata")
        if self.data_class is CloneDataClass.DURABLE_STATE and (
            parts & _EPHEMERAL_STATE_PARTS or ".git" in parts
        ):
            raise ValueError("Clone entry contains excluded ephemeral durable state")
        if self.entry_type == "file":
            if self.mode & 0o7000:
                raise ValueError("Clone regular files must not use a special mode bit")
            if self.sha256 is None or _SHA256.fullmatch(self.sha256) is None:
                raise ValueError("Clone file entry requires a lowercase SHA-256 checksum")
            if self.link_target is not None:
                raise ValueError("Clone file entry must not include a link target")
        elif self.entry_type == "directory":
            if self.mode & ~0o3777:
                raise ValueError(
                    "Clone directories permit only setgid and sticky special mode bits"
                )
            if self.size != 0 or self.sha256 is not None or self.link_target is not None:
                raise ValueError("Clone directory entry must not include file evidence")
        else:
            if self.mode & 0o7000:
                raise ValueError("Clone symbolic links must not use a special mode bit")
            if self.size != 0 or self.sha256 is not None or not self.link_target:
                raise ValueError("Clone symlink entry requires only a link target")
            _validate_link_target(self.link_target)
        return self


def _validate_link_target(target: str) -> None:
    if "\x00" in target or "\\" in target or len(target.encode("utf-8")) > MAX_CLONE_PATH_BYTES:
        raise ValueError("Clone symlink target is invalid")
    if PurePosixPath(target).is_absolute():
        raise ValueError("Clone symlink target must be relative")


class CloneManifest(_ClosedModel):
    """Authenticated, closed inventory for one complete Clone payload."""

    schema_version: Literal[1] = 1
    created_at: datetime
    release: ReleaseIdentity
    deployment_profile: Literal["compact", "scalable"]
    data_placement: Literal["host_directories", "compact_volume"]
    file_storage: Literal["local_fs", "bundled", "external_s3"]
    source_installation_id: str = Field(min_length=1, max_length=255)
    entries: tuple[CloneEntry, ...] = Field(min_length=1, max_length=MAX_CLONE_ENTRIES)
    entry_count: int = Field(ge=0, le=MAX_CLONE_ENTRIES)
    aggregate_size: int = Field(ge=0, le=MAX_CLONE_AGGREGATE_BYTES)
    imported_secret_names: tuple[str, ...] = ()
    source_secret_fingerprints: dict[str, str] = Field(default_factory=dict)
    external_effect_categories: tuple[str, ...] = ()
    database_parity_evidence: dict[str, str | int | bool] = Field(default_factory=dict)
    capture_evidence: dict[str, str | int | bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if self.created_at.utcoffset() != UTC.utcoffset(self.created_at):
            raise ValueError("Clone creation time must be UTC")
        if self.release.profile != self.deployment_profile:
            raise ValueError("Clone release and deployment profile must match")
        if self.entry_count != len(self.entries):
            raise ValueError("Clone entry_count does not match entries")
        if self.aggregate_size != sum(entry.size for entry in self.entries):
            raise ValueError("Clone aggregate_size does not match entries")
        paths = [entry.path for entry in self.entries]
        if len(paths) != len(set(paths)):
            raise ValueError("Clone entry paths must be unique")
        if paths != sorted(paths):
            raise ValueError("Clone entries must use deterministic path order")
        ownership = {entry.path: entry.data_class for entry in self.entries}
        if len(ownership) != len(self.entries):
            raise ValueError("Clone entries have duplicate ownership")
        if len(self.imported_secret_names) != len(set(self.imported_secret_names)):
            raise ValueError("Imported secret names must be unique")
        if any(_SECRET_NAME.fullmatch(name) is None for name in self.imported_secret_names):
            raise ValueError("Imported secret names must be canonical environment names")
        if set(self.source_secret_fingerprints) - set(self.imported_secret_names):
            raise ValueError("Secret fingerprints require an imported secret name")
        if any(
            _SHA256.fullmatch(fingerprint) is None
            for fingerprint in self.source_secret_fingerprints.values()
        ):
            raise ValueError("Secret fingerprints must be lowercase SHA-256 digests")
        return self


__all__ = [
    "ARCHIVE_ROOT_BY_DATA_CLASS",
    "MAX_CLONE_AGGREGATE_BYTES",
    "MAX_CLONE_ENCRYPTED_BYTES",
    "MAX_CLONE_ENTRIES",
    "MAX_CLONE_ENTRY_BYTES",
    "MAX_CLONE_PATH_BYTES",
    "CloneDataClass",
    "CloneEntry",
    "CloneEnvelopeHeader",
    "CloneKdfHeader",
    "CloneManifest",
    "canonical_clone_path",
]
