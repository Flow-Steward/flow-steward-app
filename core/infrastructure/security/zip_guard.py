"""zip_guard.py — zip archive safety validation.

Checks for zip bombs (decompression ratio, uncompressed size), path traversal,
excessive file counts, and nested zip depth. Pure Python, no external deps.
"""

from __future__ import annotations

import io
import tempfile
import zipfile
from dataclasses import dataclass
from typing import BinaryIO


@dataclass
class ZipGuardResult:
    safe: bool
    reason: str | None = None


def validate_zip_safe(
    buf: BinaryIO,
    *,
    max_ratio: float = 100.0,
    max_size_bytes: int = 100 * 1024 * 1024,
    max_nesting_depth: int = 3,
    max_file_count: int = 10000,
) -> ZipGuardResult:
    """Validate that a zip archive is safe to process.

    Args:
        buf: A binary file-like object containing the zip data.
        max_ratio: Maximum allowed decompressed-to-compressed size ratio.
        max_size_bytes: Maximum total uncompressed size in bytes.
        max_nesting_depth: Maximum depth of nested zip archives.
        max_file_count: Maximum number of files in the archive.

    Returns:
        ZipGuardResult with safe=True if the archive passes all checks,
        or safe=False with a human-readable reason if any check fails.
    """
    return _check_zip_file(
        buf,
        max_ratio=max_ratio,
        max_size_bytes=max_size_bytes,
        max_nesting_depth=max_nesting_depth,
        max_file_count=max_file_count,
        current_depth=0,
        inspection_budget=[max_size_bytes],
    )


def _check_zip(
    data: bytes,
    *,
    max_ratio: float,
    max_size_bytes: int,
    max_nesting_depth: int,
    max_file_count: int,
    current_depth: int,
) -> ZipGuardResult:
    """Compatibility wrapper for callers that already hold an in-memory ZIP."""
    return _check_zip_file(
        io.BytesIO(data),
        max_ratio=max_ratio,
        max_size_bytes=max_size_bytes,
        max_nesting_depth=max_nesting_depth,
        max_file_count=max_file_count,
        current_depth=current_depth,
        inspection_budget=[max_size_bytes],
    )


def _check_zip_file(
    buf: BinaryIO,
    *,
    max_ratio: float,
    max_size_bytes: int,
    max_nesting_depth: int,
    max_file_count: int,
    current_depth: int,
    inspection_budget: list[int],
) -> ZipGuardResult:
    """Inspect a seekable ZIP without materialising archive or nested entries."""
    if current_depth > max_nesting_depth:
        return ZipGuardResult(
            safe=False,
            reason=f"Nested zip depth {current_depth} exceeds limit of {max_nesting_depth}",
        )

    try:
        buf.seek(0)
        zf = zipfile.ZipFile(buf)
    except zipfile.BadZipFile as exc:
        return ZipGuardResult(safe=False, reason=f"Invalid or corrupt zip archive: {exc}")
    except Exception as exc:
        return ZipGuardResult(safe=False, reason=f"Could not read archive: {exc}")

    with zf:
        entries = zf.infolist()

        # File count check
        if len(entries) > max_file_count:
            return ZipGuardResult(
                safe=False,
                reason=f"File count {len(entries)} exceeds limit of {max_file_count}",
            )

        compressed_total = sum(e.compress_size for e in entries)
        uncompressed_total = sum(e.file_size for e in entries)

        # Total uncompressed size check
        if uncompressed_total > max_size_bytes:
            return ZipGuardResult(
                safe=False,
                reason=(
                    f"Total uncompressed size {uncompressed_total} bytes exceeds "
                    f"limit of {max_size_bytes} bytes"
                ),
            )

        # Decompression ratio check (guard against division by zero)
        if compressed_total > 0:
            ratio = uncompressed_total / compressed_total
            if ratio > max_ratio:
                return ZipGuardResult(
                    safe=False,
                    reason=(
                        f"Decompression ratio {ratio:.1f}x exceeds limit of {max_ratio}x "
                        f"(possible zip bomb)"
                    ),
                )

        # Path traversal check
        for entry in entries:
            name = entry.filename
            # Absolute paths or paths with ".." components are unsafe
            if name.startswith(("/", "\\")):
                return ZipGuardResult(
                    safe=False,
                    reason=f"Path traversal detected: absolute path '{name}'",
                )
            parts = name.replace("\\", "/").split("/")
            if ".." in parts:
                return ZipGuardResult(
                    safe=False,
                    reason=f"Path traversal detected: '..' in entry name '{name}'",
                )

        # Nested zip check — only recurse if depth limit hasn't been hit
        if current_depth < max_nesting_depth:
            for entry in entries:
                if entry.filename.lower().endswith(".zip"):
                    if entry.file_size > inspection_budget[0]:
                        return ZipGuardResult(
                            safe=False,
                            reason="Nested archive inspection exceeds the configured size limit",
                        )
                    try:
                        with (
                            tempfile.TemporaryFile(mode="w+b") as nested_file,
                            zf.open(entry, "r") as nested_source,
                        ):
                            while True:
                                chunk = nested_source.read(1024 * 1024)
                                if not chunk:
                                    break
                                inspection_budget[0] -= len(chunk)
                                if inspection_budget[0] < 0:
                                    return ZipGuardResult(
                                        safe=False,
                                        reason=(
                                            "Nested archive inspection exceeds the configured "
                                            "size limit"
                                        ),
                                    )
                                nested_file.write(chunk)
                            result = _check_zip_file(
                                nested_file,
                                max_ratio=max_ratio,
                                max_size_bytes=max_size_bytes,
                                max_nesting_depth=max_nesting_depth,
                                max_file_count=max_file_count,
                                current_depth=current_depth + 1,
                                inspection_budget=inspection_budget,
                            )
                    except Exception:
                        return ZipGuardResult(
                            safe=False,
                            reason=f"Could not read nested archive entry '{entry.filename}'",
                        )
                    if not result.safe:
                        return result
        else:
            # At depth limit — flag any nested zips without descending
            for entry in entries:
                if entry.filename.lower().endswith(".zip"):
                    return ZipGuardResult(
                        safe=False,
                        reason=(
                            f"Nested zip depth exceeds limit of {max_nesting_depth}: "
                            f"found '{entry.filename}' at depth {current_depth}"
                        ),
                    )

    return ZipGuardResult(safe=True)
