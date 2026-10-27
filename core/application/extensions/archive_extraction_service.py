from __future__ import annotations

import io
import shutil
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from core.application.management.errors import ServiceValidationError

from .archive_validation_service import ValidatedArchiveUpload

ArchiveFileMode = Callable[[zipfile.ZipInfo], int]


def safe_extension_archive_file_mode(member: zipfile.ZipInfo) -> int:
    raw_mode = (member.external_attr >> 16) & 0xFFFF
    parts = tuple(part for part in member.filename.split("/") if part)
    target_binary = any(
        part == "bin"
        and index + 2 < len(parts)
        and parts[index + 1] in {"linux-amd64", "linux-arm64"}
        for index, part in enumerate(parts)
    )
    regular_file = (raw_mode & 0o170000) in {0, 0o100000}
    executable_intent = member.create_system == 3 and bool(raw_mode & 0o111)
    return 0o755 if target_binary and regular_file and executable_intent else 0o644


class ExtensionArchiveExtractionService:
    def __init__(self, *, file_mode: ArchiveFileMode) -> None:
        self._file_mode = file_mode

    @contextmanager
    def extract(
        self,
        *,
        upload: ValidatedArchiveUpload,
        max_uncompressed_bytes: int,
    ) -> Iterator[Path]:
        self._validate_safe_archive(
            upload=upload,
            max_uncompressed_bytes=max_uncompressed_bytes,
        )
        with tempfile.TemporaryDirectory(prefix="extension-install-") as temp_dir:
            temp_root = Path(temp_dir)
            with zipfile.ZipFile(io.BytesIO(upload.content)) as archive:
                uncompressed_bytes = sum(member.file_size for member in archive.infolist())
            required_bytes = len(upload.content) + (uncompressed_bytes * 2)
            if shutil.disk_usage(temp_root).free < required_bytes:
                raise ServiceValidationError(
                    "Extension archive installation requires more temporary disk space "
                    f"({required_bytes} bytes required)"
                )
            archive_path = temp_root / upload.safe_name
            archive_path.write_bytes(upload.content)
            extracted_root = temp_root / "unzipped"
            extracted_root.mkdir(parents=True, exist_ok=True)
            self._extract_members(
                archive_path=archive_path,
                extracted_root=extracted_root,
                filename=upload.filename,
            )
            yield extracted_root

    @staticmethod
    def _validate_safe_archive(
        *,
        upload: ValidatedArchiveUpload,
        max_uncompressed_bytes: int,
    ) -> None:
        from core.infrastructure.security.zip_guard import validate_zip_safe

        guard = validate_zip_safe(
            io.BytesIO(upload.content),
            max_size_bytes=max_uncompressed_bytes,
        )
        if not guard.safe:
            raise ServiceValidationError(f"Rejected archive '{upload.filename}': {guard.reason}")

    def _extract_members(
        self,
        *,
        archive_path: Path,
        extracted_root: Path,
        filename: str,
    ) -> None:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    self._extract_member(
                        archive=archive,
                        member=member,
                        extracted_root=extracted_root,
                        filename=filename,
                    )
        except zipfile.BadZipFile as exc:
            raise ServiceValidationError("Uploaded file is not a valid .zip archive") from exc

    def _extract_member(
        self,
        *,
        archive: zipfile.ZipFile,
        member: zipfile.ZipInfo,
        extracted_root: Path,
        filename: str,
    ) -> None:
        resolved_root = extracted_root.resolve()
        destination = (extracted_root / member.filename).resolve()
        if resolved_root != destination.parent and resolved_root not in destination.parents:
            raise ServiceValidationError(
                f"Rejected archive '{filename}': unsafe entry '{member.filename}'"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, destination.open("wb") as target:
            shutil.copyfileobj(source, target)
        destination.chmod(self._file_mode(member))


__all__ = [
    "ArchiveFileMode",
    "ExtensionArchiveExtractionService",
    "safe_extension_archive_file_mode",
]
