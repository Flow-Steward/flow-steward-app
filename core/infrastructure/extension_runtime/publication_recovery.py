"""Crash recovery for database-committed managed extension publications."""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from core.infrastructure.extension_runtime.package_identity import extension_folder_name


def _activate_release(*, active_root: Path, release_root: Path) -> None:
    staged_link = active_root.parent / f".{active_root.name}.link-{uuid.uuid4().hex[:8]}"
    try:
        staged_link.symlink_to(
            os.path.relpath(release_root, active_root.parent),
            target_is_directory=True,
        )
        os.replace(staged_link, active_root)
    finally:
        with contextlib.suppress(FileNotFoundError):
            staged_link.unlink()


def recover_managed_release_publications(
    *,
    extensions_root: Path,
    extensions: Iterable[Mapping[str, Any]],
) -> int:
    """Publish release roots already committed as authoritative in the database."""
    root = Path(extensions_root).resolve()
    recovered = 0
    for raw in extensions:
        row = dict(raw or {})
        extension_id = str(row.get("extension_key") or "").strip()
        raw_release_root = str(row.get("bundle_root") or "").strip()
        if not extension_id or not raw_release_root:
            continue
        folder_name = extension_folder_name(extension_id)
        active_root = root / folder_name
        if active_root.parent != root:
            raise ValueError("Managed extension publication path escapes the extensions root")
        releases_root = root / ".releases"
        expected_parent = releases_root / folder_name
        for ancestor in (releases_root, expected_parent):
            if ancestor.is_symlink():
                raise ValueError(
                    "Managed extension release path may not contain symlinked ancestors"
                )
            if ancestor.exists() and not ancestor.is_dir():
                raise ValueError("Managed extension release path is invalid")
        release_root = Path(os.path.abspath(raw_release_root))
        if release_root.parent != expected_parent:
            continue
        if release_root.is_symlink() or release_root.resolve() != release_root:
            raise ValueError("Managed extension release path may not contain symlinked ancestors")
        if not release_root.is_dir():
            raise FileNotFoundError(f"Committed extension release is missing for {extension_id}")
        if active_root.is_symlink() and active_root.resolve() == release_root:
            continue
        _activate_release(active_root=active_root, release_root=release_root)
        recovered += 1
    return recovered
