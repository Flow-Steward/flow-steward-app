"""Narrow application ports for Clone archive effects."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, Protocol

from core.application.clone.contracts import CloneManifest

PassphraseSource = Callable[[], str | bytes]


class VerifiedCloneArchivePort(Protocol):
    manifest: CloneManifest
    root: Path

    def cleanup(self) -> None: ...


class CloneArchivePort(Protocol):
    def write_archive(
        self,
        source_root: Path,
        manifest: CloneManifest,
        destination: BinaryIO,
        *,
        passphrase_source: PassphraseSource,
    ) -> None: ...

    def stage_verified(
        self,
        source: BinaryIO,
        *,
        staging_parent: Path,
        passphrase_source: PassphraseSource,
    ) -> VerifiedCloneArchivePort: ...


__all__ = ["CloneArchivePort", "VerifiedCloneArchivePort"]
