"""Helpers for serving the React migration shell from the FastAPI runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ASSETS_VALUE = "assets/"


@dataclass(frozen=True, slots=True)
class FrontendShellArtifacts:
    dist_root: Path
    assets_root: Path
    index_file: Path
    manifest_file: Path


def build_frontend_shell_artifacts(
    dist_root: str | Path = "assets/frontend/dist",
) -> FrontendShellArtifacts:
    dist_path = Path(dist_root)
    return FrontendShellArtifacts(
        dist_root=dist_path,
        assets_root=dist_path / "assets",
        index_file=dist_path / ("index." + "html"),
        manifest_file=dist_path / ".vite" / "manifest.json",
    )


def frontend_shell_available(artifacts: FrontendShellArtifacts) -> bool:
    return artifacts.index_file.is_file() and artifacts.assets_root.is_dir()


def load_frontend_manifest(artifacts: FrontendShellArtifacts) -> dict[str, Any]:
    if not artifacts.manifest_file.is_file():
        return {}

    with artifacts.manifest_file.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def manifest_asset_paths(manifest: dict[str, Any]) -> set[str]:
    paths: set[str] = set()

    for entry in manifest.values():
        if not isinstance(entry, dict):
            continue

        file_name = str(entry.get("file") or "").lstrip("/")
        if file_name.startswith(ASSETS_VALUE):
            paths.add(file_name.removeprefix(ASSETS_VALUE))

        for list_key in ("css", "assets"):
            for item in entry.get(list_key) or []:
                candidate = str(item or "").lstrip("/")
                if candidate.startswith(ASSETS_VALUE):
                    paths.add(candidate.removeprefix(ASSETS_VALUE))

    return paths


def resolve_frontend_asset(
    artifacts: FrontendShellArtifacts,
    asset_path: str,
    *,
    manifest_paths: set[str] | None = None,
) -> Path | None:
    relative = str(asset_path or "").lstrip("/")
    if not relative or relative.startswith(".") or ".." in Path(relative).parts:
        return None

    if (
        manifest_paths is not None
        and relative not in manifest_paths
        and not relative.endswith(".map")
    ):
        return None

    candidate = (artifacts.assets_root / relative).resolve()
    assets_root = artifacts.assets_root.resolve()
    if not candidate.is_relative_to(assets_root) or not candidate.is_file():
        return None
    return candidate
