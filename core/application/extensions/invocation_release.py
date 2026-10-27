"""Coherent immutable filesystem selection for one extension invocation."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _repoint(value: Any, *, source: Path, target: Path) -> Any:
    if isinstance(value, dict):
        return {key: _repoint(item, source=source, target=target) for key, item in value.items()}
    if isinstance(value, list):
        return [_repoint(item, source=source, target=target) for item in value]
    if isinstance(value, tuple):
        return tuple(_repoint(item, source=source, target=target) for item in value)
    if isinstance(value, str):
        candidate = Path(value)
        if candidate.is_absolute() and (candidate == source or source in candidate.parents):
            return str(target / candidate.relative_to(source))
    return copy.deepcopy(value)


@dataclass(frozen=True, slots=True)
class InvocationReleaseSelection:
    release_id: str
    metadata: dict[str, Any]
    bundle_root: Path
    dependency_root: Path | None

    @classmethod
    def capture(cls, extension: dict[str, Any]) -> InvocationReleaseSelection:
        raw_root = str(extension.get("bundle_root") or "").strip()
        if not raw_root:
            raise ValueError("Extension invocation requires a bundle root")
        source_root = Path(raw_root).absolute()
        resolved_root = source_root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise ValueError("Extension invocation bundle root is not a directory")
        metadata = _repoint(extension, source=source_root, target=resolved_root)
        metadata["bundle_root"] = str(resolved_root)
        compiled = metadata.get("compiled_manifest")
        if isinstance(compiled, dict):
            compiled["bundle_root"] = str(resolved_root)
        from core.infrastructure.extension_runtime.python_dependencies import (
            extension_python_site_dir,
        )

        dependency_root = extension_python_site_dir(metadata)
        return cls(
            release_id=str(
                extension.get("release_id")
                or extension.get("extension_release_id")
                or resolved_root.name
            ).strip(),
            metadata=metadata,
            bundle_root=resolved_root,
            dependency_root=dependency_root,
        )

    def runtime_extension(self) -> dict[str, Any]:
        return copy.deepcopy(self.metadata)


__all__ = ["InvocationReleaseSelection"]
