"""Narrow filesystem lookup for installed extension bundles."""

from __future__ import annotations

from pathlib import Path


def _extension_identifier(payload: dict | None) -> str:
    row = dict(payload) if isinstance(payload, dict) else {}
    compiled = row.get("compiled_manifest")
    compiled_manifest = dict(compiled) if isinstance(compiled, dict) else {}
    return str(
        row.get("extension_key")
        or row.get("extension_id")
        or compiled_manifest.get("extension_id")
        or row.get("id")
        or ""
    ).strip()


def load_extension_from_filesystem(
    extension_id: str,
    *,
    canonical_folder_name: str = "",
) -> dict | None:
    """Compile and return the installed bundle matching ``extension_id``."""
    normalized_id = str(extension_id or "").strip()
    if not normalized_id:
        return None

    from .extension_runtime_compile import compile_extension_bundle
    from .extension_runtime_manifest import default_extensions_root, extension_host_metadata

    extensions_root = Path(default_extensions_root()).resolve()
    if not extensions_root.exists():
        return None

    direct_candidates: list[Path] = []
    for candidate_name in dict.fromkeys([normalized_id, canonical_folder_name]):
        if not candidate_name:
            continue
        candidate = extensions_root / candidate_name
        if candidate.is_dir():
            direct_candidates.append(candidate)
    for direct_bundle in direct_candidates:
        try:
            compiled = compile_extension_bundle(direct_bundle)
        except Exception:
            compiled = None
        if compiled and _extension_identifier(compiled) == normalized_id:
            compiled.update(extension_host_metadata(compiled))
            return compiled

    for bundle_dir in sorted(extensions_root.iterdir()):
        if not bundle_dir.is_dir():
            continue
        if bundle_dir.name.startswith(".") or bundle_dir.name == "__pycache__":
            continue
        if bundle_dir in direct_candidates:
            continue
        try:
            compiled = compile_extension_bundle(bundle_dir)
        except Exception:
            continue
        if _extension_identifier(compiled) == normalized_id:
            compiled.update(extension_host_metadata(compiled))
            return compiled
    return None
